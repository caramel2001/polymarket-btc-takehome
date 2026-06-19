"""
v10_btc_fairvalue_kelly.py — Binance fair-value model with Kelly position sizing.

HYPOTHESIS:
  Polymarket BTC 5m up/down markets systematically UNDER-react to the live
  Binance BTC price. For a market that resolves on "BTC at expiry > BTC at
  event open", the fair probability of UP is a function of the current BTC
  distance from event open scaled by remaining-horizon volatility:

      z      = ln(btc_now / btc_open) / (SIGMA_1S * sqrt(ttr))
      p_fair = sigmoid(A + B * z)        # logistic calibrated on live tape

  When p_fair - up_ask (or (1 - p_fair) - down_ask) exceeds EDGE_MIN, the
  market is lagging the BTC feed — buy the underpriced side and hold to
  settlement.

CALIBRATION (38 Binance-backed fixtures, 449 complete events, 2026-06-09 →
2026-06-11, temporal 50/50 train/test split):
  - SIGMA_1S = 0.719 bps (mean 1s BTC log-return std across fixtures)
  - logistic fit on train half: A=0.029, B=1.474 (raw probit would be B≈1.7;
    B<1.7 reflects fat tails / oracle-vs-Binance divergence shrinkage)
  - empirical mispricing: at TTR 10-130s, BTC 1-4 bps from open, the favoured
    side's true win rate exceeds its mid by 7-15 cents
  - event-level first-trigger sim (buy at ask, fee 0.072*p*(1-p), hold to
    settle), EDGE_MIN=0.12, TTR [40,240]:
        train: n=112, +7.1c/share | test: n=130, +7.7c/share  (consistent)
    Short-TTR-only windows ([8,40]) FAILED out-of-sample (-1 to -6c) — late
    broad entries are adversely selected; mid-event entries are where the
    lag edge lives.

POSITION SIZING (the point of this iteration):
  Binary-payout Kelly buying at ask `a` with win prob `p`:
      f* = (p - a) / (1 - a)
  We size HALF-Kelly, capped at MAX_FRACTION. On the train/test split,
  flat-0.40 sizing degraded ~50% out-of-sample ($14.1k → $7.2k, fixture
  Sharpe 0.41 → 0.24) while half-Kelly was stable ($3.3k → $3.6k,
  Sharpe 0.39 → 0.35). Edge-proportional sizing self-throttles when the
  model is less confident.

  Exit: hold THROUGH settlement (5-minute markets; the model's edge is in the
  resolution, not the path). No TTR-based close: emitting FLAT near expiry
  makes the simulator SELL at the bid (spread + slippage + fee) instead of
  collecting $1/share at resolution — v8/v9's "safety close at TTR<1" was
  silently costing 1-6c/share every single event.

HARNESS QUIRK — constant-share hold:
  The PnL simulator retargets `size * starting_capital / current_ask` shares
  EVERY tick. Returning a constant size while the ask moves therefore churns
  (buys dips, sells rips, paying spread + 50bps slippage + fee on each delta).
  To hold N shares we re-emit size_t = N * ask_t / starting_capital each tick
  so the harness target matches the held position.

  NOTE: only meaningful on fixtures with a real Binance feed
  (btc_source='binance', recorded after 2026-06-09 15:27 UTC). On older
  fixtures btc_last == 0 and the model stays flat. Evaluate with
  `research/eval_battery.py --binance-only`.
"""

from __future__ import annotations

import math

from polybench import FLAT, Model, Side, Signal, Tick

# ── Calibrated parameters (see docstring) ─────────────────────────────────────

SIGMA_1S    = 0.719e-4   # 1-second BTC log-return vol
LOGIT_A     = 0.029      # logistic intercept
LOGIT_B     = 1.474      # logistic slope on probit z

EDGE_MIN    = 0.25       # p_fair - ask must exceed this to enter
# 0.12 looked best with instant fills, but replay executes signals with a
# ONE-TICK delay and the market reprices toward BTC within that tick
# (fill is +4.1c worse than trigger on average). Under delayed fills the
# thin-edge trades net only ~+2.6c/share (eaten by hold-rebalance churn);
# EDGE_MIN=0.25 nets +19.7c/share train, +14.7c test (n=18/26, win 62-67%).
ENTRY_TTR_HI = 240.0     # mid-event entry window
ENTRY_TTR_LO = 40.0

BTC_MOVE_MIN_BPS = 6.0   # |BTC move since open| must exceed this (log bps).
# Masks Binance-vs-resolution-oracle basis noise: in battery traces, the
# losing high-edge entries clustered at 5-8bps moves where the model is
# overconfident relative to the oracle the market actually settles on.
# Matches the user's live-validated 0.06% filter. With EDGE_MIN=0.25:
# train n=10 win 80.0% +20.6c/sh, test n=11 win 81.8% +19.0c/sh.

ASK_LO      = 0.05       # never buy near-worthless or near-converged books
ASK_HI      = 0.97

KELLY_FRACTION = 0.50    # half-Kelly
MAX_FRACTION   = 0.80    # hard cap on size (fraction of starting capital)

ONE_ENTRY_PER_EVENT = True


def _p_fair(btc: float, btc_open: float, ttr: float) -> float:
    z = math.log(btc / btc_open) / (SIGMA_1S * math.sqrt(max(ttr, 1.0)))
    return 1.0 / (1.0 + math.exp(-(LOGIT_A + LOGIT_B * z)))


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        self._pos_side: Side | None = None
        self._entry_size: float = 0.0
        self._entry_ask: float = 0.0
        self._last_size: float = 0.0
        self._event_traded: bool = False
        self._event_first_btc: float | None = None

    def on_tick(self, tick: Tick) -> Signal | None:
        ttr = tick.time_to_resolve
        btc = tick.btc_last

        if self._event_first_btc is None and btc and btc > 0:
            self._event_first_btc = btc

        if self._pos_side is not None:
            # Hold a CONSTANT share count: the harness targets
            # size * capital / ask shares each tick, so size must scale with
            # the current ask to keep the trade delta at zero.
            ask = tick.up_ask if self._pos_side == Side.UP else tick.down_ask
            if ask > 0.0 and self._entry_ask > 0.0:
                self._last_size = min(1.0, self._entry_size * ask / self._entry_ask)
            return Signal(side=self._pos_side, size=self._last_size, confidence=1.0)

        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT

        if not (ENTRY_TTR_LO <= ttr <= ENTRY_TTR_HI):
            return FLAT

        if not self._event_first_btc or self._event_first_btc <= 0 or not btc or btc <= 0:
            return FLAT

        if abs(math.log(btc / self._event_first_btc)) * 1e4 < BTC_MOVE_MIN_BPS:
            return FLAT

        p = _p_fair(btc, self._event_first_btc, ttr)

        side: Side | None = None
        if p - tick.up_ask > EDGE_MIN and ASK_LO < tick.up_ask < ASK_HI:
            side, p_win, ask = Side.UP, p, tick.up_ask
        elif (1.0 - p) - tick.down_ask > EDGE_MIN and ASK_LO < tick.down_ask < ASK_HI:
            side, p_win, ask = Side.DOWN, 1.0 - p, tick.down_ask

        if side is None:
            return FLAT

        kelly = (p_win - ask) / (1.0 - ask)
        size = min(MAX_FRACTION, max(0.0, KELLY_FRACTION * kelly))
        if size <= 0.0:
            return FLAT

        self._pos_side = side
        self._entry_size = size
        self._entry_ask = ask
        self._last_size = size
        self._event_traded = True

        return Signal(side=side, size=size, confidence=min(1.0, p_win))
