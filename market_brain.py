"""
market_brain.py  ──  Option Buy Robot v8.2
══════════════════════════════════════════

Self-learning AI brain for trade ENTRY decisions.

Architecture:
  FeatureBuilder       → builds 7-feature vector from live market state
  RunningStats         → online mean/std normalization (Welford's algorithm)
  OnlineLR             → online logistic regression — updates weights after
                         every trade (gradient descent, no external libs)
  MarketRegimeDetector → classifies current market as:
                         trending_up | trending_down | choppy | volatile
  MarketBrain          → top-level API used by buy_app.py

Self-learning loop (each trade):
  1. At entry: extract features → score → allow / block / log reason
  2. At close:  label = 1 if profitable else 0
               update OnlineLR weights via gradient descent
               persist weights to brain_state.json

The model starts neutral (all weights = 0 → score ≈ 0.5 for every trade)
and gradually learns which entry conditions lead to wins vs losses.
After ~30–50 trades the scores become meaningfully discriminative.
"""

import json
import math
import os
from collections import deque
from datetime import datetime

from config import REGIME_TREND_SLOPE, REGIME_CHOP_ATR_CAP, REGIME_VOLATILE_ATR

BRAIN_STATE_FILE = "brain_state.json"
N_FEATURES       = 9    # added day_type feature

# ── Logistic helpers ──────────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    x = max(-30.0, min(30.0, x))  # clip to avoid overflow
    return 1.0 / (1.0 + math.exp(-x))


# ── Online feature normalization (Welford's algorithm) ───────────────────────

class RunningStats:
    """Per-feature online mean and variance. Thread-safe via copy-on-read."""

    def __init__(self, n: int):
        self.n     = n
        self.count = 0
        self.mean  = [0.0] * n
        self._M2   = [1.0] * n   # sum of squared deviations (Welford)

    def update(self, x: list):
        self.count += 1
        for i in range(self.n):
            delta       = x[i] - self.mean[i]
            self.mean[i] += delta / self.count
            delta2      = x[i] - self.mean[i]
            self._M2[i] += delta * delta2

    def normalize(self, x: list) -> list:
        result = []
        for i in range(self.n):
            if self.count > 1:
                std = math.sqrt(self._M2[i] / (self.count - 1))
            else:
                std = 1.0
            std = max(std, 1e-6)
            result.append((x[i] - self.mean[i]) / std)
        return result

    def to_dict(self) -> dict:
        return {"count": self.count, "mean": self.mean, "M2": self._M2}

    def from_dict(self, d: dict):
        self.count = d.get("count", 0)
        self.mean  = d.get("mean",  [0.0] * self.n)
        self._M2   = d.get("M2",    [1.0] * self.n)


# ── Online Logistic Regression ────────────────────────────────────────────────

class OnlineLR:
    """
    Binary logistic regression trained one sample at a time.
    Label 1 = good trade (profit), 0 = bad trade (loss/scratch).

    Update rule (stochastic gradient descent):
      p = sigmoid(w · x_norm + b)
      w += lr * (label - p) * x_norm
      b += lr * (label - p)

    L2 regularisation keeps weights from exploding on small samples.
    """

    LEARNING_RATE  = 0.05
    L2_LAMBDA      = 0.001    # regularisation strength
    MIN_SAMPLES    = 30       # model is neutral below this — don't filter entries

    def __init__(self):
        self.weights  = [0.0] * N_FEATURES
        self.bias     = 0.0
        self.n_trades = 0
        self.n_wins   = 0

    def predict(self, x_norm: list) -> float:
        """Return win-probability (0–1) for a normalised feature vector."""
        z = sum(self.weights[i] * x_norm[i] for i in range(N_FEATURES)) + self.bias
        return _sigmoid(z)

    WIN_THRESHOLD_PCT = 1.5   # used for win_rate tracking only (not gradient)

    def update(self, x_norm: list, pnl_pct: float):
        """
        PnL-weighted gradient descent.
        Uses a soft label derived from pnl_pct instead of binary 0/1:
          pnl=0%   → soft_label=0.5 (neutral)
          pnl=+16% → soft_label≈0.88 (strong positive signal)
          pnl=-16% → soft_label≈0.12 (strong negative signal)
        Weight scales gradient by |pnl| so big wins/losses teach more.
        """
        soft_label = _sigmoid(pnl_pct / 8.0)
        weight     = min(abs(pnl_pct) / 8.0 + 0.5, 3.0)
        p          = self.predict(x_norm)
        error      = (soft_label - p) * weight
        lr         = self.LEARNING_RATE

        for i in range(N_FEATURES):
            self.weights[i] = (
                self.weights[i] * (1 - lr * self.L2_LAMBDA)
                + lr * error * x_norm[i]
            )
        self.bias += lr * error

        self.n_trades += 1
        if pnl_pct >= self.WIN_THRESHOLD_PCT:
            self.n_wins += 1

    @property
    def win_rate(self) -> float:
        return round(self.n_wins / self.n_trades, 3) if self.n_trades else 0.0

    @property
    def ready(self) -> bool:
        """True once model has seen enough trades to be meaningful."""
        return self.n_trades >= self.MIN_SAMPLES

    def to_dict(self) -> dict:
        return {
            "weights":  self.weights,
            "bias":     self.bias,
            "n_trades": self.n_trades,
            "n_wins":   self.n_wins,
        }

    def from_dict(self, d: dict):
        loaded = d.get("weights", [])
        if len(loaded) != N_FEATURES:
            # Feature count changed — reset weights but preserve trade history
            print(
                f"[MarketBrain] feature count changed "
                f"({len(loaded)}→{N_FEATURES}) — resetting weights"
            )
            self.weights = [0.0] * N_FEATURES
        else:
            self.weights  = loaded
        self.bias     = d.get("bias",     0.0)
        self.n_trades = d.get("n_trades", 0)
        self.n_wins   = d.get("n_wins",   0)


# ── Market Regime Detector ────────────────────────────────────────────────────

class MarketRegimeDetector:
    """
    Classifies the current NIFTY market regime from recent tick history.

    Regimes:
      trending_up   — consistent upward drift, good for CE
      trending_down — consistent downward drift, good for PE
      choppy        — oscillating, both directions, lower win rate
      volatile      — large moves but no direction, use tighter trail

    Used to:
      • Block CE entries in trending_down, PE in trending_up
      • Tighten / loosen trail via regime passed to ExitBrain
    """

    WINDOW        = 20    # ticks for slope calculation
    # Thresholds are in normalized units: slope_pct = slope_pts/tick / price * 100
    # At NIFTY=24000, a 1pt/tick trend gives slope_pct ≈ 0.0042%.
    # Old values (0.15, 0.5, 0.8) were orders of magnitude too high — regime was
    # virtually always "choppy", making directional blocks and 2 ML features useless.
    TREND_SLOPE   = REGIME_TREND_SLOPE   # configurable via config.py
    CHOP_ATR_CAP  = REGIME_CHOP_ATR_CAP
    VOLATILE_ATR  = REGIME_VOLATILE_ATR

    def classify(self, nifty_ticks: list) -> str:
        if len(nifty_ticks) < self.WINDOW:
            return "unknown"

        prices = nifty_ticks[-self.WINDOW:]
        n      = len(prices)
        mean_p = sum(prices) / n

        # Linear slope via least-squares
        x_mean = (n - 1) / 2.0
        num    = sum((i - x_mean) * (prices[i] - mean_p) for i in range(n))
        den    = sum((i - x_mean) ** 2 for i in range(n))
        slope  = (num / den) if den else 0.0
        slope_norm = slope / mean_p * 100   # normalised to price%

        # ATR (mean |tick-to-tick|)
        trs  = [abs(prices[i] - prices[i - 1]) for i in range(1, n)]
        atr  = sum(trs) / len(trs) if trs else 0.0
        atr_pct = atr / mean_p * 100

        if atr_pct >= self.VOLATILE_ATR:
            return "volatile"
        if slope_norm >= self.TREND_SLOPE:
            return "trending_up"
        if slope_norm <= -self.TREND_SLOPE:
            return "trending_down"
        return "choppy"


# ── Feature Builder ───────────────────────────────────────────────────────────

class FeatureBuilder:
    """
    Builds an 8-element raw feature vector from market state at entry time.

    Features:
      0  spike_strength   abs(spike_pts) / jump_threshold       → spike quality
      1  slope            regression slope (momentum direction)
      2  nifty_velocity   avg |pts/tick| over last 5 NIFTY ticks → speed
      3  time_of_day      minutes since 9:15 / 375               → 0–1
      4  atr_pct          NIFTY ATR / price * 100                → volatility
      5  fast_entry       1.0 if fast entry, 0.0 if confirmed
      6  is_trending      1.0 if trending_up or trending_down, else 0.0
      7  trend_direction  1.0=trending_up, -1.0=trending_down, 0.0=other
      8  day_type         1.0=Thursday(expiry) 0.5=Mon/Fri 0.0=normal
    """

    MARKET_OPEN = 9 * 60 + 15    # 9:15 in minutes
    MARKET_MINS = 375            # total trading minutes

    def build(
        self,
        spike_pts:    float,
        jump_threshold: float,
        slope:        float,
        nifty_ticks:  list,
        nifty_price:  float,
        nifty_atr:    float,
        fast_entry:   bool,
        regime:       str,
    ) -> list:
        # 0 — spike strength
        spike_strength = abs(spike_pts) / jump_threshold if jump_threshold else 1.0

        # 1 — slope (already normalised)
        slope_val = float(slope)

        # 2 — NIFTY velocity
        recent = nifty_ticks[-6:] if len(nifty_ticks) >= 6 else nifty_ticks
        if len(recent) >= 2:
            velocity = sum(abs(recent[i] - recent[i-1]) for i in range(1, len(recent))) / (len(recent) - 1)
        else:
            velocity = 0.0

        # 3 — time of day
        now        = datetime.now()
        mins_today = now.hour * 60 + now.minute
        time_norm  = max(0.0, min(1.0, (mins_today - self.MARKET_OPEN) / self.MARKET_MINS))

        # 4 — ATR%
        atr_pct = (nifty_atr / nifty_price * 100) if nifty_price else 0.0

        # 5 — fast entry flag
        fast_flag = 1.0 if fast_entry else 0.0

        # 6 — is_trending: 1.0 if any trend direction, 0.0 if choppy/volatile/unknown
        is_trending = 1.0 if regime in ("trending_up", "trending_down") else 0.0

        # 7 — trend_direction: separates up vs down trend so LR can learn directional bias
        if regime == "trending_up":
            trend_direction = 1.0
        elif regime == "trending_down":
            trend_direction = -1.0
        else:
            trend_direction = 0.0

        # 8 — day type: Thursday=expiry(1.0), Mon/Fri=different dynamics(0.5), else 0.0
        weekday = datetime.now().weekday()   # 0=Mon, 3=Thu, 4=Fri
        if weekday == 3:
            day_type = 1.0   # Thursday — weekly NIFTY expiry
        elif weekday in (0, 4):
            day_type = 0.5   # Monday/Friday — gap risk, post-expiry dynamics
        else:
            day_type = 0.0

        return [spike_strength, slope_val, velocity, time_norm, atr_pct,
                fast_flag, is_trending, trend_direction, day_type]


# ── Market Brain (top-level API) ──────────────────────────────────────────────

class MarketBrain:
    """
    Entry-scoring and self-learning brain.

    Usage in buy_app.py:
      At spike detection:
        score, allow, reason = market_brain.score_entry(state_dict)
        if not allow: skip trade

      At trade close:
        market_brain.on_trade_closed(result)

      Dashboard:
        market_brain.state  →  dict
    """

    # Score threshold: below this → block entry (only when model is ready)
    MIN_ENTRY_SCORE   = 0.42
    # PnL% to call a trade a "win" for learning purposes
    WIN_THRESHOLD_PCT = 1.5

    def __init__(self):
        self._lr       = OnlineLR()
        self._stats    = RunningStats(N_FEATURES)
        self._regime   = MarketRegimeDetector()
        self._features = FeatureBuilder()
        self._last_raw_features: list | None = None
        self._last_regime:       str         = "unknown"
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def score_entry(self, state: dict) -> tuple[float, bool, str]:
        """
        state keys expected:
          nifty_move, jump_threshold, regression_slope, nifty_ticks,
          nifty_price, jump_atr, fast_entry

        Returns (score 0–1, allow bool, reason str)
        """
        regime = self._regime.classify(state.get("nifty_ticks", []))
        self._last_regime = regime

        raw = self._features.build(
            spike_pts      = abs(state.get("nifty_move", 0) or 0),
            jump_threshold = state.get("jump_threshold", 1),
            slope          = state.get("regression_slope", 0),
            nifty_ticks    = state.get("nifty_ticks", []),
            nifty_price    = state.get("nifty_price", 1),
            nifty_atr      = state.get("jump_atr", 0) or 0,
            fast_entry     = state.get("fast_entry", False),
            regime         = regime,
        )

        self._stats.update(raw)
        x_norm = self._stats.normalize(raw)
        score  = self._lr.predict(x_norm)

        self._last_raw_features = raw

        # Side vs regime conflict check
        side = state.get("side", "CE")
        if regime == "trending_down" and side == "CE":
            return score, False, f"AI: market trending DOWN — skip CE (score={score:.2f})"
        if regime == "trending_up" and side == "PE":
            return score, False, f"AI: market trending UP — skip PE (score={score:.2f})"

        # Score filter (only active after enough samples)
        if self._lr.ready and score < self.MIN_ENTRY_SCORE:
            return score, False, (
                f"AI: low confidence score={score:.2f} "
                f"(threshold={self.MIN_ENTRY_SCORE}, trades={self._lr.n_trades})"
            )

        confidence = self._confidence_label(score)
        return score, True, f"AI: {confidence} score={score:.2f} regime={regime}"

    def on_trade_closed(self, result: dict):
        """
        Learn from the trade outcome.
        Called after every trade close (win or loss).
        """
        if self._last_raw_features is None:
            return

        pnl_pct = result.get("pnl_pct", 0.0) or 0.0
        x_norm  = self._stats.normalize(self._last_raw_features)
        self._lr.update(x_norm, pnl_pct)   # PnL-weighted, not binary
        self._last_raw_features = None
        self._save()

    def get_regime(self, nifty_ticks: list) -> str:
        self._last_regime = self._regime.classify(nifty_ticks)
        return self._last_regime

    @property
    def state(self) -> dict:
        return {
            "n_trades":         self._lr.n_trades,
            "win_rate":         self._lr.win_rate,
            "model_ready":      self._lr.ready,
            "last_regime":      self._last_regime,
            "weights":          [round(w, 4) for w in self._lr.weights],
            "bias":             round(self._lr.bias, 4),
            "feature_names":    [
                "spike_strength", "slope", "nifty_velocity",
                "time_of_day", "atr_pct", "fast_entry",
                "is_trending", "trend_direction", "day_type",
            ],
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        try:
            data = {
                "lr":    self._lr.to_dict(),
                "stats": self._stats.to_dict(),
            }
            with open(BRAIN_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[MarketBrain] save error: {e}")

    def _load(self):
        if not os.path.exists(BRAIN_STATE_FILE):
            return
        try:
            with open(BRAIN_STATE_FILE) as f:
                data = json.load(f)
            self._lr.from_dict(data.get("lr", {}))
            self._stats.from_dict(data.get("stats", {}))
            print(
                f"[MarketBrain] loaded — "
                f"trades={self._lr.n_trades}  win_rate={self._lr.win_rate:.1%}"
            )
        except Exception as e:
            print(f"[MarketBrain] load error (starting fresh): {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _confidence_label(score: float) -> str:
        if score >= 0.72:   return "HIGH"
        if score >= 0.57:   return "MEDIUM"
        if score >= 0.42:   return "LOW"
        return "SKIP"
