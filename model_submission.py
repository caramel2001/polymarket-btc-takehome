"""
model_submission.py — Model 3: Regime-Adaptive Token Momentum

Strategy pillars:
  1. Regime detection via trend_score = |r_30s| / std(r_1s)
       TREND     → momentum strategy (multi-horizon, acceleration-filtered)
       CHOP/MEAN → fade extremes (mean reversion at 0.05-0.15 / 0.85-0.95)

  2. Token-level momentum (not BTC) — already embeds informed flow
       Multi-horizon: 5s / 15s / 30s must align
       Acceleration filter: second derivative > 0

  3. Mean reversion at extremes
       UP price near 0.1 or 0.9 → fade if TTR large + no strong momentum

  4. TTR dynamics
       sensitivity ∝ 1/TTR → scale size aggressively late
       Pre-resolution liquidation breakout: spread widening + momentum
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from enum import Enum

from polybench import FLAT, Model, Side, Signal, Tick


# ─────────────────────────────────────────────────────────────────────────────
# Hyperparameters
# ─────────────────────────────────────────────────────────────────────────────

# Regime
TREND_THRESHOLD    = 1.8    # trend_score above → TREND
CHOP_THRESHOLD     = 0.9    # trend_score below → CHOP (mean-revert)

# Momentum (multi-horizon alignment)
MOM_MIN_5          = 0.003  # minimum |r_5s| to consider
MOM_MIN_15         = 0.002
MOM_MIN_30         = 0.001
ACCEL_MIN          = 0.0    # second derivative > this

# Mean reversion bands
MR_BAND_LO_LO      = 0.05
MR_BAND_LO_HI      = 0.15
MR_BAND_HI_LO      = 0.85
MR_BAND_HI_HI      = 0.95
MR_MIN_TTR         = 60.0   # don't MR fade if less than this time left
MR_MAX_TREND_SCORE = 1.4    # don't MR if trend is strong

# Pre-resolution liquidation breakout
LIQBREAK_MAX_TTR   = 90.0   # only active within last 90s
SPREAD_SPIKE_MIN   = 1.8    # spread_now / spread_mean > this
LIQBREAK_MOM_MIN   = 0.004  # need directional price move too

# Signal / entry
SIGNAL_THRESHOLD   = 0.06   # composite |S| to enter
MAX_SIZE           = 0.50
K_SIZE             = 1.1    # base_size = min(0.5, K*|S|)

# TTR size scaling
TTR_FULL           = 300.0
TTR_LATE           = 60.0   # last 60s → aggressive
TTR_VERY_LATE      = 20.0   # last 20s → max aggression, tightest exits
TTR_EARLY          = 240.0  # first 60s → conservative

# Exit rules
CLOSE_AT_TTR       = 7.0
MAX_HOLD_TREND     = 40     # seconds
MAX_HOLD_MR        = 25     # mean reversion should be quick
MAX_HOLD_LIQBREAK  = 15
STOP_LOSS_FRAC     = 0.028
TAKE_PROFIT_FRAC   = 0.07
SIGNAL_DECAY_MIN   = 0.025  # exit if |S| drops below

MIN_COOLDOWN       = 3.5
DRAWDOWN_PAUSE     = 0.12

# Window sizes (ticks ≈ seconds)
W5, W15, W30, W60  = 5, 15, 30, 60
W_SPREAD           = 20


# ─────────────────────────────────────────────────────────────────────────────
# Regime + sub-strategy enums
# ─────────────────────────────────────────────────────────────────────────────

class Regime(Enum):
    TREND     = "TREND"
    CHOP      = "CHOP"
    UNKNOWN   = "UNKNOWN"

class SubStrategy(Enum):
    TREND_MOM  = "TREND_MOM"
    MEAN_REV   = "MEAN_REV"
    LIQBREAK   = "LIQBREAK"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _safe_mean(buf: deque) -> float:
    return sum(buf) / len(buf) if buf else 0.0

def _safe_std(buf: deque) -> float:
    return statistics.pstdev(buf) if len(buf) >= 2 else 1e-9

def _last(buf: deque, default: float = 0.0) -> float:
    return buf[-1] if buf else default

def _first(buf: deque, default: float = 0.0) -> float:
    return buf[0] if buf else default


# ─────────────────────────────────────────────────────────────────────────────
# ModelSubmission
# ─────────────────────────────────────────────────────────────────────────────

class ModelSubmission(Model):

    def on_start(self,market_info=None) -> None:
        # Token price buffers
        self._b5:  deque[float] = deque(maxlen=W5)
        self._b15: deque[float] = deque(maxlen=W15)
        self._b30: deque[float] = deque(maxlen=W30)
        self._b60: deque[float] = deque(maxlen=W60)

        # 1-second return buffer (for std in trend_score denominator)
        self._r1_buf: deque[float] = deque(maxlen=W30)
        self._prev_up_mid: float | None = None

        # Spread buffer
        self._spread_buf: deque[float] = deque(maxlen=W_SPREAD)

        # Momentum derivative tracking
        self._prev_m5:  float = 0.0
        self._prev_m15: float = 0.0

        # Signal smoothing
        self._prev_score: float = 0.0

        # Position state
        self._pos_side:    Side | None  = None
        self._pos_size:    float        = 0.0
        self._pos_strat:   SubStrategy | None = None
        self._entry_price: float        = 0.0
        self._entry_ts:    float        = 0.0
        self._last_trade_ts: float      = 0.0

        # Drawdown
        self._peak_equity: float    = 1000.0
        self._current_eq:  float    = 1000.0

    # ── main ─────────────────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> Signal | None:
        self._update_buffers(tick)

        # Hard close near expiry
        if tick.time_to_resolve < CLOSE_AT_TTR:
            return self._close(tick)

        # Need enough history
        if len(self._b30) < W30:
            return FLAT

        feats  = self._compute_features(tick)
        regime = self._classify_regime(feats)

        # Priority: liqbreak > regime-adaptive
        liqbreak = self._check_liqbreak(tick, feats)

        if liqbreak is not None:
            score, side = liqbreak
        elif regime == Regime.TREND:
            score, side = self._trend_signal(feats)
        elif regime == Regime.CHOP:  
            score, side = self._mean_rev_signal(tick, feats)
        else:
            score, side = 0.0, None

        # Smooth
        score = 0.55 * score + 0.45 * self._prev_score
        self._prev_score = score

        # Exits
        if self._pos_side is not None:
            exit_sig = self._check_exits(tick, score, feats)
            if exit_sig is not None:
                return exit_sig

        # Entry
        return self._check_entry(tick, score, side, feats,
                                  SubStrategy.LIQBREAK if liqbreak else
                                  (SubStrategy.TREND_MOM if regime == Regime.TREND
                                   else SubStrategy.MEAN_REV))

    # ── buffer update ─────────────────────────────────────────────────────────

    def _update_buffers(self, tick: Tick) -> None:
        um = tick.up_mid
        self._b5.append(um)
        self._b15.append(um)
        self._b30.append(um)
        self._b60.append(um)

        # 1s return
        if self._prev_up_mid is not None:
            r1 = um - self._prev_up_mid
            self._r1_buf.append(r1)
        self._prev_up_mid = um

        spread = tick.up_ask - tick.up_bid
        self._spread_buf.append(spread)

    # ── features ─────────────────────────────────────────────────────────────

    def _compute_features(self, tick: Tick) -> dict:
        um = tick.up_mid

        # Returns at each horizon (absolute, not normalised — price is [0,1])
        r5  = um - _first(self._b5,  um)
        r15 = um - _first(self._b15, um)
        r30 = um - _first(self._b30, um)

        # Momentum second derivative (acceleration)
        m5_now      = r5
        m15_now     = r15
        accel5      = m5_now  - self._prev_m5
        accel15     = m15_now - self._prev_m15
        self._prev_m5  = m5_now
        self._prev_m15 = m15_now

        # Trend score: |r30| / std(r1s)
        std_r1      = _safe_std(self._r1_buf)
        trend_score = abs(r30) / std_r1 if std_r1 > 1e-9 else 0.0

        # Volatility
        std30 = _safe_std(self._b30)
        mean30 = _safe_mean(self._b30)
        z30   = (um - mean30) / std30 if std30 > 1e-9 else 0.0

        # Spread
        spread     = tick.up_ask - tick.up_bid
        spread_mean = _safe_mean(self._spread_buf)
        spread_spike = spread / spread_mean if spread_mean > 1e-9 else 1.0

        # Imbalance
        denom = tick.up_mid + tick.down_mid
        imb   = (tick.up_mid - tick.down_mid) / denom if denom > 0 else 0.0

        # Parity
        parity = tick.up_mid + tick.down_mid

        # TTR-derived sensitivity (∝ 1/TTR, capped)
        ttr = max(tick.time_to_resolve, 1.0)
        sensitivity = min(5.0, TTR_FULL / ttr)

        return dict(
            um=um, r5=r5, r15=r15, r30=r30,
            accel5=accel5, accel15=accel15,
            trend_score=trend_score,
            std30=std30, mean30=mean30, z30=z30,
            spread=spread, spread_mean=spread_mean, spread_spike=spread_spike,
            imb=imb, parity=parity,
            ttr=ttr, sensitivity=sensitivity,
        )

    # ── regime ────────────────────────────────────────────────────────────────

    def _classify_regime(self, f: dict) -> Regime:
        ts = f["trend_score"]
        if ts > TREND_THRESHOLD:
            return Regime.TREND
        if ts < CHOP_THRESHOLD:
            return Regime.CHOP
        return Regime.UNKNOWN

    # ── trend momentum signal ─────────────────────────────────────────────────

    def _trend_signal(self, f: dict) -> tuple[float, Side | None]:
        """
        Multi-horizon alignment + acceleration filter.
        All three horizons must agree in sign.
        Acceleration (2nd derivative) must be positive in direction of move.
        """
        r5, r15, r30 = f["r5"], f["r15"], f["r30"]

        # All three horizons must be non-trivially aligned
        if not (abs(r5) >= MOM_MIN_5 and abs(r15) >= MOM_MIN_15 and abs(r30) >= MOM_MIN_30):
            return 0.0, None

        signs = {math.copysign(1, r5), math.copysign(1, r15), math.copysign(1, r30)}
        if len(signs) != 1:
            return 0.0, None   # not aligned

        direction = 1.0 if r5 > 0 else -1.0

        # Acceleration filter: 2nd derivative must push in same direction
        accel = f["accel5"]
        if direction * accel < ACCEL_MIN:
            return 0.0, None

        # Imbalance confirmation
        if direction * f["imb"] < 0:
            strength_mult = 0.6
        else:
            strength_mult = 1.0

        # Score magnitude from 30s return, normalised
        raw_score = _clamp(abs(r30) / 0.05, 0.0, 1.0) * strength_mult
        score     = direction * raw_score

        side = Side.UP if direction > 0 else Side.DOWN
        return score, side

    # ── mean reversion signal ─────────────────────────────────────────────────

    def _mean_rev_signal(self, tick: Tick, f: dict) -> tuple[float, Side | None]:
        """
        Fade when UP price is in extreme bands [0.05,0.15] or [0.85,0.95].
        Only if: TTR large, trend_score weak, not accelerating further.
        """
        um  = f["um"]
        ttr = f["ttr"]

        if ttr < MR_MIN_TTR:
            return 0.0, None
        if f["trend_score"] > MR_MAX_TREND_SCORE:
            return 0.0, None

        in_lo_band = MR_BAND_LO_LO <= um <= MR_BAND_LO_HI
        in_hi_band = MR_BAND_HI_LO <= um <= MR_BAND_HI_HI

        if not (in_lo_band or in_hi_band):
            return 0.0, None

        # Don't fade if still accelerating in the extreme direction
        if in_lo_band and f["accel5"] < 0:   # still falling
            return 0.0, None
        if in_hi_band and f["accel5"] > 0:   # still rising
            return 0.0, None

        if in_lo_band:
            # UP is cheap → buy UP (expect reversion up)
            dist = (MR_BAND_LO_HI - um) / (MR_BAND_LO_HI - MR_BAND_LO_LO)
            score = _clamp(dist, 0.05, 0.8)
            return score, Side.UP
        else:
            # UP is expensive → sell UP via DOWN
            dist = (um - MR_BAND_HI_LO) / (MR_BAND_HI_HI - MR_BAND_HI_LO)
            score = _clamp(dist, 0.05, 0.8)
            return -score, Side.DOWN

    # ── pre-resolution liquidation breakout ───────────────────────────────────

    def _check_liqbreak(self, tick: Tick, f: dict) -> tuple[float, Side | None] | None:
        """
        Within last 90s: spread widens (liquidity leaving) + directional move.
        Traders flattening → vacuum → momentum spike.
        """
        if f["ttr"] > LIQBREAK_MAX_TTR:
            return None
        if f["spread_spike"] < SPREAD_SPIKE_MIN:
            return None

        r5 = f["r5"]
        if abs(r5) < LIQBREAK_MOM_MIN:
            return None

        direction = 1.0 if r5 > 0 else -1.0
        # Boost strength by sensitivity (1/TTR)
        raw = _clamp(abs(r5) / 0.02, 0.0, 1.0) * min(2.0, f["sensitivity"] / 2.0)
        score = direction * _clamp(raw, 0.0, 1.0)
        side = Side.UP if direction > 0 else Side.DOWN
        return score, side

    # ── exit logic ────────────────────────────────────────────────────────────

    def _check_exits(self, tick: Tick, score: float, f: dict) -> Signal | None:
        hold = tick.ts - self._entry_ts

        # Max hold (strategy-dependent)
        max_hold = {
            SubStrategy.TREND_MOM:  MAX_HOLD_TREND,
            SubStrategy.MEAN_REV:   MAX_HOLD_MR,
            SubStrategy.LIQBREAK:   MAX_HOLD_LIQBREAK,
        }.get(self._pos_strat, MAX_HOLD_TREND)

        if hold > max_hold:
            return self._close(tick)

        # Signal decay
        if abs(score) < SIGNAL_DECAY_MIN:
            return self._close(tick)

        # Signal flip vs position
        if self._pos_side == Side.UP   and score < -0.04:
            return self._close(tick)
        if self._pos_side == Side.DOWN and score >  0.04:
            return self._close(tick)

        # P&L exits
        pnl = self._pnl_frac(tick)
        if pnl < -STOP_LOSS_FRAC:
            return self._close(tick)
        if pnl > TAKE_PROFIT_FRAC:
            return self._close(tick)

        return Signal(
            side=self._pos_side,
            size=self._pos_size,
            confidence=min(1.0, abs(score)),
        )

    def _pnl_frac(self, tick: Tick) -> float:
        ref = self._entry_price
        if ref <= 0:
            return 0.0
        if self._pos_side == Side.UP:
            return (tick.up_bid - ref) / ref
        return (ref - tick.up_ask) / ref   # short UP via DOWN

    # ── entry logic ───────────────────────────────────────────────────────────

    def _check_entry(
        self,
        tick: Tick,
        score: float,
        side: Side | None,
        f: dict,
        strat: SubStrategy,
    ) -> Signal:
        if self._pos_side is not None:
            return Signal(side=self._pos_side, size=self._pos_size, confidence=abs(score))

        if side is None or abs(score) < SIGNAL_THRESHOLD:
            return FLAT

        if tick.ts - self._last_trade_ts < MIN_COOLDOWN:
            return FLAT

        # Parity sanity: dampen if sum deviates badly
        parity_dev = abs(f["parity"] - 1.0)
        if parity_dev > 0.05:
            score *= max(0.0, 1.0 - parity_dev / 0.08)
            if abs(score) < SIGNAL_THRESHOLD:
                return FLAT

        # Base size
        base = _clamp(K_SIZE * abs(score), 0.0, MAX_SIZE)

        # TTR scaling: sensitivity ∝ 1/TTR
        ttr = f["ttr"]
        if ttr < TTR_VERY_LATE:
            f_ttr = 1.4
        elif ttr < TTR_LATE:
            f_ttr = 1.2
        elif ttr > TTR_EARLY:
            f_ttr = 0.65
        else:
            # Linear ramp between EARLY and LATE
            f_ttr = 0.65 + (TTR_EARLY - ttr) / (TTR_EARLY - TTR_LATE) * 0.55

        # Drawdown control
        dd_f = self._drawdown_factor()

        size = _clamp(base * f_ttr * dd_f, 0.02, MAX_SIZE)

        self._pos_side    = side
        self._pos_size    = size
        self._pos_strat   = strat
        self._entry_price = tick.up_mid
        self._entry_ts    = tick.ts
        self._last_trade_ts = tick.ts

        return Signal(side=side, size=size, confidence=min(1.0, abs(score)))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _close(self, tick: Tick) -> Signal:
        if self._pos_side is not None:
            self._last_trade_ts = tick.ts
        self._pos_side    = None
        self._pos_size    = 0.0
        self._pos_strat   = None
        self._entry_price = 0.0
        return FLAT

    def _drawdown_factor(self) -> float:
        if self._peak_equity <= 0:
            return 1.0
        dd = (self._peak_equity - self._current_eq) / self._peak_equity
        if dd > DRAWDOWN_PAUSE:
            return 0.25
        if dd > DRAWDOWN_PAUSE * 0.5:
            return 0.60
        return 1.0