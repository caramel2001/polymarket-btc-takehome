"""
v2_circuit_breaker.py — Conviction-Hold + Bleed Circuit Breaker

Builds on v1 (research/strategies/v1_conviction_hold_majority.py — majority-
vote multi-horizon momentum, fee-aware sizing, resolution hold, deferred
stop-loss). v1's full 29-fixture battery
(research/results/20260607T160839Z_v1_baseline.json) showed:

    mean model PnL:   -$34   (vs baseline -$316 -- we lose 9x LESS in $ terms)
    mean model score:  704   (vs baseline 2,779 -- yet we score far worse)

The reconciliation: primary_score = PnL × max(Sharpe, 0) × (1 − max_drawdown).
Splitting the 29 fixtures by outcome:

    smooth loser (PnL<0, Sharpe>0):  7 fixtures, mean score -2,955
    noisy loser  (PnL<0, Sharpe=0):  5 fixtures, mean score      0
    zero-trade:                      2 fixtures, mean score      0
    winner       (PnL>0):           15 fixtures, mean score +2,740

A "smooth loser" -- a strategy entering often, getting stopped out at a
similar small % each time -- produces a steadily declining, LOW-VARIANCE
equity curve. Low variance + negative drift = strongly POSITIVE Sharpe,
which multiplies a negative PnL into a deeply negative score. Meanwhile
simply not trading scores exactly 0 -- which already beats 7 of our 29
fixtures outright.

NEW IN v2: a circuit breaker. Track consecutive losing exits (stop-losses,
which the model decides itself, plus inferred resolution losses -- near
ttr->0, up_mid converges to ~0 or ~1, which we can compare against the side
we held). After LOSS_STREAK_HALT consecutive losses, halt new entries for
HALT_COOLDOWN_EVENTS events. This deliberately converts a forming
"smooth bleed" into a "zero-trade" outcome -- which the scoring math treats
far more kindly. A winning streak resets the counter immediately.
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters (entry/exit core copied from v1 -- proven to fill & hold) ──

R5_MIN         = 0.002
R15_MIN        = 0.001
R30_MIN        = 0.001
SIGNAL_MIN     = 0.03

BASE_SIZE      = 0.18
MAX_SIZE       = 0.42
FEE_OPT_LO     = 0.35
FEE_OPT_HI     = 0.65
FEE_OPT_MULT   = 1.25
FEE_MID_MULT   = 0.80

STOP_LOSS_FRAC      = 0.060
CLOSE_AT_TTR        = 3.0
RESOLUTION_HOLD_TTR = 50.0
WARMUP_TICKS        = 10
IMB_VETO            = -0.04

ONE_ENTRY_PER_EVENT = True

# ── NEW: circuit breaker ───────────────────────────────────────────────────────
# After this many consecutive LOSING exits (stop-loss or inferred-resolution-
# loss), stop opening new positions for HALT_COOLDOWN_EVENTS events. A single
# winning exit immediately resets the streak to 0 and clears any active halt.
LOSS_STREAK_HALT      = 2
HALT_COOLDOWN_EVENTS  = 3

# Near the very end of an event, up_mid converges toward 0 (DOWN won) or 1
# (UP won). Use this to infer the resolution outcome for positions held to
# settlement, without any harness feedback.
RESOLUTION_INFER_TTR    = 4.0
RESOLUTION_INFER_MARGIN = 0.15   # up_mid within this of 0 or 1 counts as "decided"


# ── Model ──────────────────────────────────────────────────────────────────────

class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        # Cross-event state — survives the whole run, init once.
        if not hasattr(self, "_loss_streak"):
            self._loss_streak: int = 0
            self._halt_events_remaining: int = 0

        # A halt is consumed one event at a time; decrement on each new event.
        if self._halt_events_remaining > 0:
            self._halt_events_remaining -= 1

        # Per-event state
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._entry_price: float = 0.0
        self._tick_count: int = 0
        self._event_traded: bool = False
        self._ticks_in_pos: int = 0
        self._outcome_recorded: bool = False   # avoid double-counting one trade's exit

    # ── Main loop ─────────────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> Signal | None:
        self._tick_count += 1
        ttr = tick.time_to_resolve

        # Infer resolution outcome for a position we're holding into settlement
        # (no stop-loss fired -- it either won or lost at the $1/$0 snap).
        if (self._pos_side is not None and not self._outcome_recorded
                and ttr < RESOLUTION_INFER_TTR):
            um = tick.up_mid
            if um <= RESOLUTION_INFER_MARGIN or um >= 1.0 - RESOLUTION_INFER_MARGIN:
                resolved_up = um >= 0.5
                won = (self._pos_side == Side.UP) == resolved_up
                self._record_outcome(won)

        if ttr < CLOSE_AT_TTR:
            return self._close()

        # ── EXIT LOGIC for existing position ──────────────────────────────────
        if self._pos_side is not None:
            self._ticks_in_pos += 1
            pnl = self._pnl_frac(tick)

            if ttr < RESOLUTION_HOLD_TTR and pnl > 0.0:
                return Signal(side=self._pos_side, size=self._pos_size,
                              confidence=min(1.0, pnl * 5))

            # Defer stop-loss: fills land on ~3% of ticks; checking PnL right
            # after emitting a signal measures an unfilled mark-to-market.
            if self._ticks_in_pos > 5:
                if pnl < -STOP_LOSS_FRAC:
                    self._record_outcome(won=False)   # deterministic loss -- count it now
                    self._event_traded = True
                    return self._close()

            return Signal(side=self._pos_side, size=self._pos_size,
                          confidence=max(0.0, pnl * 5))

        # ── ENTRY / DIRECTION SIGNAL ──────────────────────────────────────────
        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT

        # Circuit breaker: we're in a halt window -- sit this event out
        # entirely. This is the whole point: convert a forming smooth-bleed
        # into a deliberate zero-trade outcome (score floors at 0 instead of
        # compounding another small loss onto a low-variance losing streak).
        if self._halt_events_remaining > 0:
            return FLAT

        if self._tick_count < WARMUP_TICKS:
            return FLAT
        if ttr < RESOLUTION_HOLD_TTR:
            return FLAT

        score, side = self._directional_score(tick)
        if abs(score) < SIGNAL_MIN or side is None:
            return FLAT

        size = self._compute_size(tick, score)
        if size < 0.05:
            return FLAT

        self._pos_side = side
        self._pos_size = size
        self._entry_price = tick.up_ask if side == Side.UP else tick.down_ask
        self._event_traded = True
        self._ticks_in_pos = 0
        self._outcome_recorded = False

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── Circuit breaker bookkeeping ───────────────────────────────────────────

    def _record_outcome(self, won: bool) -> None:
        """Update the loss streak and (re)arm the halt. Idempotent per trade."""
        if self._outcome_recorded:
            return
        self._outcome_recorded = True

        if won:
            self._loss_streak = 0
            self._halt_events_remaining = 0   # a win clears any forming halt
        else:
            self._loss_streak += 1
            if self._loss_streak >= LOSS_STREAK_HALT:
                self._halt_events_remaining = HALT_COOLDOWN_EVENTS
                self._loss_streak = 0          # streak "spent" on triggering the halt

    # ── Signal computation (unchanged from v1 -- majority-vote, validated to
    #    fill on the only available depth window in event 561014) ─────────────

    def _directional_score(self, tick: Tick) -> tuple[float, Side | None]:
        w = tick.up_mid_recent
        n = len(w)
        if n < 31:
            return 0.0, None

        now = w[-1]
        r5  = now - w[max(0, n - 6)]
        r15 = now - w[max(0, n - 16)]
        r30 = now - w[max(0, n - 31)]

        if abs(r5) < R5_MIN or abs(r15) < R15_MIN or abs(r30) < R30_MIN:
            return 0.0, None

        # Majority rule: with 3 votes (each ±1), sum is ±1 (2-of-3) or ±3 (all
        # agree) -- never a tie, so a majority always exists.
        votes = [1 if r > 0 else -1 for r in (r5, r15, r30)]
        direction = 1.0 if sum(votes) > 0 else -1.0

        denom = tick.up_mid + tick.down_mid
        imb = (tick.up_mid - tick.down_mid) / denom if denom > 0 else 0.0
        if direction * imb < IMB_VETO:
            return 0.0, None

        mag = min(1.0, abs(r30) / 0.05)
        if direction * imb > 0.01:
            mag = min(1.0, mag * 1.15)

        side = Side.UP if direction > 0 else Side.DOWN
        return direction * mag, side

    # ── Sizing (unchanged from v1) ────────────────────────────────────────────

    def _compute_size(self, tick: Tick, score: float) -> float:
        size = BASE_SIZE + (MAX_SIZE - BASE_SIZE) * min(1.0, abs(score))
        um = tick.up_mid
        if um < FEE_OPT_LO or um > FEE_OPT_HI:
            size = min(MAX_SIZE, size * FEE_OPT_MULT)
        else:
            size *= FEE_MID_MULT
        return max(0.05, min(MAX_SIZE, size))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pnl_frac(self, tick: Tick) -> float:
        ref = self._entry_price
        if ref <= 0:
            return 0.0
        if self._pos_side == Side.UP:
            return (tick.up_bid - ref) / ref
        return (tick.down_bid - ref) / ref

    def _close(self) -> Signal:
        self._pos_side = None
        self._pos_size = 0.0
        self._entry_price = 0.0
        self._ticks_in_pos = 0
        return FLAT
