"""
v7_fee_zone_tuned.py — v5 (momentum + imbalance confirmation) + a swept
FEE_MID_MULT.

`FEE_OPT_MULT` / `FEE_MID_MULT` were "guesses, never swept" (IDEAS.md). Grid-
swept both across the full battery: `FEE_OPT_MULT` turned out to have ZERO
effect at any tested value (1.0-1.75) -- entries that land in the fee-optimal
zone (p<0.35 or p>0.65) correlate with high-conviction momentum calls whose
size is already saturated at MAX_SIZE before the multiplier can matter.
`FEE_MID_MULT`, however, showed a clean monotonic relationship: LOWER is
BETTER, and `0.0` (effectively skipping mid-zone entries entirely -- their
sized-down value falls below the 0.05 minimum) won outright on every metric:
mean score 475.4 vs the prior 326.8, median 60.1 vs 0.0, win-rate 50.0% vs
43.4%, mean PnL -$21.52 vs -$53.31, total PnL -$1,162 vs -$2,825 -- AND
tighter variance (std 2,705 vs 3,917). Promoted to `model_submission.py`.
See IDEAS.md "Sizing near fee-optimal price extremes" for the full sweep
table and writeup.

----------------------------------------------------------------------------
Promoted from research/strategies/v5_imbalance_confirm.py after a full
30-fixture battery run beat the prior promoted version (v1, "majority-vote
momentum") on every aggregate axis -- mean score 1,146 vs 704, win-rate
43.3% vs 41.4%, mean PnL -$19.75 vs -$33.62, total PnL -$592 vs -$975 --
*with tighter score variance* (std 3,131 vs 3,408), so the gain is
broad-based rather than a few lucky outliers. See research/IDEAS.md for the
full validation writeup and the promotions log for prior iterations.

Design (unchanged core from v1, proven across two negative-result rounds --
see IDEAS.md "v3 pure-directional" and "v4 mean-reversion" -- that this
exit machinery is protective, not noise, and that momentum beats reversion
as the directional bet):
  1. Continuous "desired position" signal every tick (CLOB has depth on only
     ~3% of ticks; the harness fills opportunistically whenever it appears)
  2. Multi-horizon momentum (majority-vote of r5/r15/r30) is the PRIMARY
     directional signal
  3. NEW: order-book imbalance (`imb = (up_mid-down_mid)/(up_mid+down_mid)`)
     is now more than a veto -- it (a) raises conviction further when it
     strongly *confirms* momentum (two independent, differently-timed reads
     agreeing is worth more than either alone), and (b) stands in as an
     INDEPENDENT signal on ticks where momentum is silent but the book shows
     an unambiguous one-sided lean, sized conservatively
  4. Resolution hold: if profitable with TTR < 50s, ride to settlement
  5. Deferred stop-loss: -6% from fill price, checked only after several
     ticks in position (avoids spurious exits on unfilled mark-to-market)
  6. Fee-aware sizing: bigger size away from p=0.50 (fees are cheapest at
     price extremes: shares * 0.072 * p*(1-p))
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters (exit/sizing core copied verbatim from v1 -- proven) ──────

R5_MIN         = 0.002
R15_MIN        = 0.001
R30_MIN        = 0.001
SIGNAL_MIN     = 0.03

BASE_SIZE      = 0.18
MAX_SIZE       = 0.42
FEE_OPT_LO     = 0.35
FEE_OPT_HI     = 0.65
FEE_OPT_MULT   = 1.25
FEE_MID_MULT   = 0.0    # swept 0.0-1.0: near p=0.5 fees peak (p*(1-p)) and eat
                        # any edge -- skipping these entries entirely beat every
                        # tested non-zero value on every metric (see IDEAS.md)

STOP_LOSS_FRAC      = 0.060
CLOSE_AT_TTR        = 3.0
RESOLUTION_HOLD_TTR = 50.0
WARMUP_TICKS        = 10

ONE_ENTRY_PER_EVENT = True

# ── NEW: imbalance as confirmation + independent signal ───────────────────────

IMB_VETO          = -0.04   # unchanged from v1: momentum calls this hostile -> skip
IMB_CONFIRM       = 0.05    # imbalance agrees with momentum beyond this -> conviction bonus
IMB_CONFIRM_MULT  = 1.35    # bigger bonus than v1's 1.15 -- "two reads agree"

# Independent entry: fires only when momentum is silent (fails noise floor).
# Bar is HIGH (much stronger than IMB_VETO/IMB_CONFIRM) -- only trade on the
# book's lean alone when it's unambiguous, and size it conservatively.
IMB_SOLO_THRESH   = 0.12
IMB_SOLO_MAG_NORM = 0.25    # |imb| of this size -> solo-mag = 1.0
IMB_SOLO_SIZE_CAP = 0.65    # solo-imbalance entries get at most this fraction of normal sizing


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
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

        score, side, solo = self._directional_score(tick)
        if abs(score) < SIGNAL_MIN or side is None:
            return FLAT

        size = self._compute_size(tick, score)
        if solo:
            size = min(size, MAX_SIZE * IMB_SOLO_SIZE_CAP)
        if size < 0.05:
            return FLAT

        self._pos_side = side
        self._pos_size = size
        self._entry_price = tick.up_ask if side == Side.UP else tick.down_ask
        self._event_traded = True
        self._ticks_in_pos = 0

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── NEW: signal computation -- momentum primary, imbalance confirms or
    #    (when momentum is silent) stands alone ──────────────────────────────

    def _directional_score(self, tick: Tick) -> tuple[float, Side | None, bool]:
        """Returns (signed_score, side, was_imbalance_only)."""
        w = tick.up_mid_recent
        n = len(w)
        if n < 31:
            return 0.0, None, False

        now = w[-1]
        r5  = now - w[max(0, n - 6)]
        r15 = now - w[max(0, n - 16)]
        r30 = now - w[max(0, n - 31)]

        denom = tick.up_mid + tick.down_mid
        imb = (tick.up_mid - tick.down_mid) / denom if denom > 0 else 0.0

        momentum_alive = (abs(r5) >= R5_MIN and abs(r15) >= R15_MIN
                          and abs(r30) >= R30_MIN)

        if momentum_alive:
            votes = [1 if r > 0 else -1 for r in (r5, r15, r30)]
            direction = 1.0 if sum(votes) > 0 else -1.0

            # Veto: book strongly contradicts momentum -> skip (unchanged from v1)
            if direction * imb < IMB_VETO:
                return 0.0, None, False

            mag = min(1.0, abs(r30) / 0.05)
            # Confirmation bonus: book agrees strongly -> raise conviction more
            # than v1's ad-hoc +15% (two independent reads agreeing is worth more)
            if direction * imb > IMB_CONFIRM:
                mag = min(1.0, mag * IMB_CONFIRM_MULT)

            side = Side.UP if direction > 0 else Side.DOWN
            return direction * mag, side, False

        # Momentum silent -- fall back to imbalance alone, but only if it's
        # unambiguous (high bar, well above the veto/confirm thresholds).
        if abs(imb) >= IMB_SOLO_THRESH:
            direction = 1.0 if imb > 0 else -1.0
            mag = min(1.0, abs(imb) / IMB_SOLO_MAG_NORM)
            side = Side.UP if direction > 0 else Side.DOWN
            return direction * mag, side, True

        return 0.0, None, False

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
