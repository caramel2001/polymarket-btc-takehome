# ML strategy exploration — findings (2026-06-19)

**Goal:** train an ML model to predict BTC 5-min candle direction, then trade
the Polymarket 5m up/down market when the model's confidence diverges from the
market's implied probability (the `up_mid`).

**Data:** 9 years of 1-min Binance BTCUSDT spot (`data/btc_binance_spot/`,
2017-08 → 2026-06, 4.64M rows, clean 1-min cadence). Target: `close[t+5] > close[t]`
(5-min UP base rate = 49.96%, no drift bias).

**Pipeline:** `features.py` (34 causal features: multi-horizon momentum,
realized vol, vol-normalised returns, candle range/position, volume ratios,
**taker-buy order-flow imbalance**, RSI, up-streaks, cyclical time-of-day /
day-of-week) + `train.py` (Logistic + LightGBM, time-split train<2025/test≥2025).

## Result 1 — there IS weak standalone predictability ✓

LightGBM on 769k OOS samples (2025-2026):
- log-loss 0.69216 vs 0.69315 baseline, **AUC 0.524**, accuracy 0.516
- **monotonic, correct calibration**: p<0.45 → 41.2% actual up; p>0.55 → 56.0% up
- top features: 30/10/60-min momentum, `close_pos_15`, volume, **taker buy_ratio**
- consistent across every sub-period incl. the June-2026 Polymarket overlap (AUC 0.526)

So 5-min direction is *genuinely* (if weakly) predictable from public OHLCV+flow.
AUC 0.524 on this n is highly significant — not noise.

## Result 2 — but NO tradeable edge over the market ✗

Aligned the model to the Polymarket fixtures (the resolution window is
`[t-1, t+4]` in minute terms — verified at 96.3% outcome agreement; the naive
`[t, t+5]` alignment is an off-by-one that spuriously *inverts* the signal).

Incremental-info test at event open (2,209 events):
- raw market `open_mid` log-loss 0.6889
- raw `model_p` log-loss 0.6914 (**worse** than the market)
- `mid + model` combined: 0.6884 (in-sample, optimistic) — only 0.0004 better than mid

**On divergence, the MARKET is right, not the model.** When `model_p − open_mid > +0.04`
(model says up, market priced low ~0.42), actual up-rate = 0.444 — the market
(0.421) is closer to truth than the model (0.503). Reason: by the event's first
tick the `up_mid` already reflects the first few seconds of *real-time* BTC,
which the model (built on the prior minute's close) is stale to. The model only
helps when the market is uninformative (~0.50), and even there only marginally.

## Result 3 — the clean "model edge when market ≈ 0.50" trade overfits ✗

Tested: model confident (`|model_p−0.5| > conf`) AND market near 0.50
(`|mid−0.5| < band`), execute on fillable ticks (`ask_size>0`), temporal
train/test split. **EVERY config is positive in train, negative in test:**

| config | train | test |
|---|---|---|
| conf>0.02, band<0.03, fillable | +9.1c | −6.5c |
| conf>0.03, band<0.03, fillable | +13.8c | −8.3c |
| conf>0.05, band<0.03, fillable | +23.6c | −6.1c |

The bigger the in-sample edge, the worse out-of-sample = textbook overfit. The
AUC-0.524 signal is too small to survive the spread+fee+market-efficiency once
sliced to the tradeable subset (n=13-330).

## Conclusion

ML reaches the **same wall** as the 12 prior taker-side strategy classes
(see IDEAS.md): the market is efficient and prices the same public data plus
real-time flow the model can't match under taker execution. The model's real
standalone edge (AUC 0.524) is not enough to overcome execution costs.

**Why the market wins:** it *is* a real-time ensemble with the same OHLCV/flow
data **plus** the live tick the model lacks. To beat it you'd need either
(a) features the market doesn't have (order-book depth history, cross-asset,
on-chain — not in this dataset), or (b) maker execution / lower latency
(infra project, see IDEAS.md "MAKER FILLS").

**Reusable artifacts kept:** `features.py`, `train.py`, `gbm_5min.pkl` — a clean
5-min-direction model for any future setting where execution isn't taker-gated.

---

## Addendum — Volatility-regime thesis (2026-06-19)

Hypothesis: the market's implied P(UP) given the current BTC distance assumes
"normal" remaining volatility. If remaining vol is predictably high/low (vol
clustering), the market may misprice how easily a favourite is overturned:
high vol → fade favourites, low vol → back them. (Predicts: high vol → favourite
hold-rate < implied.)

Tested on the Polymarket overlap using causal recorded-tick realised vol
(rolling std of 1s btc_last log-returns over 60s), favourite-priced ticks.

**Directionally CONFIRMED, but not tradeable:**
- Incremental-info (OOS): `mid+vol` test log-loss 0.4934 beats `mid only`
  0.4940, vol coef −0.103 (negative = high vol fades favourites = hypothesis).
  But raw market `favp` (0.4930) still beats it — the market mostly prices it.
- Executable favourite EV decreases **monotonically** in vol tercile
  (lowvol > midvol > highvol), confirming the mechanism.
- BUT every cell is negative OOS after spread+fee. Best cell (low-vol
  favourite≥0.78): n=574 train +0.74c → **test −4.21c**.

**Small-sample trap caught:** a tight slice (low-vol, fav≥0.78, fillable, ttr
30-180) looked great at n=13/15 (+11.9c train / +7.9c test) — but widening to
full power (n≈570/cell) flips it to −4.2c test. The n=13 "win on both halves"
was luck. Classic promotion-bar lesson: never trust a tradeable edge on tiny n.

**Verdict:** vol regime is real microstructure (monotonic effect, marginal
incremental info) but the market prices it well enough that no vol-conditioned
favourite trade survives execution costs. Same wall.

---

## Addendum 2 — Cross-asset ETH→BTC lead (2026-06-20)

Tested whether ETH carries information about BTC's near-future that the
BTC-only model (and the market) lacks. Data: 9yr ETHUSDT 1-min
(`/home/pratham/.openclaw/workspace/trading-strategy/data/crypto/ETHUSDT/`).

**Lead-lag (raw 1-min returns):** contemp corr(BTC,ETH)=0.57; ETH→BTC at k=+1
only +0.022; BTC→ETH at k=−1 is +0.041 — i.e. BTC mildly leads ETH, not the
reverse. So no *strong* linear lead.

**BUT a multi-feature model finds a genuine short-lived ETH→BTC lead.** Adding
ETH return features to the LightGBM 5-min-direction model and lagging them to
verify causality:

| ETH feature lag | test AUC |
|---|---|
| 0 min | 0.5694 |
| **1 min (strictly causal)** | **0.5496** |
| 2 min | 0.5389 |
| 5 min | 0.5253 (= BTC-only) |

The smooth decay (not a cliff at lag-1) is the signature of a real, dissipating
lead, not a timestamp leak. Lag-1 ETH (data ≤ t−1 predicting BTC [t,t+5]) lifts
AUC 0.525 → 0.550 — a verified, leakage-free cross-asset edge. The ETH-augmented
model is also more *decisive* at the Polymarket open (n=330 events at p>0.55 vs
50 for BTC-only) and still calibrated (p>0.52 → 54.2% actual up).

**Yet still NO tradeable edge vs the market:**
- Incremental-info at open: raw `model_p` log-loss 0.6940 is WORSE than the
  market `open_mid` 0.6889; `mid+model` gives the model coef just **+0.07**
  (negligible). The market already prices ETH + real-time BTC the model lacks.
- Executable divergence trade (model confident + market≈0.50), train/test:
  every config negative OOS (conf>0.03 fillable: +1.95c train → −3.12c test).

**This is the strongest evidence of market efficiency in the whole project:**
even a *genuinely predictive* second asset (verified causal lead) adds nothing
tradeable, because the market already incorporates it. Public-data ML — single
or cross asset — cannot beat this market under taker execution.

## Overall ML conclusion

Direction, volatility regime, and cross-asset all reach the same wall: real but
small standalone signal that the market fully prices and that cannot survive
taker spread+fee+latency. The market is an efficient real-time ensemble over the
same public data. The only remaining levers are non-public data or non-taker
(maker) execution — see IDEAS.md "MAKER FILLS".
