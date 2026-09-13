"""Binance spot reference feed for the crypto universe.

Polymarket resolves these events against its own oracle, but the *information*
driving them is spot price. ``polybench.pricefeed`` streams BTC only; this
streams every asset in the universe on a single combined socket.

One regression to be aware of in the old tape: ``btc_last`` is 0 in recent
fixtures while ``btc_bid``/``btc_ask`` populate. That happens when only the
bookTicker stream is subscribed and trade prints are not, so "last" never gets
set. Here we subscribe both and synthesise ``last`` from the mid when no trade
has printed yet, so the field is never silently zero.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import websockets

from polyalpha.universe import BINANCE_SYMBOL

log = logging.getLogger("polyalpha.feeds")

BINANCE_WS = "wss://stream.binance.com:9443/stream"
__all__ = ["SpotQuote", "BinanceSpotFeed"]


@dataclass(slots=True)
class SpotQuote:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts: float = 0.0
    trade_ts: float = 0.0
    updates: int = 0

    @property
    def mid(self) -> float:
        if self.bid > 0.0 and self.ask > 0.0:
            return (self.bid + self.ask) / 2.0
        return self.last or max(self.bid, self.ask)

    @property
    def effective_last(self) -> float:
        """Never returns 0 when a quote exists — see module docstring."""
        return self.last or self.mid

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.ts


class BinanceSpotFeed:
    """Background combined-stream reader. Read ``quotes`` from any thread/task.

    Deliberately never raises into the caller: a reference feed dropping out
    must degrade the data (a stale flag) rather than kill a recording that is
    also capturing the Polymarket side.
    """

    def __init__(self, assets: Sequence[str], *, reconnect_delay: float = 2.0) -> None:
        self._assets = [a for a in assets if a in BINANCE_SYMBOL]
        self._symbols = {a: BINANCE_SYMBOL[a] for a in self._assets}
        self._quotes: dict[str, SpotQuote] = {
            a: SpotQuote(symbol=s) for a, s in self._symbols.items()
        }
        self._by_symbol = {s.lower(): a for a, s in self._symbols.items()}
        self._task: asyncio.Task | None = None
        self._reconnect_delay = reconnect_delay
        self._connected = False
        self.messages = 0
        self.reconnects = 0

    @property
    def quotes(self) -> dict[str, SpotQuote]:
        return self._quotes

    @property
    def connected(self) -> bool:
        return self._connected

    def quote(self, asset: str) -> SpotQuote | None:
        return self._quotes.get(asset)

    def _stream_url(self) -> str:
        parts: list[str] = []
        for sym in self._symbols.values():
            low = sym.lower()
            parts.append(f"{low}@bookTicker")
            parts.append(f"{low}@trade")
        return f"{BINANCE_WS}?streams={'/'.join(parts)}"

    async def start(self) -> None:
        if self._task is None and self._assets:
            self._task = asyncio.create_task(self._run(), name="binance-spot-feed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected = False

    async def _run(self) -> None:
        url = self._stream_url()
        while True:
            try:
                async with websockets.connect(
                    url, max_size=None, open_timeout=20, ping_interval=20
                ) as ws:
                    self._connected = True
                    log.info("binance: connected (%d assets)", len(self._assets))
                    async for raw in ws:
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                self.reconnects += 1
                log.warning("binance: stream dropped (%s); reconnecting", exc)
                await asyncio.sleep(self._reconnect_delay)

    def _handle(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        stream = payload.get("stream") or ""
        data = payload.get("data") or {}
        if not stream or not isinstance(data, dict):
            return
        symbol, _, kind = stream.partition("@")
        asset = self._by_symbol.get(symbol)
        if asset is None:
            return
        q = self._quotes[asset]
        now = time.time()
        if kind == "bookTicker":
            q.bid = _f(data.get("b"))
            q.bid_size = _f(data.get("B"))
            q.ask = _f(data.get("a"))
            q.ask_size = _f(data.get("A"))
            q.ts = now
        elif kind == "trade":
            q.last = _f(data.get("p"))
            q.trade_ts = now
            q.ts = now
        q.updates += 1
        self.messages += 1

    def snapshot_row(self, asset: str, prefix: str = "spot") -> dict[str, float]:
        q = self._quotes.get(asset)
        if q is None:
            return {}
        return {
            f"{prefix}_bid": q.bid,
            f"{prefix}_ask": q.ask,
            f"{prefix}_last": q.effective_last,
            f"{prefix}_mid": q.mid,
            f"{prefix}_bid_size": q.bid_size,
            f"{prefix}_ask_size": q.ask_size,
            f"{prefix}_age_s": q.age(),
        }


def _f(value: object) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and abs(out) != float("inf") else 0.0
