#!/usr/bin/env python3
"""MAKER_SCOPE Phase 0 — the adverse-selection kill test.

The question that decides the maker programme
---------------------------------------------
A maker earns the half-spread but is filled precisely when someone chose to cross
it. Viability is therefore::

    maker EV = captured half-spread  -  adverse-selection markout  -  fee

``research/MAKER_SCOPE.md`` lists this as the go/no-go gate and names two blocking
unknowns. Both are now resolved, and both favourably:

* **Maker fee** — 13,612/13,612 observed prints report ``fee_rate_bps: "0"``.
* **Trade-print fidelity** — prints carry price, size, taker side and timestamp.
  They were never missing; the old recorder discarded the ``last_trade_price``
  stream.

Method
------
Every print is a fill *we could have been the passive side of*. A taker BUY of
the UP token lifted a resting ask, so the maker who was hit is now short UP at
that price. Their markout at horizon k is ``-(mid[t+k] - price)``; for a taker
SELL, ``+(mid[t+k] - price)``. Positive markout means the passive side came out
ahead — i.e. the flow is *not* toxic at that horizon.

We report markout against the **same token's own mid**, sampled from the 1 Hz
book tape, at several horizons plus settlement. Spread captured is measured from
the book at the moment of the print, so the two sides of the inequality are
measured on the same data.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HORIZONS = (1, 5, 15, 30, 60)


def load_tape(tape_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    tf = sorted(glob.glob(str(tape_dir / "trades" / "**" / "*.parquet"), recursive=True))
    bf = sorted(glob.glob(str(tape_dir / "books" / "**" / "*.parquet"), recursive=True))
    if not tf or not bf:
        raise SystemExit(f"no tape under {tape_dir} (trades={len(tf)} books={len(bf)})")
    trades = pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
    books = pd.concat([pd.read_parquet(f) for f in bf], ignore_index=True)
    return trades, books


def build_book_series(books: pd.DataFrame) -> pd.DataFrame:
    """One row per (slug, side_token, ts) carrying that token's own book top."""
    frames = []
    for side in ("up", "down"):
        cols = {
            f"{side}_mid": "mid", f"{side}_best_bid": "bid", f"{side}_best_ask": "ask",
            f"{side}_best_bid_size": "bid_size", f"{side}_best_ask_size": "ask_size",
            f"{side}_microprice": "microprice",
        }
        f = books[["ts", "slug", "time_to_resolve", "resolved_outcome", *cols]].copy()
        f = f.rename(columns=cols)
        f["side_token"] = side.upper()
        frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["slug", "side_token", "ts"]).reset_index(drop=True)


def attach_markouts(trades: pd.DataFrame, series: pd.DataFrame) -> pd.DataFrame:
    """As-of join each print to its token's book, then to the book k seconds later."""
    t = trades.sort_values("ts").reset_index(drop=True)
    t = t[(t.price > 0) & (t.price < 1) & (t["size"] > 0)].copy()

    joined = []
    for (slug, side), grp in t.groupby(["slug", "side_token"], sort=False):
        s = series[(series.slug == slug) & (series.side_token == side)]
        if s.empty or grp.empty:
            continue
        s = s.sort_values("ts")
        g = grp.sort_values("ts")
        # book state at the moment of the print
        at = pd.merge_asof(g, s[["ts", "mid", "bid", "ask", "bid_size", "ask_size"]],
                           on="ts", direction="backward", tolerance=3.0)
        for k in HORIZONS:
            fut = s[["ts", "mid"]].rename(columns={"mid": f"mid_p{k}"}).copy()
            fut["ts"] = fut["ts"] - k          # so asof finds the quote k secs later
            at = pd.merge_asof(at, fut, on="ts", direction="forward", tolerance=3.0)
        joined.append(at)
    if not joined:
        raise SystemExit("no prints could be joined to book state")
    return pd.concat(joined, ignore_index=True)


def summarise(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Markout from the perspective of the PASSIVE side that was hit."""
    # taker BUY lifts a resting ask -> maker is short; taker SELL hits a resting
    # bid -> maker is long. sign maps the mid move into the maker's PnL.
    sign = np.where(df.taker_side.str.upper() == "BUY", -1.0, 1.0)
    rows = []
    for k in HORIZONS:
        col = f"mid_p{k}"
        m = df[col].to_numpy(float) - df.price.to_numpy(float)
        mk = sign * m
        ok = ~np.isnan(mk)
        if ok.sum() < 30:
            continue
        x = mk[ok]
        se = np.std(x, ddof=1) / np.sqrt(len(x))
        rows.append({"horizon_s": k, "n": int(ok.sum()),
                     "markout_c": 100 * x.mean(), "t": x.mean() / se if se > 0 else np.nan})
    out = pd.DataFrame(rows)
    out.insert(0, "scope", label)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--out", default=str(ROOT / "research/phase3/maker_markout.csv"))
    a = ap.parse_args()

    trades, books = load_tape(Path(a.tape))
    print(f"prints={len(trades):,}  book rows={len(books):,}  events={books.slug.nunique()}")
    fee = trades.fee_rate_bps.unique()
    print(f"fee_rate_bps observed: {sorted(fee)}  <- maker fee assumption\n")

    series = build_book_series(books)
    df = attach_markouts(trades, series)
    df = df[df.mid.notna()]
    print(f"prints joined to book state: {len(df):,}\n")

    spread = (df.ask - df.bid)
    valid = spread[(spread > 0) & (spread < 0.5)]
    half = 100 * valid.mean() / 2
    print(f"mean half-spread at print time: {half:.3f}c  (n={len(valid):,})")
    print("This is what a maker captures. Adverse selection must stay below it.\n")

    parts = [summarise(df, "all prints")]
    for asset, g in df.groupby("asset"):
        if len(g) >= 200:
            parts.append(summarise(g, f"asset={asset}"))
    for hz, g in df.groupby("horizon"):
        if len(g) >= 200:
            parts.append(summarise(g, f"horizon={hz}"))
    df["ttr_band"] = pd.cut(df.time_to_resolve, [-1, 30, 60, 120, 300, 1e9],
                            labels=["<30s", "30-60s", "60-120s", "120-300s", ">300s"])
    for band, g in df.groupby("ttr_band", observed=True):
        if len(g) >= 200:
            parts.append(summarise(g, f"ttr={band}"))

    res = pd.concat(parts, ignore_index=True)
    pd.set_option("display.width", 200)
    print("=== markout from the PASSIVE side (positive = maker wins) ===")
    print(res.to_string(index=False, float_format=lambda v: f"{v:9.4f}"))

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(a.out, index=False)

    allp = res[res.scope == "all prints"]
    if not allp.empty:
        worst = allp.markout_c.min()
        print(f"\n--- GATE ---")
        print(f"half-spread captured : {half:+.3f}c")
        print(f"worst markout        : {worst:+.3f}c")
        print(f"net maker EV (fee 0) : {half + worst:+.3f}c per share")
        print("POSITIVE -> proceed to Phase 1 (passive-fill simulator)"
              if half + worst > 0 else "NEGATIVE -> flow is toxic; stop.")
        print("\nCAVEAT: n is small until the recorder accumulates days of tape,")
        print("and this assumes we are at the front of the queue for every print.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
