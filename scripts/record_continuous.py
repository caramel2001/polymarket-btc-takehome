#!/usr/bin/env python3
"""
Continuous live Polymarket BTC 5m data recorder.

Records rolling 1-hour batches from the live Polymarket CLOB.
Output: data/live_recordings/{start}_{end}_BTC5M.parquet
Logs:   logs/recorder.log

Run directly:
    python scripts/record_continuous.py

Or via systemd:
    systemctl --user start polymarket-recorder
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).resolve().parent.parent
OUTPUT_DIR     = REPO_ROOT / "data" / "live_recordings"
LOG_FILE       = REPO_ROOT / "logs" / "recorder.log"
BATCH_DURATION = 3600.0   # seconds per batch
SYMBOL         = "BTC5M"
PRICE_SOURCE   = "polymarket"
RETRY_DELAY    = 15.0     # seconds to wait after a failed batch before retrying

# ── Logging setup ─────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,  # override any root handlers already set by polybench
)
log = logging.getLogger("recorder")

# Suppress noisy HTTP request logs from httpx / polybench internals
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("polybench").setLevel(logging.WARNING)

# ── Noop model ────────────────────────────────────────────────────────────────

sys.path.insert(0, str(REPO_ROOT / "src"))

from polybench.harness import Harness, HarnessConfig
from polybench.model import FLAT, Model, Signal, Tick


class _NoopModel(Model):
    def on_tick(self, tick: Tick) -> Signal | None:
        return FLAT


# ── Core recording function ───────────────────────────────────────────────────

async def record_batch() -> Path:
    """Run one BATCH_DURATION recording and return the saved parquet path."""
    start_ts  = time.time()
    start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    tmp_dir = REPO_ROOT / "runs" / f"recorder_tmp_{int(start_ts)}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    cfg = HarnessConfig(
        duration_s=BATCH_DURATION,
        price_source=PRICE_SOURCE,
        output_dir=tmp_dir,
        postmortem_resolution_s=30.0,
    )

    log.info("batch start  start=%s  duration=%ds  output_tmp=%s",
             start_str, int(BATCH_DURATION), tmp_dir)

    await Harness(model=_NoopModel(), config=cfg).run()

    end_ts  = time.time()
    end_str = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    src = tmp_dir / "ticks.parquet"
    dst = OUTPUT_DIR / f"{start_str}_{end_str}_{SYMBOL}.parquet"
    shutil.move(str(src), dst)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = dst.stat().st_size / 1_048_576
    log.info("batch done   file=%s  size=%.2fMB", dst.name, size_mb)
    return dst


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("=== polymarket recorder starting  output=%s ===", OUTPUT_DIR)
    batch_num = 0

    while True:
        batch_num += 1
        log.info("--- batch #%d ---", batch_num)
        try:
            await record_batch()
        except KeyboardInterrupt:
            log.info("recorder stopped by user")
            break
        except Exception as exc:
            log.exception("batch #%d failed: %s — retrying in %ds", batch_num, exc, int(RETRY_DELAY))
            await asyncio.sleep(RETRY_DELAY)


if __name__ == "__main__":
    asyncio.run(main())
