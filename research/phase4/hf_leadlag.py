#!/usr/bin/env python3
"""Sub-second lead-lag: how long does this market take to reprice after spot?

The claim being re-opened
-------------------------
`research/IDEAS.md` ("DEFINITIVE", point 3) measures
``corr(btc_ret[t], mid_change[t+k])`` at **1-second** resolution, finds k=0 +0.43,
k=+1 +0.38, k=+2 +0.06, and concludes:

> *"The market lags BTC by ~1 tick — but replay fills at t+1, precisely when the
> catch-up move lands. The lag and the latency are the same length (~1s), so the
> lag is uncapturable."*

At 1 Hz that conclusion is unfalsifiable: a 100ms lag and a 900ms lag are the
same measurement. Both look like "~1 tick". Only one of them is tradeable.

The event-driven tape samples the book on change (median 9ms) with spot attached
at the same instant (median staleness 65ms), so the reaction can be resolved
properly.

What is measured
----------------
An **impulse response**: conditional on a spot move of a given size at t0, the
cumulative change in the market mid at +25ms, +50ms, ... +3000ms. If the market
has fully repriced by the time we could act, there is nothing here. If a
meaningful fraction of the move arrives later than our own latency, there is.

Our latency budget is measured from the tape, not assumed: spot staleness at
book-update time, plus WS transport delay on the Polymarket side.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
GRID_MS = (25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000)


def load_hf(tape: Path, assets: tuple[str, ...] | None = None) -> pd.DataFrame:
    fs = sorted(glob.glob(str(tape / "hf" / "**" / "*.parquet"), recursive=True))
    if not fs:
        raise SystemExit(f"no HF tape under {tape}/hf -- let the recorder run")
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    if assets:
        df = df[df.asset.isin(assets)]
    df = df[(df.spot_mid > 0) & (df.best_bid > 0) & (df.best_ask > 0)
            & (df.best_ask > df.best_bid) & (df.time_to_resolve > 5)]
    return df.sort_values(["slug", "side_token", "ts"]).reset_index(drop=True)


def impulse_response(g: pd.DataFrame, horizons_ms=GRID_MS) -> pd.DataFrame:
    """For each book update, spot return just before vs mid change just after."""
    ts = g.ts.to_numpy(float)
    mid = g.mid.to_numpy(float)
    spot = g.spot_mid.to_numpy(float)
    n = len(ts)
    if n < 200:
        return pd.DataFrame()

    # Spot impulse over the preceding 500ms — the "news" the market must absorb.
    back = np.searchsorted(ts, ts - 0.5, side="left")
    prev_spot = spot[np.clip(back, 0, n - 1)]
    spot_ret = np.where(prev_spot > 0, spot / prev_spot - 1.0, np.nan)

    # A spot rise lifts an UP token and depresses a DOWN token. Pooling the two
    # without flipping the sign cancels the effect exactly.
    tok_sign = 1.0 if str(g.side_token.iloc[0]).upper() == "UP" else -1.0
    out = {"spot_ret": spot_ret, "ts": ts, "mid": mid,
           "tok_sign": np.full(n, tok_sign),
           "ttr": g.time_to_resolve.to_numpy(float)}
    for ms in horizons_ms:
        fwd = np.searchsorted(ts, ts + ms / 1000.0, side="left")
        ok = fwd < n
        fwd_mid = np.where(ok, mid[np.clip(fwd, 0, n - 1)], np.nan)
        out[f"d{ms}"] = fwd_mid - mid
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--assets", default="")
    ap.add_argument("--out", default=str(ROOT / "research/phase4/hf_leadlag.csv"))
    a = ap.parse_args()

    assets = tuple(x for x in a.assets.split(",") if x) or None
    df = load_hf(Path(a.tape), assets)
    print(f"HF rows: {len(df):,}  events: {df.slug.nunique()}  assets: {sorted(df.asset.unique())}")
    dt = df.groupby(["slug", "side_token"]).ts.diff().dropna()
    dt = dt[dt > 0]
    print(f"book update cadence: median {1000*dt.median():.0f}ms  p25 {1000*dt.quantile(.25):.0f}ms")
    print(f"spot staleness at update: median {1000*df.spot_age.median():.0f}ms "
          f"p95 {1000*df.spot_age.quantile(.95):.0f}ms\n")

    parts = [impulse_response(g) for _, g in df.groupby(["slug", "side_token"], sort=False)]
    parts = [p for p in parts if len(p)]
    if not parts:
        raise SystemExit("not enough HF data per event yet")
    R = pd.concat(parts, ignore_index=True).dropna(subset=["spot_ret"])
    R = R[np.isfinite(R.spot_ret)]

    # Condition on a real impulse: top decile of |spot move|.
    thr = R.spot_ret.abs().quantile(0.90)
    imp = R[R.spot_ret.abs() >= thr].copy()
    sign = np.sign(imp.spot_ret.to_numpy()) * imp.tok_sign.to_numpy()
    print(f"impulse set: |spot ret| >= {thr:.6f} over 500ms   n={len(imp):,}\n")
    print("Cumulative mid response to a spot impulse, signed with the move:")
    print(f"{'horizon':>9} {'mean move':>11} {'t':>8} {'% of 3s':>9}")
    rows = []
    base = None
    for ms in GRID_MS:
        v = (sign * imp[f"d{ms}"].to_numpy(float))
        v = v[np.isfinite(v)]
        if len(v) < 100:
            continue
        m = v.mean()
        se = v.std(ddof=1) / np.sqrt(len(v))
        rows.append({"ms": ms, "resp_c": 100 * m, "t": m / se, "n": len(v)})
    tab = pd.DataFrame(rows)
    if len(tab):
        base = tab.resp_c.iloc[-1]
        tab["pct_of_final"] = 100 * tab.resp_c / base if base else np.nan
        for r in tab.itertuples():
            print(f"{r.ms:7d}ms {r.resp_c:10.4f}c {r.t:8.2f} {r.pct_of_final:8.1f}%")

    print("\nInterpretation: the fraction of the response still OUTSTANDING at our")
    print("own latency is the only part that could be captured.")
    lat_ms = 1000 * df.spot_age.median()
    if len(tab):
        near = tab.iloc[(tab.ms - lat_ms).abs().argmin()]
        print(f"  measured spot staleness (our floor): ~{lat_ms:.0f}ms")
        print(f"  response already complete by then  : {near.pct_of_final:.1f}%")
        print(f"  still outstanding                  : {100-near.pct_of_final:.1f}%")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
