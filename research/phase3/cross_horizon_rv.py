#!/usr/bin/env python3
"""Cross-horizon relative value: two strikes, one terminal price.

The structure
-------------
15m and 5m events share settlement boundaries exactly (900 is a multiple of 300;
verified 110/110 on tape). So during the final 5 minutes, the 5m market and the
15m market are bets on the **same terminal price** ``S_E`` with **different
strikes**::

    5m  pays 1 if  S_E > K5   where K5  = S(E-300)
    15m pays 1 if  S_E > K15  where K15 = S(E-900)

Both strikes are public and fixed. So the two prices pin down two points of one
CDF, and under any common distributional assumption each implies a volatility.
**They must agree.** A persistent gap means one market is mispriced relative to
the other — a relative-value signal rather than a directional forecast.

This is the first thing tested in this project that is not a variation on
"predict the direction". It needs no view on where the price goes, only that two
quotes on the same random variable be mutually consistent.

Resolution convention
---------------------
Calibrated on tape: both reference times sit **45s before** the nominal window
boundaries (92.7% outcome agreement vs 86.0% at zero offset), matching the
``[t-1, t+4]`` minute quirk noted in ``IDEAS.md``. Since the shift is common to
both ends, the window still spans the horizon exactly and the relation is intact.

The pure-arbitrage version of this (buy the dominated basket below $1) was tested
first and is **absent**: zero violations once ``|d|`` is large enough that its
sign is unambiguous (n=15,173). The market prices the hard bound correctly, so
anything left here is statistical, not riskless.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
HS = {"5m": 300, "15m": 900}
REF_OFFSET = -45.0          # calibrated; see module docstring
P_FLOOR, P_CEIL = 0.02, 0.98


def load_books(tape: Path) -> pd.DataFrame:
    bf = sorted(glob.glob(str(tape / "books" / "**" / "*.parquet"), recursive=True))
    b = pd.concat([pd.read_parquet(f) for f in bf], ignore_index=True)
    b = b[b.spot_last > 0].copy()
    b["h"] = b.horizon.map(HS)
    # Only rows where the event is genuinely open for business.
    return b[(b.time_to_resolve > 0) & (b.time_to_resolve <= b.h)]


class SpotLookup:
    def __init__(self, b: pd.DataFrame) -> None:
        self._i, self._v = {}, {}
        for a, g in b.groupby("asset"):
            s = g[["ts", "spot_last"]].sort_values("ts").drop_duplicates("ts")
            self._i[a] = s.ts.to_numpy()
            self._v[a] = s.spot_last.to_numpy()

    def at(self, asset: str, t: float, tol: float = 4.0) -> float:
        I = self._i.get(asset)
        if I is None or not len(I):
            return np.nan
        i = np.searchsorted(I, t)
        best, bd = np.nan, tol
        for j in (i - 1, i):
            if 0 <= j < len(I):
                d = abs(I[j] - t)
                if d < bd:
                    bd, best = d, self._v[asset][j]
        return best


def implied_vol(p: np.ndarray, spot: np.ndarray, strike: np.ndarray,
                tau: np.ndarray) -> np.ndarray:
    """Annualisation-free sigma such that P(S_E > K) = p under lognormal.

    p = Phi( ln(S/K) / (sigma*sqrt(tau)) )  =>  sigma = ln(S/K) / (sqrt(tau)*Phi^-1(p))
    Returns NaN where the inversion is ill-conditioned (p near 0.5 with S==K).
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        z = norm.ppf(np.clip(p, P_FLOOR, P_CEIL))
        m = np.log(spot / strike)
        sig = m / (np.sqrt(np.maximum(tau, 1e-9)) * z)
    sig = np.where(np.isfinite(sig) & (sig > 0) & (sig < 10), sig, np.nan)
    return sig


def build(tape: Path) -> pd.DataFrame:
    b = load_books(tape)
    sl = SpotLookup(b)
    b15, b5 = b[b.horizon == "15m"], b[b.horizon == "5m"]
    cols5 = ["ts", "up_mid", "up_best_ask", "up_best_bid", "down_best_ask", "down_best_bid",
             "up_best_ask_size", "down_best_ask_size", "time_to_resolve", "spot_last",
             "resolved_outcome"]
    cols15 = ["ts", "up_mid", "up_best_ask", "up_best_bid", "down_best_ask", "down_best_bid",
              "up_best_ask_size", "down_best_ask_size"]
    out = []
    for (a, E), g15 in b15.groupby(["asset", "end_ts"]):
        g5 = b5[(b5.asset == a) & (b5.end_ts == E)]
        if g5.empty:
            continue
        k5 = sl.at(a, E - 300 + REF_OFFSET)
        k15 = sl.at(a, E - 900 + REF_OFFSET)
        if not (np.isfinite(k5) and np.isfinite(k15)):
            continue
        m = pd.merge_asof(g5.sort_values("ts")[cols5], g15.sort_values("ts")[cols15],
                          on="ts", direction="nearest", tolerance=2.0,
                          suffixes=("_5", "_15")).dropna()
        if m.empty:
            continue
        m = m.assign(asset=a, end=E, K5=k5, K15=k15)
        out.append(m)
    df = pd.concat(out, ignore_index=True)

    tau = df.time_to_resolve.to_numpy(float)
    spot = df.spot_last.to_numpy(float)
    df["sig5"] = implied_vol(df.up_mid_5.to_numpy(float), spot, df.K5.to_numpy(float), tau)
    df["sig15"] = implied_vol(df.up_mid_15.to_numpy(float), spot, df.K15.to_numpy(float), tau)
    df["sig_gap"] = df.sig5 - df.sig15
    # Relative moneyness separation; when the strikes are nearly equal the two
    # markets are the same bet and the inversion carries no information.
    df["strike_sep"] = np.abs(np.log(df.K5 / df.K15))
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--out", default=str(ROOT / "research/phase3/cross_horizon_rv.csv"))
    a = ap.parse_args()

    df = build(Path(a.tape))
    ok = df.dropna(subset=["sig5", "sig15"])
    print(f"overlapping ticks: {len(df):,}   with both implied vols: {len(ok):,}")
    print(f"paired events: {ok.groupby(['asset','end']).ngroups}\n")

    print("implied vol per second of remaining time, by market:")
    print(ok[["sig5", "sig15", "sig_gap", "strike_sep"]].describe(
        percentiles=[.05, .25, .5, .75, .95]).to_string(float_format=lambda v: f"{v:10.6f}"))

    sep = ok[ok.strike_sep > ok.strike_sep.median()]
    print(f"\nrestricted to well-separated strikes (n={len(sep):,}):")
    g = sep.sig_gap
    se = g.std(ddof=1) / np.sqrt(sep.groupby(["asset", "end"]).ngroups)
    print(f"  mean sig_gap = {g.mean():+.6f}   (event-clustered t = {g.mean()/se:+.2f})")
    print(f"  |gap| > 25% of mean vol on {(g.abs() > 0.25*ok.sig5.mean()).mean():.1%} of ticks")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ok.to_csv(a.out, index=False)
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
