"""
exit_brain.py  ──  Option Buy Robot v8.3
════════════════════════════════════════

Self-learning per-tick exit policy using online logistic regression
with retrospective (post-trade) labeling.

How it learns:
  During a trade  — every tick's state vector + price is recorded.
  After trade closes — each tick is retrospectively labeled:
      label=0 (should have held)  if price rose after that tick
      label=1 (should have exited) if price fell after that tick
      weight = |price_change_pct| so bigger moves teach more
  All tick samples run through gradient descent in one batch.
  Updated weights are used from the very next trade.

State vector (6 features per tick):
  0  profit_pct     (current_price - entry) / entry * 100
  1  drawdown_pct   (peak_price - current) / peak * 100
  2  momentum       option price momentum score  0–1
  3  held_norm      elapsed_secs / MAX_HOLD_SECS  0–1
  4  atr_norm       option ATR / current_price * 100
  5  regime_enc     trending_up=1.0  trending_down=-1.0
                    volatile=0.5  choppy/unknown=0.0

Persists to exit_brain_state.json.
"""

import json
import math
import os
from collections import deque

EXIT_BRAIN_FILE = "exit_brain_state.json"
N_EXIT_FEATURES = 6
MAX_HOLD_SECS   = 120.0   # normalization ceiling for held_norm feature

REGIME_ENC = {
    "trending_up":   1.0,
    "trending_down": -1.0,
    "volatile":       0.5,
    "choppy":         0.0,
    "unknown":        0.0,
}


# ── Logistic helper ───────────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    x = max(-30.0, min(30.0, x))
    return 1.0 / (1.0 + math.exp(-x))


# ── Online running stats (Welford's algorithm) ────────────────────────────────

class RunningStats:
    def __init__(self, n: int):
        self.n     = n
        self.count = 0
        self.mean  = [0.0] * n
        self._M2   = [1.0] * n

    def update(self, x: list):
        self.count += 1
        for i in range(self.n):
            delta        = x[i] - self.mean[i]
            self.mean[i] += delta / self.count
            self._M2[i]  += delta * (x[i] - self.mean[i])

    def normalize(self, x: list) -> list:
        out = []
        for i in range(self.n):
            std = math.sqrt(self._M2[i] / max(1, self.count - 1))
            out.append((x[i] - self.mean[i]) / max(std, 1e-6))
        return out

    def to_dict(self) -> dict:
        return {"count": self.count, "mean": self.mean, "M2": self._M2}

    def from_dict(self, d: dict):
        self.count = d.get("count", 0)
        self.mean  = d.get("mean",  [0.0] * self.n)
        self._M2   = d.get("M2",    [1.0] * self.n)


# ── Exit policy logistic regression ──────────────────────────────────────────

class ExitPolicyLR:
    """
    Binary LR: 0 = hold, 1 = exit.
    Trained retrospectively with weighted samples after each trade.
    """
    LR          = 0.04
    L2          = 0.001
    MIN_SAMPLES = 30    # ticks seen before policy is trusted

    def __init__(self):
        self.weights  = [0.0] * N_EXIT_FEATURES
        self.bias     = 0.0
        self.n_ticks  = 0
        self.n_trades = 0

    def predict(self, x_norm: list) -> float:
        z = sum(self.weights[i] * x_norm[i] for i in range(N_EXIT_FEATURES)) + self.bias
        return _sigmoid(z)

    def update(self, x_norm: list, label: int, weight: float = 1.0):
        """Weighted gradient descent step."""
        p     = self.predict(x_norm)
        error = (label - p) * weight
        lr    = self.LR
        for i in range(N_EXIT_FEATURES):
            self.weights[i] = (
                self.weights[i] * (1 - lr * self.L2)
                + lr * error * x_norm[i]
            )
        self.bias    += lr * error
        self.n_ticks += 1

    @property
    def ready(self) -> bool:
        return self.n_ticks >= self.MIN_SAMPLES

    def to_dict(self) -> dict:
        return {
            "weights":  self.weights,
            "bias":     self.bias,
            "n_ticks":  self.n_ticks,
            "n_trades": self.n_trades,
        }

    def from_dict(self, d: dict):
        w = d.get("weights", [])
        self.weights  = w if len(w) == N_EXIT_FEATURES else [0.0] * N_EXIT_FEATURES
        self.bias     = d.get("bias",     0.0)
        self.n_ticks  = d.get("n_ticks",  0)
        self.n_trades = d.get("n_trades", 0)


# ── Momentum scorer ───────────────────────────────────────────────────────────

class MomentumScorer:
    """Scores option price momentum 0 (dead) → 1 (surging)."""

    def __init__(self, window: int = 5):
        self._window   = window
        self._vel_hist = deque(maxlen=4)

    def score(self, prices: deque) -> float:
        lst = list(prices)
        if len(lst) < 3:
            return 0.5
        recent   = lst[-min(self._window + 1, len(lst)):]
        changes  = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        abs_ch   = [abs(c) for c in changes]
        velocity = sum(abs_ch) / len(abs_ch) if abs_ch else 0.0
        vel_score = 1 / (1 + math.exp(-0.6 * (velocity - 2.5)))
        dirs      = [1 if c > 0 else -1 for c in changes if c != 0]
        consist   = dirs.count(max(set(dirs), key=dirs.count)) / len(dirs) if dirs else 0.5
        self._vel_hist.append(velocity)
        vl    = list(self._vel_hist)
        accel = min(max(0.5 + (vl[-1] - vl[0]) / len(vl) / 4.0, 0.1), 0.9) if len(vl) >= 2 else 0.5
        return round(min(max(0.5 * vel_score + 0.3 * consist + 0.2 * accel, 0.0), 1.0), 3)


# ── Tick recorder ─────────────────────────────────────────────────────────────

class TickRecorder:
    """Buffers (state_vector, price) during a trade for retrospective labeling."""

    def __init__(self):
        self._ticks: list = []

    def record(self, state: list, price: float):
        self._ticks.append((state, price))

    def labeled_samples(self):
        """
        Yield (state, label, weight) for each tick except the last.
        label  = 0 (hold) if next tick price is higher, 1 (exit) if lower.
        weight = proportional to |price change %|, capped at 3.0.
        """
        ticks = self._ticks
        for i in range(len(ticks) - 1):
            state,     p_now  = ticks[i]
            _,         p_next = ticks[i + 1]
            change_pct = (p_next - p_now) / p_now * 100 if p_now else 0.0
            label  = 0 if change_pct >= 0 else 1
            weight = min(abs(change_pct) / 2.0 + 0.1, 3.0)
            yield state, label, weight

    def clear(self):
        self._ticks = []

    def __len__(self):
        return len(self._ticks)


# ── Exit Brain (top-level API) ────────────────────────────────────────────────

class ExitBrain:
    """
    Self-learning per-tick exit policy.

    Usage in BuyExitStrategy:

      open_leg():
        brain.reset()

      on_price() per tick:
        state = brain.build_state(price, entry, peak, elapsed, atr, regime, prices)
        brain.record(state, price)
        if brain.should_exit(state, profit_pct): close trade

      _close():
        brain.learn()    ← retrospective training on this trade's ticks
    """

    EXIT_THRESHOLD     = 0.55    # policy score above this → recommend exit
    MIN_PROFIT_TO_EXIT = 2.0     # AI exit only fires when in profit > this %

    def __init__(self):
        self._lr       = ExitPolicyLR()
        self._stats    = RunningStats(N_EXIT_FEATURES)
        self._recorder = TickRecorder()
        self._scorer   = MomentumScorer()
        self._load()

    # ── Per-tick API ──────────────────────────────────────────────────────────

    def build_state(
        self,
        price:   float,
        entry:   float,
        peak:    float,
        elapsed: float,
        atr:     float | None,
        regime:  str,
        prices:  deque,
    ) -> list:
        profit_pct   = (price - entry) / entry * 100 if entry else 0.0
        drawdown_pct = (peak - price)  / peak  * 100 if peak > 0 else 0.0
        momentum     = self._scorer.score(prices)
        held_norm    = min(elapsed / MAX_HOLD_SECS, 1.0)
        atr_norm     = (atr / price * 100) if atr and price else 0.0
        regime_enc   = REGIME_ENC.get(regime, 0.0)
        return [profit_pct, drawdown_pct, momentum, held_norm, atr_norm, regime_enc]

    def record(self, state: list, price: float):
        """Buffer this tick. Called every tick during an open trade."""
        self._recorder.record(state, price)

    def exit_score(self, state: list) -> float:
        """Raw exit probability from the policy LR (0=hold, 1=exit)."""
        if not self._lr.ready:
            return 0.0
        return self._lr.predict(self._stats.normalize(state))

    def should_exit(self, state: list, profit_pct: float) -> bool:
        """True when policy is confident we should exit and we have enough profit to protect."""
        if profit_pct < self.MIN_PROFIT_TO_EXIT:
            return False
        return self.exit_score(state) >= self.EXIT_THRESHOLD

    # ── Post-trade learning ───────────────────────────────────────────────────

    def learn(self):
        """
        Retrospective training on all ticks from the just-closed trade.
        Must be called before reset().
        """
        for state, label, weight in self._recorder.labeled_samples():
            self._stats.update(state)
            x_norm = self._stats.normalize(state)
            self._lr.update(x_norm, label, weight)
        self._lr.n_trades += 1
        self._save()

    def reset(self):
        """Clear tick buffer and momentum state for the next trade."""
        self._recorder.clear()
        self._scorer = MomentumScorer()

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @property
    def state(self) -> dict:
        return {
            "n_ticks":       self._lr.n_ticks,
            "n_trades":      self._lr.n_trades,
            "policy_ready":  self._lr.ready,
            "exit_threshold": self.EXIT_THRESHOLD,
            "weights":       [round(w, 4) for w in self._lr.weights],
            "feature_names": [
                "profit_pct", "drawdown_pct", "momentum",
                "held_norm", "atr_norm", "regime_enc",
            ],
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        try:
            with open(EXIT_BRAIN_FILE, "w") as f:
                json.dump({
                    "lr":    self._lr.to_dict(),
                    "stats": self._stats.to_dict(),
                }, f, indent=2)
        except Exception as e:
            print(f"[ExitBrain] save error: {e}")

    def _load(self):
        if not os.path.exists(EXIT_BRAIN_FILE):
            return
        try:
            with open(EXIT_BRAIN_FILE) as f:
                d = json.load(f)
            self._lr.from_dict(d.get("lr", {}))
            self._stats.from_dict(d.get("stats", {}))
            print(
                f"[ExitBrain] loaded — "
                f"ticks={self._lr.n_ticks}  "
                f"trades={self._lr.n_trades}  "
                f"ready={self._lr.ready}"
            )
        except Exception as e:
            print(f"[ExitBrain] load error (starting fresh): {e}")
