#!/usr/bin/env python3
"""High-frequency repricing edge: buy into the window before the market catches up.

What this tests
---------------
Spot moves; this market takes time to reprice. `research/IDEAS.md` measured that
lag at **1-second** resolution, found "~1 tick", and concluded it was uncapturable
because our own execution latency was also ~1 tick. At 1 Hz that conclusion cannot
be checked: a 100ms lag and a 900ms lag are the same measurement, and only one of
them is tradeable.

The event-driven tape resolves it: book updates at a **9ms** median cadence with
spot attached at the same instant (median staleness ~99ms).

Measured impulse response (mid, signed toward the token, top-decile spot move):

    25ms  0.60c (14%) | 100ms 1.79c (41%) | 200ms 3.03c (69%) | 500ms 4.36c (99%)

So the market needs roughly **half a second** to fully absorb a spot move, and
~60% of the response is still outstanding at 100ms.

The rule
--------
On a spot impulse over the trailing 500ms favouring a token, buy at the prevailing
**ask** and exit by selling at the **bid** after ``hold``. Both legs executable,
fee 0 (verified from 13k+ prints), event-clustered statistics.

Why the result is repricing and not momentum: **spot is flat after entry**
(+0.08bp at 100ms to −0.16bp at 2s), and the mid response *peaks at 500ms and
decays*. Momentum would show spot continuing to drift.

Status: n=761 impulses over **17 event-sides**. Directionally clear and
mechanistically coherent, but far too few clusters to act on. Re-run as the HF
tape accumulates (~1 GB/day across 10 series).
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HOLDS_MS = (100, 200, 500, 1000, 2000, 5000, 10000)
IMPULSE_WINDOW_S = 0.5


def load_hf(tape: Path, min_ttr: float = 15.0) -> pd.DataFrame:
    fs = sorted(glob.glob(str(tape / "hf" / "**" / "*.parquet"), recursive=True))
    if not fs:
        raise SystemExit(f"no HF tape under {tape}/hf -- let the recorder run")
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d[(d.spot_mid > 0) & (d.best_bid > 0) & (d.best_ask > 0)
          & (d.best_ask > d.best_bid) & (d.best_ask < 1)
          & (d.time_to_resolve > min_ttr)]
    return d.sort_values(["slug", "side_token", "ts"]).reset_index(drop=True)


def build(d: pd.DataFrame) -> pd.DataFrame:
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
        mid = g.mid.to_numpy(float)
        # A spot rise lifts UP and depresses DOWN; sign the impulse toward the token.
        sgn = 1.0 if str(side).upper() == "UP" else -1.0
        back = np.searchsorted(ts, ts - IMPULSE_WINDOW_S, side="left")
        prev = spot[np.clip(back, 0, n - 1)]
        sr = np.where(prev > 0, spot / prev - 1.0, np.nan) * sgn

        r = {"sr": sr, "ask": ask, "asz": asz, "mid0": mid, "spot0": spot,
             "ev": f"{slug}|{side}", "ttr": g.time_to_resolve.to_numpy(float)}
        for h in HOLDS_MS:
            f = np.searchsorted(ts, ts + h / 1000.0, side="left")
            ok = f < n
            i = np.clip(f, 0, n - 1)
            r[f"bid_{h}"] = np.where(ok, bid[i], np.nan)
            r[f"mid_{h}"] = np.where(ok, mid[i], np.nan)
            r[f"spot_{h}"] = np.where(ok, spot[i], np.nan)
        parts.append(pd.DataFrame(r))
    R = pd.concat(parts, ignore_index=True).dropna(subset=["sr"])
    # Only genuine positive impulses; the signed series is full of exact zeros
    # (spot unchanged), so a plain quantile on it is not selective.
    return R[np.isfinite(R.sr) & (R.asz > 0) & (R.sr > 0)]


def report(R: pd.DataFrame, pct: float) -> pd.DataFrame:
    thr = R.sr.quantile(pct)
    S = R[R.sr >= thr]
    rows = []
    for h in HOLDS_MS:
        cols = ["ev", "ask", "mid0", "spot0", f"bid_{h}", f"mid_{h}", f"spot_{h}"]
        x = S[cols].dropna()
        if len(x) < 200:
            continue
        pnl = x[f"bid_{h}"].to_numpy(float) - x.ask.to_numpy(float)
        mid_mv = x[f"mid_{h}"].to_numpy(float) - x.mid0.to_numpy(float)
        sp_mv = x[f"spot_{h}"].to_numpy(float) / x.spot0.to_numpy(float) - 1.0
        per = pd.DataFrame({"ev": x.ev, "p": pnl}).groupby("ev").p.mean()
        se = per.std(ddof=1) / np.sqrt(len(per)) if len(per) > 1 else np.nan
        rows.append({"hold_ms": h, "n": len(x), "clusters": len(per),
                     "mid_move_c": 100 * mid_mv.mean(),
                     "spot_move_bp": 1e4 * sp_mv.mean(),
                     "edge_c": 100 * pnl.mean(),
                     "t": per.mean() / se if se else np.nan,
                     "win": 100 * (pnl > 0).mean()})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--pct", type=float, default=0.90,
                    help="impulse percentile within POSITIVE impulses")
    ap.add_argument("--out", default=str(ROOT / "research/phase4/hf_repricing.csv"))
    a = ap.parse_args()

    d = load_hf(Path(a.tape))
    print(f"HF rows {len(d):,}  events {d.slug.nunique()}  assets {sorted(d.asset.unique())}")
    R = build(d)
    print(f"positive-impulse rows {len(R):,}  event-sides {R.ev.nunique()}\n")
    tab = report(R, a.pct)
    if tab.empty:
        raise SystemExit("not enough data yet")
    print(f"impulse >= {a.pct:.0%} percentile of positive spot moves over "
          f"{IMPULSE_WINDOW_S*1000:.0f}ms\n")
    print(tab.to_string(index=False, float_format=lambda v: f"{v:9.3f}"))
    print("\nspot_move_bp ~ 0 after entry => repricing, not momentum.")
    print(f"clusters = {tab.clusters.max()} event-sides; t-stats on this few are weak.")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
