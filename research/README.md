# Research workflow — Polymarket BTC 5m strategy iteration

Goal: build a `model_submission.py` that *materially* beats `MomentumBaseline`
on the primary score `PnL × max(Sharpe, 0) × (1 − max_drawdown)`, robustly
across many market regimes — not just a single lucky fixture.

We now have ~25 hours of live recorded tape in `data/live_recordings/`
(quiet, trending, and choppy/volatile regimes all represented). A strategy
that only looks good on one fixture is overfit; the loop below forces
evaluation across the whole battery before calling an idea "validated".

## The loop

1. **Generate a hypothesis** — write it into `IDEAS.md` before coding.
   State *why* you think it'll help and *what evidence* would confirm/refute
   it. Good hypotheses point at a specific mechanism (e.g. "the CLOB only has
   depth on ~3% of ticks, clustered at event-open and near-resolution — a
   strategy that can't act in those windows can't get filled").

2. **Implement a variant** — copy the current best strategy from
   `research/strategies/`, modify it to test ONE hypothesis at a time, save
   it as the next version (`v{N}_{short_name}.py`). Keep changes isolated so
   you can attribute score deltas to a specific change.

3. **Evaluate on the full battery**:
   ```bash
   source .venv/bin/activate
   python research/eval_battery.py \
       --model-file research/strategies/v4_my_idea.py \
       --label v4_my_idea
   ```
   This replays the candidate against every fixture in `data/live_recordings/`
   and `tests/fixtures/`, prints a per-fixture comparison table plus an
   aggregate (win-rate vs baseline, mean/median primary score, mean PnL,
   trade counts, zero-trade fixtures), and writes a JSON report to
   `research/results/{timestamp}_{label}.json`.

   Use `--limit N` for a fast sanity pass on the first N fixtures while
   iterating; drop it for the real evaluation before declaring a result.

4. **Compare against the running log** — `research/results/` accumulates one
   JSON file per evaluation run. Diff the aggregate numbers across iterations
   to see whether a change actually helped *on average* (not just on the
   fixture you were staring at when you made the change).

5. **Update `IDEAS.md`** — mark the hypothesis validated / rejected / partial,
   write down *why* (cite the aggregate numbers), and log any new hypotheses
   the result suggests. This is the most important step — it's what keeps the
   next iteration (whether by you or a future agent) from re-testing the same
   dead end.

6. **Promote the winner** — once a variant's aggregate beats the current
   best on win-rate AND mean score (not just one of them — a strategy that
   wins big on 2 fixtures and loses small on 23 is not an improvement), copy
   it to `model_submission.py` at the repo root and note the promotion in
   `IDEAS.md`.

## Why "aggregate across the battery" matters here

Early iteration on this project showed wild variance fixture-to-fixture:
a tweak that took a strategy from 0 trades to +9% PnL on one tape produced
−43% on another. The CLOB is thin (fills on ~3% of ticks, often clustered
in a handful of windows per event), so a single fixture's score is mostly
*which random windows happened to have book depth*, not strategy quality.
Only the aggregate across many regimes is a meaningful signal.

## Directory layout

```
research/
├── README.md              — this file
├── IDEAS.md               — hypothesis log: idea → status → evidence → verdict
├── eval_battery.py        — runs a candidate against every recorded fixture
├── strategies/            — versioned snapshots, one file per iteration
│   └── v1_conviction_hold_majority.py
└── results/               — one JSON report per evaluation run (timestamped)
```

## Useful context (carried over from prior sessions — verify before trusting)

- **Fills are rare**: only ~3% of ticks have non-zero `best_bid_size` /
  `best_ask_size`; `is_ask_tradable` requires `best_ask_size > 0` AND a sane
  spread. A strategy must emit a *continuous* desired-position signal so it
  can be filled opportunistically whenever depth shows up — waiting for a
  "perfect moment" to signal usually means missing the only fillable tick.
- **Depth clusters**: heaviest at the first few ticks of an event (both
  sides) and again near resolution (TTR < ~65s, often massive size on one
  side only). Mid-event depth is sparse and often one-sided (e.g. UP ask
  depth with zero DOWN ask depth) — a DOWN signal can't fill if there's no
  DOWN-side depth, regardless of how correct the call is.
- **Resolution PnL dominates**: tokens settle to $1/$0. Holding a correctly-
  directed position to settlement is the largest source of edge; intra-event
  mark-to-market noise is largely just noise to be survived, not traded.
- **Signal → fill has a 1-tick lag**: a signal emitted on tick N is applied
  at tick N+1. When checking "would this signal have filled", always check
  the book at the *next* tick.
- **Fee formula**: `shares × 0.072 × p × (1−p)` — cheapest at price extremes
  (p near 0 or 1), most expensive at p = 0.50. Slippage is 0.5%/order
  (buys at ask×1.005, sells at bid×0.995).
