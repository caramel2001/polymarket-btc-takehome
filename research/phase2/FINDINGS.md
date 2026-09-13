# Phase 2 findings — re-testing under a corrected data layer

Session 2026-09-13. Data: `data/panel/btc5m_panel` — 6.9M ticks, **24,078 events**,
2026-06-06 → 2026-09-12. (The prior "DEFINITIVE" verdict used 2,337 events.)

---

## 1. The capture bug was real and is fixed

`research/IDEAS.md` rests on "97-99% of recorded ticks have ask_size=0". That is
a **parsing artifact**, not market illiquidity — full write-up in `research/PLAN.md` §0.1.

| measurement | old capture | corrected |
|---|---|---|
| fillable ticks (1 Hz, across rollover) | ~3-4% | **44.1%** |
| fillable ticks (mid-event) | ~3-4% | **100%** |
| ask levels per book | 1 (synthetic) | ~48 |
| deltas dropped pre-snapshot | n/a | 0 |

Reconstruction verified against independent REST snapshots: 87/87 levels exact
when quiesced, 83.3% best-bid/ask agreement against a live racing feed.

**Real fees are zero**: 644/644 live trade prints report `fee_rate_bps: "0"`.
The harness charges `0.072·p·(1−p)` (0.53c/share at p=0.92). Both are reported
below — the harness number is the take-home's scoring rule, the zero is reality.

## 2. What did NOT survive — three of my own hypotheses, killed

Recorded because the cost of re-testing a dead idea is the main tax on this project.

- **`[-]` Removing the fee does not rescue the window IDEAS.md rejected.**
  `[0.90,0.95) ttr[5,15)` goes −0.445c (harness) → −0.074c (fee=0). The fee
  correction is real and material (+0.37c) but the cell stays insignificant
  (CI [−0.48,+0.34]) and fails OOS (train +0.30 → test −1.20). IDEAS.md's
  *conclusion* on this trade was right; its stated *reason* (fee + liquidity)
  was wrong.

- **`[-]` The apparent +3.6c/share taker edge at `[0.60,0.70)` was my own
  lookahead bias.** A "sane book" filter requiring `ask_{t+1} ≥ mid_t` conditions
  on the price not falling between selection and fill. Unfiltered, the same cell
  is **+0.16c**. Any filter applied at `t+1` to a trade selected at `t` is
  lookahead — the filter has to be expressible at `t`.

- **`[-]` Naive tick-level t-stats are inflated ~5-10×.** Ticks within one event
  share a single settled outcome, so the payoff term is literally identical
  across them. A cell showing n=239,491 / t=7.6 collapses to insignificance at
  the event level. **Every statistic here uses the event as the unit.**

Under honest treatment (one trade per event, first qualifying tick, no filters,
bootstrap + temporal OOS): **no taker cell survives**, at any price bucket or
TTR window, under any cost model. IDEAS.md's headline conclusion holds.

## 3. What DID survive — a real, TTR-dependent mispricing

Pooled calibration hides it. The favourite in `[0.85,0.90)` flips sign with
time-to-resolve (all ticks, one observation per event):

| ttr band | market | actual | edge | t |
|---|---|---|---|---|
| [10,30)   | 0.8765 | 0.8852 | **+0.88c** | 1.90 |
| [30,60)   | 0.8763 | 0.8833 | **+0.70c** | 1.93 |
| [60,120)  | 0.8759 | 0.8840 | **+0.81c** | 2.79 |
| [120,300) | 0.8735 | 0.8654 | **−0.81c** | −2.75 |

**Favourites are underpriced in the final ~2 minutes and overpriced early.**
Averaging over TTR nets to ≈ −0.43c, which is why the pooled calibration — and
every prior analysis that pooled — reported "the market is efficient".

This single fact reconciles every measurement in the session:

| | value |
|---|---|
| late-event favourite mispricing | ≈ +0.8c/share |
| half-spread | ≈ 0.55c |
| slippage (50bps @ p≈0.87) | ≈ 0.44c |
| **taker net** (pay half-spread + slip) | **≈ −0.2c → no edge** ✓ matches |
| **maker net** (earn half-spread instead) | **≈ +1.35c** ✓ matches passive test |

## 4. The passive (maker) upper bound

One trade per event, first qualifying tick, no filters, fee=0, no slippage.
13 of 21 cells clear a bootstrap CI above zero **and** out-of-sample t > 1.96:

| cell | n events | edge | t | boot lo | test | test t |
|---|---|---|---|---|---|---|
| [0.85,0.90) ttr[60,120) | 12,199 | +1.79c | 6.21 | +1.23 | +2.12 | 5.24 |
| [0.90,0.95) ttr[60,120) | 12,472 | +1.44c | 6.28 | +0.98 | +1.34 | 4.22 |
| [0.95,0.98) ttr[60,120) | 10,955 | +1.11c | 6.47 | +0.76 | +1.10 | 4.85 |
| [0.80,0.85) ttr[60,120) | 11,824 | +1.54c | 4.51 | +0.85 | +1.93 | 3.96 |

Decomposition at `[0.85,0.90) ttr[60,120)`: **+1.79c = 0.55c half-spread +
1.25c alpha**. So it is not pure spread capture.

Checks run: stale/wide-quote-at-entry **rejected** (entry spread 1.09c vs 1.06c
baseline; mid drift t→t+1 only −0.08c). Lookahead **excluded** by construction.

### What this is not

It is an **upper bound**, and three things stand between it and money:

1. **Adverse selection — the decisive unknown.** A resting bid is hit precisely
   when someone informed wants to sell. `IDEAS.md` "BATCH OF 6" #5 found that
   *taking* offered liquidity is −4 to −14c/share; a maker sits on the receiving
   end of exactly that flow. Note that finding was itself measured under the
   broken capture and needs re-running.
2. **Fill probability.** The bound assumes every resting bid at the touch fills.
3. **Queue position**, unobservable from public data.

All three are measurable from **trade prints**, which `MAKER_SCOPE.md` lists as a
blocking unknown but which are simply a stream the old recorder discarded. The
new recorder captures them (`data/tape/trades/`).

## 5. Next

Phase 0 of `research/MAKER_SCOPE.md` — the markout kill-test — is now the
highest-value experiment, and is unblocked. For each observed print, assume a
resting order at the touched price and measure the mid move at +5s/+30s/settlement.
Net maker EV = captured spread − markout − fee(0). Needs several days of tape.

Open and untouched: the multi-asset cross-section (5 assets × 2 horizons settling
on shared boundaries) — structurally invisible to all prior single-asset work.

---

## 6. Correction to "BATCH OF 6" #5 — the adverse-selection prior was overstated

IDEAS.md #5 ("Offered-liquidity / take-the-favorite-when-ask-has-size") reports
**−4 to −14c/share** and reads it as adverse selection: *"When a resting ask
appears it is an informed seller hitting you right before the price moves against
you."* That reading is the stated reason the maker idea carries a red flag.

Two things are wrong with it.

**(a) The sample is volatility-selected.** In the legacy capture, depth is only
recorded on ticks right after a `book` snapshot — and snapshots arrive when the
book is churning. Depth-recorded ticks have larger mid moves both *after* and
*before* them:

| | +1 tick | +5 | +15 | +30 |
|---|---|---|---|---|
| depth-tick / no-depth-tick, mean abs move | **1.25×** | 1.24× | 1.12× | 1.03× |

| | −1 tick | −5 | −15 |
|---|---|---|---|
| same, looking backward | **1.21×** | 1.29× | 1.28× |

Elevated symmetrically in both directions is the signature of a
**volatility-selected sample**, not of directional toxicity.

**(b) The magnitude is ~5-15× too large, and the mechanism is not adverse
selection.** Measured honestly (one trade per event, first qualifying tick,
fee=0, no slippage):

| condition | n events | edge | t | OOS | OOS t |
|---|---|---|---|---|---|
| all offered-ask ticks | 23,958 | **−0.85c** | −2.70 | −1.09 | −2.25 |
| UP is favourite | 19,851 | −0.73c | −2.14 | −1.19 | −2.25 |
| UP is underdog | 19,430 | −1.23c | −3.58 | −1.73 | −3.22 |
| favourite, ttr[0,60) | 7,205 | +0.45c | 1.13 | +0.27 | 0.42 |

And the **signed markout is tiny** — the direct measure of adverse selection:

| horizon | 1 tick | 5 | 15 | 30 | 60 |
|---|---|---|---|---|---|
| markout | −0.02c | −0.05c | −0.10c | −0.12c | −0.11c |

So the loss is not informed flow picking us off. It is **paying the half-spread
(≈0.55c) plus underdog overpricing** — the same favourite-longshot structure in §3.

### Why this matters for the maker thesis

The ratio that decides maker viability is *captured spread vs markout*. Measured
here: **spread ≈ 0.55c vs adverse selection ≈ 0.02–0.12c** — roughly 5–25× in the
maker's favour. And because the sample is volatility-selected, that markout is if
anything an **over**estimate.

This does not settle it. These markouts are measured at book-snapshot ticks, not
at actual fills; a real resting order is filled precisely when someone chooses to
cross it, which is the adverse subset by construction. Only trade prints answer
that — which is exactly the `MAKER_SCOPE.md` Phase 0 test, now unblocked.

But the prior that killed the maker programme was measured wrong, and corrected
it points the other way.

### A fourth backtest trap, found here

`[-]` **Event-weighting a tick-subset conditioned on an outcome-correlated state
manufactures huge fake asymmetries.** Grouping by event and averaging per-event
means over "ticks where UP is favourite" gave **−8.5c (t=−27.7)** for favourites
and **+6.7c (t=+21.4)** for underdogs. Both are artifacts: events contribute
different tick counts, and the count correlates with the outcome. One trade per
event at the first qualifying tick gives −0.73c and −1.23c. The fix is the same
as trap #1 — make the estimator literally match a tradeable rule.
