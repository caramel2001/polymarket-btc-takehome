#!/usr/bin/env python3
"""Phase-1 gate: prove the full-depth book reconstruction is correct, and
measure the true fillable-tick rate.

Runs the live CLOB WS across every configured asset, maintains books via
``polyalpha.orderbook.BookSet``, and periodically diffs each reconstructed book
against an independent REST ``/book`` snapshot. Also samples the books at 1 Hz
— the cadence the recorder uses — to measure what fraction of ticks would
actually be fillable.

The number to watch is ``fillable`` at the end. ``research/IDEAS.md`` reports
~3-4% from the old capture path; the live books imply it should be far higher.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polyalpha.orderbook import BookSet

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def discover(assets: list[str], horizon_s: int = 300) -> dict[str, dict]:
    """Map asset -> {slug, up, down} for the currently-trading event."""
    out: dict[str, dict] = {}
    boundary = int(math.floor(time.time() / horizon_s) * horizon_s)
    with httpx.Client(timeout=20, headers={"User-Agent": "polyalpha/0.1"}) as c:
        for asset in assets:
            for step in range(3):
                slug = f"{asset}-updown-5m-{boundary + step * horizon_s}"
                try:
                    data = c.get(f"{GAMMA}/events", params={"slug": slug}).json()
                except Exception:
                    continue
                if not data:
                    continue
                market = data[0]["markets"][0]
                tokens = json.loads(market["clobTokenIds"])
                out[asset] = {"slug": slug, "up": tokens[0], "down": tokens[1],
                              "end": data[0].get("endDate")}
                break
    return out


def rest_book(client: httpx.Client, token_id: str) -> dict[str, dict[float, float]]:
    data = client.get(f"{CLOB}/book", params={"token_id": token_id}).json()
    return {
        "bids": {round(float(x["price"]), 4): float(x["size"]) for x in data.get("bids") or []},
        "asks": {round(float(x["price"]), 4): float(x["size"]) for x in data.get("asks") or []},
    }


async def run(assets: list[str], duration: float, check_every: float) -> int:
    events = discover(assets)
    if not events:
        print("no active events discovered", file=sys.stderr)
        return 1
    for a, e in events.items():
        print(f"  {a:4s} {e['slug']}  end={e['end']}")

    token_ids = [t for e in events.values() for t in (e["up"], e["down"])]
    label = {e["up"]: f"{a}.UP" for a, e in events.items()}
    label |= {e["down"]: f"{a}.DOWN" for a, e in events.items()}

    books = BookSet()
    # fillable == both sides quoted with real resting size, i.e. what pnl.py needs
    samples = {t: {"n": 0, "fillable": 0, "two_sided": 0} for t in token_ids}
    checks = {"total": 0, "exact": 0, "best_match": 0}

    http = httpx.Client(timeout=20, headers={"User-Agent": "polyalpha/0.1"})
    t0 = time.time()
    next_sample = t0 + 1.0
    next_check = t0 + check_every

    ws_cm = None
    for attempt in range(5):
        try:
            ws_cm = await websockets.connect(
                WS, max_size=None, open_timeout=30, ping_interval=20
            )
            break
        except Exception as exc:
            print(f"  ws connect attempt {attempt+1}/5 failed ({exc}); retrying")
            await asyncio.sleep(3.0 * (attempt + 1))
    if ws_cm is None:
        print("could not open CLOB websocket", file=sys.stderr)
        return 1

    async with ws_cm as ws:
        await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
        while time.time() - t0 < duration:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for msg in payload if isinstance(payload, list) else [payload]:
                if isinstance(msg, dict):
                    books.apply_message(msg)

            now = time.time()

            if now >= next_sample:                      # 1 Hz recorder cadence
                next_sample = now + 1.0
                for tid in token_ids:
                    bk = books.get(tid)
                    s = samples[tid]
                    s["n"] += 1
                    if bk and bk.is_two_sided():
                        s["two_sided"] += 1
                        if bk.best_ask_size > 0 and bk.best_bid_size > 0:
                            s["fillable"] += 1

            if now >= next_check:                       # independent REST diff
                next_check = now + check_every
                for tid in token_ids:
                    bk = books.get(tid)
                    if not bk or not bk.seeded:
                        continue
                    try:
                        ref = rest_book(http, tid)
                    except Exception:
                        continue
                    mine = {
                        "bids": {lv.price: lv.size for lv in bk.bids.levels()},
                        "asks": {lv.price: lv.size for lv in bk.asks.levels()},
                    }
                    checks["total"] += 1
                    if mine == ref:
                        checks["exact"] += 1
                    bb = max(ref["bids"]) if ref["bids"] else 0.0
                    ba = min(ref["asks"]) if ref["asks"] else 0.0
                    if abs(bk.best_bid - bb) < 1e-9 and abs(bk.best_ask - ba) < 1e-9:
                        checks["best_match"] += 1
    http.close()

    print("\n--- reconstruction vs independent REST snapshots ---")
    tot = checks["total"] or 1
    print(f"  checks={checks['total']}  full-book exact={checks['exact']} "
          f"({100*checks['exact']/tot:.1f}%)  best-bid/ask match={checks['best_match']} "
          f"({100*checks['best_match']/tot:.1f}%)")
    # A mid-flight REST fetch races the live feed, so treat best-bid/ask agreement
    # as the fidelity signal and full-book equality as a bonus.

    print("\n--- fillable-tick rate at 1 Hz (the number IDEAS.md put at ~3-4%) ---")
    agg_n = agg_f = 0
    for tid in token_ids:
        s = samples[tid]
        n = s["n"] or 1
        agg_n += s["n"]; agg_f += s["fillable"]
        print(f"  {label[tid]:10s} n={s['n']:4d}  two_sided={100*s['two_sided']/n:5.1f}%"
              f"  fillable={100*s['fillable']/n:5.1f}%")
    print(f"\n  OVERALL fillable = {100*agg_f/(agg_n or 1):.1f}%  (n={agg_n})")

    for bk in books:
        st = bk.stats
        if st.snapshots or st.deltas_applied:
            print(f"  {label.get(bk.token_id,'?'):10s} snapshots={st.snapshots:4d} "
                  f"deltas={st.deltas_applied:6d} pre_snap_dropped={st.deltas_before_snapshot:4d} "
                  f"trades={st.trades:4d} crossed={st.crossed_observed:4d}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--check-every", type=float, default=20.0)
    a = ap.parse_args()
    return asyncio.run(run(a.assets.split(","), a.duration, a.check_every))


if __name__ == "__main__":
    raise SystemExit(main())
