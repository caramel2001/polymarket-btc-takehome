# Cross-horizon structure: 15m and 5m markets on a shared settlement

The first technique tested in this project that is **not** a variation on
"predict the direction". Session 2026-09-13, tape = 105 paired events.

## 1. The structure (verified, exact)

15m and 5m events share settlement boundaries exactly — 900 is a multiple of 300,
and **110/110** 15m events on tape share an `end_ts` with a 5m event. So during
the final five minutes, the two markets are bets on the **same terminal price**
`S_E` with **different strikes**:

    5m  pays 1 if S_E > K5   where K5  = S(E-300)
    15m pays 1 if S_E > K15  where K15 = S(E-900)

With `d = K5 - K15` known and public, set inclusion gives a model-free dominance
relation:

| | implication | violations |
|---|---|---|
| d > 0 | 5m UP ⟹ 15m UP | **0 / 49** |
| d < 0 | 15m UP ⟹ 5m UP | **0 / 41** |

**Zero violations in 90 paired events.** The relation is exact.

Outcomes agree only 58% of the time, confirming these are genuinely different
bets — not a redundant pair.

## 2. Resolution convention, calibrated

Getting this right was prerequisite. Scanning reference-time offsets against
actual resolutions, both the open and close references sit **45 seconds before**
the nominal window boundaries:

| offsets | agreement |
|---|---|
| (0, 0) — naive | 86.0% |
| **(−45, −45)** | **92.7%** (15m 94.9%, 5m 92.0%) |

Consistent with the `[t-1, t+4]` minute quirk already noted in `IDEAS.md`. The
shift is common to both ends, so the window still spans the horizon exactly and
the dominance relation is unaffected.

## 3. The pure arbitrage: absent

When d > 0, buying **15m-UP + 5m-DOWN** pays at least $1 whatever happens. So a
combined ask below $1.00 is riskless. Raw, it looked abundant — 22.6% of ticks,
61c mean profit. It was not:

| filter | arb ticks |
|---|---|
| raw | 22.6% |
| exclude degenerate/post-resolution books | 8.1% |
| + only genuinely-trading windows (0 < ttr ≤ horizon) | 4.6% |
| + require `\|d\|/spot ≥ 3e-4` (sign unambiguous) | **0.0%** (n=15,173) |

The apparent arbitrage was entirely **sign error in my estimate of `d`** when the
move was small enough to sit inside spot-measurement noise. The market prices the
hard bound correctly.

> Found a real recorder bug on the way: the slug's trailing timestamp is the
> window's **start**, not its end (Gamma's `endDate` = slug_ts + horizon).
> `polybench.market` documents it as the endDate — harmless there because it
> floors to the current boundary, but it made my discovery track the *next*
> window, recording ~300s of pre-open (degenerate) book per event. Fixed in
> `universe.py`; verified `end − start` = horizon exactly.

## 4. Relative value: real but too small

The two prices pin two points of one CDF, so each implies a volatility, and they
must agree. Pricing the 5m market off the 15m market's implied vol gives a
divergence with **no systematic bias** (mean gap t = −0.20) but large transient
dispersion (|gap| > 25% of mean vol on 76% of ticks) — the classic RV setup.

The divergence does mean-revert. But roughly **40-45% of it is bid-ask bounce**:
the signal contains the contemporaneous 5m mid, and noisy mids produce negative
autocorrelation mechanically. Re-running with the mid lagged 5s breaks that link:

| signal | horizon | q5−q1 | t (event-clustered) |
|---|---|---|---|
| contemporaneous mid | 5s / 15s / 30s | −1.28c / −1.87c / −1.85c | 3.45 / 3.20 / 3.00 |
| **lagged mid (bounce-free)** | 5s / 15s / 30s | **−0.74c / −1.03c / −1.57c** | **2.46 / 1.73 / 1.87** |

**Verdict: real but not tradeable.** The bounce-free effect is marginal (t
1.73–2.46 on 77 events) and its 0.74–1.57c gross does not cover a two-leg
round trip (~2.0c at a 0.50c median half-spread).

## 5. What would change the answer

- **More tape.** 77 events is thin; t ≈ 2 could firm up or evaporate.
- **Maker execution.** The trade is long-short and pays two spreads as a taker.
  Posting both legs flips ~2.0c of cost into revenue — but reintroduces the leg
  risk that killed the maker structures in `research/phase3/FINDINGS.md`.
- **An exact `d`.** Ours is estimated from 1 Hz Binance spot; the true resolution
  source would remove the sign ambiguity entirely and sharpen the signal.
