"""
engine/strategies.py  ──  All Trading Strategies
══════════════════════════════════════════════════
Six independent strategies, each inheriting BaseStrategy.

  1. SpikeScalper     — ATR spike on NIFTY with VWAP + regime filter
  2. ORBStrategy      — Opening Range Breakout (9:15–9:30)
  3. VWAPBounce       — NIFTY touches VWAP, bounces in trend direction
  4. VWAPBandBreak    — Price breaks VWAP σ-band with momentum
  5. TrendPullback    — EMA9 pullback entry in Supertrend direction
  6. MomentumRSI      — RSI burst from neutral zone 45–55

Each on_tick() returns a Signal or None.
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from collections import deque
from typing import Optional, Dict


# ══════════════════════════════════════════════════════════════
# Signal
# ══════════════════════════════════════════════════════════════

@dataclass
class Signal:
    strategy:    str
    side:        str           # "CE" or "PE"
    confidence:  float         # 0.0–1.0
    reason:      str
    sl_pct:      float         # suggested SL %
    target_pct:  float         # suggested target %
    urgency:     str = "normal"  # "fast" | "normal"
    meta:        Dict = field(default_factory=dict)

    def __repr__(self):
        return (f"Signal({self.strategy} {self.side} conf={self.confidence:.2f} "
                f"'{self.reason[:60]}')")


# ══════════════════════════════════════════════════════════════
# Base
# ══════════════════════════════════════════════════════════════

class BaseStrategy:
    name = "base"

    def __init__(self, rc: dict):
        self.rc      = rc
        self.enabled = True
        self._wins   = 0
        self._losses = 0
        self._last_signal_ts: Optional[datetime] = None
        self._cooldown_secs = 30

    def _in_cooldown(self) -> bool:
        if not self._last_signal_ts:
            return False
        return (datetime.now() - self._last_signal_ts).total_seconds() < self._cooldown_secs

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        raise NotImplementedError

    def on_trade_closed(self, result: dict):
        if result.get("pnl", 0) >= 0:
            self._wins += 1
        else:
            self._losses += 1

    @property
    def stats(self) -> dict:
        total = self._wins + self._losses
        wr = self._wins / total if total else 0.0
        return {"wins": self._wins, "losses": self._losses, "win_rate": round(wr, 3)}


# ══════════════════════════════════════════════════════════════
# 1. Spike Scalper
# ══════════════════════════════════════════════════════════════

class SpikeScalper(BaseStrategy):
    """
    Detects fast NIFTY price spikes using short ATR as dynamic threshold.
    Enhanced with regime alignment, VWAP direction, and RSI overbought filter.
    Fast entry if slope + momentum strong; otherwise waits for confirmation ticks.
    """

    name = "spike"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs  = 45
        self._prev:          Optional[float] = None
        self._pending_side:  Optional[str]   = None
        self._confirm_count: int             = 0
        self._spike_ref:     Optional[float] = None

    def _threshold(self, ctx) -> float:
        atr = ctx.spike_atr
        if atr is None:
            return float(self.rc.get("jump_min_pts", 4.0))
        raw = atr * float(self.rc.get("jump_atr_multiplier", 1.2))
        return max(
            float(self.rc.get("jump_min_pts", 4.0)),
            min(float(self.rc.get("jump_max_pts", 20.0)), raw),
        )

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        if self._prev is None:
            self._prev = price
            return None

        move = price - self._prev
        self._prev = price

        # ── Continuation confirmation ─────────────────────────
        if self._pending_side is not None:
            self._confirm_count += 1
            net  = price - self._spike_ref
            hold = self._threshold(ctx) * float(self.rc.get("confirm_sustain_pct", 0.70))
            ok   = (net >= hold) if self._pending_side == "CE" else (net <= -hold)
            if not ok:
                self._pending_side = None
                return None
            if self._confirm_count >= 3:
                side = self._pending_side
                self._pending_side = None
                return self._emit(side, price, ctx,
                                  f"Spike CONFIRMED({self._confirm_count}t)  Δ={move:+.2f}",
                                  0.74, "normal")
            return None

        # ── Detect new spike ──────────────────────────────────
        thresh = self._threshold(ctx)
        if abs(move) < thresh:
            return None

        side = "CE" if move > 0 else "PE"

        # Regime filter: don't trade against clear trend
        if side == "CE" and ctx.regime == "trending_dn":
            return None
        if side == "PE" and ctx.regime == "trending_up":
            return None

        # RSI extreme filter
        rsi = ctx.rsi14.value
        if rsi is not None:
            if side == "CE" and rsi > 78:
                return None   # overbought
            if side == "PE" and rsi < 22:
                return None   # oversold

        if self._in_cooldown():
            return None

        slope = ctx.slope_20
        slope_strong = (
            (slope is not None) and
            ((side == "CE" and slope > 0.25) or (side == "PE" and slope < -0.25))
        )

        if slope_strong:
            vwap_ok = ctx.above_vwap(price) if side == "CE" else ctx.below_vwap(price)
            conf    = 0.85 if vwap_ok else 0.72
            return self._emit(side, price, ctx,
                              f"Spike FAST  Δ={move:+.2f}  thr={thresh:.1f}  slope={slope:+.3f}  vwap={'✓' if vwap_ok else '✗'}",
                              conf, "fast")

        # Queue for confirmation
        self._pending_side  = side
        self._confirm_count = 1
        self._spike_ref     = price
        return None

    def _emit(self, side: str, price: float, ctx, reason: str,
              conf: float, urgency: str) -> Optional[Signal]:
        if self._in_cooldown():
            return None
        self._last_signal_ts = datetime.now()
        return Signal(
            strategy=self.name, side=side, confidence=conf,
            reason=reason,
            sl_pct=float(self.rc.get("sl_phase1_pct", 15.0)),
            target_pct=30.0, urgency=urgency,
        )


# ══════════════════════════════════════════════════════════════
# 2. Opening Range Breakout
# ══════════════════════════════════════════════════════════════

class ORBStrategy(BaseStrategy):
    """
    9:15–9:30 range forms the ORB.
    Buy CE on break above ORB_high + buffer.
    Buy PE on break below ORB_low  - buffer.
    Valid until 11:00 AM; each direction fires once per day.
    """

    name = "orb"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs = 120
        self._ce_fired  = False
        self._pe_fired  = False
        self._today     = None

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        today = datetime.now().date()
        if today != self._today:
            self._today     = today
            self._ce_fired  = False
            self._pe_fired  = False

        if not ctx.orb.formed:
            return None

        rng = ctx.orb.range_pts
        if rng is None or rng < 20 or rng > 200:
            return None

        if datetime.now().time() > dtime(11, 0):
            return None

        if self._in_cooldown():
            return None

        buf = max(3.0, rng * 0.05)  # 5% of ORB range as breakout buffer

        # CE breakout
        if not self._ce_fired and price > ctx.orb.high + buf:
            if ctx.regime != "trending_dn":
                slope = ctx.slope_20
                if slope and slope > 0:
                    self._ce_fired = True
                    self._last_signal_ts = datetime.now()
                    target_pct = min(40.0, rng * 0.4)  # ~40% of ORB range, capped
                    return Signal(
                        strategy=self.name, side="CE", confidence=0.80,
                        reason=(f"ORB breakout CE  price={price:.2f}  "
                                f"orb_high={ctx.orb.high:.2f}  range={rng:.1f}pts"),
                        sl_pct=12.0, target_pct=target_pct, urgency="normal",
                        meta={"orb_high": ctx.orb.high, "orb_low": ctx.orb.low,
                              "orb_range": rng},
                    )

        # PE breakout
        if not self._pe_fired and price < ctx.orb.low - buf:
            if ctx.regime != "trending_up":
                slope = ctx.slope_20
                if slope and slope < 0:
                    self._pe_fired = True
                    self._last_signal_ts = datetime.now()
                    target_pct = min(40.0, rng * 0.4)
                    return Signal(
                        strategy=self.name, side="PE", confidence=0.80,
                        reason=(f"ORB breakout PE  price={price:.2f}  "
                                f"orb_low={ctx.orb.low:.2f}  range={rng:.1f}pts"),
                        sl_pct=12.0, target_pct=target_pct, urgency="normal",
                        meta={"orb_high": ctx.orb.high, "orb_low": ctx.orb.low,
                              "orb_range": rng},
                    )

        return None


# ══════════════════════════════════════════════════════════════
# 3. VWAP Bounce
# ══════════════════════════════════════════════════════════════

class VWAPBounce(BaseStrategy):
    """
    NIFTY approaches VWAP (within 0.5 ATR), then departs in the trend direction.
    CE: NIFTY was above VWAP, pulled to VWAP, bounced back up.
    PE: NIFTY was below VWAP, rallied to VWAP, rejected back down.
    """

    name = "vwap_bounce"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs = 60
        self._near_vwap     = False
        self._approach_side: Optional[str] = None  # which side approached
        self._near_count    = 0

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        if not ctx.vwap.is_ready:
            return None

        vwap = ctx.vwap.vwap
        atr  = ctx.spike_atr
        if not vwap or not atr:
            return None

        zone = 0.5 * atr
        near = abs(price - vwap) <= zone

        if near:
            if not self._near_vwap:
                # Just entered the zone — figure out which side we came from
                e9 = ctx.ema9.value
                if e9 and e9 > vwap:
                    self._approach_side = "CE"   # was above, expect bounce
                elif e9 and e9 < vwap:
                    self._approach_side = "PE"   # was below, expect rejection
                else:
                    self._approach_side = None
                self._near_vwap  = True
                self._near_count = 0
            self._near_count += 1
            return None

        # Left the zone
        if self._near_vwap and self._near_count >= 2:
            side = None
            if price > vwap + zone and self._approach_side == "CE":
                side = "CE"
            elif price < vwap - zone and self._approach_side == "PE":
                side = "PE"

            self._near_vwap = False

            if side and not self._in_cooldown():
                # Skip ranging market (VWAP bounce works in trend)
                if ctx.regime == "ranging" and self._near_count > 8:
                    return None
                rsi = ctx.rsi14.value
                if rsi:
                    if side == "CE" and rsi > 72: return None
                    if side == "PE" and rsi < 28:  return None
                self._last_signal_ts = datetime.now()
                return Signal(
                    strategy=self.name, side=side, confidence=0.73,
                    reason=(f"VWAP bounce {side}  price={price:.2f}  "
                            f"vwap={vwap:.2f}  zone={zone:.1f}"),
                    sl_pct=10.0, target_pct=20.0, urgency="normal",
                )
        else:
            self._near_vwap = False

        return None


# ══════════════════════════════════════════════════════════════
# 4. VWAP Band Break
# ══════════════════════════════════════════════════════════════

class VWAPBandBreak(BaseStrategy):
    """
    Price breaks outside VWAP 1.5σ band with strong momentum slope.
    High-confidence fast move — urgent entry.
    Only re-arms after price returns inside bands.
    """

    name = "vwap_band"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs = 90
        self._was_inside    = True

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        if not ctx.vwap.is_ready:
            return None

        upper = ctx.vwap.upper
        lower = ctx.vwap.lower
        if upper is None or lower is None:
            return None

        inside = lower <= price <= upper

        if inside:
            self._was_inside = True
            return None

        if not self._was_inside or self._in_cooldown():
            return None

        slope = ctx.slope_20

        if price > upper and slope and slope > 0.3:
            self._was_inside = False
            self._last_signal_ts = datetime.now()
            return Signal(
                strategy=self.name, side="CE", confidence=0.82,
                reason=(f"VWAP upper-band break  price={price:.2f}  "
                        f"upper={upper:.2f}  slope={slope:+.3f}"),
                sl_pct=11.0, target_pct=26.0, urgency="fast",
            )

        if price < lower and slope and slope < -0.3:
            self._was_inside = False
            self._last_signal_ts = datetime.now()
            return Signal(
                strategy=self.name, side="PE", confidence=0.82,
                reason=(f"VWAP lower-band break  price={price:.2f}  "
                        f"lower={lower:.2f}  slope={slope:+.3f}"),
                sl_pct=11.0, target_pct=26.0, urgency="fast",
            )

        return None


# ══════════════════════════════════════════════════════════════
# 5. Trend EMA Pullback
# ══════════════════════════════════════════════════════════════

def _candle_ema(closes: list, period: int) -> Optional[float]:
    """EMA of a candle-close list. Returns None if not enough data."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _candle_atr(candles) -> Optional[float]:
    """Average candle range over last N candles."""
    if len(candles) < 3:
        return None
    ranges = [c.range for c in candles[-10:] if c.range > 0]
    return sum(ranges) / len(ranges) if ranges else None


class TrendPullback(BaseStrategy):
    """
    Trend-following pullback on 3m candles.
    Uses EMA9 and EMA21 computed from 3m candle closes — NOT tick-stream EMAs
    (tick EMAs track price instantly, making pullback zones meaningless).

    Logic:
      Uptrend:   3m EMA9 > EMA21 + Supertrend bullish
                 → wait for price to pull back within 1.0× candle-ATR of EMA9
                 → bounce confirmed by positive 20-tick slope
      Downtrend: 3m EMA9 < EMA21 + Supertrend bearish → same logic inverted
    """

    name = "trend_pb"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs  = 150
        self._last_candle_ts = None   # avoid re-computing on same candle

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        if ctx.supertrend_dir == 0:
            return None

        # Use 3m candle-based EMAs (meaningful pullback zones)
        candles = ctx.candles_3m.get_candles(30)
        if len(candles) < 23:          # need 21 + 2 for EMA9/21 warm-up
            return None

        closes = [c.close for c in candles]
        e9  = _candle_ema(closes, 9)
        e21 = _candle_ema(closes, 21)
        if e9 is None or e21 is None:
            return None

        # Pullback zone width = 1.0× candle ATR (meaningful distance on 3m bars)
        c_atr = _candle_atr(candles) or 5.0
        slope = ctx.slope_20

        # Uptrend pullback
        if (ctx.supertrend_dir > 0
                and e9 > e21
                and ctx.regime in ("trending_up", "volatile")):
            near = price <= e9 + c_atr and price >= e9 - c_atr
            if near and slope and slope > 0.1:
                if not self._in_cooldown():
                    rsi = ctx.rsi14.value
                    if rsi and rsi > 72:
                        return None
                    self._last_signal_ts = datetime.now()
                    return Signal(
                        strategy=self.name, side="CE", confidence=0.76,
                        reason=(f"Trend CE pullback  3m EMA9={e9:.2f}  "
                                f"price={price:.2f}  zone±{c_atr:.1f}  super=+"),
                        sl_pct=10.0, target_pct=22.0, urgency="normal",
                    )

        # Downtrend pullback
        if (ctx.supertrend_dir < 0
                and e9 < e21
                and ctx.regime in ("trending_dn", "volatile")):
            near = price >= e9 - c_atr and price <= e9 + c_atr
            if near and slope and slope < -0.1:
                if not self._in_cooldown():
                    rsi = ctx.rsi14.value
                    if rsi and rsi < 28:
                        return None
                    self._last_signal_ts = datetime.now()
                    return Signal(
                        strategy=self.name, side="PE", confidence=0.76,
                        reason=(f"Trend PE pullback  3m EMA9={e9:.2f}  "
                                f"price={price:.2f}  zone±{c_atr:.1f}  super=-"),
                        sl_pct=10.0, target_pct=22.0, urgency="normal",
                    )

        return None


# ══════════════════════════════════════════════════════════════
# 6. Momentum RSI Burst
# ══════════════════════════════════════════════════════════════

class MomentumRSI(BaseStrategy):
    """
    RSI bursts out of the neutral zone (45–55) in one direction
    while slope confirms.  Early signal before price fully moves.
    """

    name = "rsi_burst"

    def __init__(self, rc: dict):
        super().__init__(rc)
        self._cooldown_secs = 90
        self._prev_rsi: Optional[float] = None
        self._neutral_count = 0

    def on_tick(self, price: float, ctx, state: dict) -> Optional[Signal]:
        rsi = ctx.rsi14.value
        if rsi is None:
            return None

        if self._prev_rsi is None:
            self._prev_rsi = rsi
            return None

        if 45 <= rsi <= 55:
            self._neutral_count += 1
        else:
            self._neutral_count = 0

        sig = None

        # CE burst
        if self._prev_rsi < 60 <= rsi and self._neutral_count >= 5:
            if ctx.regime not in ("trending_dn",):
                slope = ctx.slope_20
                if slope and slope > 0.2 and not self._in_cooldown():
                    self._last_signal_ts = datetime.now()
                    sig = Signal(
                        strategy=self.name, side="CE", confidence=0.70,
                        reason=(f"RSI burst CE  rsi={rsi:.1f}↑  "
                                f"slope={slope:+.3f}  neutral_ticks={self._neutral_count}"),
                        sl_pct=12.0, target_pct=20.0, urgency="normal",
                    )

        # PE burst
        elif self._prev_rsi > 40 >= rsi and self._neutral_count >= 5:
            if ctx.regime not in ("trending_up",):
                slope = ctx.slope_20
                if slope and slope < -0.2 and not self._in_cooldown():
                    self._last_signal_ts = datetime.now()
                    sig = Signal(
                        strategy=self.name, side="PE", confidence=0.70,
                        reason=(f"RSI burst PE  rsi={rsi:.1f}↓  "
                                f"slope={slope:+.3f}  neutral_ticks={self._neutral_count}"),
                        sl_pct=12.0, target_pct=20.0, urgency="normal",
                    )

        self._prev_rsi = rsi
        return sig


# ══════════════════════════════════════════════════════════════
# Strategy Registry helper
# ══════════════════════════════════════════════════════════════

ALL_STRATEGIES = [
    SpikeScalper,
    ORBStrategy,
    VWAPBounce,
    VWAPBandBreak,
    TrendPullback,
    MomentumRSI,
]


def build_strategies(rc: dict) -> list:
    """Instantiate all strategies with current RC config."""
    return [cls(rc) for cls in ALL_STRATEGIES]
