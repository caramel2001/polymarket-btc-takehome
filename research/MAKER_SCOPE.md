# Maker-fills capability — scope & build plan

**Status:** scoping (2026-06-20). No code written yet.

## Why

Every taker-side avenue is exhausted (IDEAS.md): the market is efficient on
direction, vol regime, and even cross-asset (a *verified* ETH→BTC lead adds
nothing tradeable because the market already prices it), and 97% of recorded
ticks are unfillable. The ML work did, however, confirm a **real standalone
edge** (lag-1 ETH model: 5-min direction AUC 0.55, calibrated). The only way
that edge converts to PnL is to stop being a taker (pay the half-spread + fee
on a 1-tick delay) and become a **maker**: post resting limit orders, earn the
spread, supply the liquidity the book lacks.

## The one question that kills or greenlights this

**Adverse selection.** We empirically found (IDEAS.md "BATCH OF 6", strategy 5)
that *taking* offered liquidity is −4 to −14c/share — the flow is toxic: a
resting order exists precisely when an informed trader wants to hit it. A maker
sits on the **receiving** end of exactly that flow. A resting bid gets filled
when an informed seller dumps into it, right before the price drops.

So maker viability = **does the captured spread exceed the adverse-selection
("markout") cost?** This is measurable from data BEFORE building any order
machinery, and the plan front-loads it as a go/no-go gate. We do not build the
strategy stack until the economics are proven on real prints.

## Two gating unknowns to resolve first (cheap, external)

1. **Polymarket maker fee.** The harness models a symmetric taker fee
   `0.072·p·(1−p)` (`pnl.py _execute_leg`). Polymarket has historically run
   **zero trading fees**; if makers pay 0 (or get a rebate), maker economics
   improve dramatically vs. this assumption. Must verify the *current* real
   fee schedule for resting orders. **If makers pay the same `0.072·p·(1−p)`
   as takers, the spread-capture advantage largely evaporates — verify before
   investing.**
2. **Trade-print fidelity.** Confirm the CLOB `market` WS `last_trade_price` /
   trade messages carry **size** and ideally **aggressor side**. Fill-model
   fidelity depends on it (see Phase 0). The messages already arrive on the
   existing socket — we just don't parse them.

## Current architecture (integration points)

| Concern | File / symbol | Note |
|---|---|---|
| CLOB market WS | `harness.py:_stream_clob_books` (`CLOB_MARKET_WS_URL`) | subscribes `assets_ids` + `type:market`; trade msgs arrive here |
| WS message parse | `harness.py:_apply_clob_ws_payload` → `_books_from_clob_ws_message` | **only books kept; trade prints discarded** |
| Recorded row | `recorder.py:TickRow` | has book top + sizes; **no trade-print fields** |
| Fill model | `pnl.py:PaperSimulator._execute_leg` / `_target_shares` / `_reconcile` | **taker-only**: buys lift `ask·(1+slip)`, sells hit `bid·(1−slip)` |
| Strategy API | `model.py:Signal(side,size,confidence)` | target-position; **no limit price / post-only** |
| Replay | `replay.py` | 1-tick delay; settles pending at resolution row |

## Phased plan (go/no-go gates between phases)

### Phase 0 — Data capture + adverse-selection measurement  ⟵ THE KILL TEST
*Goal: answer "can a maker be profitable here at all" with data, no strategy.*
- **0a. Capture trade prints.** Parse `last_trade_price`/trade messages in
  `_apply_clob_ws_payload`; add `last_trade_price / last_trade_size /
  last_trade_side / trade_seq` to `TickRow` (`recorder.py`). One-feed change —
  no new connection. Backfill nothing; record forward.
- **0b. Run recorder** ~5-10 days to accumulate prints alongside books.
- **0c. Markout analysis (the gate).** For each trade print, assume we had a
  resting order at the touched price and compute the **markout**: mid move at
  +5s / +30s / settlement after our hypothetical fill. Aggregate:
  realized spread captured vs. markout loss vs. fee. Net maker EV per share.
- **GATE:** if markout-adjusted maker EV ≤ 0 (toxic flow dominates) → **STOP**,
  document as the final negative result. If clearly > 0 → proceed.
- *Effort: ~0.5 day code (0a) + passive recording wait + ~1 day analysis (0c).*

### Phase 1 — Passive-fill simulator
- Extend `PaperSimulator` with a resting-order model: post at price X, fill
  when trade prints cross X; model **queue position** (assume back-of-queue
  worst case first; refine if size/side available), partial fills, cancel/replace.
- Validate simulated fills + markouts against Phase-0 empirics.
- *Effort: ~2-3 days. Gate: sim markouts match measured within tolerance.*

### Phase 2 — Order-intent API + harness wiring
- Extend the strategy surface: `OrderIntent(side, size, limit_price, post_only,
  ttl)` (or extend `Signal`), backward-compatible (no limit ⇒ existing taker).
- Track open resting orders across ticks in the harness/replay loop; feed to
  the Phase-1 simulator; expose fills back to the model.
- *Effort: ~2-3 days.*

### Phase 3 — Maker strategy driven by the ML signal
- Two-sided quoting around the ML fair value (`gbm_eth_lag1` / `gbm_5min`);
  skew quotes by model confidence and inventory; cancel/repost on signal flip;
  delta/inventory limits. Backtest on the maker sim with train/test discipline.
- *Effort: ~3-5 days iteration.*

### Phase 4 — Live paper/shadow validation
- Run in the live harness in **paper mode** (compute intended quotes + fills,
  place NO real orders); compare simulated fills to actual subsequent prints to
  validate the queue/fill model in production before any capital.
- *Effort: ~2-3 days + observation.*

## Risks / open questions
- **Adverse selection (primary)** — gated in Phase 0; everything depends on it.
- **Maker fee** — gating unknown #1; verify before Phase 1.
- **Queue position is unobservable** from public data — must assume/model;
  worst-case back-of-queue keeps the sim honest (under-fills rather than over).
- **5-minute event horizon** — orders live only seconds-to-minutes; little time
  to earn spread before settlement risk. May favor quoting only mid-event, not
  near expiry.
- **Fill realism** — without aggressor side we approximate fills from book
  crossings; cruder, but Phase 0 markouts still bound the economics.
- **Live execution** (beyond this scope) — real order placement/cancel via CLOB
  REST/WS, auth, rate limits — only after Phase 4 validates the model.

## Recommended first step
Do **Phase 0 only**, then decide. It's ~1.5 days of work plus a passive recording
window, and it definitively answers whether maker economics clear the
adverse-selection bar on *this* market — before committing to the ~10-day
simulator+API+strategy build. Resolve the two gating unknowns (real maker fee,
trade-print fidelity) in parallel during the recording window.
```
Phase 0 (kill test) ──gate──> Phase 1 (sim) ─> Phase 2 (API) ─> Phase 3 (strategy) ─> Phase 4 (paper)
  ~1.5d + record           ~2-3d        ~2-3d         ~3-5d            ~2-3d
```
```
Total if greenlit end-to-end: ~2-3 weeks of engineering, front-loaded with a cheap kill test.
```
