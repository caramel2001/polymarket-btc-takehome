"""
v3_pure_directional.py — Isolate the directional signal: enter once, hold to settlement, no exits.

Why: v1's full-battery aggregate (research/results/20260607T160839Z_v1_baseline.json,
mean score 704) showed wide fixture-to-fixture score variance dominated by a
"smooth loser" pattern (consistent small losses -> deep negative scores). We
checked whether that pattern is *predictable* from market structure
(return autocorrelation, momentum agreement) -- it isn't; both features are
statistically identical between winner and smooth-loser tapes (~0.059 / ~0.70
either way). That points the problem upstream: either (a) the directional
signal itself has near-zero edge and outcomes are luck-dominated by which of
our few fills land right, or (b) the intra-event exit machinery (deferred
stop-loss, conditional resolution-hold) is itself injecting path-dependent
noise on top of a perfectly fine signal.

This variant isolates (a) from (b) by deleting all of v1's exit logic:

  - NO stop-loss
  - NO conditional "hold only if currently profitable" resolution check
  - NO early exit of any kind

Once a position is opened (majority-vote signal -- unchanged from v1, proven
to fill on the only available depth window in event 561014), it is held
UNCONDITIONALLY to settlement. The harness still force-flattens at
CLOSE_AT_TTR. This makes the strategy's fate depend on exactly one thing:
was the directional call right at the $1/$0 snap? If this aggregate beats
v1's 704 materially, v1's exit logic was net-harmful noise. If it's much
worse, the exit logic was doing real protective work and the signal itself
needs attention.
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters (entry signal + sizing copied verbatim from v1) ───────────

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

CLOSE_AT_TTR   = 3.0
WARMUP_TICKS   = 10
IMB_VETO       = -0.04

# No RESOLUTION_HOLD_TTR gate either -- entries allowed any time after warmup.
# No STOP_LOSS_FRAC -- removed entirely, this is the whole point of v3.
ONE_ENTRY_PER_EVENT = True


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        # Per-event state only -- no cross-event bookkeeping needed for this test.
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._tick_count: int = 0
        self._event_traded: bool = False

    def on_tick(self, tick: Tick) -> Signal | None:
        self._tick_count += 1
        ttr = tick.time_to_resolve

        if ttr < CLOSE_AT_TTR:
            return self._close()

        # ── Holding: unconditional. No stop-loss, no early exit -- ride to settlement.
        if self._pos_side is not None:
            return Signal(side=self._pos_side, size=self._pos_size, confidence=1.0)

        # ── Entry ──────────────────────────────────────────────────────────────
        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT
        if self._tick_count < WARMUP_TICKS:
            return FLAT

        score, side = self._directional_score(tick)
        if abs(score) < SIGNAL_MIN or side is None:
            return FLAT

        size = self._compute_size(tick, score)
        if size < 0.05:
            return FLAT

        self._pos_side = side
        self._pos_size = size
        self._event_traded = True

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── Signal computation (unchanged from v1 — majority-vote) ───────────────

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

    def _close(self) -> Signal:
        self._pos_side = None
        self._pos_size = 0.0
        return FLAT
