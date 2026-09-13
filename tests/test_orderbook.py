"""Unit tests for the full-depth order book.

The regression these guard against: ``polybench.harness`` reduced every
``price_change`` to a one-level book with size 0, making ~96% of recorded ticks
look unfillable. Anything here that asserts a *size* is defending that fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polyalpha.orderbook import BookSet, OrderBook


def _snap(bk: OrderBook) -> None:
    bk.apply_snapshot(
        bids=[{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "200"}],
        asks=[{"price": "0.42", "size": "150"}, {"price": "0.43", "size": "250"}],
        ts=1.0,
    )


def test_snapshot_sets_best_and_sizes():
    bk = OrderBook(token_id="t")
    _snap(bk)
    assert bk.best_bid == 0.40 and bk.best_bid_size == 100
    assert bk.best_ask == 0.42 and bk.best_ask_size == 150
    assert bk.is_two_sided() and not bk.is_crossed()


def test_delta_is_absolute_set_not_increment():
    """The semantics verified live: size replaces, it does not accumulate."""
    bk = OrderBook(token_id="t")
    _snap(bk)
    bk.apply_delta(price="0.42", size="75", side="SELL", ts=2.0)
    assert bk.best_ask_size == 75


def test_delta_zero_removes_level():
    bk = OrderBook(token_id="t")
    _snap(bk)
    bk.apply_delta(price="0.42", size="0", side="SELL", ts=2.0)
    assert bk.best_ask == 0.43 and bk.best_ask_size == 250


def test_delta_inserts_new_inside_level_in_sorted_position():
    bk = OrderBook(token_id="t")
    _snap(bk)
    bk.apply_delta(price="0.41", size="10", side="SELL", ts=2.0)
    assert bk.best_ask == 0.41
    assert [lv.price for lv in bk.asks.levels()] == [0.41, 0.42, 0.43]


def test_deltas_before_snapshot_are_dropped_not_guessed():
    bk = OrderBook(token_id="t")
    assert bk.apply_delta(price="0.42", size="75", side="SELL", ts=1.0) is False
    assert bk.stats.deltas_before_snapshot == 1
    assert not bk.seeded and bk.best_ask == 0.0


def test_top_of_book_size_survives_a_delta_storm():
    """The exact bug: many price_changes must not zero out the book."""
    bk = OrderBook(token_id="t")
    _snap(bk)
    for i in range(500):
        bk.apply_delta(price="0.43", size=str(200 + i), side="SELL", ts=2.0 + i)
    assert bk.best_ask == 0.42 and bk.best_ask_size == 150  # untouched level intact


def test_walk_the_book_averages_across_levels():
    bk = OrderBook(token_id="t")
    _snap(bk)
    filled, avg = bk.walk("BUY", 200)          # 150@0.42 + 50@0.43
    assert filled == 200
    assert avg == round((150 * 0.42 + 50 * 0.43) / 200, 10)


def test_walk_reports_partial_fill_when_depth_runs_out():
    bk = OrderBook(token_id="t")
    _snap(bk)
    filled, _ = bk.walk("BUY", 10_000)
    assert filled == 400                        # 150 + 250, not 10_000


def test_depth_for_notional():
    bk = OrderBook(token_id="t")
    _snap(bk)
    # $63 buys exactly the 150 shares resting at 0.42
    assert abs(bk.depth_for_notional("BUY", 63.0) - 150.0) < 1e-9


def test_microprice_leans_toward_thinner_side():
    bk = OrderBook(token_id="t")
    _snap(bk)                                   # bid 100 @.40, ask 150 @.42
    assert abs(bk.mid - 0.41) < 1e-9
    assert bk.microprice < bk.mid               # more ask size -> leans to bid


def test_imbalance_sign():
    bk = OrderBook(token_id="t")
    _snap(bk)
    assert bk.imbalance(1) < 0                  # ask-heavy at L1


def test_bookset_routes_by_asset_id_and_handles_trades():
    bs = BookSet()
    bs.apply_message({
        "event_type": "book", "asset_id": "A", "timestamp": "1789245483474",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "150"}],
    })
    bs.apply_message({
        "event_type": "price_change", "timestamp": "1789245484000",
        "price_changes": [
            {"asset_id": "A", "price": "0.41", "size": "20", "side": "SELL"},
            {"asset_id": "B", "price": "0.50", "size": "10", "side": "BUY"},
        ],
    })
    bs.apply_message({
        "event_type": "last_trade_price", "asset_id": "A",
        "price": "0.41", "size": "5", "side": "BUY", "timestamp": "1789245485000",
    })
    a = bs.get("A")
    assert a.best_ask == 0.41 and a.best_ask_size == 20
    assert a.last_trade_price == 0.41 and a.stats.trades == 1
    # B never got a snapshot, so its delta must have been dropped
    assert bs.get("B").seeded is False
    assert bs.get("B").stats.deltas_before_snapshot == 1


def test_ws_millisecond_timestamp_normalised():
    bs = BookSet()
    bs.apply_message({
        "event_type": "book", "asset_id": "A", "timestamp": "1789245483474",
        "bids": [{"price": "0.4", "size": "1"}], "asks": [{"price": "0.5", "size": "1"}],
    })
    assert 1.7e9 < bs.get("A").ts < 1.9e9       # seconds, not milliseconds


def test_bookset_evicts_finished_tokens():
    """A recorder running for days must not grow a book per event forever."""
    bs = BookSet()
    for tid in ("A", "B", "C"):
        bs.apply_message({
            "event_type": "book", "asset_id": tid, "timestamp": "1789245483474",
            "bids": [{"price": "0.4", "size": "1"}], "asks": [{"price": "0.5", "size": "1"}],
        })
    assert len(bs) == 3
    assert bs.evict(["A", "B", "missing"]) == 2
    assert len(bs) == 1 and bs.get("C") is not None and bs.get("A") is None
