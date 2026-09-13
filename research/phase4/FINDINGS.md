# Phase 4 — high-frequency: the repricing window is real and ~500ms wide

Session 2026-09-13. **Preliminary: 11 events / 17-20 event-sides.** Directionally
clear and mechanistically coherent, nowhere near enough clusters to act on.

## 1. Why 1 Hz was hiding this

`research/IDEAS.md` ("DEFINITIVE", point 3) measures the spot→market lag at
1-second resolution, gets "~1 tick", and concludes:

> *"The lag and the latency are the same length (~1s), so the lag is uncapturable."*

At 1 Hz that is unfalsifiable — a 100ms lag and a 900ms lag are the same
measurement, and only one is tradeable.

Measured from the trade prints, 1 Hz sampling was discarding most of the market:

| | |
|---|---|
| prints arriving <1s after the previous | **73.6%** |
| prints arriving <100ms | **37.1%** |
| median inter-arrival | 230ms |
| **volume landing in multi-print seconds** | **73.8%** |
| max prints in one second | 76 |

So the recorder now captures **event-driven top-of-book** (emit on change only),
with spot attached at the same instant:

| | 1 Hz sampling | event-driven |
|---|---|---|
| book cadence | 1000ms | **9ms median** (p25 3ms) |
| rows per 161s | 1,360 | **46,765** (34×) |
| disk | — | ~1.0 GB/day, all 10 series |

## 2. The impulse response

Conditional on a top-decile spot move, cumulative mid response signed toward the
token (t-stats 40–75):

| horizon | response | % of final |
|---|---|---|
| 25ms | 0.60c | 13.6% |
| 100ms | 1.79c | 40.5% |
| 200ms | 3.03c | 68.5% |
| 300ms | 3.74c | 84.6% |
| **500ms** | **4.36c** | **98.6%** |

**The market needs ~half a second to absorb a spot move**, and ~60% of the
response is still outstanding at 100ms. That is the window.

> Bug caught: pooling UP and DOWN tokens cancels this exactly (a spot rise lifts
> UP and depresses DOWN), which produced a flat 0.0000c response at t=0.00. The
> impulse must be signed toward the token.

## 3. Is it tradeable?

Rule: on a spot impulse over the trailing 500ms favouring a token, **buy at the
prevailing ask**, exit by **selling at the bid** after `hold`. Both legs
executable, fee 0.

| hold | mid move | spot move after | edge/share | t (event) | win% |
|---|---|---|---|---|---|
| 100ms | 3.24c | +0.08bp | **−1.39c** | −2.53 | 28% |
| 200ms | 5.41c | +0.09bp | +1.03c | −0.02 | 53% |
| **500ms** | **6.70c** | −0.05bp | **+2.89c** | 1.64 | 64% |
| 1000ms | 6.18c | −0.16bp | +2.30c | 2.41 | 64% |
| 2000ms | 5.61c | −0.16bp | +2.01c | 2.76 | 62% |

Unconditional (no impulse filter): **−3.44c** — exactly the spread, as it should be.

**It is repricing, not momentum.** Spot is flat after entry (+0.08bp → −0.16bp)
and the mid response *peaks at 500ms and decays*. Momentum would show spot
continuing to drift and the mid continuing to rise.

At 100ms the edge is **negative**: we have paid the spread before the move
arrives. The window is roughly 200ms–1s after the spot move.

## 4. What this explains

It is the other side of the maker result in `research/phase3/FINDINGS.md`.
There, whichever side we ended up net short **won 60–73%** of the time, costing
−31.7c per naked share. Here is the mechanism: during a ~500ms repricing window
resting quotes are stale, and whoever is fast lifts them. The maker is the one
being picked off. Both measurements are the same phenomenon from opposite sides,
which is the main reason to take this seriously.

Notably, market makers do **not** defend by widening — spread at impulse is
0.80–0.94× the quiet baseline, i.e. slightly *tighter*.

## 5. Why this is not yet a strategy

- **17 event-sides.** t of 1.6–3.0 on that few clusters is weak. The single most
  valuable thing now is tape, and it accumulates ~10× faster across 10 series.
- **Order latency is not modelled.** Entry is at the instant we observe the book;
  real order transmission to Polymarket would push us later down the curve. The
  edge is still positive at 1–2s, but that needs measuring, not assuming.
- **Competition.** If faster participants already work this window, we are the
  ones being picked off rather than doing the picking. Our own measured floor:
  spot staleness ~99ms median, p95 1025ms.

## 6. Next

1. Accumulate HF tape; re-run `research/phase4/hf_repricing.py`. Watch whether
   the 500ms peak and the sign flip at 100ms survive with more clusters.
2. Model order latency explicitly — shift entry by a configurable delay and find
   the latency at which the edge dies. That number decides whether this is
   reachable from a normal host or needs colocation.
3. Cross-asset: BTC leads the smaller-cap markets in absorbing the same
   information? The 10-series tape can answer it; nothing has looked yet.

---

## 7. The reachability bar: the edge dies at ~100ms of order latency

`hf_repricing.py` enters the instant a book update is observed — implicitly
assuming zero order-transmission time. `hf_latency_curve.py` sweeps an explicit
delay: the impulse is detected at `t`, the order arrives at `t + L`, and we lift
whatever ask is prevailing **then**.

| order latency | hold 500ms | hold 1s | hold 2s | t @1s | fillable |
|---|---|---|---|---|---|
| 0ms | +4.22c | **+3.73c** | +2.94c | 2.52 | 100.0% |
| 25ms | +2.79c | +2.18c | +1.45c | 1.33 | 99.5% |
| 50ms | +2.14c | +1.39c | +0.76c | 0.69 | 99.2% |
| **100ms** | +0.51c | **−0.48c** | −1.10c | −0.66 | 99.0% |
| 200ms | −1.64c | −2.93c | −3.44c | −3.11 | 98.3% |
| 500ms | −3.13c | −3.67c | −4.39c | −3.28 | 97.5% |

**The edge crosses zero at ~100ms of total latency.** Fillability stays 97–100%
throughout, so this is purely a latency constraint, not a liquidity one.

### Measured against our own stack

| component | median | p95 |
|---|---|---|
| Binance spot staleness | **91ms** | 881ms |
| Polymarket WS transport | **162ms** | 697ms |

Our *detection floor alone* is ~91ms — at the bar before a single order is sent.
Adding WS transport and order submission puts us around 250ms+, which the table
prices at **−3c/share**.

### Verdict

**The inefficiency is real and it is not reachable from this setup.** It lives
entirely inside a window narrower than retail latency, which is precisely why the
market looks efficient at 1 Hz and why every taker study in `IDEAS.md` found
nothing: they were sampling far coarser than the effect.

It also closes the loop on the maker result. At ~250ms of latency we are not the
one lifting stale quotes — we are the stale quote. That is the −31.7c per naked
share in `research/phase3/FINDINGS.md`, measured from the other side.

What it would take: a sub-10ms spot feed (colocated or direct), a fast order path
to Polymarket, and a total budget under ~50ms to retain roughly half the edge.
That is an infrastructure decision, not a research one — and it should be costed
against ~+2 to +4c/share on roughly the top decile of impulses.

---

## 8. Narrowing it: the edge is **BTC-only**, and the bar is a hosting decision

### Illiquid assets are worse, not better — hypothesis rejected

The natural idea was that thinly-traded markets reprice more slowly, widening the
window. They do, in *percentage* terms — at 200ms, btc is 91% repriced while doge
is 54% and eth 45%. But their spreads are proportionally far wider, so the move
cannot cover the cost. Edge per share at 0ms / 100ms latency, hold 1s:

| asset | spread | 0ms | 100ms |
|---|---|---|---|
| **btc** | 1c | **+4.36c** | **+2.08c** |
| sol | 2c | +1.16c | −0.48c |
| xrp | 2c | +0.40c | −1.60c |
| eth | 1c | −0.14c | −0.71c |
| doge | 4c | −1.35c | −2.29c |

**The entire aggregate edge was BTC.** Everything else is negative at any
reachable latency. Narrowing to BTC also roughly doubles the measured edge,
because the other four were diluting it.

### BTC alone, properly powered

Top-decile impulse, hold 1s, 27 event-sides, bootstrap over events:

| latency | edge | t | boot 2.5% | win% |
|---|---|---|---|---|
| 0ms | +4.36c | 3.99 | +2.80c | 62.5% |
| 50ms | +3.13c | 3.59 | +1.90c | 54.4% |
| **100ms** | **+2.08c** | **2.98** | **+0.95c** | 48.6% |
| 150ms | +0.96c | 1.75 | −0.05c | 40.9% |
| 200ms | +0.22c | 0.44 | −0.78c | 34.5% |

By horizon at 100ms: **5m +2.71c (t=2.89)**, 15m +1.14c (t=1.08). The 5-minute
series carries it.

So the bar is **~150ms of total order latency**, and the edge is comfortably
significant at 100ms.

### Correcting an error in §7

§7 stated our "detection floor alone is ~91ms". That was wrong. The 91ms is the
**Binance update cadence**, not our latency: BTC bookTicker inter-arrival is 49ms
median, and observed staleness is 49ms median — exactly half-cadence, which is
what you see at a random sampling instant with *zero* network delay. Spot
freshness is not the constraint.

The real constraint is the **order path**, which §7 never measured.

### Measured order path

| | median | min |
|---|---|---|
| Polymarket CLOB HTTP RTT (`/book`, warm client) | **284ms** | 266ms |
| Polymarket TCP connect | 120ms | 104ms |
| google.com TCP connect (local-network sanity) | **7.4ms** | 6.7ms |
| Binance stream TCP (Tokyo, `54.178.x`) | 346ms | 294ms |

Local connectivity is fine (7ms to Google), so the 284ms is distance to
Polymarket's origin, not a bad link.

**284ms against a ~150ms bar: from this host the strategy loses ~0.5c/share.**

### The actual conclusion

This is a **hosting decision, not a research result**. The edge is real,
significant, mechanism-backed, and confirmed from three independent directions
(impulse response, taker PnL, and the maker's mirror-image loss). It is simply
being measured from the wrong side of the planet.

The cheap decisive experiment: re-run this measurement from a VPS near
Polymarket's origin. If RTT there is ~10–30ms, total latency lands around
30–50ms, where the measured edge is **+3.1 to +3.7c/share**. That costs a few
dollars to test and is the single highest-value next step in this project.

Caveats that remain regardless of hosting: 27 event-sides is still thin; the top
decile of impulses is a minority of ticks, so capacity is limited; and if faster
participants already work this window from closer hosts, we would be trading
against them rather than with them.
