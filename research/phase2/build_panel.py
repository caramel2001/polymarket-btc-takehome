#!/usr/bin/env python3
"""Build an event-level panel from the legacy BTC-5m tape.

Why the legacy tape is still usable
-----------------------------------
The capture bug (see ``research/PLAN.md`` §0.1) corrupted top-of-book **sizes**,
not **prices**: ``price_change`` messages carry correct ``best_bid``/``best_ask``,
and those were parsed correctly. So every price series in
``data/live_recordings/`` is sound, and only fill-feasibility was wrong.

That matters because fill-feasibility was exactly the constraint used to kill
most strategies in ``IDEAS.md``. With the corrected measurement (~100% of
mid-event ticks fillable, vs the ~3-4% the broken capture implied), those
rejections have to be re-run against a realistic execution assumption.

Output: one row per (event, tick) with causal features + the settled outcome.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

COLS = [
    "ts", "event_id", "slug", "time_to_resolve",
    "btc_last", "btc_bid", "btc_ask",
    "up_bid", "up_ask", "up_mid", "up_bid_size", "up_ask_size",
    "down_bid", "down_ask", "down_mid", "down_bid_size", "down_ask_size",
    "resolution_up", "resolution_down", "resolved_outcome",
]


def _event_outcome(g: pd.DataFrame) -> str:
    """Settled outcome for an event.

    Prefer the explicit resolution columns. Fall back to the terminal mid only
    when it is unambiguous — a mid that has snapped to <0.02 or >0.98 at expiry
    is the market reporting the settlement, not a trade we could act on. Events
    that stay ambiguous are dropped rather than guessed.
    """
    res = g["resolved_outcome"].astype(str)
    tagged = res[res.isin(["UP", "DOWN"])]
    if len(tagged):
        return tagged.iloc[-1]
    ru = g["resolution_up"].dropna()
    if len(ru):
        v = float(ru.iloc[-1])
        if v > 0.99:
            return "UP"
        if v < 0.01:
            return "DOWN"
    tail = g.loc[g["time_to_resolve"] <= 2.0, "up_mid"]
    if len(tail):
        v = float(tail.iloc[-1])
        if v > 0.98:
            return "UP"
        if v < 0.02:
            return "DOWN"
    return ""


def process_file(path: str) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(path, columns=COLS)
    except Exception:
        return None
    if df.empty:
        return None

    out = []
    for event_id, g in df.groupby("event_id", sort=False):
        g = g.sort_values("ts")
        outcome = _event_outcome(g)
        if outcome not in ("UP", "DOWN"):
            continue
        g = g[g["time_to_resolve"] > 0].copy()
        if len(g) < 30:
            continue

        # Spot reference: btc_last is 0 in newer fixtures while bid/ask populate.
        spot = g["btc_last"].to_numpy(float)
        bid = g["btc_bid"].to_numpy(float)
        ask = g["btc_ask"].to_numpy(float)
        mid = np.where((bid > 0) & (ask > 0), (bid + ask) / 2.0, 0.0)
        spot = np.where(spot > 0, spot, mid)
        # Fixtures before 2026-06-09 predate the Binance feed and carry no spot.
        # They are still valid for price-only work, so flag rather than drop.
        has_spot = bool((spot > 0).any())
        if has_spot:
            spot = pd.Series(spot).replace(0.0, np.nan).ffill().bfill().to_numpy()
            g["spot"] = spot
            g["spot_open"] = spot[0]
            g["spot_ret"] = spot / spot[0] - 1.0
        else:
            g["spot"] = np.nan
            g["spot_open"] = np.nan
            g["spot_ret"] = np.nan
        g["has_spot"] = int(has_spot)
        g["outcome_up"] = int(outcome == "UP")
        g["event_key"] = f"{Path(path).stem}::{event_id}"
        g["tick_idx"] = np.arange(len(g))

        # Causal price features only — everything below uses data at or before t.
        um = g["up_mid"].to_numpy(float)
        for w in (5, 15, 30):
            g[f"up_mid_ret_{w}"] = pd.Series(um).diff(w).to_numpy()
        g["up_mid_vol_30"] = pd.Series(um).rolling(30, min_periods=5).std().to_numpy()
        sr = g["spot_ret"].to_numpy(float)
        for w in (5, 15, 30):
            g[f"spot_ret_{w}"] = pd.Series(sr).diff(w).to_numpy()

        g["spread_up"] = g["up_ask"] - g["up_bid"]
        g["spread_down"] = g["down_ask"] - g["down_bid"]
        g["ask_sum"] = g["up_ask"] + g["down_ask"]
        g["mid_sum"] = g["up_mid"] + g["down_mid"]
        # Legacy-capture flag: depth recorded only right after a book snapshot.
        g["had_depth"] = ((g["up_ask_size"] > 0) | (g["down_ask_size"] > 0)).astype(int)
        out.append(g)

    if not out:
        return None
    res = pd.concat(out, ignore_index=True)
    res["src"] = os.path.basename(path)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default=str(ROOT / "data/live_recordings/*.parquet"))
    ap.add_argument("--out", default=str(ROOT / "data/panel/btc5m_panel.parquet"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()

    files = sorted(glob.glob(a.glob))
    if a.limit:
        files = files[: a.limit]
    print(f"processing {len(files)} files with {a.workers} workers")

    out_dir = Path(a.out)
    if out_dir.suffix == ".parquet":          # accept a file path, write a dir
        out_dir = out_dir.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("part-*.parquet"):
        stale.unlink()

    done = written = 0
    n_ticks = n_events = 0
    depth_num = depth_den = 0
    up_events = 0
    ts_min, ts_max = float("inf"), 0.0

    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(process_file, f): f for f in files}
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
            except Exception as exc:
                print(f"  ! {os.path.basename(futs[fut])}: {exc}")
                continue
            if r is None or r.empty:
                continue
            # Shard per source file: bounded memory, and the analysis layer
            # reads it back as one dataset via pyarrow.
            r.to_parquet(out_dir / f"part-{written:05d}.parquet",
                         engine="pyarrow", index=False)
            written += 1
            n_ticks += len(r)
            ev = r.groupby("event_key").outcome_up.first()
            n_events += len(ev)
            up_events += int(ev.sum())
            depth_num += int(r.had_depth.sum())
            depth_den += len(r)
            ts_min = min(ts_min, float(r.ts.min()))
            ts_max = max(ts_max, float(r.ts.max()))
            del r
            if done % 200 == 0:
                print(f"  {done}/{len(files)} files, {written} shards, "
                      f"{n_events:,} events")

    if not written:
        print("no usable data")
        return 1
    print(f"\npanel: {n_ticks:,} ticks  {n_events:,} events  "
          f"-> {out_dir} ({written} shards)")
    print(f"date range: {pd.to_datetime(ts_min, unit='s')} .. "
          f"{pd.to_datetime(ts_max, unit='s')}")
    print(f"UP rate: {up_events / max(n_events, 1):.4f}")
    print(f"ticks with recorded depth (legacy capture): "
          f"{depth_num / max(depth_den, 1):.4%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
