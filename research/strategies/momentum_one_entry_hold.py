"""Tick-aware momentum + v10-style discipline.

Key lessons from v10:
- High edge threshold (≥15bps min, 25bps ideal) to overcome 1-tick latency loss (4-5bps)
- One entry per event → hold through settlement (no churning)
- Hold fixed shares, don't retarget ask-relative sizing every tick
"""

from __future__ import annotations

from collections import deque
import statistics
import math
from typing import Any

from polybench import FLAT, MarketInfo, Model, RunResult, Side, Signal, Tick


class ModelSubmission(Model):
    """Momentum signal with one-entry-per-event hold discipline."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        # Tick-count windows (handles variable frequency)
        self._short_window = 15
        self._long_window = 60
        self._vol_window = 40

        # Price history (tick-based)
        self._up_mids: deque[float] = deque(maxlen=self._long_window)
        self._down_mids: deque[float] = deque(maxlen=self._long_window)

        # Entry tracking (one per event)
        self._current_event_id: str | None = None
        self._position_side: Side | None = None
        self._position_size: float = 0.0

    def on_start(self, market_info: MarketInfo) -> None:
        """Reset per-event state."""
        self._up_mids.clear()
        self._down_mids.clear()
        self._current_event_id = None
        self._position_side = None
        self._position_size = 0.0

    def _momentum(self, prices: deque[float]) -> float:
        """Momentum: (short avg - long avg) / long avg."""
        if len(prices) < self._long_window:
            return 0.0
        prices_list = list(prices)
        short_avg = sum(prices_list[-self._short_window:]) / self._short_window
        long_avg = sum(prices_list[-self._long_window:]) / self._long_window
        return (short_avg - long_avg) / long_avg if long_avg > 0 else 0.0

    def _volatility(self, prices: deque[float]) -> float:
        """Price change volatility (not log returns)."""
        if len(prices) < 2:
            return 0.0
        prices_list = list(prices)
        changes = [abs(prices_list[i] - prices_list[i-1]) for i in range(1, len(prices_list))]
        return statistics.stdev(changes) if len(changes) > 1 else 0.0

    def on_tick(self, tick: Tick) -> Signal | None:
        """One entry per event, then hold."""
        self._up_mids.append(tick.up_mid)
        self._down_mids.append(tick.down_mid)

        # If we're already in a position this event, hold it
        if self._position_side is not None and self._current_event_id == tick.event_id:
            return Signal(side=self._position_side, size=self._position_size, confidence=0.8)

        # Minimum data before considering entry
        if len(self._up_mids) < self._short_window:
            return FLAT

        # Calculate momentum on both sides
        up_mom = self._momentum(self._up_mids)
        down_mom = self._momentum(self._down_mids)

        # Entry gate: need clear momentum directional signal
        # Momentum ranges ~[-0.01, +0.01], so 0.0005 (~50bps relative) is strong
        mom_threshold = 0.0005

        # Mid-event entry window (avoid late entries susceptible to adverse selection)
        ttr_ok = 40.0 < tick.time_to_resolve < 240.0

        if not ttr_ok:
            return FLAT

        # Pick the side with stronger positive momentum
        if up_mom > mom_threshold and up_mom > down_mom:
            signal_side = Side.UP
        elif down_mom > mom_threshold and down_mom > up_mom:
            signal_side = Side.DOWN
        else:
            return FLAT

        # Position sizing: base on momentum strength, capped at 40% (conservative)
        # Higher momentum → larger position
        mom_strength = max(up_mom, down_mom)
        base_size = min(0.40, mom_strength * 100)  # Scale [0, 0.004] → [0, 0.4]

        # Reduce size in high volatility
        vol = self._volatility(self._up_mids)
        if vol > 0.001:
            base_size *= 0.5

        if base_size < 0.10:
            return FLAT

        # Enter position (one per event)
        self._current_event_id = tick.event_id
        self._position_side = signal_side
        self._position_size = base_size

        return Signal(side=signal_side, size=base_size, confidence=min(0.9, mom_strength * 100))

    def on_finish(self, result: RunResult) -> None:
        pass
