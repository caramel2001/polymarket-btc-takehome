"""
v11_endgame_convergence.py — Capital-preservation endgame convergence capture.

CONTEXT (2026-06-18, the "[✗] DEFINITIVE" finding in IDEAS.md):
  Across 195 Binance-backed fixtures (~9 days, 2,348 events) the BTC 5m market
  was proven EFFICIENT on six independent axes — no directional / fair-value /
  favorite-fade / lead-lag / cross-event edge survives taker-only execution
  with 1-tick latency. v10 (directional fair value) is a dead end.

  Goal therefore shifts from "directional alpha" (impossible) to CAPITAL
  PRESERVATION: stay flat almost always, and trade only the ONE setup that is
  reliably non-negative out-of-sample.

THE ONE SETUP THAT SURVIVES TRAIN/TEST:
  In the final 0.5-3 seconds before resolution, a near-decided favourite
  priced at 0.95-0.99 still has a few cents of residual convergence to $1 that
  the market leaves on the table (liquidity/attention thins out in the last
  seconds). Buying it and holding to settlement (zero exit cost) captures that.

  Validation (temporal 50/50 split, buy at ask·(1+0.5% slip) one tick after
  trigger, fee 0.072·p·(1−p), hold to settlement):
    config: ttr ∈ [0.5, 3.0], favourite ask ∈ [0.95, 0.99]
      train: n=133, win 99.25%, +1.04c/share, total +$1.39
      test:  n=124, win 99.19%, +0.84c/share, total +$1.04
  Both halves positive — the ONLY setup in the whole research arc that does
  not flip negative out-of-sample. The wider ttr ∈ [1,4] window does NOT hold
  (train −0.11c), so the edge is specifically the final ~3 seconds.

DESIGN:
  - Default FLAT. No fees, no losses, no exposure 99% of the time.
  - Enter once per event when ttr ∈ [ENTRY_TTR_LO, ENTRY_TTR_HI] AND a side's
    ask ∈ [ASK_LO, ASK_HI]. Buy that side.
  - Conservative SIZE: the edge is tiny (~+0.85c/share) and the ~0.8% upset is
    a near-total loss on the position, so size small for capital preservation.
    EV is positive but realized PnL variance is dominated by the rare loss;
    small size keeps any single-event drawdown minor.
  - Hold THROUGH settlement: never emit FLAT near expiry (that sells at the bid
    instead of settling at $1 — see IDEAS.md harness quirks). Re-emit a
    constant-share hold each remaining tick.

  This will NOT make large profits — nothing in this market does. It aims to be
  slightly positive / near-breakeven with a very high hit rate (≈99%), hence
  high Sharpe and tiny drawdown, which also scores well on the primary metric.
"""

from __future__ import annotations

from polybench import FLAT, Model, Side, Signal, Tick

# ── Parameters (validated on train/test split) ────────────────────────────────

ENTRY_TTR_HI = 3.0       # only the final few seconds
ENTRY_TTR_LO = 0.5

ASK_LO       = 0.95      # near-decided favourite, but with convergence left
ASK_HI       = 0.99      # above this, no residual worth the fee/upset risk

ENTRY_SIZE   = 0.20      # conservative — capital preservation, not maximisation

ONE_ENTRY_PER_EVENT = True


class ModelSubmission(Model):

    def on_start(self, market_info=None) -> None:
        self._pos_side: Side | None = None
        self._entry_size: float = 0.0
        self._entry_ask: float = 0.0
        self._last_size: float = 0.0
        self._event_traded: bool = False

    def on_tick(self, tick: Tick) -> Signal | None:
        # Hold an existing position through settlement (constant share count).
        if self._pos_side is not None:
            ask = tick.up_ask if self._pos_side == Side.UP else tick.down_ask
            if ask > 0.0 and self._entry_ask > 0.0:
                self._last_size = min(1.0, self._entry_size * ask / self._entry_ask)
            return Signal(side=self._pos_side, size=self._last_size, confidence=1.0)

        if ONE_ENTRY_PER_EVENT and self._event_traded:
            return FLAT

        ttr = tick.time_to_resolve
        if not (ENTRY_TTR_LO <= ttr <= ENTRY_TTR_HI):
            return FLAT

        side: Side | None = None
        if ASK_LO <= tick.up_ask <= ASK_HI:
            side, ask = Side.UP, tick.up_ask
        elif ASK_LO <= tick.down_ask <= ASK_HI:
            side, ask = Side.DOWN, tick.down_ask

        if side is None:
            return FLAT

        self._pos_side = side
        self._entry_size = ENTRY_SIZE
        self._entry_ask = ask
        self._last_size = ENTRY_SIZE
        self._event_traded = True
        return Signal(side=side, size=ENTRY_SIZE, confidence=1.0)
