"""Passive-order fill simulator with a queue model.

Why this exists
---------------
``polybench.pnl`` is **taker-only**: buys lift `best_ask*(1+slip)`, sells hit
`best_bid*(1-slip)`. There is no way to express "post a resting order at price X",
so the maker thesis cannot be backtested in it at all.

The Phase-0 markout test (`research/phase3/`) says the economics *may* work:
half-spread ~0.66c against adverse selection ~0.17c, with toxicity concentrated
in the final ~2 minutes of an event. But a markout is not PnL. It assumes we are
filled on every print, at the touch, instantly. The two things standing between
that and money are **queue position** and **fill probability**, and both need a
simulator.

The fill model
--------------
When we post ``size`` at ``price`` on a side, we join the back of the queue at
that level::

    queue_ahead := resting size at that price, from the book snapshot

Then, as the tape advances:

* A **trade print** that consumes our level (a taker BUY at/above our ask, or a
  taker SELL at/below our bid) eats the queue ahead of us first; any remainder
  fills us.
* A **book snapshot** showing less size at our level than we last saw is
  ambiguous — it could be cancellations ahead of us (good) or behind us
  (neutral). We assume **behind us**, which keeps us at the back. This
  under-fills rather than over-fills, and under-filling is the honest direction
  for a model whose job is to avoid inventing profits.
* If the market trades **through** our price, we are filled in full — the level
  cannot survive being crossed.

Everything here is deliberately conservative. A maker backtest that flatters
itself is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

__all__ = ["Side", "OrderState", "PassiveOrder", "Fill", "PassiveSimulator"]


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Fill:
    ts: float
    token_id: str
    side: Side
    price: float
    size: float
    order_id: int
    queue_waited: float      # size consumed ahead of us before we filled
    seconds_resting: float


@dataclass(slots=True)
class PassiveOrder:
    order_id: int
    token_id: str
    side: Side
    price: float
    size: float
    posted_ts: float
    queue_ahead: float
    remaining: float
    state: OrderState = OrderState.OPEN
    filled_size: float = 0.0
    queue_consumed: float = 0.0
    last_level_size: float = 0.0   # for detecting cancellations at our level

    @property
    def is_open(self) -> bool:
        return self.state is OrderState.OPEN


class PassiveSimulator:
    """Tracks resting orders across a replayed tape and reports fills.

    Usage per tick: ``on_book`` with the current top-of-book, then ``on_trade``
    for each print since the last tick. Post/cancel via ``post``/``cancel``.
    """

    def __init__(self, *, fee_rate: float = 0.0, assume_cancels_behind: bool = True) -> None:
        self.fee_rate = fee_rate
        self.assume_cancels_behind = assume_cancels_behind
        self._orders: dict[int, PassiveOrder] = {}
        self._next_id = 1
        self.fills: list[Fill] = []

    # ---- order management -------------------------------------------------

    def post(self, *, ts: float, token_id: str, side: Side, price: float,
             size: float, queue_ahead: float) -> PassiveOrder:
        """Post a resting order behind ``queue_ahead`` shares at that price."""
        order = PassiveOrder(
            order_id=self._next_id, token_id=token_id, side=Side(side),
            price=round(float(price), 4), size=float(size), posted_ts=float(ts),
            queue_ahead=max(0.0, float(queue_ahead)), remaining=float(size),
            last_level_size=max(0.0, float(queue_ahead)),
        )
        self._orders[order.order_id] = order
        self._next_id += 1
        return order

    def cancel(self, order_id: int) -> bool:
        o = self._orders.get(order_id)
        if o is None or not o.is_open:
            return False
        o.state = OrderState.CANCELLED
        return True

    def cancel_all(self, token_id: str | None = None) -> int:
        n = 0
        for o in self._orders.values():
            if o.is_open and (token_id is None or o.token_id == token_id):
                o.state = OrderState.CANCELLED
                n += 1
        return n

    @property
    def open_orders(self) -> list[PassiveOrder]:
        return [o for o in self._orders.values() if o.is_open]

    def orders_for(self, token_id: str) -> list[PassiveOrder]:
        return [o for o in self._orders.values() if o.is_open and o.token_id == token_id]

    # ---- tape events ------------------------------------------------------

    def on_trade(self, *, ts: float, token_id: str, price: float,
                 size: float, taker_side: str) -> list[Fill]:
        """Apply one print. Returns fills it caused.

        A taker BUY consumes resting **asks** (so it can fill our SELL); a taker
        SELL consumes resting **bids** (filling our BUY).
        """
        price = round(float(price), 4)
        size = float(size)
        taker = str(taker_side).upper()
        hits_side = Side.SELL if taker == "BUY" else Side.BUY

        out: list[Fill] = []
        for o in self._orders.values():
            if not o.is_open or o.token_id != token_id or o.side is not hits_side:
                continue
            # Does this print reach our price?
            if o.side is Side.SELL and price < o.price:
                continue          # bought below our ask; our level untouched
            if o.side is Side.BUY and price > o.price:
                continue          # sold above our bid; our level untouched

            traded_through = (price > o.price) if o.side is Side.SELL else (price < o.price)
            if traded_through:
                # The market crossed our price entirely; our level cannot survive.
                fill_size = o.remaining
                o.queue_consumed += o.queue_ahead
                o.queue_ahead = 0.0
            else:
                eat = min(size, o.queue_ahead)
                o.queue_ahead -= eat
                o.queue_consumed += eat
                fill_size = min(o.remaining, size - eat)
            if fill_size <= 0.0:
                continue
            o.remaining -= fill_size
            o.filled_size += fill_size
            if o.remaining <= 1e-9:
                o.state = OrderState.FILLED
            f = Fill(ts=float(ts), token_id=token_id, side=o.side, price=o.price,
                     size=fill_size, order_id=o.order_id,
                     queue_waited=o.queue_consumed, seconds_resting=float(ts) - o.posted_ts)
            out.append(f)
            self.fills.append(f)
        return out

    def on_book(self, *, token_id: str, bid: float, ask: float,
                bid_size: float, ask_size: float) -> None:
        """Refresh queue estimates from a book snapshot.

        Size shrinking at our level without a corresponding print means
        cancellations. We cannot see *where* in the queue they happened, so we
        assume behind us — the assumption that keeps the model honest.
        """
        for o in self._orders.values():
            if not o.is_open or o.token_id != token_id:
                continue
            if o.side is Side.BUY and abs(o.price - round(bid, 4)) < 1e-9:
                level = float(bid_size)
            elif o.side is Side.SELL and abs(o.price - round(ask, 4)) < 1e-9:
                level = float(ask_size)
            else:
                # Our price is no longer at the touch. If the book has moved
                # away from us we are simply not going to trade; leave the
                # queue as-is rather than guessing.
                continue
            if not self.assume_cancels_behind and level < o.last_level_size:
                o.queue_ahead = max(0.0, min(o.queue_ahead, level - o.remaining))
            o.last_level_size = level

    # ---- accounting -------------------------------------------------------

    def fee_for(self, price: float, size: float) -> float:
        p = min(max(float(price), 0.0), 1.0)
        return abs(size) * self.fee_rate * p * (1.0 - p)

    def realised(self, settle_value: dict[str, float]) -> dict[str, float]:
        """Mark all fills to settlement.

        ``settle_value`` maps token_id -> 1.0 / 0.0. A BUY fill pays
        ``settle - price`` per share; a SELL fill (we sold a token we would have
        to deliver) pays ``price - settle``.
        """
        gross = fees = 0.0
        bought = sold = 0.0
        for f in self.fills:
            sv = settle_value.get(f.token_id)
            if sv is None:
                continue
            if f.side is Side.BUY:
                gross += (sv - f.price) * f.size
                bought += f.size
            else:
                gross += (f.price - sv) * f.size
                sold += f.size
            fees += self.fee_for(f.price, f.size)
        return {
            "gross": gross, "fees": fees, "net": gross - fees,
            "n_fills": float(len(self.fills)),
            "shares_bought": bought, "shares_sold": sold,
        }

    def stats(self) -> dict[str, float]:
        orders = list(self._orders.values())
        filled = [o for o in orders if o.state is OrderState.FILLED or o.filled_size > 0]
        return {
            "orders_posted": float(len(orders)),
            "orders_filled": float(len(filled)),
            "fill_rate": len(filled) / len(orders) if orders else 0.0,
            "mean_queue_waited": (sum(o.queue_consumed for o in filled) / len(filled))
            if filled else 0.0,
        }
