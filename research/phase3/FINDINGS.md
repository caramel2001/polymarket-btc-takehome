# Phase 3 — MAKER_SCOPE Phase 0 kill test: **PASSED** (preliminary)

Session 2026-09-13. Tape: `data/tape`, 35 events, 13,612 prints, ~40 minutes.
**n is small — this is a first read, not a verdict.** Re-run as tape accumulates.

## Both gating unknowns resolved, both favourably

`research/MAKER_SCOPE.md` names two unknowns that must be settled before any
maker work. Neither needed the multi-week build it budgets for.

1. **Maker fee.** 13,612/13,612 observed prints report `fee_rate_bps: "0"`.
   The doc's worry — *"if makers pay the same 0.072·p·(1−p) as takers, the
   spread-capture advantage largely evaporates"* — does not apply.
2. **Trade-print fidelity.** Prints carry `price, size, side, timestamp,
   transaction_hash`. They were never missing; `polybench` simply discarded the
   `last_trade_price` stream. No new connection was required.

## The gate

Maker viability is `captured half-spread − adverse-selection markout − fee`.

| term | value |
|---|---|
| mean half-spread at print time | **+0.664c** |
| worst markout across horizons (all prints) | **−0.171c** |
| fee | **0** |
| **net maker EV** | **+0.493c / share** |

Markout is measured from the passive side's perspective: a taker BUY lifts a
resting ask, leaving the maker short, so their PnL is `−(mid[t+k] − price)`.

**Sign convention verified directly.** Taker BUYs land at/above the ask 78.3% of
the time and mid then *falls* −0.35c; taker SELLs land at/below the bid 79.9% and
mid then *rises* +0.21c. In both directions the passive side gains — temporary
impact that reverts, which is the signature of uninformed, liquidity-driven flow
rather than informed pick-off.

## The result that matters: adverse selection is a function of time-to-resolve

| ttr band | +1s | +5s | +15s | +30s | +60s |
|---|---|---|---|---|---|
| **120–300s** | **+0.42** | **+0.49** | **+0.68** | **+0.60** | **+0.72** |
| 60–120s | −0.06 | −0.07 | −0.05 | −0.52 | −1.88 |
| 30–60s | +0.58 | +0.07 | −0.09 | −0.97 | −1.58 |
| **<30s** | +0.82 | −0.44 | **−1.62** | **−1.93** | **−1.70** |

(cents, positive = the maker wins; t reaches +9.8 early and −5.5 late)

**Early in the event the crossing flow is benign and the passive side is paid.
In the final ~2 minutes it turns toxic and runs the maker over.**

The mechanism is coherent with Phase 2 §3, and the two findings explain each
other. Late in an event, informed traders know the outcome with rising certainty
and cross to buy the favourite — which Phase 2 measured as **underpriced by
~0.8c in exactly that window**. Those takers have real edge (it is just eaten by
the spread), and the counterparty being picked off is the maker. Early, the
favourite is *over*priced and flow is noise, so the maker collects.

### Implied strategy skeleton

> Quote both sides mid-event (ttr ≈ 120–300s); **withdraw entirely inside the
> last ~2 minutes.**

Expected on early quotes: half-spread ≈ 0.66c + positive markout ≈ 0.5c ≈
**1.1c/share**, against a 5-minute event that runs ~12 times an hour per asset,
across 10 series.

## What this is not

- **n = 35 events.** Everything above is one 40-minute window. The 15m series
  shows negative markouts throughout, but on n=822 prints.
- **Front-of-queue is assumed.** Every print is treated as fillable by us at the
  touch. Real queue position will cut the fill rate, and will cut it *hardest*
  on the benign flow (passive queues are longest when nobody is in a hurry).
- **Markout ≠ realised PnL.** It measures the mid moving in our favour, not a
  round trip actually closed.

## Next

1. Accumulate tape (recorder running as `polyalpha-tape.service`), re-run
   `research/phase3/maker_markout.py`. Watch whether the TTR structure holds.
2. Phase 1 of `MAKER_SCOPE.md`: passive-fill simulator with a queue model, so
   markout becomes simulated PnL.
3. Cross-asset: 5 assets × 2 horizons on shared settlement boundaries — still
   completely untouched.

---

# Phase 1 — the simulator OVERTURNS the Phase 0 gate

Built `src/polyalpha/passive_sim.py` (queue-aware passive fills, 12 unit tests)
and ran three maker structures over 125 resolved events / 37,934 prints.

**All three lose money.** The Phase 0 markout gate above was too optimistic, and
this section supersedes its conclusion.

| structure | net / event | t | per share |
|---|---|---|---|
| two-sided quoting, ttr[120,300] | **−$26.42** | −4.22 | −4.31c |
| complement pair-maker (sell UP+DOWN) | **−$9.32** | −4.39 | −6.87c/pair |
| one-sided, sell the favourite | **−$16.12** | −1.29 | −5.94c |
| one-sided, sell the underdog, ttr[120,300] | +$2.43 | **0.21** | +1.00c |

A 24-cell parameter sweep (leg tolerance × min-edge × size) found positive cells
**only** at `min_edge ≥ 2.5c`, where the strategy barely trades (650 pairs vs
16,954), carries more naked than paired inventory (ratio 3.4), and is **not
significant** (best t = 0.81). Every configuration with real volume is negative
at t ≈ −4 to −6.

## Why Phase 0 was wrong

Markout measures the mid moving in your favour over 1–60s. It silently assumes
you can **flatten at the mid**. Three things break that:

1. **Flow is ~96% taker-BUY.** In a binary market users buy the side they want
   rather than selling the other, so resting *asks* are lifted constantly and
   resting *bids* almost never fill. The naive two-sided quoter sold 60,852
   shares against 15,835 bought — it is not a market maker, it is a forced seller.
2. **Inventory accumulates and is adverse.** Whichever side we end up net short,
   **that side wins 60–73% of the time** (n=50 / n=41 events). Cost: **−31.7c per
   naked share**. Takers buy the winner; the passive seller is on the other side.
3. **Flattening costs more than the markout.** Crossing to flatten pays the full
   spread (~1.1c) against a markout of ~0.68c. Flattening passively just
   reintroduces leg risk.

## The `ask_sum` structure, correctly understood

`ask_sum` sits at 1.0191 and is above 1.00 on **99.9%** of ticks. Selling both
legs pays exactly $1.00 at settlement, so a matched pair books +1.91c
outcome-independently. That looked like free money. It is not:

> **The +1.9c in `ask_sum` is precisely the market makers' compensation for leg
> risk.** Measured leg risk here (−31.7c per naked share) dominates it by an
> order of magnitude. The spread is that wide *because* the flow is informed.

This also explains why `IDEAS.md` #1 was right for the wrong reason: it killed
*buying* both legs below $1 (which never happens) without noticing the mirror
trade — which does exist, and still does not pay.

## Status

**Negative.** The obvious maker structures are closed on this tape. Caveat: 125
events ≈ 2 hours. The high-volume configurations are consistently negative at
t ≈ −4 to −6, which is more informative than a marginal positive would be, but
this should be re-run as tape accumulates.

Untested and still open:
- **Sub-second leg synchronisation.** Our book is 1 Hz; fills inside a second
  cannot be reacted to. Real leg risk may be materially lower with a faster loop.
- **Predictive quote skew** — widening the side about to be bought. This needs a
  short-horizon flow-direction signal, which is a genuine research problem rather
  than a parameter.
- **The cross-asset cross-section**, still completely untouched.
