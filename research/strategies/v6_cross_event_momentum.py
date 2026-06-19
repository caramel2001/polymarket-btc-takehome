"""
v6_cross_event_momentum.py — Use the previous event's drift as a prior for
the first ~10 ticks of a new event, when the single largest depth cluster
sits unreachable behind the in-event momentum signal's warmup requirement.

Context: builds on the promoted `model_submission.py`
(research/strategies/v5_imbalance_confirm.py, "v3: momentum + imbalance
confirmation" — full battery: mean score 1,146, win-rate 43.3%, beats prior
on every axis with tighter variance). That strategy (like v1 before it)
can't act on the single biggest depth cluster of every event: ticks 0-5,
where two-sided book depth is often massive (e.g. event 561014 tick 1:
up_ask_size=57, down_ask_size=1059) -- because `up_mid_recent` is reset on
event rollover and the momentum signal needs ~31 samples plus
`WARMUP_TICKS = 10` before it can fire. By the time it can, that depth
window has often closed.

NEW HYPOTHESIS: BTC's short-term momentum carries across consecutive 5-
minute windows often enough to be a useful (if weak) PRIOR for the opening
ticks of a new event -- before the in-event signal has enough history to
speak for itself. Track, across event rollovers:
  - `_prev_drift`: net up_mid movement over the previous event
    (final up_mid_recent sample minus the first) -- "did the market trend
    up or down last window"
  - `_prev_resolved_up`: which side actually won the previous event
    (inferred the same way v2 did: up_mid converging to ~0/~1 near ttr->0)//
Both agreeing on a direction, with sufficient magnitude, becomes a
LOW-CONVICTION entry signal usable ONLY during the early-event window
(tick_count < WARMUP_TICKS) where the in-event signal is silent --
i.e. this never competes with or overrides the (already-validated)
in-event momentum+imbalance signal; it only fires where that signal cannot.

Sized conservatively (at most CROSS_EVENT_SIZE_CAP of normal max) since
it's a weaker, more speculative bet than the in-event signal -- and because
each event is nominally an independent question, so a stale prior could
easily be just noise.

Falsification: if this doesn't beat v5/model_submission's mean (1,146),
median (106), AND win-rate (43.3%) without blowing up variance, cross-event
information doesn't carry into the next window -- each 5-minute BTC
question really is close to independent, and the early-event depth cluster
should be left untraded (a "we don't have an edge here yet" zero, which the
scoring math treats kindly -- see IDEAS.md's smooth-loser finding).
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters (entry/exit/sizing core copied verbatim from v5/promoted) ─

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

IMB_VETO          = -0.04
IMB_CONFIRM       = 0.05
IMB_CONFIRM_MULT  = 1.35

IMB_SOLO_THRESH   = 0.12
IMB_SOLO_MAG_NORM = 0.25
IMB_SOLO_SIZE_CAP = 0.65

# ── NEW: cross-event prior ─────────────────────────────────────────────────────
# How much the previous event must have drifted (in up_mid units) before its
# direction counts as a usable prior -- below this, treat it as noise.
PREV_DRIFT_MIN        = 0.03
# Resolution-outcome inference (copied from v2 -- proven to work mechanically)
RESOLUTION_INFER_TTR    = 4.0
RESOLUTION_INFER_MARGIN = 0.15
# Cross-event entries are capped well below normal sizing -- weaker, more
# speculative bet; we want exposure to the depth cluster without betting big
# on a prior that might just be noise.
CROSS_EVENT_SIZE_CAP  = 0.45
CROSS_EVENT_MAG       = 0.5   # fixed, modest conviction (no in-event evidence yet)


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        # Cross-event state -- persists across the whole run, init once.
        if not hasattr(self, "_prev_drift"):
            self._prev_drift: float = 0.0          # signed net up_mid move, prior event
            self._prev_resolved_up: bool | None = None
        if not hasattr(self, "_event_first_um"):
            self._event_first_um: float | None = None

        # Snapshot the event we're LEAVING (only meaningful from 2nd event on):
        # _event_first_um / _event_last_um were updated every tick of the prior
        # event; carry them into _prev_* before resetting for the new event.
        if self._event_first_um is not None and hasattr(self, "_event_last_um"):
            self._prev_drift = self._event_last_um - self._event_first_um
            if hasattr(self, "_event_resolved_up") :
                self._prev_resolved_up = self._event_resolved_up

        # Per-event state
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._entry_price: float = 0.0
        self._tick_count: int = 0
        self._event_traded: bool = False
        self._ticks_in_pos: int = 0
        self._cross_event_entry: bool = False

        self._event_first_um = None
        self._event_last_um: float = 0.0
        self._event_resolved_up: bool | None = None
        self._outcome_inferred: bool = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> Signal | None:
        self._tick_count += 1
        ttr = tick.time_to_resolve
        um = tick.up_mid

        # Track this event's drift (first vs latest up_mid sample).
        if self._event_first_um is None:
            self._event_first_um = um
        self._event_last_um = um

        # Infer this event's resolution outcome near settlement (same
        # mechanism v2 used -- up_mid converges to ~0/~1 as ttr -> 0).
        if not self._outcome_inferred and ttr < RESOLUTION_INFER_TTR:
            if um <= RESOLUTION_INFER_MARGIN or um >= 1.0 - RESOLUTION_INFER_MARGIN:
                self._event_resolved_up = um >= 0.5
                self._outcome_inferred = True

        if ttr < CLOSE_AT_TTR:
            return self._close()

        # ── EXIT LOGIC for existing position (unchanged from v5) ─────────────
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
        if ttr < RESOLUTION_HOLD_TTR:
            return FLAT

        # NEW: early-event window -- in-event momentum can't fire yet
        # (needs ~31 up_mid_recent samples + WARMUP_TICKS), but this is
        # exactly where the largest two-sided depth cluster sits. Try the
        # cross-event prior here ONLY -- it never competes with the
        # validated in-event signal below.
        if self._tick_count < WARMUP_TICKS:
            return self._cross_event_entry_signal(tick)

        score, side, solo = self._directional_score(tick)
        if abs(score) < SIGNAL_MIN or side is None:
            return FLAT

        size = self._compute_size(tick, score)
        if solo:
            size = min(size, MAX_SIZE * IMB_SOLO_SIZE_CAP)
        if size < 0.05:
            return FLAT

        self._open(side, size, tick)
        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── NEW: cross-event prior entry (only valid pre-warmup) ─────────────────

    def _cross_event_entry_signal(self, tick: Tick) -> Signal | None:
        if self._prev_resolved_up is None:
            return FLAT
        if abs(self._prev_drift) < PREV_DRIFT_MIN:
            return FLAT

        # Continuation hypothesis: bet that the side which won (and the
        # direction the market drifted) last event tends to repeat. Both
        # reads must agree -- if the prior event's drift contradicts its own
        # resolution (e.g. drifted down but UP still won -- a noisy/choppy
        # event), treat the prior as unreliable and skip.
        drift_side_up = self._prev_drift > 0
        if drift_side_up != self._prev_resolved_up:
            return FLAT

        side = Side.UP if self._prev_resolved_up else Side.DOWN
        size = min(MAX_SIZE * CROSS_EVENT_SIZE_CAP,
                   BASE_SIZE + (MAX_SIZE - BASE_SIZE) * CROSS_EVENT_MAG)
        if size < 0.05:
            return FLAT

        self._open(side, size, tick)
        self._cross_event_entry = True
        return Signal(side=side, size=size, confidence=CROSS_EVENT_MAG)

    def _open(self, side: Side, size: float, tick: Tick) -> None:
        self._pos_side = side
        self._pos_size = size
        self._entry_price = tick.up_ask if side == Side.UP else tick.down_ask
        self._event_traded = True
        self._ticks_in_pos = 0

    # ── Signal computation (unchanged from v5 -- momentum primary, imbalance
    #    confirms or stands alone) ────────────────────────────────────────────

    def _directional_score(self, tick: Tick) -> tuple[float, Side | None, bool]:
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

            if direction * imb < IMB_VETO:
                return 0.0, None, False

            mag = min(1.0, abs(r30) / 0.05)
            if direction * imb > IMB_CONFIRM:
                mag = min(1.0, mag * IMB_CONFIRM_MULT)

            side = Side.UP if direction > 0 else Side.DOWN
            return direction * mag, side, False

        if abs(imb) >= IMB_SOLO_THRESH:
            direction = 1.0 if imb > 0 else -1.0
            mag = min(1.0, abs(imb) / IMB_SOLO_MAG_NORM)
            side = Side.UP if direction > 0 else Side.DOWN
            return direction * mag, side, True

        return 0.0, None, False

    # ── Sizing (unchanged from v5) ────────────────────────────────────────────

    def _compute_size(self, tick: Tick, score: float) -> float:
        size = BASE_SIZE + (MAX_SIZE - BASE_SIZE) * min(1.0, abs(score))
        um = tick.up_mid
        if um < FEE_OPT_LO or um > FEE_OPT_HI:
            size = min(MAX_SIZE, size * FEE_OPT_MULT)
        else:
            size *= FEE_MID_MULT
        return max(0.05, min(MAX_SIZE, size))

    # ── Helpers (unchanged from v5) ───────────────────────────────────────────

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
