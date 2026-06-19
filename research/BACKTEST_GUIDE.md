# Backtest & Strategy Evaluation Guide

How to backtest and evaluate strategies on polymarket-btc-takehome using `eval_battery.py`.

## Quick Start: Run a strategy against the fixture battery

```bash
cd /home/pratham/.openclaw/workspace/polymarket-btc-takehome
source .venv/bin/activate
python3 research/eval_battery.py --model-file research/strategies/my_strategy.py --label my_v1
python3 research/eval_battery.py --model-file research/strategies/my_strategy.py --label my_v1 --binance-only  # Real Binance feed only
python3 research/eval_battery.py --model-file research/strategies/my_strategy.py --label my_v1 --limit 5    # Quick test on 5 fixtures
```

Outputs JSON to `research/results/{timestamp}_{label}.json`.

## Fixtures & data

- **All fixtures**: `data/live_recordings/*.parquet` (110+ fixtures, growing daily via `scripts/record_continuous.py`)
  - Old fixtures (before 2026-06-09 ~15:27 UTC): `btc_last=0` everywhere (Polymarket prices only, no Binance)
  - New fixtures (2026-06-09 onward): real `btc_last/btc_bid/btc_ask` from Binance WebSocket
- **Binance-backed count**: Use `--binance-only` to evaluate only fixtures where `btc_last > 0` in >50% of ticks (~95 as of 2026-06-14)
- **Test fixtures**: `tests/fixtures/` (tiny, synthetic events for unit testing)

## Metrics in the JSON report

**Primary aggregates** (per fixture):
- `model_score`: primary score = `pnl * sharpe * max(0, 1 - max_drawdown)`. Floored at 0 if sharpe≤0. This is the **promotion gate**.
- `model_pnl`: dollar profit/loss
- `model_trades`: number of fills
- `model_sharpe`: per-fixture Sharpe ratio

**Aggregated across all fixtures**:
- `mean primary_score`: expected headline number (BUT CHECK DISTRIBUTION)
- `median primary_score`: CRITICAL. If median≈0 and mean>>0, outlier mirage
- `std score`: high std + high mean = red flag for non-robust edge
- `win_rate vs baseline`: % of fixtures where `model_score > baseline_score`
- `total PnL`: sum of all fixture PnLs (THE ACTUAL BOTTOM LINE)
- `mean PnL per fixture`: `total_pnl / n_fixtures` (often negative!)
- `PnL positive fixtures`: count/% of fixtures making money
- `mean trades per fixture`: activity level
- `fixtures with zero trades`: count of fixtures where model stayed flat

## Interpretation: the promotion bar

**✅ Promotion checklist**:
1. **Distribution sanity**: `median ≈ mean` (no outlier mirage). If `median=0` and `std >> mean`, reject.
2. **Positive aggregate PnL** or at least `total_pnl` better than baseline by >30%
3. **Win-rate check**: beat baseline on >45% of fixtures (if lower, the gains are concentrated)
4. **Train/test stability**: run on train & test halves if available; verify out-of-sample metrics don't degrade >20%

**❌ Red flags** (don't promote):
- `median=0, mean=1000, std=5000` → outlier mirage (high mean driven by 2-3 lucky fixtures)
- Positive mean score but negative total PnL → the wins don't cover the losses
- `std >> mean` → high variance, unreliable
- In-sample/out-of-sample collapse (train +$1k, test -$2k) → overfitting
- Win-rate vs baseline <40% → gains are too concentrated

## Key harness quirks affecting backtest results

**1. One-tick execution latency**
- `replay.py` applies the signal from tick `t` to the book at tick `t+1` (NOT `t`)
- Market reprices toward BTC within ~1 second: fills are on average **+4 bps worse** than trigger prices
- Thin edges (margin <0.15) collapse under latency; only fat edges (>0.25) survive
- **Implication**: back-of-envelope sims overestimate profitability by 2-3x vs. actual battery results

**2. Retargeting churn**
- The simulator recomputes `size * capital / ask` shares **every tick**
- A constant `Signal.size` rebalances, paying spread + 50bps slippage + fee on each delta
- **Fix**: to hold N shares, emit `size_t = (entry_size * ask_t) / entry_ask` each tick
- Otherwise flat-0.40 looks much better in theory than practice

**3. FLAT near expiry sells at the bid**
- Emitting `FLAT` (or TTR < 1) sells pending shares at `best_bid * (1 - slippage)`, NOT at settlement ($1)
- v8/v9 "safety close at TTR<1" silently cost **1-6¢/share every event**
- **Fix**: hold through settlement; the harness settles pending positions at resolution

**4. Position rebalancing impact**
- In-event PnL vs. resolution PnL are tracked separately (`pnl_intra_event` vs `pnl_resolution`)
- If you're constantly rebalancing, you capture/lose intra-event noise, not the directional edge
- Strategy should either: (a) enter once and hold, or (b) have explicit exit logic

## Example: reading a battery result

```json
{
  "n_fixtures": 95,
  "aggregate": {
    "mean_primary_score": 712.3,
    "median_primary_score": 0.0,
    "std_primary_score": 3778.0,
    "win_rate": 42.3,
    "total_pnl": -1700.06,
    "mean_pnl_per_fixture": -17.53,
    "pnl_positive_fixtures": 13,
    "mean_trades_per_fixture": 6.4
  },
  "fixtures": [
    {
      "fixture": "20260609_162822_20260609_172858_BTC5M.parquet",
      "model_pnl": 437.0,
      "model_score": 23000.5,
      "model_trades": 17
    }
  ]
}
```

**Diagnosis**:
- Median=0 + mean=712 + std=3778 → outlier mirage (wins on 13/95 fixtures, loses on rest)
- Total PnL −$1,700 → underwater despite "good" mean score
- 6.4 trades/fixture vs 95 fixtures = ~608 total fills → decent activity
- ❌ **Don't promote** (fails PnL test)

## Calibration & sweep workflow

**Phase 1: Grid sweep on small sample** (`--limit 10-20`)
```bash
python3 research/eval_battery.py --model-file research/strategies/v_test.py --label v_test_limit10 --limit 10
# Quick feedback on 10 fixtures (1-2 min), tweak params, re-run
```

**Phase 2: Full battery on current fixture set**
```bash
python3 research/eval_battery.py --model-file research/strategies/v_final.py --label v_final_full
# ~5-10 min on 95 fixtures, get aggregate stats
```

**Phase 3: Train/test split validation** (if needed)
- Manually load the JSON, take first 50% of fixtures as train, second 50% as test
- Compare mean scores, PnLs, Sharpe ratios between splits
- If train >> test (>30% degradation), the model is overfit

**Phase 4: Head-to-head comparison**
```bash
# Run both candidates on IDENTICAL fixture set
python3 research/eval_battery.py --model-file v_candidate.py --label v_candidate --binance-only
python3 research/eval_battery.py --model-file model_submission.py --label baseline --binance-only
# Compare total PnL, win-rate, median score, std
```

## The Model class interface

Every strategy is a `Model` subclass in `src/polybench/model.py`:

```python
class ModelSubmission(Model):
    def on_start(self, market_info=None) -> None:
        # Called once at event start; reset state here
        pass
    
    def on_tick(self, tick: Tick) -> Signal | None:
        # Called on every tick (≈1 Hz); return Signal or FLAT
        # tick has: ts, event_id, time_to_resolve, up_bid/ask/mid, 
        #           down_bid/ask/mid, btc_last, resolution_up, etc.
        pass
```

**Return `FLAT`** (or `None`) to do nothing. Return `Signal(side=Side.UP, size=0.40, confidence=1.0)` to trade.
- `size`: fraction of starting capital (clipped to [0, 1])
- `side`: `Side.UP` | `Side.DOWN` | `Side.FLAT`
- `confidence`: optional (0-1), used in some harness configurations

**One-entry-per-event pattern**:
```python
def on_start(self):
    self._pos_side = None
    self._pos_size = 0.0
    self._event_traded = False

def on_tick(self, tick):
    if self._pos_side is not None:
        # Hold existing position
        return Signal(side=self._pos_side, size=self._pos_size, confidence=1.0)
    
    if self._event_traded:
        # Already exited this event, don't re-enter
        return FLAT
    
    # Entry logic here...
    if entry_condition:
        self._pos_side = Side.UP
        self._pos_size = 0.40
        self._event_traded = True
        return Signal(side=Side.UP, size=0.40, confidence=1.0)
    
    return FLAT
```

## Running a single fixture for debugging

```python
import sys; sys.path.insert(0, 'src')
from pathlib import Path
from polybench.cli import _load_model_from_file
from polybench.replay import ReplayConfig, replay

model = _load_model_from_file('research/strategies/my_strategy.py', 'ModelSubmission', {})
result = replay(model, Path('data/live_recordings/20260609_162822_20260609_172858_BTC5M.parquet'),
                config=ReplayConfig(output_dir=Path('runs/debug')))
print(result.metrics)
```

Output in `runs/debug/report.json` includes per-event breakdown.

## See also

- `IDEAS.md` — hypothesis tracker, promotion history
- `research/strategies/` — reference implementations (v7, v8, v9, v10)
