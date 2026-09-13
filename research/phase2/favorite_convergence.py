#!/usr/bin/env python3
"""Re-test the favourite-convergence trade under a corrected cost model.

The claim being re-opened
-------------------------
``research/IDEAS.md`` ("DEFINITIVE: no capturable directional edge", point 2)
finds that favourites at mid 0.90-0.95 genuinely beat their mid by +1 to +3.3%,
strongest near expiry — then rejects the trade because after "ASK + 1-tick
latency + 50bps slip + fee, EVERY window is net-negative per share", the
tightest being ``p[0.90,0.95] ttr[5,15] = -0.1c``.

Two of those cost terms are now known to be wrong:

* **Fee.** The harness charges ``0.072*p*(1-p)``; at p=0.92 that is 0.53c/share.
  Measured live, 644/644 trade prints report ``fee_rate_bps: "0"``. Removing a
  cost that is not charged moves a -0.1c/share result to about +0.43c/share.
* **Fill feasibility.** The rejection assumed fills are possible on ~3% of ticks.
  That was a capture artifact; the corrected measurement is ~100% mid-event.

So the trade is re-run here on the full tape, under both cost models, with a
temporal train/test split and confidence intervals — because the thing this
research arc has repeatedly got wrong is mistaking a small-n fluke for an edge.

Nothing here is a promotion. It is a measurement.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

HARNESS_FEE_RATE = 0.072      # pnl.py default
HARNESS_SLIP = 0.005          # 50 bps


@dataclass(frozen=True)
class CostModel:
    name: str
    fee_rate: float
    slippage: float

    def fill_price(self, ask: np.ndarray) -> np.ndarray:
        return ask * (1.0 + self.slippage)

    def fee(self, price: np.ndarray) -> np.ndarray:
        p = np.clip(price, 0.0, 1.0)
        return self.fee_rate * p * (1.0 - p)


# What the take-home scores you on, and what a real account would actually pay.
SCORE_COSTS = CostModel("score (harness)", HARNESS_FEE_RATE, HARNESS_SLIP)
REAL_COSTS = CostModel("real (fee=0)", 0.0, HARNESS_SLIP)
REAL_NOSLIP = CostModel("real (fee=0, no slip)", 0.0, 0.0)


def load_panel(path: str, columns: list[str] | None = None) -> pd.DataFrame:
    """Read the panel, whether it is a single file or a directory of shards."""
    p = Path(path)
    if p.is_dir():
        import pyarrow.dataset as ds
        return ds.dataset(p, format="parquet").to_table(columns=columns).to_pandas()
    if not p.exists() and p.with_suffix("").is_dir():
        import pyarrow.dataset as ds
        return ds.dataset(p.with_suffix(""), format="parquet").to_table(
            columns=columns).to_pandas()
    return pd.read_parquet(p, columns=columns)


def _mean_ci(x: np.ndarray, alpha: float = 0.05) -> tuple[float, float, float]:
    """Mean with a normal-approx CI. n here is thousands, so this is fine."""
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    m = float(np.mean(x))
    if n < 2:
        return m, float("nan"), float("nan")
    se = float(np.std(x, ddof=1)) / np.sqrt(n)
    z = 1.959963985
    return m, m - z * se, m + z * se


def build_trades(panel: pd.DataFrame, *, p_lo: float, p_hi: float,
                 ttr_lo: float, ttr_hi: float,
                 max_spread: float = 1.0) -> pd.DataFrame:
    """One candidate trade per qualifying tick: buy the favourite, hold to settle.

    Fill uses the **next** tick's ask, matching the harness's one-tick execution
    latency. Payoff is the settled value of the side bought.
    """
    df = panel
    # Favourite side and its executable ask, per tick.
    up_fav = df["up_mid"].to_numpy(float) >= df["down_mid"].to_numpy(float)
    fav_mid = np.where(up_fav, df["up_mid"], df["down_mid"]).astype(float)
    fav_ask_now = np.where(up_fav, df["up_ask"], df["down_ask"]).astype(float)
    fav_bid_now = np.where(up_fav, df["up_bid"], df["down_bid"]).astype(float)

    ek = df["event_key"].to_numpy()
    same_event_next = np.r_[ek[1:] == ek[:-1], False]
    nxt = np.r_[np.arange(1, len(df)), len(df) - 1]

    up_ask_next = df["up_ask"].to_numpy(float)[nxt]
    down_ask_next = df["down_ask"].to_numpy(float)[nxt]
    fill_ask = np.where(up_fav, up_ask_next, down_ask_next)
    up_bid_next = df["up_bid"].to_numpy(float)[nxt]
    down_bid_next = df["down_bid"].to_numpy(float)[nxt]
    fill_bid = np.where(up_fav, up_bid_next, down_bid_next)

    ttr = df["time_to_resolve"].to_numpy(float)
    spread = np.where(up_fav, df["spread_up"], df["spread_down"]).astype(float)
    outcome_up = df["outcome_up"].to_numpy(int)
    payoff = np.where(up_fav, outcome_up, 1 - outcome_up).astype(float)

    ok = (
        same_event_next
        & (fav_mid >= p_lo) & (fav_mid < p_hi)
        & (ttr >= ttr_lo) & (ttr < ttr_hi)
        & (fill_ask > 0.0) & (fill_ask < 1.0)
        & (fav_bid_now > 0.0)
        & (spread <= max_spread)
    )
    if not ok.any():
        return pd.DataFrame()

    return pd.DataFrame({
        "ts": df["ts"].to_numpy()[ok],
        "event_key": ek[ok],
        "fav_mid": fav_mid[ok],
        "fill_ask": fill_ask[ok],
        "fill_bid": fill_bid[ok],
        "spread": spread[ok],
        "ttr": ttr[ok],
        "payoff": payoff[ok],
        "side_up": up_fav[ok].astype(int),
    })


def score_trades(tr: pd.DataFrame, cost: CostModel) -> dict[str, float]:
    if tr.empty:
        return {"n": 0}
    fill = cost.fill_price(tr["fill_ask"].to_numpy(float))
    fee = cost.fee(fill)
    pnl = tr["payoff"].to_numpy(float) - fill - fee
    m, lo, hi = _mean_ci(pnl)
    return {
        "n": len(tr),
        "n_events": int(tr["event_key"].nunique()),
        "win_rate": float(tr["payoff"].mean()),
        "mean_fill": float(np.mean(fill)),
        "edge_c": 100.0 * m,
        "ci_lo_c": 100.0 * lo,
        "ci_hi_c": 100.0 * hi,
        "t_stat": (m / (np.std(pnl, ddof=1) / np.sqrt(len(pnl)))) if len(pnl) > 1 else float("nan"),
    }


def run_grid(panel: pd.DataFrame, costs: list[CostModel], *,
             split_frac: float = 0.5, max_spread: float = 1.0) -> pd.DataFrame:
    """Grid over price bucket x ttr window, each with a temporal train/test split.

    The split is by time across the whole tape, so 'test' is strictly later
    data — the only split that answers "would this have kept working".
    """
    cut = panel["ts"].quantile(split_frac)
    rows = []
    price_buckets = [(0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.98)]
    ttr_windows = [(2, 10), (5, 15), (10, 30), (30, 60), (60, 120), (120, 300)]

    for p_lo, p_hi in price_buckets:
        for t_lo, t_hi in ttr_windows:
            tr = build_trades(panel, p_lo=p_lo, p_hi=p_hi,
                              ttr_lo=t_lo, ttr_hi=t_hi, max_spread=max_spread)
            if tr.empty:
                continue
            train = tr[tr["ts"] <= cut]
            test = tr[tr["ts"] > cut]
            for cost in costs:
                a = score_trades(tr, cost)
                b = score_trades(train, cost)
                c = score_trades(test, cost)
                rows.append({
                    "price": f"[{p_lo:.2f},{p_hi:.2f})",
                    "ttr": f"[{t_lo},{t_hi})",
                    "cost": cost.name,
                    "n": a.get("n", 0),
                    "win": a.get("win_rate", float("nan")),
                    "fill": a.get("mean_fill", float("nan")),
                    "edge_c": a.get("edge_c", float("nan")),
                    "ci_lo": a.get("ci_lo_c", float("nan")),
                    "ci_hi": a.get("ci_hi_c", float("nan")),
                    "t": a.get("t_stat", float("nan")),
                    "train_c": b.get("edge_c", float("nan")),
                    "test_c": c.get("edge_c", float("nan")),
                    "n_test": c.get("n", 0),
                })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=str(ROOT / "data/panel/btc5m_panel"))
    ap.add_argument("--out", default=str(ROOT / "research/phase2/favorite_convergence.csv"))
    ap.add_argument("--max-spread", type=float, default=1.0)
    a = ap.parse_args()

    panel = load_panel(a.panel, columns=[
        "ts", "event_key", "time_to_resolve", "up_mid", "down_mid",
        "up_ask", "down_ask", "up_bid", "down_bid",
        "spread_up", "spread_down", "outcome_up",
    ])
    panel = panel.sort_values(["event_key", "ts"]).reset_index(drop=True)
    print(f"panel: {len(panel):,} ticks  {panel.event_key.nunique():,} events")
    print(f"range: {pd.to_datetime(panel.ts.min(), unit='s')} .. "
          f"{pd.to_datetime(panel.ts.max(), unit='s')}\n")

    grid = run_grid(panel, [SCORE_COSTS, REAL_COSTS, REAL_NOSLIP],
                    max_spread=a.max_spread)
    if grid.empty:
        print("no qualifying trades")
        return 1

    pd.set_option("display.width", 220)
    for cost_name in grid["cost"].unique():
        sub = grid[grid.cost == cost_name].drop(columns=["cost"])
        print(f"===== cost model: {cost_name} =====")
        print(sub.to_string(index=False, float_format=lambda v: f"{v:8.3f}"))
        print()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    grid.to_csv(a.out, index=False)
    print(f"wrote {a.out}")

    # Headline: the exact window IDEAS.md rejected, under each cost model.
    key = grid[(grid.price == "[0.90,0.95)") & (grid.ttr == "[5,15)")]
    if not key.empty:
        print("\n--- the window IDEAS.md rejected at -0.1c/share ---")
        print(key[["cost", "n", "win", "fill", "edge_c", "ci_lo", "ci_hi",
                   "train_c", "test_c"]].to_string(index=False,
                                                   float_format=lambda v: f"{v:8.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


class PassiveCosts(CostModel):
    """Upper bound on maker economics: assume we posted and got hit at the bid.

    This is deliberately optimistic — it grants a passive fill at the touch with
    no queue risk, no fill-probability haircut, and no adverse selection. It
    exists to *bound* the maker thesis: if buying the favourite at the bid is
    still unprofitable, no realistic maker implementation can rescue it, and the
    whole maker programme in ``research/MAKER_SCOPE.md`` can be closed cheaply.
    If it is profitable, the number is the ceiling that adverse selection then
    has to be subtracted from — not a result.
    """

    def __init__(self) -> None:
        super().__init__("passive maker bound (fill@bid, fee=0)", 0.0, 0.0)


PASSIVE_COSTS = PassiveCosts()
