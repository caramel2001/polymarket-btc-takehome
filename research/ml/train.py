"""
train.py — train + evaluate ML models for BTC 5-min direction prediction.

Pipeline:
  1. Load all years of 1-min Binance spot (data/btc_binance_spot/).
  2. Build causal features (features.py).
  3. Time-split train (<2025) / test (>=2025), train Logistic + LightGBM.
  4. Report OOS log-loss / AUC / accuracy / calibration.
  5. Save model to research/ml/gbm_5min.pkl.

Run:  python research/ml/train.py

KEY RESULT (2026-06-19, see FINDINGS.md): the model has REAL but tiny
standalone edge (test AUC ~0.524, monotonic calibration), yet NO tradeable
incremental edge over the Polymarket market price — every divergence-trade
config is positive in-sample, negative out-of-sample. ML confirms market
efficiency; it does not beat it under taker execution.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import build_features, split_xy  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "btc_binance_spot"
MODEL_OUT = Path(__file__).resolve().parent / "gbm_5min.pkl"


def load_1min() -> pd.DataFrame:
    cols = ["open", "high", "low", "close", "volume",
            "number_of_trades", "taker_buy_base_asset_volume"]
    parts = []
    for f in sorted(DATA.glob("*.parquet.snappy")):
        df = pd.read_parquet(f, columns=cols)
        for c in ["open", "high", "low", "close", "volume", "taker_buy_base_asset_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        parts.append(df)
    D = pd.concat(parts).sort_index()
    return D[~D.index.duplicated(keep="first")]


def main() -> None:
    t0 = time.time()
    D = load_1min()
    print(f"loaded {len(D):,} rows  {D.index.min()}..{D.index.max()}  ({time.time()-t0:.0f}s)")

    df = build_features(D)
    X, y, fwd, feats = split_xy(df)
    tr, te = X.index < "2025-01-01", X.index >= "2025-01-01"
    Xtr, ytr, Xte, yte = X[tr], y[tr], X[te], y[te]
    print(f"train {len(Xtr):,}  test {len(Xte):,}  ({len(feats)} features)")
    print(f"baseline log_loss={log_loss(yte, np.full(len(yte), ytr.mean())):.5f}")

    sc = StandardScaler().fit(Xtr)
    lr = LogisticRegression(C=1.0, max_iter=1000).fit(sc.transform(Xtr), ytr)
    pl = lr.predict_proba(sc.transform(Xte))[:, 1]
    print(f"LOGISTIC  ll={log_loss(yte, pl):.5f}  auc={roc_auc_score(yte, pl):.4f}  acc={accuracy_score(yte, pl>0.5):.4f}")

    n = len(Xtr); cv = int(n * 0.9)
    params = dict(objective="binary", metric="binary_logloss", num_leaves=64,
                  learning_rate=0.03, feature_fraction=0.7, bagging_fraction=0.7,
                  bagging_freq=1, min_data_in_leaf=200, verbose=-1)
    gbm = lgb.train(params, lgb.Dataset(Xtr.iloc[:cv], ytr.iloc[:cv]),
                    num_boost_round=2000, valid_sets=[lgb.Dataset(Xtr.iloc[cv:], ytr.iloc[cv:])],
                    callbacks=[lgb.early_stopping(50, verbose=False)])
    pg = gbm.predict(Xte, num_iteration=gbm.best_iteration)
    print(f"LIGHTGBM  ll={log_loss(yte, pg):.5f}  auc={roc_auc_score(yte, pg):.4f}  acc={accuracy_score(yte, pg>0.5):.4f}")

    print("\ncalibration (test):")
    dfp = pd.DataFrame({"p": pg, "y": yte.values})
    for lo, hi in [(0, 0.45), (0.45, 0.48), (0.48, 0.52), (0.52, 0.55), (0.55, 1)]:
        s = dfp[(dfp.p >= lo) & (dfp.p < hi)]
        if len(s) > 100:
            print(f"  p[{lo},{hi}): n={len(s):>7} actual_up={s.y.mean():.4f}")

    joblib.dump(gbm, MODEL_OUT)
    print(f"\nsaved {MODEL_OUT}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
