"""
v4_mean_reversion.py — Bet against short-horizon bursts, not with them.

v1/v3 both bet WITH recent up_mid momentum (trend-following). v3
(`v3_pure_directional.py`, full battery: mean score 11,621 but median 0,
std 35,134 -- ~10x v1's -- and only 9/30 fixtures PnL-positive) proved that
v1's exit machinery (deferred stop-loss, conditional resolution-hold) is
doing real protective work, NOT injecting noise: stripping it just amplifies
whatever edge the entry signal has into a high-variance gamble. So the
problem is upstream, in the entry signal itself.

NEW HYPOTHESIS: this is a thin, lumpy CLOB -- a handful of large orders can
snap `up_mid` sharply without that reflecting genuine new information about
where BTC will be in 5 minutes. If those snaps partially revert (classic
thin-market overreaction), betting AGAINST an unusually large, fast 5s move
(relative to the market's own recent volatility) should out-call momentum.

Mechanically: keep v1's proven exit core UNCHANGED (continuous signal,
fee-aware sizing, deferred stop-loss, conditional resolution-hold -- v3
showed these are net-positive, not noise) and swap ONLY the entry signal:

  - OLD (momentum):     direction = sign(majority vote of r5,r15,r30)
  - NEW (mean-reversion): direction = -sign(r5),  gated on "r5 is unusually
    large relative to recent 30s volatility" (a genuine burst, not just
    everyday drift)

If this aggregate beats v1's mean score (704) AND median (212) without
exploding variance the way v3 did, momentum was the wrong bet on this tape
and reversion is the real edge. If it's no better (or worse), short-horizon
`up_mid` moves carry ~zero predictive information in EITHER direction at
this timescale -- i.e. it's close to a random walk, and the only durable
edge is the resolution-time market-implied probability itself.
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters (sizing/exit core copied verbatim from v1 -- proven) ──────

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

# ── NEW: burst-detection / reversion entry ────────────────────────────────────
# A "burst" is a 5s move that's large relative to the market's own recent
# volatility -- i.e. unusual for THIS tape, not an arbitrary fixed threshold.
# BURST_VOL_MULT: |r5| must exceed this multiple of the 30s stdev of up_mid.
# BURST_MIN_ABS:  absolute floor so near-zero-vol regimes don't fire on noise.
BURST_VOL_MULT = 1.6
BURST_MIN_ABS  = 0.004

# Composite score normalisation (mirrors v1's SIGNAL_MIN / mag scaling, but
# keyed off the burst size rather than the 30s trend).
SIGNAL_MIN     = 0.03
MAG_NORM       = 0.03   # |r5| of this size -> mag = 1.0 (bursts are short/sharp)


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        # Per-event state only -- no cross-event bookkeeping needed for this test.
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._entry_price: float = 0.0
        self._tick_count: int = 0
        self._event_traded: bool = False
        self._ticks_in_pos: int = 0

    # ── Main loop (exit machinery copied verbatim from v1) ───────────────────

    def on_tick(self, tick: Tick) -> Signal | None:
        self._tick_count += 1
        ttr = tick.time_to_resolve

        if ttr < CLOSE_AT_TTR:
            return self._close()

        if self._pos_side is not None:
            self._ticks_in_pos += 1
            pnl = self._pnl_frac(tick)

            if ttr < RESOLUTION_HOLD_TTR and pnl > 0.0:
                return Signal(side=self._pos_side, size=self._pos_size,
                              confidence=min(1.0, pnl * 5))

            if self._ticks_in_pos > 5:
                if pnl < -STOP_LOSS_FRAC:
                    self._event_traded = True
                    return self._close()

            return Signal(side=self._pos_side, size=self._pos_size,
                          confidence=max(0.0, pnl * 5))

        # ── ENTRY / DIRECTION SIGNAL ──────────────────────────────────────────
        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT
        if self._tick_count < WARMUP_TICKS:
            return FLAT
        if ttr < RESOLUTION_HOLD_TTR:
            return FLAT

        score, side = self._reversion_score(tick)
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

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── NEW: mean-reversion entry signal ──────────────────────────────────────

    def _reversion_score(self, tick: Tick) -> tuple[float, Side | None]:
        """
        Bet AGAINST an unusually large, fast 5s move -- a "burst" relative to
        this tape's own recent volatility -- on the theory that thin-market
        snaps partially revert rather than persist.
        """
        w = tick.up_mid_recent
        n = len(w)
        if n < 31:
            return 0.0, None

        now = w[-1]
        r5 = now - w[max(0, n - 6)]

        vol = self._market_vol(tick)
        burst_thresh = max(BURST_MIN_ABS, BURST_VOL_MULT * vol)
        if abs(r5) < burst_thresh:
            return 0.0, None

        # Reversion: bet opposite the burst's direction.
        direction = -1.0 if r5 > 0 else 1.0

        # Imbalance veto: if the order book strongly agrees with the BURST
        # (i.e. disagrees with our contrarian call), the move may be real
        # information rather than overreaction -- skip it.
        denom = tick.up_mid + tick.down_mid
        imb = (tick.up_mid - tick.down_mid) / denom if denom > 0 else 0.0
        if direction * imb < IMB_VETO:
            return 0.0, None

        mag = min(1.0, abs(r5) / MAG_NORM)
        side = Side.UP if direction > 0 else Side.DOWN
        return direction * mag, side

    def _market_vol(self, tick: Tick) -> float:
        """30s standard deviation of up_mid -- the tape's own noise floor."""
        w = tick.up_mid_recent
        n = len(w)
        if n < 10:
            return 0.0
        window = w[max(0, n - 31):]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        return variance ** 0.5

    # ── Sizing (unchanged from v1) ────────────────────────────────────────────

    def _compute_size(self, tick: Tick, score: float) -> float:
        size = BASE_SIZE + (MAX_SIZE - BASE_SIZE) * min(1.0, abs(score))
        um = tick.up_mid
        if um < FEE_OPT_LO or um > FEE_OPT_HI:
            size = min(MAX_SIZE, size * FEE_OPT_MULT)
        else:
            size *= FEE_MID_MULT
        return max(0.05, min(MAX_SIZE, size))

    # ── Helpers (unchanged from v1) ───────────────────────────────────────────

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
