#!/usr/bin/env python
"""Evaluation battery — run a candidate strategy against every recorded fixture.

Why this exists: a single fixture tells you almost nothing (one market regime,
one slice of luck). The `data/live_recordings/` folder now has ~25 hours of
live BTC 5m tape spanning many regimes (quiet, trending, choppy/volatile).
This script replays a candidate model against *all* of them, aggregates the
primary-score comparison against MomentumBaseline, and writes a JSON report
so iterations can be tracked and compared over time.

Usage:
    python research/eval_battery.py --model-file model_submission.py --label v3_majority_vote
    python research/eval_battery.py --model-file research/strategies/v2_majority_vote.py --label v2 --limit 8

Output:
    Prints a per-fixture table + aggregate summary.
    Writes research/results/{timestamp}_{label}.json with full detail.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polybench.cli import _load_model_from_file          # noqa: E402
from polybench.replay import ReplayConfig, replay        # noqa: E402

FIXTURE_DIRS = [
    REPO_ROOT / "data" / "live_recordings",
    REPO_ROOT / "tests" / "fixtures",
]
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def discover_fixtures(limit: int | None = None, binance_only: bool = False) -> list[Path]:
    paths: list[Path] = []
    for d in FIXTURE_DIRS:
        if d.exists():
            paths.extend(sorted(d.glob("*.parquet")))
    if binance_only:
        import pandas as pd
        kept = []
        for p in paths:
            try:
                col = pd.read_parquet(p, columns=["btc_last"])["btc_last"]
            except Exception:
                continue
            if (col > 0).mean() > 0.5:
                kept.append(p)
        paths = kept
    if limit:
        paths = paths[:limit]
    return paths


def run_one(model_file: Path, class_name: str, fixture: Path) -> dict:
    # Fresh model instance per fixture — strategies must not leak state across tapes.
    model = _load_model_from_file(str(model_file), class_name, {})
    cfg = ReplayConfig(output_dir=REPO_ROOT / "runs" / "research_eval_tmp")
    result = replay(model, fixture, config=cfg)
    m, b = result.metrics, result.baseline_metrics
    return {
        "fixture": fixture.name,
        "model_pnl": m.get("pnl_total", 0.0),
        "model_score": m.get("primary_score", 0.0),
        "model_trades": m.get("n_trades", 0),
        "model_sharpe": m.get("sharpe", 0.0),
        "model_max_dd": m.get("max_drawdown", 0.0),
        "baseline_pnl": b.get("pnl_total", 0.0),
        "baseline_score": b.get("primary_score", 0.0),
        "baseline_trades": b.get("n_trades", 0),
        "beats_baseline": m.get("primary_score", 0.0) > b.get("primary_score", 0.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-file", required=True, help="Path to model_submission.py-style file")
    ap.add_argument("--class-name", default="ModelSubmission")
    ap.add_argument("--label", required=True, help="Short name for this iteration, used in the report filename")
    ap.add_argument("--limit", type=int, default=None, help="Only run the first N fixtures (for quick iteration)")
    ap.add_argument("--binance-only", action="store_true",
                    help="Only fixtures where btc_last is populated (real Binance feed)")
    args = ap.parse_args()

    model_file = Path(args.model_file).resolve()
    fixtures = discover_fixtures(limit=args.limit, binance_only=args.binance_only)
    if not fixtures:
        print("No fixtures found under data/live_recordings/ or tests/fixtures/", file=sys.stderr)
        return 1

    print(f"Running '{args.label}' ({model_file.relative_to(REPO_ROOT)}) against {len(fixtures)} fixtures...\n")

    rows = []
    t0 = time.time()
    for i, fx in enumerate(fixtures, 1):
        try:
            row = run_one(model_file, args.class_name, fx)
        except Exception as exc:  # keep going — one bad tape shouldn't kill the battery
            print(f"  [{i}/{len(fixtures)}] {fx.name}: ERROR — {exc}")
            rows.append({"fixture": fx.name, "error": str(exc)})
            continue
        flag = "BEAT " if row["beats_baseline"] else "lose "
        print(f"  [{i}/{len(fixtures)}] {fx.name:45s} "
              f"score {row['model_score']:>10,.1f}  vs baseline {row['baseline_score']:>10,.1f}  "
              f"[{flag}]  pnl ${row['model_pnl']:>9,.2f}  trades {row['model_trades']:>3d}")
        rows.append(row)

    ok_rows = [r for r in rows if "error" not in r]
    n = len(ok_rows)
    elapsed = time.time() - t0

    summary = {}
    if n:
        wins = sum(1 for r in ok_rows if r["beats_baseline"])
        summary = {
            "n_fixtures": n,
            "n_errors": len(rows) - n,
            "win_rate_vs_baseline": wins / n,
            "mean_model_score": sum(r["model_score"] for r in ok_rows) / n,
            "median_model_score": sorted(r["model_score"] for r in ok_rows)[n // 2],
            "mean_baseline_score": sum(r["baseline_score"] for r in ok_rows) / n,
            "mean_model_pnl": sum(r["model_pnl"] for r in ok_rows) / n,
            "mean_baseline_pnl": sum(r["baseline_pnl"] for r in ok_rows) / n,
            "total_model_pnl": sum(r["model_pnl"] for r in ok_rows),
            "total_baseline_pnl": sum(r["baseline_pnl"] for r in ok_rows),
            "mean_trades": sum(r["model_trades"] for r in ok_rows) / n,
            "fixtures_with_zero_trades": sum(1 for r in ok_rows if r["model_trades"] == 0),
        }
        print("\n" + "=" * 78)
        print(f"AGGREGATE — '{args.label}'  ({n} fixtures, {elapsed:.0f}s)")
        print("=" * 78)
        print(f"  win rate vs baseline      : {summary['win_rate_vs_baseline']:.1%}  ({wins}/{n})")
        print(f"  mean primary score        : {summary['mean_model_score']:>12,.1f}   (baseline: {summary['mean_baseline_score']:>12,.1f})")
        print(f"  median primary score      : {summary['median_model_score']:>12,.1f}")
        print(f"  mean PnL per fixture      : ${summary['mean_model_pnl']:>10,.2f}   (baseline: ${summary['mean_baseline_pnl']:>10,.2f})")
        print(f"  total PnL across fixtures : ${summary['total_model_pnl']:>10,.2f}   (baseline: ${summary['total_baseline_pnl']:>10,.2f})")
        print(f"  mean trades per fixture   : {summary['mean_trades']:.1f}")
        print(f"  fixtures with zero trades : {summary['fixtures_with_zero_trades']}/{n}")
        print("=" * 78)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{ts}_{args.label}.json"
    out_path.write_text(json.dumps({
        "label": args.label,
        "model_file": str(model_file.relative_to(REPO_ROOT)),
        "timestamp_utc": ts,
        "elapsed_s": elapsed,
        "summary": summary,
        "fixtures": rows,
    }, indent=2))
    print(f"\nFull report written to {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
