"""
model_submission.py — Conviction-Hold Strategy v2

Key lesson from analysis:
  - Polymarket CLOB has book depth on only ~3% of ticks
  - The simulator fills only when book depth exists (best_bid_size > 0)
  - Must emit a position signal on EVERY tick to capture those rare fill windows
  - Asymmetric hold: cut losers fast (-6%), let winners ride to settlement ($1/$0)

Design:
  1. Compute directional conviction every tick from multi-horizon up_mid momentum
  2. Emit a continuous "desired position" signal (the harness handles fills opportunistically)
  3. Resolution hold: when TTR < 50s and PnL > 0, hold to settlement
  4. Stop loss: -6% from fill price → go flat immediately
  5. Fee-optimal sizing: Polymarket fee = shares * 0.072 * p*(1-p), cheapest at extremes
     Prefer larger size when up_mid < 0.35 or > 0.65
  6. Drawdown control: reduce size after losses
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters ────────────────────────────────────────────────────────────

# Momentum thresholds (up_mid in [0, 1] probability units)
R5_MIN         = 0.002   # 5s minimum return to count as signal
R15_MIN        = 0.001   # 15s minimum return
R30_MIN        = 0.001   # 30s minimum return
SIGNAL_MIN     = 0.03    # composite score must exceed this to hold a position

# Sizing — smaller positions reduce intra-event drawdown
BASE_SIZE      = 0.18
MAX_SIZE       = 0.42
FEE_OPT_LO    = 0.35    # below this: fee-cheap zone (p*(1-p) lower)
FEE_OPT_HI    = 0.65    # above this: fee-cheap zone
FEE_OPT_MULT  = 1.25    # size boost in fee-cheap zone
FEE_MID_MULT  = 0.80    # mild penalty near 0.50 (slightly expensive fees)

# Exits
STOP_LOSS_FRAC       = 0.060   # -6% from fill price → close
CLOSE_AT_TTR         = 3.0     # hard close at 3s remaining
RESOLUTION_HOLD_TTR  = 50.0    # if profitable and TTR < this → hold to settlement

# Reversal: 10s momentum opposes position → exit (only when far from resolution)
REVERSAL_THRESH = 0.008   # slightly less trigger-happy than before

# Warmup: need this many ticks of up_mid history before directional signals
WARMUP_TICKS   = 10       # was 30 — captures earlier fill windows each event

# Imbalance veto threshold
IMB_VETO = -0.04

# Volatility gate: skip entry if 30s std of up_mid exceeds this (market too noisy)
VOL_MAX = 0.08

# One entry per event: after any exit (stop or reversal), sit out the rest
# of the current event to avoid fee-spiral in volatile conditions
ONE_ENTRY_PER_EVENT = True

# Drawdown control
DD_THRESH_1 = 0.10   # 10% drawdown → 60% size
DD_THRESH_2 = 0.20   # 20% drawdown → 25% size


# ── Model ──────────────────────────────────────────────────────────────────────

class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        # Equity tracking persists across events; only init on first call
        if not hasattr(self, "_equity"):
            self._equity: float = 1000.0
            self._peak_equity: float = 1000.0

        # Per-event state
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._entry_price: float = 0.0
        self._tick_count: int = 0
        self._event_traded: bool = False   # one entry per event
        self._ticks_in_pos: int = 0        # ticks since signal emitted

    # ── Main loop ─────────────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> Signal | None:
        self._tick_count += 1
        ttr = tick.time_to_resolve

        # Hard close near expiry
        if ttr < CLOSE_AT_TTR:
            return self._close()

        # ── EXIT LOGIC for existing position ──────────────────────────────────
        if self._pos_side is not None:
            self._ticks_in_pos += 1
            pnl = self._pnl_frac(tick)

            # Resolution hold: if we're profitable as clock winds down, ride it
            if ttr < RESOLUTION_HOLD_TTR and pnl > 0.0:
                return Signal(side=self._pos_side, size=self._pos_size,
                              confidence=min(1.0, pnl * 5))

            # Defer stop-loss check for a few ticks — fills take time to land
            # on a thin CLOB (~3% of ticks have book depth). Checking too early
            # triggers spurious exits on price noise before a fill executes.
            if self._ticks_in_pos > 5:
                if pnl < -STOP_LOSS_FRAC:
                    self._event_traded = True
                    return self._close()

            # Hold
            return Signal(side=self._pos_side, size=self._pos_size,
                          confidence=max(0.0, pnl * 5))

        # ── ENTRY / DIRECTION SIGNAL ──────────────────────────────────────────
        # One entry per event — sit out after any stop/reversal exit
        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT

        # Need warmup history before making directional calls
        if self._tick_count < WARMUP_TICKS:
            return FLAT

        # Don't enter in final window — too late for a new position
        if ttr < RESOLUTION_HOLD_TTR:
            return FLAT

        score, side = self._directional_score(tick)

        if abs(score) < SIGNAL_MIN or side is None:
            return FLAT

        size = self._compute_size(tick, score)
        if size < 0.05:
            return FLAT

        # Record entry state (will be filled opportunistically by the simulator)
        self._pos_side = side
        self._pos_size = size
        # Use actual fill price for each side:
        # UP  → buy UP tokens at up_ask
        # DOWN → buy DOWN tokens at down_ask (not up_bid)
        self._entry_price = tick.up_ask if side == Side.UP else tick.down_ask
        self._event_traded = True
        self._ticks_in_pos = 0

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── Signal computation ────────────────────────────────────────────────────

    def _directional_score(self, tick: Tick) -> tuple[float, Side | None]:
        """
        Multi-horizon momentum consensus. All three horizons must agree in
        sign and exceed noise floor to produce a signal.
        """
        w = tick.up_mid_recent
        n = len(w)
        if n < 31:
            return 0.0, None

        now = w[-1]
        r5  = now - w[max(0, n - 6)]
        r15 = now - w[max(0, n - 16)]
        r30 = now - w[max(0, n - 31)]

        # All horizons must exceed noise floor
        if abs(r5) < R5_MIN or abs(r15) < R15_MIN or abs(r30) < R30_MIN:
            return 0.0, None

        # Majority rule: at least 2-of-3 horizons must agree in direction.
        # With 3 votes (each ±1), sum is ±1 (2-of-3) or ±3 (all agree) — no ties.
        votes = [1 if r > 0 else -1 for r in (r5, r15, r30)]
        direction = 1.0 if sum(votes) > 0 else -1.0

        # Imbalance veto: if order book strongly contradicts, skip
        denom = tick.up_mid + tick.down_mid
        imb = (tick.up_mid - tick.down_mid) / denom if denom > 0 else 0.0
        if direction * imb < IMB_VETO:
            return 0.0, None

        # Signal magnitude: normalise 30s return; boost if imbalance confirms
        mag = min(1.0, abs(r30) / 0.05)
        if direction * imb > 0.01:
            mag = min(1.0, mag * 1.15)

        side = Side.UP if direction > 0 else Side.DOWN
        return direction * mag, side

    def _market_vol(self, tick: Tick) -> float:
        """30s standard deviation of up_mid — proxy for market noise."""
        w = tick.up_mid_recent
        n = len(w)
        if n < 10:
            return 0.0
        window = w[max(0, n - 31):]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        return variance ** 0.5

    def _reversed(self, tick: Tick) -> bool:
        """True if 10s momentum has flipped against current position."""
        w = tick.up_mid_recent
        n = len(w)
        if n < 11:
            return False
        r10 = w[-1] - w[max(0, n - 11)]
        if self._pos_side == Side.UP   and r10 < -REVERSAL_THRESH:
            return True
        if self._pos_side == Side.DOWN and r10 >  REVERSAL_THRESH:
            return True
        return False

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _compute_size(self, tick: Tick, score: float) -> float:
        # Scale with conviction
        size = BASE_SIZE + (MAX_SIZE - BASE_SIZE) * min(1.0, abs(score))

        # Fee-optimality: prefer entries away from 0.50
        um = tick.up_mid
        if um < FEE_OPT_LO or um > FEE_OPT_HI:
            size = min(MAX_SIZE, size * FEE_OPT_MULT)
        else:
            size *= FEE_MID_MULT

        return max(0.05, min(MAX_SIZE, size * self._drawdown_factor()))

    def _drawdown_factor(self) -> float:
        if self._peak_equity <= 0:
            return 1.0
        dd = (self._peak_equity - self._equity) / self._peak_equity
        if dd > DD_THRESH_2:
            return 0.25
        if dd > DD_THRESH_1:
            return 0.60
        return 1.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pnl_frac(self, tick: Tick) -> float:
        ref = self._entry_price
        if ref <= 0:
            return 0.0
        if self._pos_side == Side.UP:
            # entered at up_ask, mark exit at up_bid
            return (tick.up_bid - ref) / ref
        # entered at down_ask, mark exit at down_bid
        return (tick.down_bid - ref) / ref

    def _close(self) -> Signal:
        self._pos_side = None
        self._pos_size = 0.0
        self._entry_price = 0.0
        self._ticks_in_pos = 0
        return FLAT
