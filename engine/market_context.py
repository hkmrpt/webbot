"""
engine/market_context.py  ──  Complete Market Intelligence Hub
══════════════════════════════════════════════════════════════
Converts raw NIFTY tick stream into every indicator strategies need:

  Candles:    1m, 3m, 5m OHLCV
  VWAP:       Session VWAP + 1.5σ bands (volume-weighted)
  EMAs:       9, 21, 50 on tick stream
  RSI:        14-period Wilder RSI
  ATR:        14-period tick ATR  +  short 20-tick ATR (spike threshold)
  Supertrend: 7-period × 3.0 ATR on 3m candles
  Regime:     trending_up / trending_dn / ranging / volatile / unknown
  ORB:        9:15–9:30 opening range tracker
  Slope:      20-tick linear regression slope (fast momentum proxy)
  Velocity:   avg |Δprice| per tick over last 10 ticks
"""

import math
from collections import deque
from datetime import datetime, time as dtime
from dataclasses import dataclass, field
from typing import Optional, List


# ══════════════════════════════════════════════════════════════
# Candle
# ══════════════════════════════════════════════════════════════

@dataclass
class Candle:
    ts:     datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int   = 0
    ticks:  int   = 0

    @property
    def body(self)  -> float: return abs(self.close - self.open)
    @property
    def range(self) -> float: return self.high  - self.low
    @property
    def is_bullish(self) -> bool: return self.close >= self.open


class CandleBuilder:
    """Converts tick stream to OHLCV candles for given timeframe (minutes)."""

    def __init__(self, tf_minutes: int = 1, maxlen: int = 200):
        self.tf = tf_minutes
        self.candles: deque = deque(maxlen=maxlen)
        self._current: Optional[Candle] = None

    def _floor(self, ts: datetime) -> datetime:
        total = ts.hour * 60 + ts.minute
        floored = (total // self.tf) * self.tf
        return ts.replace(hour=floored // 60, minute=floored % 60,
                          second=0, microsecond=0)

    def add_tick(self, price: float, volume: int = 0,
                 ts: Optional[datetime] = None) -> Optional[Candle]:
        """Returns the just-completed candle (if any), else None."""
        if ts is None:
            ts = datetime.now()
        cts = self._floor(ts)
        completed = None

        if self._current is None:
            self._current = Candle(cts, price, price, price, price, volume, 1)
        elif cts != self._current.ts:
            completed = self._current
            self.candles.append(completed)
            self._current = Candle(cts, price, price, price, price, volume, 1)
        else:
            c = self._current
            c.high   = max(c.high, price)
            c.low    = min(c.low,  price)
            c.close  = price
            c.volume += volume
            c.ticks  += 1
        return completed

    def get_candles(self, n: int = None) -> List[Candle]:
        result = list(self.candles)
        if self._current:
            result.append(self._current)
        return result[-n:] if n else result


# ══════════════════════════════════════════════════════════════
# VWAP
# ══════════════════════════════════════════════════════════════

class VWAPCalculator:
    """Session VWAP with upper/lower σ-bands (resets each session)."""

    def __init__(self, band_mult: float = 1.5):
        self.band_mult = band_mult
        self._reset()

    def _reset(self):
        self._cum_pv   = 0.0
        self._cum_v    = 0
        self._cum_pv2  = 0.0
        self.vwap      = None
        self.upper     = None
        self.lower     = None

    def reset(self):
        self._reset()

    def add_tick(self, price: float, volume: int = 1):
        if volume <= 0:
            volume = 1
        self._cum_pv  += price * volume
        self._cum_v   += volume
        self._cum_pv2 += price * price * volume

        self.vwap = self._cum_pv / self._cum_v
        variance  = max(0.0, self._cum_pv2 / self._cum_v - self.vwap ** 2)
        std       = math.sqrt(variance)
        self.upper = self.vwap + self.band_mult * std
        self.lower = self.vwap - self.band_mult * std

    @property
    def is_ready(self) -> bool:
        return self.vwap is not None and self._cum_v > 200


# ══════════════════════════════════════════════════════════════
# Indicators
# ══════════════════════════════════════════════════════════════

class EMA:
    """Exponential Moving Average."""

    def __init__(self, period: int):
        self.period = period
        self.k      = 2.0 / (period + 1)
        self.value: Optional[float] = None
        self._n     = 0
        self._sum   = 0.0

    def update(self, price: float) -> Optional[float]:
        self._n += 1
        if self._n <= self.period:
            self._sum += price
            if self._n == self.period:
                self.value = self._sum / self.period
        elif self.value is not None:
            self.value = price * self.k + self.value * (1 - self.k)
        return self.value


class RSI:
    """RSI with Wilder smoothing."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: Optional[float] = None
        self._prev  = None
        self._ag    = None  # avg gain
        self._al    = None  # avg loss
        self._n     = 0
        self._gains: deque = deque(maxlen=period)
        self._losses: deque = deque(maxlen=period)

    def update(self, price: float) -> Optional[float]:
        if self._prev is None:
            self._prev = price
            return None
        chg = price - self._prev
        self._prev = price
        self._n   += 1
        g = max(0.0, chg)
        l = max(0.0, -chg)

        if self._n <= self.period:
            self._gains.append(g)
            self._losses.append(l)
            if self._n == self.period:
                self._ag = sum(self._gains)  / self.period
                self._al = sum(self._losses) / self.period
                rs = self._ag / (self._al + 1e-10)
                self.value = 100 - 100 / (1 + rs)
        else:
            self._ag = (self._ag * (self.period - 1) + g) / self.period
            self._al = (self._al * (self.period - 1) + l) / self.period
            rs = self._ag / (self._al + 1e-10)
            self.value = 100 - 100 / (1 + rs)

        return self.value


class ATRTick:
    """ATR computed from tick-to-tick absolute changes."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: Optional[float] = None
        self._prev  = None
        self._buf: deque = deque(maxlen=period)

    def update(self, price: float) -> Optional[float]:
        if self._prev is not None:
            self._buf.append(abs(price - self._prev))
        self._prev = price
        if len(self._buf) >= max(2, self.period // 2):
            self.value = sum(self._buf) / len(self._buf)
        return self.value


class Supertrend:
    """Supertrend on candle data (period × ATR multiplier)."""

    def __init__(self, period: int = 7, multiplier: float = 3.0):
        self.period = period
        self.mult   = multiplier
        self.direction = 0   # +1 bullish, -1 bearish
        self.value: Optional[float] = None
        self._up: Optional[float] = None
        self._dn: Optional[float] = None
        self._atr_buf: deque = deque(maxlen=period)
        self._prev_close: Optional[float] = None

    def update_candle(self, c: Candle) -> int:
        hl2 = (c.high + c.low) / 2
        tr  = c.high - c.low   # simplified TR (no gap correction needed for NIFTY)
        self._atr_buf.append(tr)
        if len(self._atr_buf) < self.period:
            self._prev_close = c.close
            return self.direction

        atr      = sum(self._atr_buf) / len(self._atr_buf)
        basic_up = hl2 + self.mult * atr
        basic_dn = hl2 - self.mult * atr

        if self._up is None:
            self._up = basic_up
            self._dn = basic_dn
        else:
            prev_close = self._prev_close or c.close
            self._up = basic_up if (basic_up < self._up or prev_close > self._up) else self._up
            self._dn = basic_dn if (basic_dn > self._dn or prev_close < self._dn) else self._dn

        self._prev_close = c.close

        if self.direction <= 0 and c.close > self._up:
            self.direction = 1
        elif self.direction >= 0 and c.close < self._dn:
            self.direction = -1

        self.value = self._dn if self.direction == 1 else self._up
        return self.direction


# ══════════════════════════════════════════════════════════════
# Regime Detector
# ══════════════════════════════════════════════════════════════

class RegimeDetector:
    """
    Classifies market regime from EMA alignment + ATR.
      trending_up   — EMA9 > EMA21 > EMA50
      trending_dn   — EMA9 < EMA21 < EMA50
      volatile      — ATR significantly above its own 50-tick average
      ranging       — EMAs interleaved, ATR normal
      unknown       — insufficient data
    """

    def __init__(self):
        self._e9  = EMA(9)
        self._e21 = EMA(21)
        self._e50 = EMA(50)
        self._atr = ATRTick(14)
        self._atr_hist: deque = deque(maxlen=50)
        self.regime = "unknown"

    def update(self, price: float) -> str:
        e9  = self._e9.update(price)
        e21 = self._e21.update(price)
        e50 = self._e50.update(price)
        atr = self._atr.update(price)

        if atr:
            self._atr_hist.append(atr)

        if e9 is None or e21 is None or e50 is None or not self._atr_hist:
            return "unknown"

        avg_atr   = sum(self._atr_hist) / len(self._atr_hist)
        atr_ratio = atr / avg_atr if avg_atr > 0 else 1.0

        if atr_ratio > 1.8:
            self.regime = "volatile"
        elif e9 > e21 > e50:
            self.regime = "trending_up"
        elif e9 < e21 < e50:
            self.regime = "trending_dn"
        else:
            self.regime = "ranging"

        return self.regime


# ══════════════════════════════════════════════════════════════
# ORB Tracker
# ══════════════════════════════════════════════════════════════

class ORBTracker:
    """Tracks Opening Range (9:15 – orb_end_m) high/low."""

    def __init__(self, orb_end_h: int = 9, orb_end_m: int = 30):
        self._end   = dtime(orb_end_h, orb_end_m)
        self._open  = dtime(9, 15)
        self.high:   Optional[float] = None
        self.low:    Optional[float] = None
        self.formed: bool = False
        self._today = None

    def update(self, price: float):
        now   = datetime.now()
        today = now.date()
        if today != self._today:
            self.high   = None
            self.low    = None
            self.formed = False
            self._today = today

        t = now.time()
        if self._open <= t <= self._end:
            self.high = max(self.high, price) if self.high else price
            self.low  = min(self.low,  price) if self.low  else price
        elif self.high and self.low and t > self._end:
            self.formed = True

    @property
    def range_pts(self) -> Optional[float]:
        if self.high and self.low:
            return self.high - self.low
        return None


# ══════════════════════════════════════════════════════════════
# MarketContext  (central hub)
# ══════════════════════════════════════════════════════════════

class MarketContext:
    """
    Single object passed to every strategy on each tick.
    Call on_tick() with each NIFTY price update.
    """

    def __init__(self):
        # Candle builders
        self.candles_1m = CandleBuilder(1,  200)
        self.candles_3m = CandleBuilder(3,  120)
        self.candles_5m = CandleBuilder(5,  100)

        # VWAP
        self.vwap = VWAPCalculator(band_mult=1.5)

        # EMAs on tick stream
        self.ema9  = EMA(9)
        self.ema21 = EMA(21)
        self.ema50 = EMA(50)

        # RSI
        self.rsi14 = RSI(14)

        # ATR – two timeframes
        self.atr_long  = ATRTick(14)   # 14-tick  (position sizing)
        self.atr_short = ATRTick(20)   # 20-tick  (spike threshold)

        # Supertrend on 3m candles
        self.supertrend     = Supertrend(7, 3.0)
        self.supertrend_dir = 0

        # Regime
        self.regime_det = RegimeDetector()
        self.regime     = "unknown"

        # ORB
        self.orb = ORBTracker(9, 30)

        # Price / velocity buffers
        self.ticks: deque    = deque(maxlen=200)
        self._vel_buf: deque = deque(maxlen=10)
        self._prev: Optional[float] = None
        self.velocity = 0.0

        # Session open price (first tick after 9:15)
        self.session_open: Optional[float] = None

    # ── Main update ───────────────────────────────────────────

    def on_tick(self, price: float, volume: int = 0) -> "MarketContext":
        now = datetime.now()
        self.ticks.append(price)

        # Record session open
        if self.session_open is None and now.time() >= dtime(9, 15):
            self.session_open = price

        # VWAP
        self.vwap.add_tick(price, volume)

        # EMAs
        self.ema9.update(price)
        self.ema21.update(price)
        self.ema50.update(price)

        # RSI
        self.rsi14.update(price)

        # ATR
        self.atr_long.update(price)
        self.atr_short.update(price)

        # Regime
        self.regime = self.regime_det.update(price)

        # ORB
        self.orb.update(price)

        # Velocity
        if self._prev is not None:
            delta = abs(price - self._prev)
            self._vel_buf.append(delta)
            self.velocity = sum(self._vel_buf) / len(self._vel_buf)
        self._prev = price

        # Candles
        c3 = self.candles_3m.add_tick(price, volume, now)
        self.candles_1m.add_tick(price, volume, now)
        self.candles_5m.add_tick(price, volume, now)

        if c3:
            self.supertrend_dir = self.supertrend.update_candle(c3)

        return self

    # ── Derived helpers ───────────────────────────────────────

    @property
    def slope_20(self) -> Optional[float]:
        """Linear regression slope of last 20 NIFTY ticks."""
        if len(self.ticks) < 20:
            return None
        pts  = list(self.ticks)[-20:]
        n    = len(pts)
        mx   = (n - 1) / 2
        my   = sum(pts) / n
        num  = sum((i - mx) * (pts[i] - my) for i in range(n))
        den  = sum((i - mx) ** 2 for i in range(n))
        return num / den if den else None

    @property
    def spike_atr(self) -> Optional[float]:
        return self.atr_short.value

    @property
    def position_atr(self) -> Optional[float]:
        return self.atr_long.value

    def above_vwap(self, price: float) -> bool:
        return self.vwap.is_ready and price > (self.vwap.vwap or 0)

    def below_vwap(self, price: float) -> bool:
        return self.vwap.is_ready and price < (self.vwap.vwap or 0)

    def session_reset(self):
        self.vwap.reset()
        self.session_open  = None
        self.candles_1m    = CandleBuilder(1, 200)
        self.candles_3m    = CandleBuilder(3, 120)
        self.candles_5m    = CandleBuilder(5, 100)

    # ── Snapshot for UI ───────────────────────────────────────

    def snapshot(self) -> dict:
        c1 = self.candles_1m.get_candles(2)
        c3 = self.candles_3m.get_candles(2)
        c5 = self.candles_5m.get_candles(2)

        def _c(candles):
            if not candles:
                return None
            c = candles[-1]
            return {"o": c.open, "h": c.high, "l": c.low, "c": c.close,
                    "v": c.volume}

        return {
            "vwap":           round(self.vwap.vwap,  2) if self.vwap.vwap  else None,
            "vwap_upper":     round(self.vwap.upper, 2) if self.vwap.upper else None,
            "vwap_lower":     round(self.vwap.lower, 2) if self.vwap.lower else None,
            "ema9":           round(self.ema9.value,  2) if self.ema9.value  else None,
            "ema21":          round(self.ema21.value, 2) if self.ema21.value else None,
            "ema50":          round(self.ema50.value, 2) if self.ema50.value else None,
            "rsi14":          round(self.rsi14.value, 1) if self.rsi14.value else None,
            "atr_long":       round(self.atr_long.value,  3) if self.atr_long.value  else None,
            "atr_short":      round(self.atr_short.value, 3) if self.atr_short.value else None,
            "slope_20":       round(self.slope_20, 4) if self.slope_20 else None,
            "velocity":       round(self.velocity, 3),
            "regime":         self.regime,
            "supertrend_dir": self.supertrend_dir,
            "orb_high":       self.orb.high,
            "orb_low":        self.orb.low,
            "orb_formed":     self.orb.formed,
            "orb_range":      self.orb.range_pts,
            "candle_1m":      _c(c1),
            "candle_3m":      _c(c3),
            "candle_5m":      _c(c5),
        }
