# Multi-asset crypto up/down trading framework — build plan

**Author:** research session 2026-09-13. Supersedes the "no edge" verdict in `IDEAS.md`.

---

## 0. Why the existing conclusions must be re-opened

`IDEAS.md` closes with a **[✗] DEFINITIVE: no capturable directional edge**
verdict and parks all taker strategies. That verdict rests on two load-bearing
premises. Both were measured on 2026-09-13 and **both are wrong**.

### 0.1 The "97% of ticks are unfillable" premise is a parsing bug

Not market illiquidity — our own capture code throwing depth away.

| Evidence | Result |
|---|---|
| Live REST `/book`, all 4 assets | 28–58 ask levels, 34–69 bid levels, real size. Books are **deep**. |
| Live WS `price_change` payload keys | `asset_id, best_ask, best_bid, hash, price, side, size` — **no** `best_bid_size`/`best_ask_size` |
| WS message mix over 55s | `book` 1,026 vs `price_change` **16,679** (~94%) |
| Recorded tape, 40 fixtures spanning Jun–Sep | median **4.2%** of ticks with any ask depth |

Mechanism: `harness.py:_books_from_clob_ws_message` handles `price_change` via
`_book_from_top`, which builds a **1-level synthetic book** whose size comes from
`best_bid_size`/`best_ask_size`. Those keys do not exist in the payload, so
`_first_float_or_zero` returns `0.0`. `_apply_clob_ws_payload` then *overwrites*
the good full-depth cached book with that size-0 synthetic. `pnl.py` requires
`best_ask_size > 0` to fill. So depth survives only on the ~4% of ticks landing
right after a `book` snapshot — matching the observed number almost exactly.

**Fix validated bit-perfect:** seed a level map from the `book` snapshot, then
apply `price_change` deltas as *absolute set* semantics (`size == 0` deletes the
level, else assign). Reconstruction matched a fresh REST snapshot **87/87 levels,
exact sizes, after 2,974 deltas.**

### 0.2 The fee model charges a fee that does not exist

`pnl.py` charges `shares × 0.072 × p × (1−p)` — 1.8c/share at p=0.50,
0.53c/share at p=0.92. Measured live: **644/644** `last_trade_price` prints
across all four assets report `fee_rate_bps: "0"`.

This directly flips a rejected result. `IDEAS.md` dismisses favorite mispricing
because the real +1 to +3.3% mid-edge at p=0.90–0.95 is "exactly eaten by
half-spread + latency + fee", tightest window at **−0.1c/share**. Remove a
0.53c/share fee that isn't charged and that window is **≈ +0.43c/share**.

> **Scoring vs reality.** The harness fee is the take-home's scoring rule and we
> do not get to change it. But the user's goal is real financial success, so every
> result from here reports **both**: `score_cost` (harness: 0.072 fee + 50bps
> slip) and `real_cost` (fee 0 + measured slippage). Divergence between them is
> itself a finding.

### 0.3 Two more things the prior work missed

- **Trade prints are already on the wire.** `last_trade_price` messages carry
  `price, size, side, timestamp, transaction_hash`. `MAKER_SCOPE.md` lists
  trade-print fidelity as a blocking unknown for maker research — it is not
  blocking, we simply discard the messages. Both of its gating unknowns
  (maker fee, print fidelity) are now resolved, and both resolve *favorably*.
- **The tape is 19× bigger than documented.** `BACKTEST_GUIDE.md` says "110+
  fixtures"; there are **2,065** (~2,000 hours, Jun–Sep 2026). Several IDEAS.md
  rejections were decided on 30–195 fixtures.

---

## 1. Scope

**Assets:** BTC, ETH, SOL, XRP (all four `*-updown-5m-<end_ts>` series confirmed
live and trading concurrently). **Horizons:** 5m now; probe and add 15m/1h.

Concurrency is the point: four correlated assets settling on the *same* 300s
boundary is a cross-sectional structure the single-asset BTC work could not see.

---

## 2. Phases

### Phase 1 — Fix the base (prerequisite for everything)

1. **`OrderBook` level-map** — seed from `book`, apply `price_change` deltas,
   full depth maintained. Replaces `_book_from_top` overwrite. Unit-test against
   recorded REST snapshots.
2. **Multi-asset / multi-horizon discovery** — generalize
   `market.py:find_active_btc_event` (hardcoded `btc-` prefix + 300s boundary)
   to `(asset, horizon) -> slug`.
3. **Recorder v2** — N assets concurrently on one WS; persist **full depth**
   (top 5 levels/side), **trade prints**, and a book-integrity checksum.
   New schema, versioned; old tape stays readable.
4. **Binance multi-asset feed** — ETH/SOL/XRP spot alongside BTC. Fix the
   `btc_last == 0` regression (bid/ask populate, last does not).

**Gate:** measured fillable-tick rate on fresh tape. If it lands near the ~50%+
the live books imply rather than 4%, the base is fixed.

### Phase 2 — Re-validate what the bug invalidated

Re-run against corrected fills, under both cost models, on the full 2,065-fixture
tape with purged walk-forward splits:
favorite convergence (§0.2 — highest prior), v11 endgame, the 6 "dead" classes
(esp. #5 offered-liquidity, whose "adverse selection" reading is confounded with
the size-0 bug), v10 fair-value Kelly, cross-token arb (needs full depth, not top).

**This phase is where the near-term edge most likely already exists.**

### Phase 3 — Research infrastructure

- **Event panel dataset** — one row per (asset, event, tick) in parquet/DuckDB,
  with causal features only; the unit of analysis becomes the *event*, not the
  fixture, giving ~2,000 hrs × 12 events × 4 assets ≈ 96k events.
- **Leakage-safe CV** — purged + embargoed walk-forward by event boundary.
  Note the `[t−1, t+4]` resolution-window alignment trap already documented.
- **Execution sim** — full-depth walk-the-book fills, queue-aware passive fills
  from trade prints, both cost models.
- **Significance discipline** — every candidate reports n, CI, and a
  deflated Sharpe / multiple-testing correction. The prior arc repeatedly
  mistook small-n noise for signal, then caught itself; make that automatic.

### Phase 4 — Strategies

Ordered by prior-probability given what we now know:
1. **Favorite convergence** (re-opened by §0.2, mechanism already measured).
2. **Cross-asset relative value** — 4 assets, same settlement boundary. Verified
   ETH→BTC lag-1 lead (AUC 0.525→0.550) that "the market prices in" — but that
   was measured under the broken fill model.
3. **Maker / spread capture** — both gating unknowns now resolved favorably
   (fee 0, prints available). `MAKER_SCOPE.md` Phase 0 markout test is now cheap.
4. **Cross-token & complement arb** — re-test with full depth.
5. **ML** — reuse `research/ml/`, but as a *maker* fair value, not a taker signal.

### Phase 5 — Live paper validation

Shadow mode: compute intended orders, place none, compare against subsequent
prints. Promote only what survives out-of-sample **and** shadow.

---

## 3. Promotion bar

Unchanged from `BACKTEST_GUIDE.md` and enforced: median ≈ mean (no outlier
mirage), positive total PnL, >45% win-rate vs baseline, <20% OOS degradation.
Plus new: must hold under **both** cost models, and report n + CI.

## 4. Sequencing note

Phase 1 is strictly prerequisite — every backtest until the book is fixed is
measuring our parser, not the market.
