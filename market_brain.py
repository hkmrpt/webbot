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


# State dir anchored next to the module (or exe when frozen) so learned state
# never silently lands in a different working directory. The replay harness
# overrides STATE_DIR to isolate its runs from live state.
import sys as _sys
STATE_DIR = (os.path.dirname(os.path.abspath(_sys.executable))
             if getattr(_sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
BRAIN_STATE_FILE = "brain_state.json"

def _state_path(mode: str = "") -> str:
    """Per-mode state file (brain_state_demo.json / brain_state_real.json) —
    frictionless demo fills must never pollute real-money weights."""
    if mode:
        base, ext = os.path.splitext(BRAIN_STATE_FILE)
        return os.path.join(STATE_DIR, f"{base}_{mode}{ext}")
    return os.path.join(STATE_DIR, BRAIN_STATE_FILE)
N_FEATURES       = 9

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
        # Welford's M2 starts at 0 (sum of squared deviations of ZERO samples).
        # The old init of 1.0 was a phantom unit of variance that under-scaled
        # every feature for the first dozen trades.
        self._M2   = [0.0] * n

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
            if std < 1e-8:
                # (Near-)constant feature carries no information — emit 0
                # instead of exploding to a huge z-score via a tiny epsilon.
                result.append(0.0)
            else:
                result.append((x[i] - self.mean[i]) / std)
        return result

    def to_dict(self) -> dict:
        return {"count": self.count, "mean": self.mean, "M2": self._M2}

    def from_dict(self, d: dict):
        self.count = d.get("count", 0)
        loaded_mean = d.get("mean", [0.0] * self.n)
        loaded_m2   = d.get("M2",   [0.0] * self.n)
        # Extend if feature count grew
        if len(loaded_mean) < self.n:
            loaded_mean.extend([0.0] * (self.n - len(loaded_mean)))
        if len(loaded_m2) < self.n:
            loaded_m2.extend([0.0] * (self.n - len(loaded_m2)))
        self.mean = loaded_mean[:self.n]
        self._M2  = loaded_m2[:self.n]


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
    MIN_SAMPLES    = 15       # model is neutral below this — don't filter entries

    def __init__(self):
        self.weights  = [0.0] * N_FEATURES
        self.bias     = 0.0
        self.n_trades = 0
        self.n_wins   = 0

    def predict(self, x_norm: list) -> float:
        """Return win-probability (0–1) for a normalised feature vector."""
        z = sum(self.weights[i] * x_norm[i] for i in range(N_FEATURES)) + self.bias
        return _sigmoid(z)

    def update(self, x_norm: list, label: int, sample_weight: float = 1.0):
        """Perform one gradient-descent step.

        sample_weight balances class imbalance (importance weighting): with a
        17% win rate the unweighted model collapses to "always predict loss"
        via the bias term and never becomes discriminative.
        """
        p     = self.predict(x_norm)
        error = (label - p) * sample_weight
        lr    = self.LEARNING_RATE

        for i in range(N_FEATURES):
            self.weights[i] = (
                self.weights[i] * (1 - lr * self.L2_LAMBDA)
                + lr * error * x_norm[i]
            )
        self.bias += lr * error

        self.n_trades += 1
        if label == 1:
            self.n_wins += 1

    def class_weight(self, label: int) -> float:
        """Balanced importance weight n/(2·n_class), clamped to [0.5, 3.0]."""
        if self.n_trades < 4:
            return 1.0
        n_class = self.n_wins if label == 1 else (self.n_trades - self.n_wins)
        if n_class <= 0:
            return 3.0
        return max(0.5, min(3.0, self.n_trades / (2.0 * n_class)))

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
        loaded = d.get("weights", [0.0] * N_FEATURES)
        # Extend if feature count grew (new features start neutral at 0.0)
        if len(loaded) < N_FEATURES:
            loaded.extend([0.0] * (N_FEATURES - len(loaded)))
        self.weights  = loaded[:N_FEATURES]
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

    WINDOW        = 30    # was 20 — wider window = more stable slope
    TREND_SLOPE   = 0.20  # was 0.15 — stricter: only clear trends qualify
    CHOP_ATR_CAP  = 0.5   # ATR/price% below this + flat slope = choppy
    VOLATILE_ATR  = 0.8   # ATR/price% above this = volatile
    CONFIRM_TICKS = 3     # regime must sustain this many ticks before changing

    def __init__(self):
        self._last_classified = "unknown"
        self._candidate       = "unknown"
        self._candidate_count = 0

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
            raw = "volatile"
        elif slope_norm >= self.TREND_SLOPE:
            raw = "trending_up"
        elif slope_norm <= -self.TREND_SLOPE:
            raw = "trending_down"
        else:
            raw = "choppy"

        # Regime persistence: only switch after CONFIRM_TICKS consecutive ticks
        if raw == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate       = raw
            self._candidate_count = 1

        if self._candidate_count >= self.CONFIRM_TICKS:
            self._last_classified = raw
        return self._last_classified


# ── Feature Builder ───────────────────────────────────────────────────────────

class FeatureBuilder:
    """
    Builds a 7-element raw feature vector from market state at entry time.

    Features:
      0  spike_strength  abs(spike_pts) / jump_threshold       → spike quality
      1  slope           regression slope (momentum direction)
      2  nifty_velocity  avg |pts/tick| over last 5 NIFTY ticks → speed
      3  time_of_day     minutes since 9:15 / 375               → 0–1
      4  atr_pct         NIFTY ATR / price * 100                → volatility
      5  fast_entry      1.0 if fast entry, 0.0 if confirmed
      6  regime_align    SIDE-RELATIVE regime alignment:
                          +1 trend favours the trade side, −1 against,
                          −0.5 choppy, +0.25 volatile, 0 unknown
                         (the old encoding mapped trending_up AND
                          trending_down both to 1.0 — trend sign destroyed)
      7  rsi             RSI over last 14 ticks (0–100)          → overbought/oversold
      8  range_position  position in session high-low (0–1)       → extremes
    """

    MARKET_OPEN = 9 * 60 + 15    # 9:15 in minutes
    MARKET_MINS = 375            # total trading minutes

    @staticmethod
    def regime_alignment(regime: str, side: str) -> float:
        """Side-relative regime encoding — the feature the model actually
        needs to separate with-trend entries from against-trend ones."""
        if regime == "trending_up":
            return 1.0 if side == "CE" else -1.0
        if regime == "trending_down":
            return 1.0 if side == "PE" else -1.0
        if regime == "choppy":
            return -0.5
        if regime == "volatile":
            return 0.25
        return 0.0

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
        side:         str = "CE",
        rsi:          float = 50.0,
        range_position: float = 0.5,
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

        # 3 — time of day (injectable IST clock — correct on any host + replay)
        from core.clock import now as _now
        now        = _now()
        mins_today = now.hour * 60 + now.minute
        time_norm  = max(0.0, min(1.0, (mins_today - self.MARKET_OPEN) / self.MARKET_MINS))

        # 4 — ATR%
        atr_pct = (nifty_atr / nifty_price * 100) if nifty_price else 0.0

        # 5 — fast entry flag
        fast_flag = 1.0 if fast_entry else 0.0

        # 6 — regime alignment (side-relative, sign preserved)
        regime_enc = self.regime_alignment(regime, side)

        # 7 — RSI (already 0-100 scale, normalize in pipeline)
        rsi_val = float(rsi)

        # 8 — Session range position (0 = at session low, 1 = at session high)
        range_val = float(range_position)

        return [spike_strength, slope_val, velocity, time_norm, atr_pct, fast_flag, regime_enc, rsi_val, range_val]


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
    MIN_ENTRY_SCORE   = 0.52          # was 0.42 — require higher confidence
    # PnL% to call a trade a "win" for learning purposes
    WIN_THRESHOLD_PCT = 0.8           # was 1.5 — account for transaction costs

    def __init__(self, mode: str | None = None):
        from config import TRADING_MODE
        self._mode     = mode or TRADING_MODE
        self._lr       = OnlineLR()
        self._stats    = RunningStats(N_FEATURES)
        self._regime   = MarketRegimeDetector()
        self._features = FeatureBuilder()
        self._last_raw_features: list | None = None
        self._last_x_norm:       list | None = None
        self._last_regime:       str         = "unknown"
        self._load()

    def set_mode(self, mode: str):
        """Switch demo/real: persist the current mode's state, then load the
        other mode's state fresh."""
        if mode == self._mode:
            return
        self._save()
        self._mode  = mode
        self._lr    = OnlineLR()
        self._stats = RunningStats(N_FEATURES)
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
        side = state.get("side", "CE")

        raw = self._features.build(
            spike_pts      = abs(state.get("nifty_move", 0) or 0),
            jump_threshold = state.get("jump_threshold", 1),
            slope          = state.get("regression_slope", 0),
            nifty_ticks    = state.get("nifty_ticks", []),
            nifty_price    = state.get("nifty_price", 1),
            nifty_atr      = state.get("jump_atr", 0) or 0,
            fast_entry     = state.get("fast_entry", False),
            regime         = regime,
            side           = side,
            rsi            = state.get("rsi", 50.0),
            range_position = state.get("range_position", 0.5),
        )

        # Normalize with PRE-update stats, then fold the sample in — the old
        # order normalized each sample with stats that already contained it
        # (self-inclusion leakage). The normalized vector is stored so
        # training uses exactly what was scored (no distribution mismatch).
        x_norm = self._stats.normalize(raw)
        self._stats.update(raw)
        score  = self._lr.predict(x_norm)

        self._last_raw_features = raw
        self._last_x_norm       = x_norm

        # Side vs regime conflict check
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
        # Mode isolation: never learn from a trade made in a different
        # trading mode (frictionless demo fills must not train real weights).
        if result.get("mode") and result["mode"] != self._mode:
            self._last_raw_features = None
            self._last_x_norm       = None
            return

        # Use pnl_total when partial bookings occurred — the trade's real outcome
        pnl_pct = result.get("pnl_pct", 0.0) or 0.0
        label   = 1 if pnl_pct >= self.WIN_THRESHOLD_PCT else 0

        # Train on the exact vector that was scored (stored at entry time)
        x_norm = self._last_x_norm or self._stats.normalize(self._last_raw_features)
        self._lr.update(x_norm, label, self._lr.class_weight(label))
        self._last_raw_features = None
        self._last_x_norm       = None
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
            "mode":             self._mode,
            "feature_names":    [
                "spike_strength", "slope", "nifty_velocity",
                "time_of_day", "atr_pct", "fast_entry", "regime_align",
                "rsi", "range_position"
            ],
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        try:
            data = {
                "lr":    self._lr.to_dict(),
                "stats": self._stats.to_dict(),
                "mode":  self._mode,
            }
            with open(_state_path(self._mode), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[MarketBrain] save error: {e}")

    def _load(self):
        if not os.path.exists(_state_path(self._mode)):
            return
        try:
            with open(_state_path(self._mode)) as f:
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
