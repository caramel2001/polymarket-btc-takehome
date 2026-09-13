#!/usr/bin/env python3
"""At what execution latency does the HF repricing edge die?

`hf_repricing.py` enters at the instant the book update is observed, which
implicitly assumes zero order-transmission time. That is not reachable: a real
order has to travel to Polymarket and rest before it can lift anything.

This sweeps an explicit delay ``L``. The impulse is detected at time ``t`` (which
already carries ~99ms of spot staleness), the order arrives at ``t + L``, and we
lift whatever ask is prevailing **then** — not the one that tempted us. Exit sells
at the bid after ``hold``, measured from arrival.

The output is the number that decides reachability: the latency at which the edge
crosses zero. Above it the idea needs colocation; below it a normal host suffices.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LATENCY_MS = (0, 25, 50, 100, 150, 200, 300, 500, 750, 1000)
IMPULSE_WINDOW_S = 0.5


def build(d: pd.DataFrame, holds_ms=(500, 1000, 2000)) -> pd.DataFrame:
    parts = []
    for (slug, side), g in d.groupby(["slug", "side_token"], sort=False):
        ts = g.ts.to_numpy(float)
        n = len(ts)
        if n < 300:
            continue
        ask = g.best_ask.to_numpy(float)
        bid = g.best_bid.to_numpy(float)
        asz = g.best_ask_size.to_numpy(float)
        spot = g.spot_mid.to_numpy(float)
        sgn = 1.0 if str(side).upper() == "UP" else -1.0
        back = np.searchsorted(ts, ts - IMPULSE_WINDOW_S, side="left")
        prev = spot[np.clip(back, 0, n - 1)]
        sr = np.where(prev > 0, spot / prev - 1.0, np.nan) * sgn

        r = {"sr": sr, "ev": f"{slug}|{side}"}
        for L in LATENCY_MS:
            f = np.searchsorted(ts, ts + L / 1000.0, side="left")
            ok = f < n
            i = np.clip(f, 0, n - 1)
            # The ask we can actually lift on arrival, and whether it has size.
            r[f"entry_{L}"] = np.where(ok & (asz[i] > 0), ask[i], np.nan)
            for h in holds_ms:
                f2 = np.searchsorted(ts, ts + (L + h) / 1000.0, side="left")
                ok2 = f2 < n
                i2 = np.clip(f2, 0, n - 1)
                r[f"exit_{L}_{h}"] = np.where(ok2, bid[i2], np.nan)
        parts.append(pd.DataFrame(r))
    R = pd.concat(parts, ignore_index=True).dropna(subset=["sr"])
    return R[np.isfinite(R.sr) & (R.sr > 0)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--pct", type=float, default=0.90)
    ap.add_argument("--out", default=str(ROOT / "research/phase4/hf_latency_curve.csv"))
    a = ap.parse_args()

    fs = sorted(glob.glob(str(Path(a.tape) / "hf" / "**" / "*.parquet"), recursive=True))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d[(d.spot_mid > 0) & (d.best_bid > 0) & (d.best_ask > 0)
          & (d.best_ask > d.best_bid) & (d.best_ask < 1) & (d.time_to_resolve > 15)]
    d = d.sort_values(["slug", "side_token", "ts"]).reset_index(drop=True)
    holds = (500, 1000, 2000)
    R = build(d, holds)
    S = R[R.sr >= R.sr.quantile(a.pct)]
    print(f"HF rows {len(d):,}  impulses {len(S):,}  event-sides {S.ev.nunique()}\n")
    print("Edge per share (cents) by order latency and hold. Entry lifts the ask")
    print("prevailing ON ARRIVAL; NaN entries are arrivals with no ask size.\n")
    rows = []
    hdr = "  ".join(f"hold{h}ms" for h in holds)
    print(f"{'latency':>9}  {hdr}      {'t@1s':>7}  {'fillable':>9}")
    for L in LATENCY_MS:
        line, tstat, fillable = [], np.nan, np.nan
        for h in holds:
            x = S[["ev", f"entry_{L}", f"exit_{L}_{h}"]].dropna()
            if len(x) < 150:
                line.append("     n/a")
                continue
            pnl = x[f"exit_{L}_{h}"].to_numpy(float) - x[f"entry_{L}"].to_numpy(float)
            per = pd.DataFrame({"ev": x.ev, "p": pnl}).groupby("ev").p.mean()
            se = per.std(ddof=1) / np.sqrt(len(per)) if len(per) > 1 else np.nan
            line.append(f"{100*pnl.mean():+8.3f}")
            if h == 1000:
                tstat = per.mean() / se if se else np.nan
                fillable = S[f"entry_{L}"].notna().mean()
                rows.append({"latency_ms": L, "edge_c": 100 * pnl.mean(),
                             "t": tstat, "fillable": fillable, "n": len(x)})
        print(f"{L:7d}ms  {'  '.join(line)}   {tstat:7.2f}  {fillable:8.1%}")
    tab = pd.DataFrame(rows)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(a.out, index=False)
    pos = tab[tab.edge_c > 0]
    if len(pos):
        print(f"\nedge stays positive (hold 1s) out to {pos.latency_ms.max()}ms of order latency")
        neg = tab[tab.edge_c <= 0]
        if len(neg):
            print(f"first non-positive latency: {neg.latency_ms.min()}ms  <- the reachability bar")
        else:
            print("edge positive across the whole swept range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
