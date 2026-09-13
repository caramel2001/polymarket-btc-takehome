"""Multi-asset, full-depth Polymarket up/down recorder.

What this fixes relative to ``polybench``'s recorder
----------------------------------------------------
1. **Full depth.** Books are maintained as level maps seeded from ``book``
   snapshots and updated by ``price_change`` deltas (absolute-set semantics),
   so top-of-book size is real. The old path rebuilt a one-level book with
   size 0 on every delta, which is what produced the spurious "97% of ticks
   unfillable" result in ``research/IDEAS.md``.
2. **The whole universe.** All ``{asset} x {horizon}`` series on one socket,
   not BTC-5m alone.
3. **Trade prints.** Every ``last_trade_price`` message is persisted with
   price/size/side. ``research/MAKER_SCOPE.md`` lists this as a blocking
   unknown for maker research; it is simply a stream we were discarding.
4. **Integrity telemetry.** Snapshot/delta/drop counters and a crossed-book
   count are written alongside the data, so a later analysis can tell a real
   market state from a capture failure instead of assuming.

Output layout (hive-partitioned, one set of files per UTC hour)::

    data/tape/books/date=YYYY-MM-DD/hour=HH/{start}_{asset}_{horizon}.parquet
    data/tape/trades/date=YYYY-MM-DD/hour=HH/{start}_{asset}_{horizon}.parquet
    data/tape/events/date=YYYY-MM-DD/events.parquet
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import websockets

from polyalpha.feeds import BinanceSpotFeed
from polyalpha.orderbook import BookSet
from polyalpha.universe import (
    ASSETS, HORIZONS, EventRef, Series, UniverseClient, default_universe,
)

log = logging.getLogger("polyalpha.recorder")

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_DEPTH = 5


@dataclass(slots=True)
class RecorderConfig:
    hf_book: bool = True                # event-driven top-of-book capture
    hf_max_rows_per_event: int = 400_000
    out_dir: Path = Path("data/tape")
    assets: Sequence[str] = ASSETS
    horizons: Sequence[str] = tuple(HORIZONS)
    sample_hz: float = 1.0
    flush_every_s: float = 300.0
    resolution_grace_s: float = 240.0   # keep polling Gamma this long past end
    discovery_every_s: float = 20.0
    ws_stale_after_s: float = 45.0
    duration_s: float | None = None     # None = run forever


@dataclass(slots=True)
class _Tracked:
    """An event we are recording, plus the rows collected for it."""
    ref: EventRef
    book_rows: list[dict[str, Any]] = field(default_factory=list)
    trade_rows: list[dict[str, Any]] = field(default_factory=list)
    hf_rows: list[dict[str, Any]] = field(default_factory=list)
    resolved: bool = False
    settled_ts: float = 0.0


class TapeRecorder:
    def __init__(self, config: RecorderConfig | None = None) -> None:
        self.cfg = config or RecorderConfig()
        self.series: list[Series] = default_universe(self.cfg.assets, self.cfg.horizons)
        self.books = BookSet()
        self.spot = BinanceSpotFeed(list(self.cfg.assets))
        self._tracked: dict[str, _Tracked] = {}        # slug -> tracked
        self._token_to_slug: dict[str, str] = {}
        self._subscribed: set[str] = set()
        self._ws: Any = None
        self._resubscribe = asyncio.Event()
        self._stop = asyncio.Event()
        self._last_msg_ts = 0.0
        self._hf_last: dict[str, tuple[float, float, float, float]] = {}
        self.stats = {"ws_messages": 0, "rows": 0, "trades": 0, "hf_rows": 0,
                      "hf_dropped": 0, "events_started": 0, "events_resolved": 0,
                      "ws_reconnects": 0}

    # ---- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        started = time.time()
        await self.spot.start()
        async with UniverseClient() as uni:
            tasks = [
                asyncio.create_task(self._discovery_loop(uni), name="discovery"),
                asyncio.create_task(self._ws_loop(), name="clob-ws"),
                asyncio.create_task(self._sample_loop(), name="sampler"),
                asyncio.create_task(self._resolution_loop(uni), name="resolution"),
                asyncio.create_task(self._flush_loop(), name="flusher"),
            ]
            try:
                if self.cfg.duration_s is not None:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.duration_s)
                else:
                    await self._stop.wait()
            except asyncio.TimeoutError:
                pass
            finally:
                self._stop.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.spot.stop()
                self._flush(force=True)
        log.info("recorder finished after %.0fs  stats=%s",
                 time.time() - started, self.stats)

    # ---- discovery --------------------------------------------------------

    async def _discovery_loop(self, uni: UniverseClient) -> None:
        while not self._stop.is_set():
            try:
                found = await uni.active_events(self.series)
                new_tokens = False
                for ref in found.values():
                    if ref.slug in self._tracked:
                        continue
                    self._tracked[ref.slug] = _Tracked(ref=ref)
                    for tid in ref.token_ids:
                        self._token_to_slug[tid] = ref.slug
                    self.stats["events_started"] += 1
                    new_tokens = True
                    log.info("tracking %s (%s) ttr=%.0fs",
                             ref.slug, ref.question[:60], ref.time_to_resolve())
                if new_tokens:
                    self._resubscribe.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery failed: %s", exc)
            await asyncio.sleep(self.cfg.discovery_every_s)

    # ---- websocket --------------------------------------------------------

    def _active_tokens(self) -> list[str]:
        """Tokens worth subscribing: events not yet past their grace window."""
        now = time.time()
        out: list[str] = []
        for tr in self._tracked.values():
            if tr.resolved:
                continue
            if now - tr.ref.end_ts > self.cfg.resolution_grace_s:
                continue
            out.extend(tr.ref.token_ids)
        return out

    async def _ws_loop(self) -> None:
        while not self._stop.is_set():
            tokens = self._active_tokens()
            if not tokens:
                await asyncio.sleep(2.0)
                continue
            try:
                async with websockets.connect(
                    CLOB_WS, max_size=None, open_timeout=30, ping_interval=20
                ) as ws:
                    self._ws = ws
                    self._subscribed = set(tokens)
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    log.info("clob ws: subscribed to %d tokens", len(tokens))
                    self._resubscribe.clear()
                    self._last_msg_ts = time.time()
                    while not self._stop.is_set() and not self._resubscribe.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self.cfg.ws_stale_after_s
                            )
                        except asyncio.TimeoutError:
                            log.warning("clob ws: stale, reconnecting")
                            break
                        self._on_ws(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats["ws_reconnects"] += 1
                log.warning("clob ws: %s; reconnecting", exc)
                await asyncio.sleep(2.0)

    def _on_ws(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if not text or text.upper() in {"PING", "PONG"}:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        self._last_msg_ts = time.time()
        for msg in payload if isinstance(payload, list) else [payload]:
            if not isinstance(msg, dict):
                continue
            self.stats["ws_messages"] += 1
            touched = self.books.apply_message(msg)
            if self.cfg.hf_book and touched:
                self._record_hf(touched, msg)
            if str(msg.get("event_type") or "") == "last_trade_price":
                self._record_trade(msg)

    def _record_hf(self, token_ids: list[str], msg: dict[str, Any]) -> None:
        """Append a row whenever a tracked token's top-of-book actually changes.

        The 1 Hz sample loop is blind to sub-second structure: 73.6% of trade
        prints arrive within 1s of the previous one and 73.8% of volume lands in
        seconds carrying more than one print. A simulator driven at 1 Hz sees a
        burst as a single aggregated event and cannot react inside it, which is
        precisely where a two-legged quote accumulates naked inventory.

        Only emitted on change, so a quiet book costs nothing.
        """
        recv = time.time()
        ws_ts = _ws_ts(msg.get("timestamp")) or recv
        for token_id in dict.fromkeys(token_ids):        # de-dup, keep order
            slug = self._token_to_slug.get(token_id)
            tr = self._tracked.get(slug) if slug else None
            bk = self.books.get(token_id)
            if tr is None or bk is None or not bk.seeded:
                continue
            q = self.spot.quote(tr.ref.asset)
            top = (bk.best_bid, bk.best_ask, bk.best_bid_size, bk.best_ask_size)
            if self._hf_last.get(token_id) == top:
                continue
            self._hf_last[token_id] = top
            if len(tr.hf_rows) >= self.cfg.hf_max_rows_per_event:
                self.stats["hf_dropped"] += 1
                continue
            tr.hf_rows.append({
                "ts": ws_ts, "recv_ts": recv, "slug": slug,
                "asset": tr.ref.asset, "horizon": tr.ref.horizon,
                "token_id": token_id, "side_token": tr.ref.side_of(token_id),
                "best_bid": top[0], "best_ask": top[1],
                "best_bid_size": top[2], "best_ask_size": top[3],
                "mid": bk.mid, "microprice": bk.microprice,
                "imbalance_l1": bk.imbalance(1), "imbalance_l5": bk.imbalance(5),
                "time_to_resolve": tr.ref.end_ts - ws_ts,
                "event_type": str(msg.get("event_type") or ""),
                # Spot sampled at book-update time (~9ms cadence), not at 1 Hz.
                # Lead-lag between spot and this market is the whole question at
                # high frequency, and it cannot be asked with 1 Hz spot.
                "spot_mid": q.mid if q else 0.0,
                "spot_last": q.effective_last if q else 0.0,
                "spot_ts": q.ts if q else 0.0,
                "spot_age": (recv - q.ts) if (q and q.ts) else float("nan"),
            })
            self.stats["hf_rows"] += 1

    def _record_trade(self, msg: dict[str, Any]) -> None:
        token_id = str(msg.get("asset_id") or "")
        slug = self._token_to_slug.get(token_id)
        if slug is None:
            return
        tr = self._tracked[slug]
        ts = _ws_ts(msg.get("timestamp"))
        tr.trade_rows.append({
            "ts": ts or time.time(),
            "recv_ts": time.time(),
            "slug": slug,
            "asset": tr.ref.asset,
            "horizon": tr.ref.horizon,
            "event_id": tr.ref.event_id,
            "token_id": token_id,
            "side_token": tr.ref.side_of(token_id),
            "price": _f(msg.get("price")),
            "size": _f(msg.get("size")),
            "taker_side": str(msg.get("side") or ""),
            "fee_rate_bps": _f(msg.get("fee_rate_bps")),
            "time_to_resolve": tr.ref.end_ts - (ts or time.time()),
            "tx_hash": str(msg.get("transaction_hash") or ""),
        })
        self.stats["trades"] += 1

    # ---- sampling ---------------------------------------------------------

    async def _sample_loop(self) -> None:
        period = 1.0 / max(self.cfg.sample_hz, 0.01)
        next_at = time.time()
        while not self._stop.is_set():
            next_at += period
            await asyncio.sleep(max(0.0, next_at - time.time()))
            now = time.time()
            for slug, tr in list(self._tracked.items()):
                if tr.resolved:
                    continue
                ttr = tr.ref.end_ts - now
                if ttr < -self.cfg.resolution_grace_s:
                    continue
                up = self.books.get(tr.ref.up_token_id)
                down = self.books.get(tr.ref.down_token_id)
                if up is None or down is None or not (up.seeded or down.seeded):
                    continue
                row: dict[str, Any] = {
                    "ts": now,
                    "slug": slug,
                    "asset": tr.ref.asset,
                    "horizon": tr.ref.horizon,
                    "event_id": tr.ref.event_id,
                    "end_ts": tr.ref.end_ts,
                    "time_to_resolve": ttr,
                }
                for side, bk in (("up", up), ("down", down)):
                    for k, v in bk.snapshot_row(BOOK_DEPTH).items():
                        row[f"{side}_{k}"] = v
                    row[f"{side}_seeded"] = bool(bk.seeded)
                    row[f"{side}_crossed"] = bk.is_crossed()
                    st = bk.stats
                    row[f"{side}_n_snapshots"] = st.snapshots
                    row[f"{side}_n_deltas"] = st.deltas_applied
                    row[f"{side}_n_dropped"] = st.deltas_before_snapshot
                row.update(self.spot.snapshot_row(tr.ref.asset, prefix="spot"))
                row["spot_connected"] = self.spot.connected
                # Complement check: UP and DOWN mids should sum to ~1. A
                # persistent deviation is either a real arb or a capture fault,
                # and recording it is what lets us tell those apart later.
                row["mid_sum"] = row.get("up_mid", 0.0) + row.get("down_mid", 0.0)
                row["ask_sum"] = row.get("up_best_ask", 0.0) + row.get("down_best_ask", 0.0)
                row["bid_sum"] = row.get("up_best_bid", 0.0) + row.get("down_best_bid", 0.0)
                tr.book_rows.append(row)
                self.stats["rows"] += 1

    # ---- resolution -------------------------------------------------------

    async def _resolution_loop(self, uni: UniverseClient) -> None:
        while not self._stop.is_set():
            now = time.time()
            for slug, tr in list(self._tracked.items()):
                if tr.resolved or now < tr.ref.end_ts:
                    continue
                try:
                    fresh = await uni.refresh(tr.ref)
                except Exception:
                    continue
                if fresh is not None and fresh.is_resolved():
                    tr.ref.outcome_prices = fresh.outcome_prices
                    tr.ref.closed = fresh.closed
                    tr.resolved = True
                    tr.settled_ts = time.time()
                    self.stats["events_resolved"] += 1
                    log.info("resolved %s -> %s (lag %.0fs)",
                             slug, fresh.resolved_outcome, tr.settled_ts - tr.ref.end_ts)
                elif now - tr.ref.end_ts > self.cfg.resolution_grace_s:
                    tr.resolved = True            # give up; recorded as UNKNOWN
                    tr.settled_ts = time.time()
                    log.warning("resolution timeout for %s", slug)
                if tr.resolved:
                    self._resubscribe.set()
            await asyncio.sleep(5.0)

    # ---- persistence ------------------------------------------------------

    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.flush_every_s)
            self._flush()

    def _flush(self, *, force: bool = False) -> None:
        """Write out completed events; keep in-flight ones buffered."""
        done = [
            slug for slug, tr in self._tracked.items()
            if tr.resolved or (force and tr.book_rows)
        ]
        evicted = 0
        for slug in done:
            tr = self._tracked.pop(slug, None)
            if tr is None:
                continue
            for tid in tr.ref.token_ids:
                self._token_to_slug.pop(tid, None)
            evicted += self.books.evict(tr.ref.token_ids)
            self._write_event(tr)
            # Rows are persisted; drop the buffers so a long run stays flat.
            tr.book_rows.clear()
            tr.trade_rows.clear()
            tr.hf_rows.clear()
        if done:
            log.info("flushed %d events (books evicted=%d, tracked=%d, live books=%d)",
                     len(done), evicted, len(self._tracked), len(self.books))

    def _write_event(self, tr: _Tracked) -> None:
        if not tr.book_rows and not tr.trade_rows:
            return
        outcome = tr.ref.resolved_outcome or "UNKNOWN"
        stamp = datetime.fromtimestamp(tr.ref.end_ts, tz=timezone.utc)
        date_part = stamp.strftime("%Y-%m-%d")
        hour_part = stamp.strftime("%H")
        base = f"{stamp.strftime('%Y%m%dT%H%M%S')}_{tr.ref.asset}_{tr.ref.horizon}"

        if tr.book_rows:
            df = pd.DataFrame(tr.book_rows)
            df["resolved_outcome"] = outcome
            df["resolution_up"] = tr.ref.outcome_prices[0]
            df["resolution_down"] = tr.ref.outcome_prices[1]
            df["settled_ts"] = tr.settled_ts
            self._write(df, self.cfg.out_dir / "books" /
                        f"date={date_part}" / f"hour={hour_part}" / f"{base}.parquet")

        if tr.hf_rows:
            hdf = pd.DataFrame(tr.hf_rows)
            hdf["resolved_outcome"] = outcome
            self._write(hdf, self.cfg.out_dir / "hf" /
                        f"date={date_part}" / f"hour={hour_part}" / f"{base}.parquet")

        if tr.trade_rows:
            tdf = pd.DataFrame(tr.trade_rows)
            tdf["resolved_outcome"] = outcome
            self._write(tdf, self.cfg.out_dir / "trades" /
                        f"date={date_part}" / f"hour={hour_part}" / f"{base}.parquet")

        meta = pd.DataFrame([{
            "slug": tr.ref.slug, "event_id": tr.ref.event_id,
            "asset": tr.ref.asset, "horizon": tr.ref.horizon,
            "question": tr.ref.question, "end_ts": tr.ref.end_ts,
            "up_token_id": tr.ref.up_token_id, "down_token_id": tr.ref.down_token_id,
            "resolved_outcome": outcome,
            "resolution_up": tr.ref.outcome_prices[0],
            "resolution_down": tr.ref.outcome_prices[1],
            "settled_ts": tr.settled_ts,
            "n_book_rows": len(tr.book_rows), "n_trades": len(tr.trade_rows),
            "n_hf_rows": len(tr.hf_rows),
        }])
        path = self.cfg.out_dir / "events" / f"date={date_part}" / "events.parquet"
        if path.exists():
            with contextlib.suppress(Exception):
                meta = pd.concat([pd.read_parquet(path), meta], ignore_index=True)
        self._write(meta, path)

    @staticmethod
    def _write(df: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, engine="pyarrow", index=False)
        log.debug("wrote %d rows -> %s", len(df), path)


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ws_ts(value: Any) -> float:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    return ts / 1000.0 if ts > 1e11 else ts
