"""Tests for the passive-fill simulator.

These pin the conservative choices. A maker backtest that flatters itself is
worse than none, so most of these assert that the model fills us *less* than a
naive reading would.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polyalpha.passive_sim import PassiveSimulator, Side, OrderState


def sim() -> PassiveSimulator:
    return PassiveSimulator(fee_rate=0.0)


def test_queue_must_be_consumed_before_we_fill():
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=100, queue_ahead=250)
    f = s.on_trade(ts=1, token_id="T", price=0.40, size=200, taker_side="SELL")
    assert f == [] and o.filled_size == 0        # 200 < 250 ahead of us
    assert o.queue_ahead == 50
    f = s.on_trade(ts=2, token_id="T", price=0.40, size=120, taker_side="SELL")
    assert len(f) == 1 and f[0].size == 70       # 50 clears queue, 70 fills us
    assert o.remaining == 30 and o.is_open


def test_full_fill_marks_order_filled():
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=100, queue_ahead=0)
    f = s.on_trade(ts=1, token_id="T", price=0.40, size=100, taker_side="SELL")
    assert f[0].size == 100 and o.state is OrderState.FILLED


def test_taker_buy_fills_our_sell_not_our_buy():
    s = sim()
    buy = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=0)
    sell = s.post(ts=0, token_id="T", side=Side.SELL, price=0.42, size=50, queue_ahead=0)
    s.on_trade(ts=1, token_id="T", price=0.42, size=50, taker_side="BUY")
    assert sell.filled_size == 50 and buy.filled_size == 0


def test_print_away_from_our_price_does_not_touch_us():
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=10)
    s.on_trade(ts=1, token_id="T", price=0.45, size=500, taker_side="SELL")
    assert o.filled_size == 0 and o.queue_ahead == 10


def test_trade_through_fills_us_completely():
    """If the market sells below our bid, our level cannot survive."""
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=999)
    f = s.on_trade(ts=1, token_id="T", price=0.38, size=10, taker_side="SELL")
    assert f[0].size == 50 and o.state is OrderState.FILLED


def test_cancellations_are_assumed_behind_us():
    """The conservative choice: shrinking level size does not advance our queue."""
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=500)
    s.on_book(token_id="T", bid=0.40, ask=0.42, bid_size=100, ask_size=10)
    assert o.queue_ahead == 500                  # unchanged: we stay at the back


def test_cancelled_order_never_fills():
    s = sim()
    o = s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=0)
    assert s.cancel(o.order_id) is True
    assert s.on_trade(ts=1, token_id="T", price=0.40, size=50, taker_side="SELL") == []
    assert o.filled_size == 0


def test_orders_are_isolated_by_token():
    s = sim()
    a = s.post(ts=0, token_id="A", side=Side.BUY, price=0.40, size=50, queue_ahead=0)
    b = s.post(ts=0, token_id="B", side=Side.BUY, price=0.40, size=50, queue_ahead=0)
    s.on_trade(ts=1, token_id="A", price=0.40, size=50, taker_side="SELL")
    assert a.filled_size == 50 and b.filled_size == 0


def test_realised_pnl_buy_side():
    s = sim()
    s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=100, queue_ahead=0)
    s.on_trade(ts=1, token_id="T", price=0.40, size=100, taker_side="SELL")
    r = s.realised({"T": 1.0})
    assert abs(r["gross"] - 60.0) < 1e-9         # 100 shares * (1.00 - 0.40)
    assert r["net"] == r["gross"]                # fee_rate 0


def test_realised_pnl_sell_side_and_loss():
    s = sim()
    s.post(ts=0, token_id="T", side=Side.SELL, price=0.60, size=100, queue_ahead=0)
    s.on_trade(ts=1, token_id="T", price=0.60, size=100, taker_side="BUY")
    r = s.realised({"T": 1.0})
    assert abs(r["gross"] - (-40.0)) < 1e-9      # sold at 0.60, settles at 1.00


def test_harness_fee_is_charged_when_configured():
    s = PassiveSimulator(fee_rate=0.072)
    s.post(ts=0, token_id="T", side=Side.BUY, price=0.50, size=100, queue_ahead=0)
    s.on_trade(ts=1, token_id="T", price=0.50, size=100, taker_side="SELL")
    r = s.realised({"T": 1.0})
    assert abs(r["fees"] - 100 * 0.072 * 0.25) < 1e-9   # 1.8c/share at p=0.50


def test_fill_rate_stats():
    s = sim()
    s.post(ts=0, token_id="T", side=Side.BUY, price=0.40, size=50, queue_ahead=0)
    s.post(ts=0, token_id="T", side=Side.BUY, price=0.39, size=50, queue_ahead=1e6)
    s.on_trade(ts=1, token_id="T", price=0.40, size=50, taker_side="SELL")
    st = s.stats()
    assert st["orders_posted"] == 2 and st["orders_filled"] == 1
    assert abs(st["fill_rate"] - 0.5) < 1e-9
