"""Asset / horizon universe for Polymarket crypto up-down events.

``polybench.market.find_active_btc_event`` hardcodes the ``btc-`` slug prefix
and a 300-second boundary, so it can only ever see one tenth of what trades.
Probed live on 2026-09-13, the actual concurrent universe is::

    {btc, eth, sol, xrp, doge} x {5m, 15m}   = 10 series

Slug shape is uniform: ``{asset}-updown-{horizon}-{end_ts}`` where ``end_ts`` is
the unix-seconds endDate aligned to the horizon. That regularity is what makes
discovery a computation rather than a search: we can name the event that is
trading right now without enumerating anything.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

__all__ = [
    "Horizon", "HORIZONS", "ASSETS", "Series", "EventRef",
    "UniverseClient", "default_universe",
]

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True, slots=True)
class Horizon:
    label: str
    seconds: int


HORIZONS: dict[str, Horizon] = {
    "5m": Horizon("5m", 300),
    "15m": Horizon("15m", 900),
}

# Confirmed trading concurrently. Kept as data so adding a series that
# Polymarket launches later is a one-line change, not a code change.
ASSETS: tuple[str, ...] = ("btc", "eth", "sol", "xrp", "doge")

# Binance spot symbol per asset, for the reference-price feed.
BINANCE_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
    "xrp": "XRPUSDT", "doge": "DOGEUSDT",
}


@dataclass(frozen=True, slots=True)
class Series:
    asset: str
    horizon: Horizon

    @property
    def key(self) -> str:
        return f"{self.asset}-{self.horizon.label}"

    def slug_for(self, start_ts: int) -> str:
        """Slug key for the window *starting* at ``start_ts``.

        Verified against Gamma 2026-09-13: the trailing number in
        ``{asset}-updown-{horizon}-{ts}`` is the window's **start**, and the
        event's ``endDate`` comes back as ``ts + horizon``. ``polybench.market``
        documents it as the endDate, which is wrong — harmless there because it
        floors to the current boundary, but it makes an off-by-one-window error
        easy to introduce.
        """
        return f"{self.asset}-updown-{self.horizon.label}-{int(start_ts)}"

    def current_start_ts(self, now: float | None = None) -> int:
        """Start of the window currently trading.

        Floor, not ceil: the event whose window contains ``now`` is the one
        taking orders. Using ceil would name the *next* window and skip the
        live one.
        """
        t = time.time() if now is None else now
        h = self.horizon.seconds
        return int(math.floor(t / h) * h)

    def upcoming_start_ts(self, count: int = 3, now: float | None = None) -> list[int]:
        start = self.current_start_ts(now)
        return [start + i * self.horizon.seconds for i in range(count)]


@dataclass(slots=True)
class EventRef:
    """A concrete tradable event with its two CLOB tokens."""

    series: Series
    slug: str
    event_id: str
    question: str
    end_ts: float
    up_token_id: str
    down_token_id: str
    closed: bool = False
    outcome_prices: tuple[float, float] = (0.0, 0.0)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def asset(self) -> str:
        return self.series.asset

    @property
    def horizon(self) -> str:
        return self.series.horizon.label

    @property
    def token_ids(self) -> tuple[str, str]:
        return (self.up_token_id, self.down_token_id)

    def side_of(self, token_id: str) -> str:
        if token_id == self.up_token_id:
            return "UP"
        if token_id == self.down_token_id:
            return "DOWN"
        return "?"

    def time_to_resolve(self, now: float | None = None) -> float:
        return self.end_ts - (time.time() if now is None else now)

    def is_resolved(self) -> bool:
        """Gamma snaps ``outcomePrices`` to (1,0)/(0,1) once UMA settles."""
        up, down = self.outcome_prices
        return (up > 0.99 and down < 0.01) or (down > 0.99 and up < 0.01)

    @property
    def resolved_outcome(self) -> str:
        if not self.is_resolved():
            return ""
        return "UP" if self.outcome_prices[0] > 0.99 else "DOWN"


def default_universe(
    assets: Sequence[str] = ASSETS,
    horizons: Sequence[str] = tuple(HORIZONS),
) -> list[Series]:
    return [
        Series(asset=a, horizon=HORIZONS[h])
        for a in assets
        for h in horizons
        if h in HORIZONS
    ]


def _stringified_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _event_ref(series: Series, slug: str, obj: dict[str, Any]) -> EventRef | None:
    markets = obj.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    tokens = _stringified_array(market.get("clobTokenIds"))
    if len(tokens) < 2:
        return None
    end_ts = _iso_to_ts(obj.get("endDate") or market.get("endDate"))
    if end_ts <= 0:
        return None
    prices = _stringified_array(market.get("outcomePrices"))

    def _f(idx: int) -> float:
        try:
            return float(prices[idx])
        except (IndexError, TypeError, ValueError):
            return 0.0

    return EventRef(
        series=series,
        slug=slug,
        event_id=str(obj.get("id") or market.get("id") or slug),
        question=str(obj.get("title") or market.get("question") or ""),
        end_ts=end_ts,
        up_token_id=str(tokens[0]),
        down_token_id=str(tokens[1]),
        closed=bool(obj.get("closed") or market.get("closed")),
        outcome_prices=(_f(0), _f(1)),
        raw=obj,
    )


class UniverseClient:
    """Async Gamma/CLOB reader for the multi-asset universe."""

    def __init__(self, *, gamma_base: str = GAMMA_BASE, clob_base: str = CLOB_BASE,
                 timeout: float = 10.0, concurrency: int = 8) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "polyalpha/0.1"},
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        self._gamma = gamma_base.rstrip("/")
        self._clob = clob_base.rstrip("/")
        self._sem = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "UniverseClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        async with self._sem:
            for attempt in range(4):
                try:
                    r = await self._http.get(f"{self._gamma}{path}", params=params)
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        return None
                    if attempt == 3:
                        raise
                except Exception:
                    if attempt == 3:
                        raise
                await asyncio.sleep(0.4 * (2 ** attempt))
        return None

    async def event_by_slug(self, series: Series, slug: str) -> EventRef | None:
        data = await self._get("/events", {"slug": slug})
        if not isinstance(data, list) or not data:
            return None
        return _event_ref(series, slug, data[0])

    async def active_event(self, series: Series, *, now: float | None = None,
                           max_steps: int = 3) -> EventRef | None:
        """The event currently taking orders for this series."""
        threshold = time.time() if now is None else now
        for start_ts in series.upcoming_start_ts(max_steps, now=threshold):
            ref = await self.event_by_slug(series, series.slug_for(start_ts))
            if ref and not ref.closed and ref.end_ts > threshold:
                return ref
        return None

    async def active_events(self, series_list: Iterable[Series], *,
                            now: float | None = None) -> dict[str, EventRef]:
        """Fan out across the universe. Keyed by ``Series.key``."""
        series_list = list(series_list)
        results = await asyncio.gather(
            *(self.active_event(s, now=now) for s in series_list),
            return_exceptions=True,
        )
        out: dict[str, EventRef] = {}
        for s, res in zip(series_list, results):
            if isinstance(res, EventRef):
                out[s.key] = res
        return out

    async def refresh(self, ref: EventRef) -> EventRef | None:
        """Re-read an event, to pick up ``outcomePrices`` at settlement."""
        return await self.event_by_slug(ref.series, ref.slug)

    async def rest_book(self, token_id: str) -> dict[str, Any] | None:
        async with self._sem:
            try:
                r = await self._http.get(f"{self._clob}/book", params={"token_id": token_id})
                r.raise_for_status()
                return r.json()
            except Exception:
                return None
