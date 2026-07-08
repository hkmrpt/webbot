"""
engine/indicators.py — incremental O(1)-per-tick indicators.

The hot tick path previously recomputed everything from scratch on every
tick under the global state lock:
  * regression slope — full np.polyfit over the tick deque
  * tick ATR         — full mean-of-|Δ| scan
  * RSI              — full gains/losses scan

These classes maintain running sums so each tick costs O(1). Batch
equivalence is unit-tested against the original formulas in
tests/test_indicators.py.
"""

from collections import deque


class RollingSlope:
    """
    O(1) least-squares slope over the last `window` values.

    Equivalent to np.polyfit(range(n), values, 1)[0] over the window.
    Uses the closed form  slope = (n·Σxy − Σx·Σy) / (n·Σx² − (Σx)²)
    with x re-indexed 0..n−1; running sums are updated on push/evict.
    """

    def __init__(self, window: int):
        self.window = int(window)
        self._vals: deque = deque()
        self._sum_y  = 0.0   # Σ y_i
        self._sum_iy = 0.0   # Σ i·y_i  (i = age-ordered index 0..n−1)

    def push(self, y: float):
        n = len(self._vals)
        if n >= self.window:
            old = self._vals.popleft()
            # old sat at index 0, contributing 0·old to Σi·y — remove it from
            # Σy FIRST, then shift every remaining index down by one:
            #   Σ_{i≥1} (i−1)·y = Σ i·y − Σ_{i≥1} y
            self._sum_y  -= old
            self._sum_iy -= self._sum_y
            n -= 1
        # sum_iy currently indexed 0..n−1; append new value at index n
        self._vals.append(y)
        self._sum_iy += n * y
        self._sum_y  += y

    def slope(self) -> float | None:
        n = len(self._vals)
        if n < 3:
            return None
        sum_x  = n * (n - 1) / 2.0
        sum_x2 = (n - 1) * n * (2 * n - 1) / 6.0
        den = n * sum_x2 - sum_x * sum_x
        if den == 0:
            return None
        return (n * self._sum_iy - sum_x * self._sum_y) / den

    def reset(self):
        self._vals.clear()
        self._sum_y = self._sum_iy = 0.0

    def __len__(self):
        return len(self._vals)


class RollingATR:
    """
    O(1) mean of |Δprice| over the last `window` diffs
    (the bot's tick-ATR definition: sum(|p_i − p_{i−1}|) / n).
    """

    def __init__(self, window: int):
        self.window = int(window)
        self._diffs: deque = deque()
        self._last: float | None = None
        self._sum_abs = 0.0

    def push(self, price: float):
        if self._last is not None:
            d = abs(price - self._last)
            self._diffs.append(d)
            self._sum_abs += d
            if len(self._diffs) > self.window:
                self._sum_abs -= self._diffs.popleft()
        self._last = price

    def atr(self) -> float | None:
        if not self._diffs:
            return None
        return self._sum_abs / len(self._diffs)

    def reset(self):
        self._diffs.clear()
        self._last = None
        self._sum_abs = 0.0

    def __len__(self):
        return len(self._diffs)


class WilderRSI:
    """
    Incremental RSI. For the first `period` diffs it matches the bot's
    simple-average RSI; afterwards it uses Wilder smoothing (the standard
    incremental formulation). O(1) per tick.
    """

    def __init__(self, period: int = 14):
        self.period = int(period)
        self._last: float | None = None
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._n_diffs = 0

    def push(self, price: float):
        if self._last is None:
            self._last = price
            return
        change = price - self._last
        self._last = price
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._n_diffs += 1
        if self._n_diffs <= self.period:
            # accumulation phase — simple average over first `period` diffs
            self._avg_gain += (gain - self._avg_gain) / self._n_diffs
            self._avg_loss += (loss - self._avg_loss) / self._n_diffs
        else:
            p = self.period
            self._avg_gain = (self._avg_gain * (p - 1) + gain) / p
            self._avg_loss = (self._avg_loss * (p - 1) + loss) / p

    def rsi(self) -> float:
        if self._n_diffs < self.period:
            return 50.0
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def reset(self):
        self._last = None
        self._avg_gain = self._avg_loss = 0.0
        self._n_diffs = 0
