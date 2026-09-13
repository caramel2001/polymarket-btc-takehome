"""Full-depth order book maintained from Polymarket CLOB WebSocket messages.

Why this module exists
----------------------
``polybench.harness`` reconstructs books by calling ``_book_from_top`` on every
``price_change`` message, producing a **one-level synthetic book** whose size is
read from ``best_bid_size`` / ``best_ask_size``. Those keys do not exist in the
live payload::

    {"asset_id": "...", "price": "0.39", "size": "208.93", "side": "BUY",
     "hash": "...", "best_bid": "0.39", "best_ask": "0.4"}

so the size resolves to ``0.0`` and the good full-depth snapshot in the cache is
overwritten by a zero-size stub. Since ``price_change`` outnumbers ``book`` by
roughly 16:1 on the wire, ~96% of recorded ticks end up looking unfillable. That
artifact — not market illiquidity — is what produced the "97% of ticks have
ask_size=0" finding in ``research/IDEAS.md``.

Correct semantics, verified against a live REST snapshot (87/87 levels matched
exactly after applying 2,974 deltas): a ``price_change`` carries the **new
absolute resting size at one price level**. ``size == 0`` removes the level;
anything else assigns it.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__all__ = ["Level", "BookSide", "OrderBook", "BookStats"]

# Polymarket prices live on a 1c (sometimes 0.1c) grid in [0, 1]. Rounding on
# ingest keeps float keys from splitting a level in two.
_PRICE_DP = 4


def _px(value: Any) -> float:
    return round(float(value), _PRICE_DP)


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    size: float


class BookSide:
    """One side of the book as a price -> size map with a sorted price index.

    Kept sorted incrementally rather than re-sorting per message: at ~300
    messages/second across four assets, re-sorting the whole side on every
    delta is the difference between keeping up with the feed and falling behind.
    """

    __slots__ = ("_sizes", "_prices", "_descending")

    def __init__(self, *, descending: bool) -> None:
        self._sizes: dict[float, float] = {}
        self._prices: list[float] = []       # ascending; reversed on read for bids
        self._descending = descending

    def clear(self) -> None:
        self._sizes.clear()
        self._prices.clear()

    def replace(self, levels: Iterable[Any]) -> None:
        """Reseed from a full snapshot (``book`` message)."""
        self.clear()
        for lv in levels or ():
            price = _px(lv["price"] if isinstance(lv, dict) else lv.price)
            size = float(lv["size"] if isinstance(lv, dict) else lv.size)
            if size > 0.0:
                self._sizes[price] = size
        self._prices = sorted(self._sizes)

    def set_level(self, price: float, size: float) -> None:
        """Apply one delta. ``size <= 0`` deletes the level."""
        price = _px(price)
        size = float(size)
        if size <= 0.0:
            if self._sizes.pop(price, None) is not None:
                # list.remove is O(n) but n is the level count (~30-90), and
                # deletions are a minority of deltas.
                self._prices.remove(price)
            return
        if price not in self._sizes:
            insort(self._prices, price)
        self._sizes[price] = size

    def best(self) -> Level | None:
        if not self._prices:
            return None
        price = self._prices[-1] if self._descending else self._prices[0]
        return Level(price=price, size=self._sizes[price])

    def levels(self, depth: int | None = None) -> list[Level]:
        """Best-first. Bids descend in price, asks ascend."""
        prices = reversed(self._prices) if self._descending else iter(self._prices)
        out = [Level(price=p, size=self._sizes[p]) for p in prices]
        return out[:depth] if depth is not None else out

    def notional(self, depth: int | None = None) -> float:
        return sum(lv.price * lv.size for lv in self.levels(depth))

    def total_size(self, depth: int | None = None) -> float:
        return sum(lv.size for lv in self.levels(depth))

    def __len__(self) -> int:
        return len(self._prices)


@dataclass(slots=True)
class BookStats:
    """Feed-health counters. Used to prove the capture is sound, not assume it."""

    snapshots: int = 0
    deltas_applied: int = 0
    deltas_before_snapshot: int = 0   # dropped: no snapshot to apply them to
    trades: int = 0
    crossed_observed: int = 0


@dataclass(slots=True)
class OrderBook:
    """Full-depth book for one CLOB token.

    Not fillable until ``seeded`` — a delta stream with no snapshot beneath it
    describes changes to a book we have never seen, and guessing is how you get
    a plausible-looking book that is quietly wrong.
    """

    token_id: str
    bids: BookSide = field(default_factory=lambda: BookSide(descending=True))
    asks: BookSide = field(default_factory=lambda: BookSide(descending=False))
    seeded: bool = False
    ts: float = 0.0
    last_trade_price: float = 0.0
    last_trade_size: float = 0.0
    last_trade_side: str = ""
    last_trade_ts: float = 0.0
    stats: BookStats = field(default_factory=BookStats)

    # ---- ingest -----------------------------------------------------------

    def apply_snapshot(self, bids: Iterable[Any], asks: Iterable[Any], ts: float) -> None:
        self.bids.replace(bids)
        self.asks.replace(asks)
        self.seeded = True
        self.ts = ts
        self.stats.snapshots += 1

    def apply_delta(self, price: Any, size: Any, side: str, ts: float) -> bool:
        """Apply one ``price_changes[]`` entry. Returns False if dropped."""
        if not self.seeded:
            self.stats.deltas_before_snapshot += 1
            return False
        target = self.bids if str(side).upper() in {"BUY", "BID"} else self.asks
        target.set_level(price, size)
        self.ts = ts
        self.stats.deltas_applied += 1
        if self.is_crossed():
            self.stats.crossed_observed += 1
        return True

    def apply_trade(self, price: Any, size: Any, side: str, ts: float) -> None:
        self.last_trade_price = _px(price)
        self.last_trade_size = float(size)
        self.last_trade_side = str(side).upper()
        self.last_trade_ts = ts
        self.stats.trades += 1

    # ---- reads ------------------------------------------------------------

    @property
    def best_bid(self) -> float:
        lv = self.bids.best()
        return lv.price if lv else 0.0

    @property
    def best_ask(self) -> float:
        lv = self.asks.best()
        return lv.price if lv else 0.0

    @property
    def best_bid_size(self) -> float:
        lv = self.bids.best()
        return lv.size if lv else 0.0

    @property
    def best_ask_size(self) -> float:
        lv = self.asks.best()
        return lv.size if lv else 0.0

    @property
    def mid(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        if bid > 0.0 and ask > 0.0:
            return (bid + ask) / 2.0
        return self.last_trade_price or max(bid, ask)

    @property
    def spread(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        return ask - bid if bid > 0.0 and ask > 0.0 else 0.0

    @property
    def microprice(self) -> float:
        """Size-weighted mid. Leans toward the side with less resting size,
        which is the side more likely to be consumed next."""
        bs, as_ = self.best_bid_size, self.best_ask_size
        if bs <= 0.0 or as_ <= 0.0:
            return self.mid
        return (self.best_bid * as_ + self.best_ask * bs) / (bs + as_)

    def is_crossed(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bid > 0.0 and ask > 0.0 and bid >= ask

    def is_two_sided(self) -> bool:
        return self.best_bid > 0.0 and self.best_ask > 0.0

    def imbalance(self, depth: int = 1) -> float:
        """(bid − ask) / (bid + ask) resting size over ``depth`` levels."""
        b = self.bids.total_size(depth)
        a = self.asks.total_size(depth)
        return (b - a) / (b + a) if (b + a) > 0.0 else 0.0

    # ---- execution --------------------------------------------------------

    def walk(self, side: str, shares: float) -> tuple[float, float]:
        """Walk the book to fill ``shares``.

        ``side='BUY'`` consumes asks, ``'SELL'`` consumes bids. Returns
        ``(filled_shares, avg_price)``. Partial fills when depth runs out — the
        caller must check, because silently reporting a full fill against a
        book that could not supply it is exactly the error this module exists
        to undo.
        """
        if shares <= 0.0:
            return 0.0, 0.0
        levels = self.asks.levels() if str(side).upper() == "BUY" else self.bids.levels()
        remaining = shares
        notional = 0.0
        for lv in levels:
            take = min(remaining, lv.size)
            notional += take * lv.price
            remaining -= take
            if remaining <= 1e-9:
                break
        filled = shares - remaining
        return (filled, notional / filled) if filled > 0.0 else (0.0, 0.0)

    def depth_for_notional(self, side: str, dollars: float) -> float:
        """Shares obtainable for ``dollars`` — the practical sizing question."""
        if dollars <= 0.0:
            return 0.0
        levels = self.asks.levels() if str(side).upper() == "BUY" else self.bids.levels()
        budget, shares = dollars, 0.0
        for lv in levels:
            if lv.price <= 0.0:
                continue
            cost = lv.price * lv.size
            if cost <= budget:
                budget -= cost
                shares += lv.size
            else:
                shares += budget / lv.price
                return shares
        return shares

    def snapshot_row(self, depth: int = 5) -> dict[str, float]:
        """Flatten to recorder columns: top-``depth`` levels per side."""
        row: dict[str, float] = {
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "best_bid_size": self.best_bid_size,
            "best_ask_size": self.best_ask_size,
            "mid": self.mid,
            "microprice": self.microprice,
            "spread": self.spread,
            "imbalance_l1": self.imbalance(1),
            "imbalance_l5": self.imbalance(5),
            "bid_depth_notional": self.bids.notional(depth),
            "ask_depth_notional": self.asks.notional(depth),
            "n_bid_levels": float(len(self.bids)),
            "n_ask_levels": float(len(self.asks)),
            "last_trade_price": self.last_trade_price,
            "last_trade_size": self.last_trade_size,
        }
        for i, lv in enumerate(self.bids.levels(depth)):
            row[f"bid_px_{i}"], row[f"bid_sz_{i}"] = lv.price, lv.size
        for i, lv in enumerate(self.asks.levels(depth)):
            row[f"ask_px_{i}"], row[f"ask_sz_{i}"] = lv.price, lv.size
        return row


class BookSet:
    """Routes a multiplexed WS feed to per-token books."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def book(self, token_id: str) -> OrderBook:
        bk = self._books.get(token_id)
        if bk is None:
            bk = OrderBook(token_id=token_id)
            self._books[token_id] = bk
        return bk

    def get(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def evict(self, token_ids: Iterable[str]) -> int:
        """Drop books for tokens we no longer track.

        Without this the set grows by two books per event forever — ~240 tokens
        an hour across the universe — which matters for a recorder meant to run
        for days.
        """
        dropped = 0
        for tid in list(token_ids):
            if self._books.pop(tid, None) is not None:
                dropped += 1
        return dropped

    def __len__(self) -> int:
        return len(self._books)

    def __iter__(self) -> Iterator[OrderBook]:
        return iter(self._books.values())

    def apply_message(self, msg: dict[str, Any]) -> list[str]:
        """Apply one decoded WS message. Returns touched token ids.

        Handles ``book`` (snapshot), ``price_change`` (deltas), and
        ``last_trade_price`` (prints). Unknown types are ignored rather than
        guessed at.
        """
        event_type = str(msg.get("event_type") or msg.get("type") or "")
        ts = _ws_ts(msg.get("timestamp"))
        touched: list[str] = []

        if event_type == "book":
            token_id = str(msg.get("asset_id") or "")
            if token_id:
                self.book(token_id).apply_snapshot(
                    msg.get("bids") or (), msg.get("asks") or (), ts
                )
                touched.append(token_id)

        elif event_type == "price_change":
            for ch in msg.get("price_changes") or ():
                if not isinstance(ch, dict):
                    continue
                token_id = str(ch.get("asset_id") or "")
                if not token_id:
                    continue
                self.book(token_id).apply_delta(
                    ch.get("price"), ch.get("size"), str(ch.get("side") or ""), ts
                )
                touched.append(token_id)

        elif event_type == "last_trade_price":
            token_id = str(msg.get("asset_id") or "")
            if token_id:
                self.book(token_id).apply_trade(
                    msg.get("price"), msg.get("size"), str(msg.get("side") or ""), ts
                )
                touched.append(token_id)

        return touched


def _ws_ts(value: Any) -> float:
    """Polymarket stamps milliseconds; tolerate seconds."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    return ts / 1000.0 if ts > 1e11 else ts
