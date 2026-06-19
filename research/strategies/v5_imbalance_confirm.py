"""
v5_imbalance_confirm.py — Order-book imbalance as an independent signal
source, not just a veto on momentum.

Context: v4 (`v4_mean_reversion.py`, REJECTED — see IDEAS.md) proved
momentum is the right directional bet on this tape (reversion was strictly
worse on every metric: mean score 291.7 vs v1's 704, median 0.0 vs 211.9,
total PnL -$2,645 vs -$975). v3 proved v1's exit machinery is protective,
not noise. So the lever left is: refine WHICH momentum calls to trust /
when to also trust a second, independent signal.

`imb = (up_mid - down_mid) / (up_mid + down_mid)` is a *forward-looking*,
market-aggregated read (the live order book's collective lean) -- distinct
from momentum's *backward-looking* read (recent price changes). v1 only
ever uses it as a veto (`direction * imb < IMB_VETO -> skip`). Two changes:

  1. CONFIRMATION BONUS (replaces v1's small ad-hoc +15% mag boost): when
     imbalance and momentum agree strongly, raise conviction (and therefore
     size) more aggressively -- "two independent reads agree" should be
     worth more than either alone.

  2. INDEPENDENT IMBALANCE ENTRY: on ticks where momentum is silent (one or
     more horizons fail the noise floor -- this happens often; v1 just
     returns FLAT), but the order book shows a STRONG, one-sided lean
     (|imb| above a high bar), trade on imbalance alone, sized smaller
     (lower conviction than a momentum+imbalance double-confirmation).
     This tests whether the book's aggregate lean carries standalone
     directional information momentum misses.

Exit machinery is v1's, unchanged (proven protective by the v3 result).

Falsification: if this doesn't beat v1's mean score (704) AND median (212)
AND doesn't blow up variance the way v3's "more trades" pattern did, then
imbalance carries no usable information beyond what it already contributes
as a veto, and entry-signal refinement is likely near its ceiling on this
tape -- the next lever would have to be sizing/risk, not direction-calling.
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
FEE_MID_MULT   = 0.80

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
