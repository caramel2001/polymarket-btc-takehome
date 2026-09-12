"""
features.py — causal feature engineering for BTC 5-min direction prediction.

Target: will close[t+HORIZON] > close[t]?  (HORIZON=5 to match Polymarket 5m).
All features use ONLY information available at minute t (no lookahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZON = 5


def build_features(D: pd.DataFrame) -> pd.DataFrame:
    """D: 1-min OHLCV indexed by UTC timestamp. Returns feature frame + target `y`."""
    df = pd.DataFrame(index=D.index)
    c = D["close"].astype(float)
    logc = np.log(c)
    ret1 = logc.diff()

    # ── Multi-horizon momentum (log returns over N minutes) ──────────────────
    for n in [1, 2, 3, 5, 10, 15, 30, 60, 120, 240]:
        df[f"ret_{n}"] = (logc - logc.shift(n))

    # ── Realized volatility (rolling std of 1-min returns) ───────────────────
    for n in [5, 15, 30, 60, 120]:
        df[f"vol_{n}"] = ret1.rolling(n).std()

    # ── Return normalised by vol (z-score momentum) ──────────────────────────
    df["ret5_z"] = df["ret_5"] / (ret1.rolling(30).std() * np.sqrt(5) + 1e-9)
    df["ret15_z"] = df["ret_15"] / (ret1.rolling(60).std() * np.sqrt(15) + 1e-9)

    # ── Range / candle shape ─────────────────────────────────────────────────
    hl = (D["high"].astype(float) - D["low"].astype(float)) / c
    df["range_1"] = hl
    df["range_15"] = hl.rolling(15).mean()
    df["close_pos_15"] = (c - D["low"].astype(float).rolling(15).min()) / (
        D["high"].astype(float).rolling(15).max() - D["low"].astype(float).rolling(15).min() + 1e-9
    )

    # ── Volume & order flow ──────────────────────────────────────────────────
    vol = D["volume"].astype(float)
    df["logvol_1"] = np.log1p(vol)
    df["vol_ratio_15"] = vol / (vol.rolling(15).mean() + 1e-9)
    df["vol_ratio_60"] = vol / (vol.rolling(60).mean() + 1e-9)
    tb = D["taker_buy_base_asset_volume"].astype(float)
    buy_ratio = (tb / (vol + 1e-9)).clip(0, 1)        # fraction of volume that lifted the ask
    df["buy_ratio_1"] = buy_ratio
    df["buy_ratio_5"] = buy_ratio.rolling(5).mean()
    df["buy_ratio_15"] = buy_ratio.rolling(15).mean()
    df["buy_imb_15"] = (buy_ratio - 0.5).rolling(15).mean()   # net flow imbalance
    if "number_of_trades" in D:
        nt = D["number_of_trades"].astype(float)
        df["ntrades_ratio_15"] = nt / (nt.rolling(15).mean() + 1e-9)

    # ── Momentum/streak signals ──────────────────────────────────────────────
    up = (ret1 > 0).astype(float)
    df["up_frac_15"] = up.rolling(15).mean()
    # RSI(14) on 1-min
    gain = ret1.clip(lower=0).rolling(14).mean()
    loss = (-ret1.clip(upper=0)).rolling(14).mean()
    df["rsi_14"] = 100 - 100 / (1 + gain / (loss + 1e-9))

    # ── Time-of-day / day-of-week (cyclical) ─────────────────────────────────
    mins = df.index.hour * 60 + df.index.minute
    df["tod_sin"] = np.sin(2 * np.pi * mins / 1440)
    df["tod_cos"] = np.cos(2 * np.pi * mins / 1440)
    dow = df.index.dayofweek
    df["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # ── Target ───────────────────────────────────────────────────────────────
    fut = c.shift(-HORIZON)
    df["y"] = (fut > c).astype(int)
    df["fwd_ret_bps"] = np.log(fut / c) * 1e4    # for EV-aware evaluation

    return df


FEATURE_COLS = None  # set after first build


def split_xy(df: pd.DataFrame):
    feats = [col for col in df.columns if col not in ("y", "fwd_ret_bps")]
    sub = df.dropna()
    return sub[feats], sub["y"], sub["fwd_ret_bps"], feats
