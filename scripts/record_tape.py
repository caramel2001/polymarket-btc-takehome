#!/usr/bin/env python3
"""Run the multi-asset full-depth tape recorder.

    python scripts/record_tape.py                       # all assets, both horizons, forever
    python scripts/record_tape.py --duration 600        # smoke test
    python scripts/record_tape.py --assets btc,eth --horizons 5m
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.recorder import RecorderConfig, TapeRecorder
from polyalpha.universe import ASSETS, HORIZONS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default=",".join(ASSETS))
    ap.add_argument("--horizons", default=",".join(HORIZONS))
    ap.add_argument("--out", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--flush-every", type=float, default=300.0)
    ap.add_argument("--log", default=str(ROOT / "logs" / "tape_recorder.log"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.FileHandler(a.log), logging.StreamHandler(sys.stdout)],
        force=True,
    )
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = RecorderConfig(
        out_dir=Path(a.out),
        assets=tuple(a.assets.split(",")),
        horizons=tuple(a.horizons.split(",")),
        duration_s=a.duration,
        flush_every_s=a.flush_every,
    )
    rec = TapeRecorder(cfg)
    try:
        asyncio.run(rec.run())
    except KeyboardInterrupt:
        print("\nstopped by user")
    print("stats:", rec.stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
