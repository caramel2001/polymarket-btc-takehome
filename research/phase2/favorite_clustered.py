#!/usr/bin/env python3
"""Favourite-convergence edge, with statistics that respect the data's structure.

The trap in the naive grid
--------------------------
``favorite_convergence.py`` reports cells like ``[0.90,0.95) ttr[120,300)`` with
n=239,491 and t=7.6. Those are *ticks*, and ticks inside one 5-minute event are
almost perfectly dependent: the same favourite, the same book, and above all the
**same settled outcome**. The payoff term is literally identical across every
tick of an event. Treating them as independent inflates t by roughly
``sqrt(ticks per event)`` — here a factor of ~5-10.

So this script re-estimates the same quantity three honest ways:

1. **One trade per event** — the first qualifying tick. Fully independent
   observations, at the cost of sample size.
2. **Event-mean then t-test** — average the per-tick PnL within each event,
   then test across events. Uses all the data, one observation per event.
3. **Block bootstrap by event** — resample whole events with replacement.
   Makes no normality assumption.

A cell only counts as real if it survives all three *and* holds on a temporal
out-of-sample split.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research" / "phase2"))

from favorite_convergence import (  # noqa: E402
    PASSIVE_COSTS, REAL_COSTS, REAL_NOSLIP, SCORE_COSTS, CostModel,
    build_trades, load_panel,
)

RNG = np.random.default_rng(20260913)


def event_level_stats(tr: pd.DataFrame, cost: CostModel,
                      n_boot: int = 2000) -> dict[str, float]:
    """Per-share edge with event as the unit of observation."""
    if tr.empty:
        return {"n_events": 0}
    # The passive model fills at the bid we posted; taker models lift the ask.
    ref = "fill_bid" if cost is PASSIVE_COSTS else "fill_ask"
    fill = cost.fill_price(tr[ref].to_numpy(float))
    pnl = tr["payoff"].to_numpy(float) - fill - cost.fee(fill)

    df = pd.DataFrame({"event_key": tr["event_key"].to_numpy(),
                       "pnl": pnl, "ts": tr["ts"].to_numpy()})

    # (1) first qualifying tick per event
    first = df.groupby("event_key", sort=False).first()
    x1 = first["pnl"].to_numpy()

    # (2) event-mean
    means = df.groupby("event_key", sort=False)["pnl"].mean().to_numpy()

    def _t(x: np.ndarray) -> tuple[float, float]:
        if len(x) < 2:
            return float("nan"), float("nan")
        se = np.std(x, ddof=1) / np.sqrt(len(x))
        return float(np.mean(x)), float(np.mean(x) / se) if se > 0 else float("nan")

    m1, t1 = _t(x1)
    m2, t2 = _t(means)

    # (3) bootstrap over events
    lo = hi = float("nan")
    if len(means) > 10:
        idx = RNG.integers(0, len(means), size=(n_boot, len(means)))
        boots = means[idx].mean(axis=1)
        lo, hi = np.percentile(boots, [2.5, 97.5])

    return {
        "n_events": int(len(means)),
        "n_ticks": int(len(df)),
        "first_c": 100 * m1, "first_t": t1,
        "mean_c": 100 * m2, "mean_t": t2,
        "boot_lo_c": 100 * lo, "boot_hi_c": 100 * hi,
        "win_rate": float(tr["payoff"].mean()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=str(ROOT / "data/panel/btc5m_panel"))
    ap.add_argument("--out", default=str(ROOT / "research/phase2/favorite_clustered.csv"))
    a = ap.parse_args()

    panel = load_panel(a.panel, columns=[
        "ts", "event_key", "time_to_resolve", "up_mid", "down_mid",
        "up_ask", "down_ask", "up_bid", "down_bid",
        "spread_up", "spread_down", "outcome_up",
    ])
    panel = panel.sort_values(["event_key", "ts"]).reset_index(drop=True)
    cut = panel["ts"].quantile(0.5)
    print(f"panel {len(panel):,} ticks / {panel.event_key.nunique():,} events")
    print(f"train <= {pd.to_datetime(cut, unit='s')} < test\n")

    rows = []
    for p_lo, p_hi in [(0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.98)]:
        for t_lo, t_hi in [(5, 15), (10, 30), (30, 60), (60, 120), (120, 300)]:
            tr = build_trades(panel, p_lo=p_lo, p_hi=p_hi, ttr_lo=t_lo, ttr_hi=t_hi)
            if tr.empty:
                continue
            for cost in (SCORE_COSTS, REAL_COSTS, REAL_NOSLIP, PASSIVE_COSTS):
                full = event_level_stats(tr, cost)
                tr_tr = tr[tr.ts <= cut]
                tr_te = tr[tr.ts > cut]
                s_tr = event_level_stats(tr_tr, cost, n_boot=500)
                s_te = event_level_stats(tr_te, cost, n_boot=500)
                rows.append({
                    "price": f"[{p_lo:.2f},{p_hi:.2f})", "ttr": f"[{t_lo},{t_hi})",
                    "cost": cost.name,
                    "n_ev": full.get("n_events", 0),
                    "win": full.get("win_rate", np.nan),
                    "first_c": full.get("first_c", np.nan),
                    "first_t": full.get("first_t", np.nan),
                    "mean_c": full.get("mean_c", np.nan),
                    "mean_t": full.get("mean_t", np.nan),
                    "boot_lo": full.get("boot_lo_c", np.nan),
                    "boot_hi": full.get("boot_hi_c", np.nan),
                    "train_c": s_tr.get("mean_c", np.nan),
                    "test_c": s_te.get("mean_c", np.nan),
                    "test_t": s_te.get("mean_t", np.nan),
                })

    grid = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    for cname in grid["cost"].unique():
        sub = grid[grid.cost == cname].drop(columns=["cost"])
        print(f"===== {cname} =====")
        print(sub.to_string(index=False, float_format=lambda v: f"{v:7.3f}"))
        print()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    grid.to_csv(a.out, index=False)

    print("\n=== cells that survive every check (real fee=0) ===")
    ok = grid[(grid.cost == REAL_COSTS.name) & (grid.boot_lo > 0)
              & (grid.test_c > 0) & (grid.first_t > 1.96) & (grid.mean_t > 1.96)]
    print(ok.to_string(index=False, float_format=lambda v: f"{v:7.3f}") if len(ok)
          else "  NONE — no cell clears event-clustered significance + OOS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
