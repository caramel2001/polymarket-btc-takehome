"""
v9_near_resolution_btc_confirm.py — Near-resolution entry with REAL Binance
BTC price confirmation (supersedes v8's up_mid-drift proxy).

HYPOTHESIS (user's live-trading strategy, reported 98% win rate over 2 months):
  Entry conditions (ALL must hold):
    1. TTR in [ENTRY_TTR_LO, ENTRY_TTR_HI] — ~15 seconds before market expiry
    2. up_mid OR down_mid is in [PRICE_LO, PRICE_HI] (0.90-0.95) — favourite
       hasn't fully converged to ~$1 yet; buying at 90-95c gives 5-10c
       upside if the outcome matches
    3. Binance BTC price (tick.btc_last) has moved >= BTC_MOVE_MIN (0.06%)
       since the EVENT OPEN, in the same direction as the favoured side

  v8 (research/strategies/v8_near_resolution_confirm.py) validated the
  MECHANISM at 96.8% theoretical win rate using an up_mid-drift proxy,
  because btc_last was always 0 in recordings (Polymarket-only price
  source). The recording service was switched to `price_source="binance"`
  (scripts/record_continuous.py) and restarted 2026-06-09 23:27 — new
  fixtures from that point on have real btc_last/btc_bid/btc_ask populated.
  This variant implements the ORIGINAL spec exactly using that real feed.

  Exit: hold to settlement — no stop-loss (window too short), no early close.

  Hour filter: scaffold included (EXCLUDED_HOURS set), currently empty — user
  mentioned filtering specific hours but did not specify which.

  Note: only fixtures recorded after 2026-06-09 23:27 (binance source) will
  produce any signal here — older fixtures have btc_last == 0 everywhere,
  so `_event_first_btc` is never set and on_tick returns FLAT throughout.
"""

from __future__ import annotations

import datetime

from polybench import FLAT, Model, Side, Signal, Tick

# ── Hyperparameters ───────────────────────────────────────────────────────────

ENTRY_TTR_HI   = 16.0    # ~15s before expiry, per user spec
ENTRY_TTR_LO   = 6.0     # need a few seconds of window for a fill

PRICE_LO       = 0.90    # favourite must be priced in this range
PRICE_HI       = 0.95

BTC_MOVE_MIN   = 0.0006  # 0.06% BTC move since event open, per user spec

ENTRY_SIZE     = 0.40    # near MAX; price extreme = cheapest fee zone

CLOSE_AT_TTR   = 1.0     # safety only

ONE_ENTRY_PER_EVENT = True

# Hours (UTC) to skip — add here once user specifies which hours to filter
EXCLUDED_HOURS: set[int] = set()


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        self._pos_side: Side | None = None
        self._pos_size: float = 0.0
        self._event_traded: bool = False
        self._event_first_btc: float | None = None

    def on_tick(self, tick: Tick) -> Signal | None:
        ttr = tick.time_to_resolve
        btc = tick.btc_last

        if self._event_first_btc is None and btc and btc > 0:
            self._event_first_btc = btc

        if ttr < CLOSE_AT_TTR:
            return self._close()

        if self._pos_side is not None:
            return Signal(side=self._pos_side, size=self._pos_size, confidence=1.0)

        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT

        if not (ENTRY_TTR_LO <= ttr <= ENTRY_TTR_HI):
            return FLAT

        if EXCLUDED_HOURS:
            hour = datetime.datetime.fromtimestamp(tick.ts, tz=datetime.timezone.utc).hour
            if hour in EXCLUDED_HOURS:
                return FLAT

        if not self._event_first_btc or self._event_first_btc <= 0 or not btc:
            return FLAT

        btc_move = (btc - self._event_first_btc) / self._event_first_btc

        if btc_move >= BTC_MOVE_MIN and PRICE_LO <= tick.up_mid <= PRICE_HI:
            side = Side.UP
        elif btc_move <= -BTC_MOVE_MIN and PRICE_LO <= tick.down_mid <= PRICE_HI:
            side = Side.DOWN
        else:
            return FLAT

        self._pos_side = side
        self._pos_size = ENTRY_SIZE
        self._event_traded = True

        return Signal(side=side, size=ENTRY_SIZE, confidence=1.0)

    def _close(self) -> Signal:
        self._pos_side = None
        self._pos_size = 0.0
        return FLAT
