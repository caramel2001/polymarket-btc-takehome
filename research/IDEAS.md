# Hypothesis log — Polymarket BTC 5m strategy

Format per entry: **Hypothesis** (the mechanism, stated so it's falsifiable) →
**Test** (what variant / fixture run checks it) → **Verdict** (validated /
rejected / partial, with the aggregate numbers that justify it) →
**Follow-ups** (new hypotheses the result suggested).

Status tags: `[ ]` open / untested · `[~]` in progress · `[x]` validated ·
`[-]` rejected · `[±]` partial / regime-dependent.

---

## Resolved during initial debugging (context for new ideas — don't re-test)

- `[x]` **A strategy must emit a continuous desired-position signal, not a
  one-shot entry.** Fills land on ~3% of ticks; if you only "signal when you
  decide to enter" you'll usually decide on a tick with no depth and never
  get filled. *Evidence*: switching `model_submission.py` from gated one-shot
  signals to continuous direction-with-confidence signals took it from 0
  trades on most fixtures to consistent fills.

- `[x]` **All-3-horizons-must-agree momentum gating misses the only fillable
  tick.** Traced event 561014 in `live_1h.parquet`: depth existed at tick 31
  (both UP and DOWN ask size > 0). At tick 30 the 3 horizons were
  r5=−0.05 / r15=+0.02 / r30=−0.13 — a clear 2-of-3 DOWN majority, but the
  "all must agree" rule blocked it. The first all-agree tick was 32, one tick
  *after* the only depth window of the event. Switched to majority-vote
  (`sum(votes) > 0`); event 561014 now fills at tick 31.
  *Caveat*: this is inherently regime-dependent — see the open hypothesis
  below about whether majority-vote helps or hurts in choppy tape.

- `[x]` **Entry price must reference the side actually being bought.** UP
  fills at `up_ask`, DOWN fills at `down_ask` — using `up_bid` as the
  reference for a DOWN position inflates/deflates the PnL fraction by a
  factor of `down_ask/up_bid` (≈2x in observed cases), causing spurious
  stop-outs on noise.

- `[x]` **Stop-loss must be deferred several ticks past signal emission.**
  ~97% of ticks have no fill; checking PnL immediately after emitting a
  signal measures the *unfilled* mark-to-market against a reference price
  that was never actually paid, triggering exits before a position even
  exists. Deferring the check until `_ticks_in_pos > 5` fixed spurious
  lockouts.

- `[x]` **Reversal-exit-on-short-horizon-flip is too trigger-happy in
  trending-but-noisy tape.** A `REVERSAL_THRESH = 0.008` (0.8%/10s) fired on
  a brief 2% bounce inside a 12%/30s trend move (event 561004), exiting a
  correct position and (with one-entry-per-event) locking out the rest of
  the event including its only later depth window. Removed entirely; only
  stop-loss and resolution-hold exits remain.

---

## Critical scoring-mechanics finding (changes strategy priorities — read first)

- `[x]` **"Smooth losing" is the single biggest score destroyer; simply not
  trading beats it.** Ran the full 29-fixture battery on v1
  (`20260607T160839Z_v1_baseline.json`): mean model PnL is **−$34** vs
  baseline's **−$316** — we lose 9x less in raw dollars — yet our mean
  *score* (704) is far below baseline's (2,779). Why: `primary_score = PnL ×
  max(Sharpe, 0) × (1 − max_drawdown)`, and `Sharpe` is floored at 0. Splitting
  the 29 fixtures by outcome type:
  | type | n | mean score |
  |---|---|---|
  | smooth loser (PnL<0, Sharpe>0 — consistent small losses) | 7 | **−2,955** |
  | noisy loser (PnL<0, Sharpe=0 — floored) | 5 | 0 |
  | zero-trade | 2 | 0 |
  | winner (PnL>0) | 15 | +2,740 |

  A "smooth loser" — a strategy that enters often, gets stopped out at a
  similar small % each time, producing a steadily-declining low-variance
  equity curve — gets a *strongly positive* Sharpe multiplied against
  negative PnL, yielding a deeply negative score. Meanwhile baseline often
  scores exactly **0.0** even on tapes where it loses $1,600+, because its
  noisy equity curve has negative raw Sharpe (floored to 0 → score 0).
  **Implication: a circuit breaker that detects "I'm bleeding slowly and
  consistently" and halts trading (→ score floors at 0) is worth more than
  any amount of entry-signal tuning.** Converting those 7 smooth-loser
  fixtures (avg −2,955) to zero-trade (0) alone would roughly double our
  aggregate mean score (704 → ~1,400); converting them to winner-like
  (+2,740) would put us ahead of baseline's 2,779.

- `[ ]` **`_equity` / `_peak_equity` / `_drawdown_factor()` in v1 are dead
  code.** They're initialized to 1000 in `on_start` but never updated
  anywhere in `on_tick` — the harness doesn't feed equity back to the model
  via any callback (`on_finish` only fires once, at the very end of the
  whole run, with the final `RunResult`). So `_drawdown_factor()` always
  computes `dd = 0` and returns `1.0`; the "reduce size after losses" logic
  in v1 never actually fires. Any loss-streak detection has to be
  self-tracked from realized exit outcomes (stop-loss triggers, and inferred
  resolution outcomes from `up_mid` converging to ~0/~1 near `ttr → 0`).

---

## Open hypotheses

- `[-]` **REJECTED: a reactive circuit breaker on consecutive losing exits
  nets *negative* — it can't tell "bad regime" from "rough patch".**
  *Test*: `v2_circuit_breaker.py` (`20260607T162839Z_v2_circuit_breaker.json`)
  — added a halt state machine (2 consecutive losing exits → stop entries for
  3 events; tracked deterministic stop-loss exits + *inferred* resolution
  outcomes from `up_mid` converging to ≈0/≈1 near `ttr→0`) on top of the
  unchanged v1 entry/exit core. Full 29-fixture battery result: mean score
  **204** (down from v1's 704), total PnL **−$1,615** (worse than v1's
  −$975), win-rate 31.0% (down from 41.4%).
  *Why it failed*: fixture-by-fixture diff
  (v1 `20260607T160839Z` vs v2 `20260607T162839Z`) shows it helped 7
  fixtures (mostly converting deep-negative smooth-losers like −9,215 and
  −5,238 toward 0 — the mechanism *does* work when triggered on a true bad
  regime) but actively hurt 11, including gutting several of the best
  winners (+7,027→−2, +8,909→4,138, +3,634→629, +6,969-equivalent→0). A
  2-loss streak is just as likely to precede a recovery as a continued bleed
  — the halt has no way to know which, and a 3-event cooldown forfeits real
  upside whenever it guesses wrong. Net: −14,508 aggregate score delta.
  *Takeaway*: **don't gate on our own trade outcomes** (that's reactive and
  path-dependent — by the time you've lost enough to detect a pattern,
  you've already lost it, AND you poison the trade sequence for the rest of
  the tape). A regime filter needs to be **predictive**, built from
  market-observable features *before* entering, not from post-hoc trade
  results. See the new hypothesis below.

- `[x]` **Tape-level market features (return autocorrelation, short/long
  momentum agreement) do NOT separate winner from smooth-loser fixtures —
  the difference is much more likely small-sample luck than detectable
  regime.** Computed both features per-fixture across the 7 smooth-loser +
  15 winner fixtures from the v1 battery: mean autocorrelation 0.0586 vs
  0.0592, mean momentum-agreement 0.696 vs 0.700 — statistically
  indistinguishable. *However*, `hit_rate` (fraction of the 13 events/tape
  closed with positive total PnL) DOES differ: smooth losers average 8.8%,
  winners average 16.0% — roughly 2x. But this is an *outcome* metric
  computed post-hoc over only ~5-18 trades per tape; at that sample size,
  getting 1-2 vs 3-4 winning events by chance is well within binomial noise
  for a coin-flip-ish true win rate. It tells us "when our few calls land
  right, we make money" (tautological) but gives no way to predict, before
  the fact, which tapes will be which. **Conclusion: a market-observable
  predictive regime filter is probably not the right lever here** — the
  result variance across fixtures looks dominated by which of our (few,
  thin-CLOB-constrained) fills happened to land in the right direction, not
  by some detectable "this regime suits momentum" property of the tape.

- `[-]` **Strip out intra-event complexity entirely — test whether the raw
  directional call has positive expectancy once isolated from stop-loss /
  hold-conditional noise.** Given the above, the highest-leverage open
  question isn't "how do I detect bad regimes" but "is the core entry signal
  even right more often than wrong, on average, across enough independent
  bets for the law of large numbers to matter". The current strategy's
  intra-event exit machinery (deferred stop-loss, conditional resolution
  hold) adds path-dependent noise on top of the directional call, making it
  hard to isolate whether the *signal* or the *exit logic* is the source of
  the smooth-loser pattern. *Test*: `v3_pure_directional.py` — enter once
  per event on the majority-vote signal (unchanged, proven to fill), THEN
  hold unconditionally to settlement (no stop-loss, no early exit, no
  conditional-hold check) — strips the strategy down to "is my directional
  call right at settlement, yes or no". If this aggregate beats v1 (704)
  materially, the exit logic is net-harmful noise and should be simplified
  away; if it's much worse, the exit logic is doing real work and the
  problem is upstream in the signal itself.

  **RESULT — `[-]` REJECTED, but highly informative**: ran the full battery
  (`20260607T164526Z_v3_pure_directional.json`, 30 fixtures — one new
  recording appeared mid-session). The headline mean score *looks*
  spectacular (11,621 vs v1's 704), but the distribution shows it's an
  illusion:
  | metric | v3 (no exits) | v1 (with exits) |
  |---|---|---|
  | mean score | 11,621 | 704 |
  | median score | **0** | 212 |
  | score std | **35,134** | 3,408 |
  | mean PnL | −$223 | −$34 |
  | median PnL | **−$346** | (small loss) |
  | fixtures PnL-positive | **9 / 30** | majority |
  | mean trades/fixture | **58.9** | 27.0 |

  The mean is dragged entirely by 2 extreme outlier wins (+$163,691 and
  +$89,020) against 2 extreme losses (−$11,798, −$11,016); 15/30 fixtures
  scored within ±100 of zero. *Why*: removing the exits doesn't just "remove
  noise" — it changes the position dynamics. With a continuously-active
  signal and nothing to close the position, the harness keeps adding to it
  on every subsequent fill for the rest of the event (trades jumped from
  v1's ~27 to ~59/fixture), so the eventual settlement payoff lands on a
  much larger, path-accumulated position. That **amplifies whatever edge
  (or lack of it) the raw signal has** into a high-variance bet — closer to
  gambling than to "v1 but smoother". On a different battery draw this same
  mechanism could just as easily produce 2 huge losses and a deeply negative
  mean.

  **Conclusion: v1's exit machinery (deferred stop-loss, conditional
  resolution-hold) is doing real, valuable variance-containment — it is NOT
  the source of the smooth-loser problem, and should NOT be stripped or
  loosened.** The bottleneck is upstream: the directional signal's edge is
  too weak/noisy for an unconditional let-it-ride to pay off reliably.
  *Follow-up*: stop iterating on exit logic; go after signal quality itself
  (see new hypotheses below — mean-reversion and imbalance-as-primary).

- `[~]` *(superseded by the negative tape-feature finding above — low priority)* **A predictive (not reactive) regime filter, built from
  market-observable features, could separate "momentum works here" tapes
  from "momentum bleeds here" tapes.** The rejected circuit breaker proved
  the *mechanism* works when it fires on a genuinely bad regime (several
  deep-negative fixtures did improve toward 0) — the problem was *timing*:
  it can only react after losses have already accumulated, and can't
  distinguish a true bad-regime from a temporary rough patch. A filter that
  instead looks at **recent market structure** (computable every tick, no
  lookahead, no dependency on our own P&L) could gate entries *before* the
  damage: e.g., trend-persistence / autocorrelation of `up_mid` returns
  (momentum-friendly markets show positive autocorrelation; choppy
  mean-reverting ones show negative), or the fraction of recent ticks where
  short- and long-horizon momentum *agree* (a proxy for "is there a real
  trend or just noise"). *Test*: compute these features across the v1
  battery's 7 smooth-loser vs. 15 winner fixtures and check whether any
  cleanly separates the two groups *before* knowing the outcome — only build
  the filter into a strategy if the separation is real, not a story.

- `[-]` **REJECTED: mean-reversion is strictly worse than momentum, on every
  metric — short-horizon `up_mid` bursts do NOT predictably revert.**
  *Test*: `v4_mean_reversion.py`
  (`20260607T172154Z_v4_mean_reversion.json`, full 30-fixture battery) —
  kept v1's exact exit machinery (proven protective by the v3 result above)
  and replaced ONLY the entry: trigger on a "burst" (`|r5|` exceeding
  `1.6 ×` the tape's own 30s stdev, floored at 0.004) and bet on reversion
  (`direction = -sign(r5)`), with the same imbalance veto. Result, vs v1
  head-to-head on the same battery:
  | metric | v4 (reversion) | v1 (momentum) |
  |---|---|---|
  | mean score | 291.7 | 704.0 |
  | median score | **0.0** | 211.9 |
  | win-rate vs baseline | 40.0% | 41.4% |
  | mean PnL | −$88.16 | −$33.62 |
  | total PnL | −$2,644.84 | −$974.98 |

  Worse on every single axis — not a close call. *Why this matters*:
  this isn't just "reversion loses" — it's a clean **falsification of the
  premise that short-horizon `up_mid` moves carry exploitable directional
  information at all**, in either sense. If reversion had even matched
  momentum we could conclude "no info, pick either"; instead it's
  *materially worse*, meaning momentum's modest edge (v1's mean score 704,
  ~9x lower $ losses than baseline) is real, even if weak — bursts tend to
  *persist* slightly more often than they revert, consistent with these
  markets reflecting incremental new information (CLOB depth arriving in
  small bursts as traders react to live BTC price ticks) rather than pure
  noise-driven overreaction. **Conclusion: keep the momentum-based entry —
  it is the better of the two directional hypotheses tested. Don't pursue
  reversion-based entries further** (including burst-detection variants with
  different thresholds — the sign of the effect, not just its magnitude, is
  wrong). The remaining lever is refining *which* momentum signals to trust
  (e.g. weighting by order-book imbalance as a primary confirmation, not
  just a veto — see hypothesis below) or accepting that v1's ~704 mean score
  is close to the ceiling for a pure-direction strategy on this tape, and
  the bigger win is in *position sizing / when to size up* on higher-
  conviction calls rather than in the directional call itself.

- `[ ]` **Order-book imbalance could be a primary signal, not just a veto.**
  Currently `imb = (up_mid - down_mid) / (up_mid + down_mid)` only *vetoes*
  momentum calls that strongly disagree with it (`IMB_VETO = -0.04`). But
  imbalance is a *forward-looking*, market-aggregated read on where
  participants think the price is headed — momentum is backward-looking and
  reactive. *Idea*: weight entries by `imb` magnitude directly (strong
  one-sided imbalance = high-conviction directional bet), independent of or
  in addition to momentum agreement — e.g. require imbalance to *confirm*
  (not just "not strongly veto") before sizing up, or use imbalance alone
  as a secondary signal source on ticks where momentum is silent (below
  `R5_MIN`/`R15_MIN`/`R30_MIN` floors). *Now the most promising open lever*
  for entry-signal quality, since the v4 rejection confirmed momentum (not
  reversion) is the right directional bet — this would refine *which*
  momentum calls to trust / size up, rather than replace the bet itself.
  This is a natural candidate for v5.

  **RESULT — `[x]` VALIDATED — first variant to robustly beat v1, PROMOTED.**
  *Test*: `v5_imbalance_confirm.py`
  (`20260607T173413Z_v5_imbalance_confirm.json`, full 30-fixture battery) —
  kept v1's exact exit machinery and momentum-primary entry, but (a) raised
  the confirmation bonus when imbalance agrees strongly with momentum
  (`IMB_CONFIRM_MULT = 1.35` vs v1's ad-hoc `1.15`, gated on `imb > 0.05`
  rather than `> 0.01`), and (b) added an independent "imbalance-alone"
  entry path for ticks where momentum fails its noise floor but the book
  shows an unambiguous one-sided lean (`|imb| >= 0.12`), sized
  conservatively (`<= 65%` of normal max). Head-to-head vs v1:
  | metric | v5 (imbalance-aware) | v1 (momentum-only) |
  |---|---|---|
  | mean score | **1,145.7** | 704.0 |
  | median score | 105.9 | 211.9 |
  | score std | **3,131** (tighter!) | 3,408 |
  | win-rate vs baseline | **43.3%** | 41.4% |
  | mean PnL | **−$19.75** | −$33.62 |
  | total PnL | **−$592** | −$975 |
  | PnL-positive fixtures | 15/30 | 15/29 |
  | min / max score | −7,890 / +8,909 | −9,215 / +8,909 |

  *Why this is credible (not another v3-style mirage)*: unlike v3's
  "improvement" (driven entirely by 2 outlier wins against a 10x-wider
  distribution), v5's score **std is actually lower** than v1's — the gain
  comes from compressing the worst losses (min improved from −9,215 to
  −7,890) while keeping the best wins intact (max identical, +8,909 — same
  fixture, same trade pattern: momentum fired cleanly there and imbalance
  didn't need to intervene). Trade count is essentially unchanged (26.1 vs
  27.0/fixture) — this is *better-targeted* trading, not *more* trading.
  *Why it works*: imbalance is a forward-looking, market-aggregated read
  (the live book's collective lean) genuinely distinct from momentum's
  backward-looking one — letting them confirm each other (bigger size when
  both agree) and occasionally substitute for each other (trade on a loud,
  unambiguous book lean even when price action is quiet) squeezes a bit more
  signal out of the same underlying data, without the variance explosion
  that came from "trade more" (v3) or the wrong-signed bet (v4, reversion).
  **Promoted to `model_submission.py` — see Promotions log.**

- `[~]` **Majority-vote (2-of-3) momentum entry is a net win across regimes.**
  *Why this matters*: it was validated on ONE event in ONE quiet fixture
  (561014 / `live_1h.parquet`, +$94 net, 4 trades, score 3,049 vs baseline
  4,767 — better than the 0-trade all-agree version, but still below
  baseline). A quick 4-fixture battery run on the volatile recordings showed
  mixed results: BEAT on 1/4, lost badly on 2/4 (including one −$435 tape).
  *Test*: run `eval_battery.py` with no `--limit` across all ~25 fixtures,
  compare win-rate and mean score against a saved all-agree variant.
  *Open question*: does majority-vote's extra fill-rate in quiet tape
  outweigh its extra false-signal rate in choppy tape? Might need a
  *regime-adaptive* rule — majority-vote only when recent volatility is low,
  all-agree (or no entry) when it's high.

- `[-]` **REJECTED — decisively: cross-event continuation is not just absent,
  it's actively anti-predictive, and landing in the huge tick-0-5 depth
  cluster turns that into large, fully-filled, mostly-wrong bets.**
  *Hypothesis (as posed)*: the single biggest depth cluster in every event
  is ticks 0–5 (e.g. event 561014 tick 1: `up_ask_size=57,
  down_ask_size=1059`) but the in-event momentum signal can't compute there
  (`up_mid_recent` resets on rollover, needs ~31 samples + `WARMUP_TICKS`).
  *Idea*: persist the prior event's net `up_mid` drift and inferred
  resolution side; if both agree (a "clean" prior event), bet continuation
  during the current event's first ~10 ticks — exactly where the unreachable
  depth sits — sized conservatively (≤45% of max).
  *Test*: `v6_cross_event_momentum.py`
  (`20260608T150930Z_v6_cross_event_momentum.json`, full 52-fixture battery
  — fixture count grew from 30 to 52 as more recordings landed). Result vs
  the promoted baseline (model_submission.py / v5, which scored mean 1,146 /
  median 106 / win-rate 43.3% on its own 30-fixture run):
  | metric | v6 (cross-event prior) |
  |---|---|
  | mean score | **−1,253.7** |
  | median score | **−1,179.6** |
  | win-rate vs `MomentumBaseline` | **15.4%** (8/52 — collapsed from ~43%) |
  | mean PnL | −$347.64 |
  | total PnL | −$18,077 |
  | mean trades/fixture | **58.0** (vs v5's ~26) |

  *Why it failed so badly*: traced `_open()` calls directly — the
  cross-event signal fired on **nearly every single event** (12/13 in the
  first fixture alone, vs the in-event signal's ~5/13), because
  `PREV_DRIFT_MIN = 0.03` is a low bar that almost any event clears, and
  "drift direction agrees with resolution side" is true often enough by
  construction (a trending event usually resolves the way it trended) to
  pass the filter most of the time. So instead of a rare, high-conviction
  early entry, this became "bet on (recent-trend) continuation nearly every
  event, at meaningful size, landing in the biggest depth cluster so the bet
  fills almost completely" — full exposure to a signal that, it turns out,
  has *negative* edge: BTC's 5-minute windows are close enough to
  independent that "last window trended up" is no better than a coin flip
  for "this window will trend up", and the massive early fills just make
  being wrong much more expensive (mean trades 58 vs v5's 26 — more than
  double the fee bleed too). **Conclusion: don't try to bridge event
  boundaries with directional priors — treat each 5-minute question as
  independent, exactly as the market design intends.** This also reinforces
  the broader finding that "more trades / bigger fills" is the wrong lever
  (see the `v3_pure_directional` rejection) — the edge here is in *signal
  quality on the calls you do make*, not in *making more of them*.

- `[ ]` **Near-resolution entries (TTR 25–65s) sit on the second-largest
  depth cluster but are currently blocked by `RESOLUTION_HOLD_TTR = 50`.**
  At TTR ≈ 25–65s, depth balloons to thousands–tens-of-thousands of shares
  (e.g. event 561014 tick 274, TTR=25: `up_ask_size = down_ask_size = 36,741`)
  — but it's frequently *one-sided* (often only the UP side has ask depth).
  Naive EV math suggests resolution-time entries are roughly a wash if you
  just follow the market-implied probability (you're paying close to fair
  value for the edge you're capturing) — any edge has to come from the
  signal being *better* than the current price, not from the proximity to
  settlement itself. *Test*: a variant that allows entry down to TTR ≈ 30s
  IF the directional signal disagrees with (or strongly leads) the current
  market-implied probability, sized small. Compare aggregate score, watch
  max-drawdown — late entries are higher-variance per trade.

- `[ ]` **One-entry-per-event may be leaving real money on the table in
  strongly-trending events.** Event 561116 (quiet fixture) has 13 separate
  depth windows; `MomentumBaseline` (which re-enters freely) trades it ~13x,
  we trade it once. If the event trend is consistent, multiple same-direction
  entries compound the resolution payoff; if it's choppy, they compound fee
  bleed (this is *why* one-entry-per-event was added — see resolved section).
  *Idea*: relax to "one entry per *direction* per event" — allow re-entry
  only when the new signal agrees with the original direction (compounding a
  correct call) but never allow a direction-flip re-entry (which is what
  caused the 33-trade fee-bleed spiral on the volatile fixture).

- `[ ]` **Volatility-based entry gating doesn't cleanly separate "quiet" from
  "volatile" fixtures.** Measured 30s-stdev of `up_mid` across all three
  early fixtures: median ≈ 0.037–0.045, p75 ≈ 0.060–0.076 — nearly
  identical distributions for the "quiet" and "volatile" tapes. A flat
  `VOL_MAX` threshold can't distinguish them. *Idea*: the real distinguishing
  signal is probably *event-level directionality* (does the event trend
  monotonically vs. whipsaw?), not point-in-time volatility — e.g. the
  fraction of ticks where short-horizon momentum agrees with the 30s trend,
  measured over the last 1–2 events, might be a better regime classifier
  than instantaneous stdev.

- `[x]` **VALIDATED & PROMOTED — `FEE_MID_MULT` should be ~0 (skip mid-zone
  entries entirely); `FEE_OPT_MULT` is dead weight at any tested value.**
  *Test*: grid-swept both constants across the FULL battery (54 fixtures —
  the live-recording set keeps growing; was 29→30→52→53→54 across this
  session, so always re-baseline rather than compare across saved JSONs from
  different days) by patching the loaded module's globals directly:
  - `FEE_OPT_MULT ∈ {1.0, 1.25, 1.5, 1.75}` → **bit-for-bit identical
    aggregate at every value**. *Why*: entries that land in the fee-optimal
    zone (p<0.35 or p>0.65) correlate with strong momentum / high `mag`
    scores (price has moved away from 0.5 *because* of a real directional
    move), so `size` is already saturated at `MAX_SIZE` before the
    multiplier is applied — `min(MAX_SIZE, size * anything >= 1.0)` always
    clips to the same value. The multiplier has no headroom to matter. Left
    at its current value (1.25) since changing it provably does nothing.
  - `FEE_MID_MULT ∈ {0.0, 0.05, 0.10, 0.15, 0.20, 0.35, 0.50, 0.65, 0.80,
    1.00}` → **clean, near-monotonic "lower is better"**, with `0.0-0.10`
    (functionally identical — they all push sized-down mid-zone entries
    below the `0.05` minimum, i.e. skip them) winning outright:
    | `mid_mult` | mean score | median | std | win-rate | mean PnL | total PnL |
    |---|---|---|---|---|---|---|
    | **0.0–0.10** | **475.4** | **60.1** | **2,705** | **50.0%** | **−$21.52** | **−$1,162** |
    | 0.20 | 459.1 | 131.6 | 2,703 | 50.0% | −$24.71 | −$1,334 |
    | 0.50 | 443.5 | 7.0 | 3,290 | 42.6% | −$41.15 | −$2,222 |
    | 0.80 *(prior value)* | 326.8 | 0.0 | 3,917 | 43.4% | −$53.31 | −$2,825 |
    | 1.00 | 272.8 | 0.0 | — | 43.4% | −$58.65 | −$3,108 |

  *Why*: fees follow `shares × 0.072 × p × (1−p)`, which **peaks exactly at
  p=0.50** — the mid-zone is where every trade costs the most in fees per
  dollar of exposure, for no compensating edge advantage (the directional
  signal doesn't get *better* near p=0.50; if anything it's noisier there,
  closer to a coin-flip price). Sizing DOWN in that zone helps; sizing to
  ZERO (i.e. not trading there at all) helps more, cleanly, on every single
  metric — including a **lower std** (2,705 vs 3,917), so this is a broad
  derisking, not a lucky truncation. **Promoted**: changed
  `FEE_MID_MULT = 0.80 → 0.0` in `model_submission.py`
  (snapshot: `research/strategies/v7_fee_zone_tuned.py`,
  `20260608T165652Z_v7_fee_zone_sweep.json`). This is the single largest,
  cleanest, lowest-risk win of the whole iteration arc — a one-line constant
  change beat every structural variant tried (v2-v6) by a wide margin, on
  every metric, with *less* variance. *Generalizable lesson*: before
  redesigning signals, always check whether a parameter is silently dead
  (like `FEE_OPT_MULT`) or simply mis-set relative to a known cost structure
  (like `FEE_MID_MULT` vs the `p(1-p)` fee curve) — those are far cheaper to
  fix and validate than new hypotheses about market structure.

- `[±]` **MECHANISM VALIDATED, PRACTICALLY BLOCKED — Near-resolution
  confirmation entries (TTR ≤ 22s, price 90-95c, full-event drift ≥ 0.20)
  have 96.8% theoretical win rate but can't be profitably backtested without
  a Binance price feed.** (`v8_near_resolution_confirm.py`,
  `20260609T150308Z_v8_near_resolution.json`, 76-fixture battery)

  **Hypothesis (user's live strategy, reported 98% win rate over 2 months)**:
  With ~15s left, if one side is priced at 90-95¢ AND BTC has moved >0.06%
  in that direction since the market opened, buy the favoured side. The
  5-10¢ discount to the near-certain $1 payout is exploitable because:
  (a) the time remaining is too short for a reversal of the already-realised
  BTC move, and (b) the market hasn't yet fully repriced to 99¢.

  **Data limitation**: `btc_last`, `btc_bid`, `btc_ask`, `btc_recent` are
  ALL zero in every recorded fixture — the recordings only store Polymarket's
  own price oracle (`btc_source = 'polymarket'`), not the Binance feed the
  original strategy relies on. Substitute: `up_mid_recent[0]` (the market's
  own price at event-open) as a proxy. A 0.20+ drift (e.g. 50¢ → 92¢ over 5
  min) reflects the same sustained BTC directional move. Validated at the
  event level: 222 qualifying events across all 71 files → **96.8% correct
  (215/222)**, directly matching the user's reported 98%.

  **Backtest result** (proxy signal, fill-timing uncontrolled):
  | metric | v8 (76 fx) | v7 baseline (76 fx) |
  |---|---|---|
  | mean score | 94.5 | 1,148.0 |
  | median score | 0.0 | — |
  | win-rate vs baseline | 42.1% | 50.0% |
  | mean PnL/fixture | −$7.32 | −$683.61 |
  | trades/fixture | 0.7 | — |
  | fixtures with 0 trades | 41/76 | — |

  On the **35 fixtures that actually got fills**:
  - Win rate 80% (28/35) — vs 96.8% theoretical
  - Wins: +$358 total ($12.8/fill avg); losses: −$914 total (−$131/fill avg)
  - 74.3% beat-baseline rate — strong when it trades, but infrequent

  **Why it fails in backtesting despite being valid in theory**:
  1. **Fill timing uncontrolled**: we signal for every tick in TTR [6,22] and
     get filled whenever recorded depth appears — sometimes at TTR=20s, where
     18 seconds of remaining time is enough for occasional reversals.
     The user's live strategy fills at a precise "15 seconds before" moment;
     our backtest can't enforce that.
  2. **Payoff asymmetry requires ≥91% win rate to break even**: buying at
     90-95¢ wins +5-10¢ on correct calls and loses −90-95¢ on wrong ones — a
     ~10:1 loss-to-win magnitude ratio. At the user's 98% live rate,
     EV ≈ +$3/fill. At our backtested 80%, EV ≈ −$16/fill. The 18pp gap is
     entirely explained by fill timing (early fills at TTR≈20s vs intended
     TTR≈15s in live trading).
  3. **Proxy signal adds noise**: `up_mid_recent[0]` drift is correlated with
     the Binance price move but is not the same signal — the Polymarket oracle
     may lag, overshoot, or be driven by book imbalance rather than spot price.

  **Why the mechanism itself IS sound**: the event-level 96.8% win rate is
  robust and independently discovered from our recorded data. Every theoretical
  argument holds: at TTR<15s, BTC would need an instantaneous reversal of the
  entire 5-minute move to change the outcome — very unlikely if the move was
  sustained (which the drift condition confirms).

  **Follow-up if Binance data becomes available**: re-run with `btc_last`
  populated + narrow entry window to TTR [6, 16] only, matching the user's
  "15 seconds before" spec. Expected to recover close to the 96.8% win rate
  in backtest.  `EXCLUDED_HOURS` scaffold is in `v8_near_resolution_confirm.py`
  — user mentioned filtering specific hours; add once known.

  **UPDATE 2026-06-10**: `scripts/record_continuous.py` switched to
  `price_source="binance"` and restarted — `btc_last/btc_bid/btc_ask` now
  populate with real Binance prices (`btc_source='binance'`) starting from
  fixture `20260609_152746_...`. Built `v9_near_resolution_btc_confirm.py`
  implementing the ORIGINAL spec exactly (real `tick.btc_last`,
  `BTC_MOVE_MIN=0.0006`, `ENTRY_TTR_HI=16` matching "~15s before expiry").
  **Status: data-starved, not yet evaluable.** Only ~13 hours of
  Binance-backed fixtures exist so far (`20260609T...` through
  `20260610T043501`); the BTC_MOVE_MIN=0.0006 condition fired exactly ONCE
  in that window (fixture `20260609_203045_20260609_213120`, score 94.7,
  PnL +$4.25 — a win, directionally consistent with the hypothesis but n=1).
  Re-run `research/eval_battery.py --model-file
  research/strategies/v9_near_resolution_btc_confirm.py` once several days
  of Binance-backed fixtures have accumulated (need ~50-100+ hours for a
  meaningful fill count, based on ~1 fill/13hr observed rate).

---

## [✗] DEFINITIVE: no capturable directional edge — market is efficient to execution resolution (2026-06-18)

Settles the v10 question with 195 Binance-backed fixtures (~9 days, 2,337
complete events — 5× the 2026-06-11 sample). Four independent tests, all
pointing the same way:

1. **Calibration / incremental-info test** (67,083 sampled ticks, temporal
   50/50 split). Out-of-sample log-loss predicting the UP outcome:
   - raw market mid: **0.4389** (best)
   - logistic recal of mid: 0.4396
   - mid + BTC z: 0.4405 (WORSE than mid alone; z coef shrinks 1.67→0.25)
   - BTC z alone: 0.4769
   The market mid is a better outcome predictor than any model, and the
   Binance feed adds **negative** incremental value OOS. The market fully
   prices in BTC. v10's apparent edge was overfitting, full stop.

2. **Favorite mispricing doesn't survive execution.** Calibration shows
   favorites at mid 0.90–0.95 beat their mid by +1 to +3.3% (strongest near
   expiry). But buying at ASK + 1-tick latency + 50bps slip + fee, EVERY
   window is net-negative per share: p[0.90,0.95] ttr[5,15] = −0.1c (tightest),
   degrading to −2.8c for wider/earlier windows. The +1-3% mid-edge is exactly
   eaten by half-spread + latency.

3. **Lead-lag = execution latency.** corr(btc_ret[t], mid_change[t+k]):
   k=0 +0.43, k=+1 **+0.38**, k=+2 +0.06. The market lags BTC by ~1 tick — but
   replay fills at t+1, precisely when the catch-up move lands. The lag and the
   latency are the same length (~1s), so the lag is uncapturable. This IS the
   +4bps-worse-fill phenomenon, mechanistically explained.

4. **Filtered favorite, train/test.** Added spread<0.015 + BTC-direction
   agreement + ask<0.95 + ttr<12 (tightest, highest-conviction). Best train
   config +2.1c/share (96.2% win) → **−0.7c test** (93.2% win). Every
   in-sample-positive config flips negative OOS. Textbook overfit.

**Conclusion for future sessions: stop building directional / fair-value /
favorite-fade strategies for BTC 5m.** Under taker-only fills with 1-tick
latency and the `0.072·p·(1−p)` fee, this market has no capturable directional
alpha. Profit, if achievable at all in this harness, must come from a
fundamentally different source — maker/spread capture (NOT simulated here —
execution is taker-only), or scoring-metric optimization (minimize loss +
maximize Sharpe vs the badly-losing MomentumBaseline, which is what the
promoted `model_submission.py` already does: −$1.3k total vs baseline −$37.2k
on the 195-fixture set). The realistic bar is "lose least / best Sharpe," not
absolute profit. Battery results live in
`research/results/20260618T142232Z_v10_195fix.json` (v10) and
`20260618T142503Z_model_submission_195fix.json` (promoted).

## [±] BTC fair-value model with Kelly sizing — edge is real but mostly a latency race (2026-06-11)

**Hypothesis**: with the real Binance feed, the market's `up_mid` systematically
LAGS the BTC price. Fair value: `z = ln(btc/btc_open)/(σ₁ₛ·√ttr)`,
`p_fair = sigmoid(0.029 + 1.474·z)` (logistic calibrated on first half of the
38-fixture Binance-backed tape, 449 complete events; σ₁ₛ = 0.719 bps). Trade
when `p_fair − ask > EDGE_MIN`, size by half-Kelly `f* = (p−a)/(1−a)`.
Strategy: `research/strategies/v10_btc_fairvalue_kelly.py`. Battery now
supports `--binance-only` (fixtures where `btc_last` is populated).

**Validated (temporal 50/50 train/test split, all results out-of-sample-checked):**
- Calibration table: at TTR 10–130s with BTC 1–4 bps from open, the favoured
  side's true win rate exceeds its market mid by 7–15 cents. The market
  under-reacts to the BTC feed. Mechanism confirmed.
- **Kelly sizing beats flat sizing on robustness**: flat-0.40 degraded ~50%
  out-of-sample ($14.1k→$7.2k, fixture-Sharpe 0.41→0.24); half-Kelly was
  stable ($3.3k→$3.6k, 0.39→0.35). Edge-proportional sizing self-throttles.
- **Latency kills the thin edge**: replay executes signals with a ONE-TICK
  delay (`replay.py` applies last tick's signal to this tick's book) and the
  fill is on average **+4.1c worse** than the trigger ask — the market
  reprices toward BTC within ~1 second. EDGE_MIN=0.12 collapses from
  +6.5c/share (instant fills) to +2.6c (delayed). Only fat-edge entries
  survive: EDGE_MIN=0.25 → +19.7c train / +14.7c test (n=18/26).
- **6 bps minimum-move filter** (user's live-validated 0.06%): masks
  Binance-vs-resolution-oracle basis noise — traced losing high-edge entries
  clustered at 5–8 bps moves. With EDGE_MIN=0.25: train n=10 win 80.0%
  +20.6c/sh, test n=11 win 81.8% +19.0c/sh.

**Two harness landmines documented for all future strategies:**
1. The simulator retargets `size·capital/ask` shares EVERY tick — a constant
   `Signal.size` churns (buys dips/sells rips paying spread+50bps slippage+fee
   each delta). To hold N shares, re-emit `size_t = entry_size·ask_t/ask_entry`.
   Even then the one-tick delay leaves residual wobble fills.
2. Emitting FLAT near expiry SELLS at the bid instead of settling at $1 —
   v8/v9's "safety close at TTR<1" silently cost 1–6c/share every event.
   Hold through settlement; the simulator settles pending shares at resolution.

**Battery head-to-head, identical 45 Binance-only fixtures (43 real + 2 synthetic):**

| | v10 t=0.25 | v10 t=0.25+mv6 | model_submission (promoted) | MomentumBaseline |
|---|---|---|---|---|
| mean score | **2,971** | 1,219 | 534 | 1,696 |
| median score | 0 | 0 | 0 | — |
| win-rate vs baseline | 51.1% | 51.1% | 55.6% | — |
| total PnL | −$1,210 | **−$407** | −$471 | −$15,660 |
| trades/fixture | 14.2 | 12.9 | 23.3 | — |
| real-fixture PnL detail | −$1,047, 10/23 trading fixtures positive | −$244, 8/13 positive | — | — |

**Status: NOT promotable yet (promotion-bar discipline).** Median score 0,
mean driven by a handful of big winners, and the mv6 variant's PnL is dragged
by two outsized Kelly-confident losses (−$655, −$610) on ~25 total entries.
~38 hours of Binance tape is data-starved for a strategy taking ~1 trade/hour.
The direction is the most promising so far on absolute PnL (near-breakeven vs
baseline's −$15.7k) and the sizing result is real. **Re-run when the
Binance-backed set reaches ~100+ hours.** Also worth trying: blending
`p_fair` with the market mid (the market knows the oracle basis we don't),
and an explicit reversal-exit instead of pure hold-to-settle.

## Promotions log

| Date (UTC) | Version | Aggregate win-rate | Mean score | Notes |
|---|---|---|---|---|
| 2026-06-07 | `v5_imbalance_confirm` → `model_submission.py` (now "v3: momentum + imbalance confirmation") | 43.3% (13/30) | 1,145.7 | First variant to robustly beat the prior baseline (`v1_conviction_hold_majority`, win-rate 41.4%, mean 704.0) on every aggregate axis — mean PnL −$19.75 vs −$33.62, total PnL −$592 vs −$975 — *with tighter score variance* (std 3,131 vs 3,408), confirming the gain is broad-based, not outlier-driven (contrast with the rejected v3 "pure directional" mirage, std 35,134). Change: imbalance promoted from veto-only to (a) a stronger confirmation-conviction bonus and (b) an independent low-conviction entry signal when momentum is silent. See full writeup under "Order-book imbalance could be a primary signal" above. |
| 2026-06-08 | `v7_fee_zone_tuned` → `model_submission.py` (one-line change: `FEE_MID_MULT 0.80 → 0.0`) | 50.0% (27/54) | 475.4 | Single largest, cleanest win of the whole arc — a grid sweep (see "Sizing near fee-optimal price extremes" above) showed entries near p≈0.50 (where `fee = shares×0.072×p×(1−p)` peaks) are net-harmful with no compensating signal-quality benefit; sizing them to zero beat every non-zero multiplier on every metric: mean 475.4 vs prior 326.8, median 60.1 vs 0.0, win-rate 50.0% vs 43.4%, mean PnL −$21.52 vs −$53.31, total PnL −$1,162 vs −$2,825 — *and* lower variance (std 2,705 vs 3,917). Note: the live-recording set grew from 30→54 fixtures between this and the prior promotion, so these win-rates/means aren't directly comparable to the v5 row above — both candidates in this sweep were re-run on the same (current, 54-fixture) battery for a fair head-to-head. `FEE_OPT_MULT` was also swept (1.0–1.75) and found to have **zero** effect at any value (saturates at `MAX_SIZE` regardless) — left unchanged since altering it provably does nothing. |

When promoting: copy the winning variant to `model_submission.py` at the repo
root, add a row here with the **full-battery** aggregate numbers (not a
`--limit` subset), and note what changed vs. the previous promoted version.
