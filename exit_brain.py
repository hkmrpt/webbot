"""
exit_brain.py  ──  Option Buy Robot v8.2
════════════════════════════════════════

AI exit brain — replaces hardcoded fast/slow rules with:
  1. MomentumScorer  : computes a 0-1 momentum score each tick from
                       velocity, direction consistency, and acceleration.
  2. AdaptiveTrailEngine : translates momentum score → trail%, and
                           self-tunes its multiplier after every trade.
  3. ExitBrain (combined): adds profit-decay detection to trigger an
                           early exit when momentum collapses.

No external ML library needed — pure online statistics.
"""

import math
from collections import deque


# ── Momentum Scorer ───────────────────────────────────────────────────────────

class MomentumScorer:
    """
    Scores current price momentum 0.0 (dead/reversing) to 1.0 (strong surge).

    Three components (weighted sum):
      velocity    (50%) — how fast price is moving in absolute pts/tick
      consistency (30%) — fraction of ticks moving in the same direction
      acceleration(20%) — is velocity itself increasing or decreasing?
    """

    def __init__(self, velocity_window: int = 5, accel_window: int = 4):
        self._vel_win   = velocity_window
        self._accel_win = accel_window
        self._vel_hist  = deque(maxlen=accel_window)

    def score(self, prices: deque) -> float:
        lst = list(prices)
        if len(lst) < 3:
            return 0.5   # neutral — not enough data yet

        recent  = lst[-min(self._vel_win + 1, len(lst)):]
        changes = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        abs_ch  = [abs(c) for c in changes]

        # ── Velocity component ──────────────────────────────────────────────
        velocity  = sum(abs_ch) / len(abs_ch) if abs_ch else 0.0
        # sigmoid so ~2 pts/tick scores ~0.5, ~5 pts/tick scores ~0.85
        vel_score = 1 / (1 + math.exp(-0.6 * (velocity - 2.5)))

        # ── Consistency component ───────────────────────────────────────────
        dirs = [1 if c > 0 else -1 for c in changes if c != 0]
        if dirs:
            dominant    = 1 if dirs.count(1) >= dirs.count(-1) else -1
            consistency = dirs.count(dominant) / len(dirs)
        else:
            consistency = 0.5

        # ── Acceleration component ──────────────────────────────────────────
        self._vel_hist.append(velocity)
        vl = list(self._vel_hist)
        if len(vl) >= 2:
            # positive slope → accelerating → score > 0.5
            slope       = (vl[-1] - vl[0]) / len(vl)
            accel_score = min(max(0.5 + slope / 4.0, 0.1), 0.9)
        else:
            accel_score = 0.5

        combined = 0.50 * vel_score + 0.30 * consistency + 0.20 * accel_score
        return round(min(max(combined, 0.0), 1.0), 3)


# ── Adaptive Trail Engine ─────────────────────────────────────────────────────

class AdaptiveTrailEngine:
    """
    Turns a momentum score + base trail% into an adaptive trail%.

    Logic:
      - High momentum (score → 1.0) → wider trail → let the trade run
      - Low momentum  (score → 0.0) → tight trail → protect gains fast

    Self-tuning after each trade (EWMA updates):
      - If trade gave back > GIVE_BACK_THRESHOLD% of peak profit
        → trail was too wide → tighten multiplier
      - If trade closed with good profit
        → trail was working → nudge wider
    """

    LEARNING_RATE      = 0.12   # EWMA step size per trade
    GIVE_BACK_THRESHOLD = 8.0   # % of peak profit — more than this = too wide
    MIN_TRAIL          = 2.0    # absolute floor for trail%
    MAX_TRAIL          = 30.0   # absolute ceiling

    def __init__(self):
        self._multiplier  = 1.0          # learned width scaler (0.5 – 2.0)
        self._outcomes    = deque(maxlen=30)
        self.win_streak   = 0
        self.loss_streak  = 0

    def get_trail_pct(self, base_trail: float, momentum_score: float) -> float:
        """
        momentum_score 0→1 maps to a momentum_factor 0.5→1.5.
        Combined with learned multiplier and base trail.
        """
        momentum_factor = 0.5 + momentum_score          # 0.5 (tight) → 1.5 (wide)
        adaptive = base_trail * momentum_factor * self._multiplier
        return round(min(max(adaptive, self.MIN_TRAIL), self.MAX_TRAIL), 1)

    def on_trade_closed(self, result: dict):
        """Update multiplier based on trade outcome."""
        pnl_pct  = result.get("pnl_pct",          0.0) or 0.0
        peak_pct = result.get("peak_profit_pct",   0.0) or 0.0
        reason   = result.get("reason",            "")

        self._outcomes.append(pnl_pct)

        gave_back = peak_pct - pnl_pct

        if gave_back > self.GIVE_BACK_THRESHOLD:
            # Gave back too much → tighten
            self._multiplier *= (1.0 - self.LEARNING_RATE)
            self.loss_streak += 1
            self.win_streak   = 0
        elif pnl_pct > 4.0:
            # Good profit captured → widen slightly
            self._multiplier *= (1.0 + self.LEARNING_RATE * 0.5)
            self.win_streak  += 1
            self.loss_streak  = 0

        self._multiplier = round(min(max(self._multiplier, 0.5), 2.0), 4)

    @property
    def multiplier(self) -> float:
        return self._multiplier

    @property
    def avg_pnl(self) -> float:
        return round(sum(self._outcomes) / len(self._outcomes), 2) if self._outcomes else 0.0


# ── Exit Brain (combined) ─────────────────────────────────────────────────────

class ExitBrain:
    """
    Full AI exit brain used by BuyExitStrategy.

    Per-tick:
      - score()           → momentum_score (0-1)
      - adaptive_trail()  → trail% adjusted for momentum + learning
      - check_ai_exit()   → True if profit is decaying + momentum collapsed
                            (fires before the normal trail/SL checks)

    Post-trade:
      - on_trade_closed() → forwards result to AdaptiveTrailEngine for learning
    """

    DECAY_WINDOW        = 6     # ticks to observe profit decay
    DECAY_MOMENTUM_CAP  = 0.30  # momentum must be below this to trigger decay exit
    DECAY_MIN_PROFIT    = 2.0   # only protect if profit > this %

    def __init__(self):
        self._scorer  = MomentumScorer()
        self._engine  = AdaptiveTrailEngine()
        self._profit_hist = deque(maxlen=self.DECAY_WINDOW)

    def score(self, prices: deque) -> float:
        """Compute momentum score from recent option prices."""
        return self._scorer.score(prices)

    def adaptive_trail(self, base_trail: float, momentum_score: float) -> float:
        """Return trail% adapted to current momentum and learned history."""
        return self._engine.get_trail_pct(base_trail, momentum_score)

    def update_profit(self, profit_pct: float):
        """Call each tick with current profit% so decay can be detected."""
        self._profit_hist.append(profit_pct)

    def check_ai_exit(self, profit_pct: float, momentum_score: float) -> bool:
        """
        Returns True when the brain recommends exiting early because:
          - Profit is declining consistently (all recent ticks lower)
          - AND momentum score is low (no recovery expected)
          - AND we have enough profit worth protecting
        """
        if profit_pct < self.DECAY_MIN_PROFIT:
            return False
        if momentum_score >= self.DECAY_MOMENTUM_CAP:
            return False

        hist = list(self._profit_hist)
        if len(hist) < self.DECAY_WINDOW:
            return False

        # True only if EVERY tick in window shows declining profit
        declining = all(hist[i] < hist[i - 1] for i in range(1, len(hist)))
        return declining

    def on_trade_closed(self, result: dict):
        """Teach the engine from the closed trade."""
        self._engine.on_trade_closed(result)
        self._profit_hist.clear()

    @property
    def state(self) -> dict:
        return {
            "trail_multiplier": self._engine.multiplier,
            "avg_pnl":          self._engine.avg_pnl,
            "win_streak":       self._engine.win_streak,
            "loss_streak":      self._engine.loss_streak,
        }
