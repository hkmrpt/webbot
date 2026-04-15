"""
VolatilityDetector
──────────────────
Watches a rolling window of Nifty prices and reports:
  • is_ranging()  → True when high-low spread <= RANGE_MAX_POINTS
  • range_info()  → dict with current window stats for the UI

Entry is allowed only when is_ranging() returns True.
"""

from collections import deque
from config import RANGE_WINDOW, RANGE_MAX_POINTS, RANGE_MIN_TICKS


class VolatilityDetector:
    def __init__(self):
        self._prices: deque = deque(maxlen=RANGE_WINDOW)

    # ── public API ────────────────────────────────────────────
    def add(self, price: float) -> None:
        self._prices.append(price)

    def reset(self) -> None:
        self._prices.clear()

    def is_ranging(self) -> bool:
        if len(self._prices) < RANGE_MIN_TICKS:
            return False
        return self._spread() <= RANGE_MAX_POINTS

    def range_info(self) -> dict:
        if not self._prices:
            return {"spread": 0.0, "high": 0.0, "low": 0.0,
                    "ticks": 0, "ready": False, "ranging": False}
        spread  = self._spread()
        return {
            "spread":  round(spread, 2),
            "high":    round(max(self._prices), 2),
            "low":     round(min(self._prices), 2),
            "ticks":   len(self._prices),
            "ready":   len(self._prices) >= RANGE_MIN_TICKS,
            "ranging": self.is_ranging(),
        }

    # ── internal ──────────────────────────────────────────────
    def _spread(self) -> float:
        if not self._prices:
            return 999.0
        return max(self._prices) - min(self._prices)
