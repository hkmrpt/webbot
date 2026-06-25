"""
option_chain_intel.py  ──  Option Buy Robot v9
═══════════════════════════════════════════════

Option Chain Intelligence + VIX Integration.

Provides:
  1. PCR (Put-Call Ratio) from nearby strikes OI
  2. Max Pain calculation
  3. OI change detection (smart money positioning)
  4. IV regime classification
  5. VIX-based market fear scoring
  6. Combined option intelligence score for entry decisions
"""

import json
import math
import urllib.request
import urllib.parse
from datetime import datetime


VIX_TOKEN = 264969  # India VIX instrument token


class VIXTracker:
    """
    Tracks India VIX from live ticks.

    VIX levels:
      < 12  = very calm (avoid buying options — low premium, small moves)
      12-15 = normal-low (standard trading)
      15-20 = normal-high (good for option buying)
      20-25 = elevated fear (bigger moves, tighter stops)
      > 25  = panic (very wide moves, reduce size)
    """

    def __init__(self):
        self.current = None
        self.prev = None
        self.day_high = None
        self.day_low = None
        self._history = []  # last 100 ticks

    def on_tick(self, price: float):
        """Called when a VIX tick arrives."""
        self.prev = self.current
        self.current = price
        if self.day_high is None or price > self.day_high:
            self.day_high = price
        if self.day_low is None or price < self.day_low:
            self.day_low = price
        self._history.append(price)
        if len(self._history) > 100:
            self._history = self._history[-100:]

    def score(self) -> dict:
        """
        Score current VIX for option buying suitability.
        Returns score (0-1), regime, and recommendation.
        """
        if self.current is None:
            return {"score": 0.5, "regime": "unknown", "vix": 0,
                    "recommendation": "no_data", "trend": "flat"}

        v = self.current

        if v < 10:
            score = 0.15
            regime = "dead_calm"
            rec = "avoid"  # options too cheap, no movement expected
        elif v < 13:
            score = 0.5
            regime = "low"
            rec = "cautious"
        elif v < 16:
            score = 0.85
            regime = "normal"
            rec = "optimal"  # sweet spot for option buying
        elif v < 20:
            score = 0.75
            regime = "elevated"
            rec = "good"  # bigger moves but more risk
        elif v < 25:
            score = 0.55
            regime = "high"
            rec = "reduce_size"
        else:
            score = 0.3
            regime = "panic"
            rec = "minimal"  # very risky

        # VIX trend (rising = expect bigger moves, falling = calming)
        trend = "flat"
        if len(self._history) >= 10:
            recent_avg = sum(self._history[-5:]) / 5
            older_avg = sum(self._history[-10:-5]) / 5
            if recent_avg > older_avg * 1.05:
                trend = "rising"
            elif recent_avg < older_avg * 0.95:
                trend = "falling"

        return {
            "score": round(score, 3),
            "regime": regime,
            "vix": round(v, 2),
            "day_high": round(self.day_high, 2) if self.day_high else 0,
            "day_low": round(self.day_low, 2) if self.day_low else 0,
            "trend": trend,
            "recommendation": rec,
        }

    def reset(self):
        self.current = None
        self.prev = None
        self.day_high = None
        self.day_low = None
        self._history = []


class OptionChainAnalyzer:
    """
    Analyzes option chain data for trading intelligence.

    Computes:
      - PCR (Put-Call Ratio) from OI of nearby strikes
      - Max Pain strike
      - OI concentration analysis
      - Directional bias from option activity
    """

    def __init__(self):
        self._chain_data = {}  # {strike: {ce_oi, pe_oi, ce_vol, pe_vol, ce_price, pe_price}}
        self._last_fetch = 0
        self._pcr = None
        self._max_pain = None

    def update_from_quotes(self, quotes: dict, atm_strike: float, step: int = 50):
        """
        Update chain data from Zerodha quote responses.

        quotes: dict of {instrument_key: {last_price, oi, volume, ...}}
        """
        self._chain_data = {}

        for key, data in quotes.items():
            if not key.startswith("NFO:"):
                continue
            sym = key.replace("NFO:", "")
            oi = data.get("oi", 0) or 0
            vol = data.get("volume", 0) or 0
            ltp = data.get("last_price", 0) or 0

            # Parse strike and type from symbol
            is_ce = sym.endswith("CE")
            is_pe = sym.endswith("PE")
            if not (is_ce or is_pe):
                continue

            # Extract strike (last digits before CE/PE)
            try:
                strike_str = ""
                sym_body = sym[:-2]  # remove CE/PE
                for ch in reversed(sym_body):
                    if ch.isdigit() or ch == '.':
                        strike_str = ch + strike_str
                    else:
                        break
                strike = float(strike_str) if strike_str else 0
            except (ValueError, IndexError):
                continue

            if strike not in self._chain_data:
                self._chain_data[strike] = {
                    "ce_oi": 0, "pe_oi": 0, "ce_vol": 0, "pe_vol": 0,
                    "ce_price": 0, "pe_price": 0
                }

            if is_ce:
                self._chain_data[strike]["ce_oi"] = oi
                self._chain_data[strike]["ce_vol"] = vol
                self._chain_data[strike]["ce_price"] = ltp
            else:
                self._chain_data[strike]["pe_oi"] = oi
                self._chain_data[strike]["pe_vol"] = vol
                self._chain_data[strike]["pe_price"] = ltp

        self._compute_metrics(atm_strike)

    def update_simple(self, ce_oi: int, pe_oi: int, ce_vol: int, pe_vol: int,
                      ce_price: float, pe_price: float, strike: float):
        """
        Simple update with just ATM CE/PE data (when full chain not available).
        """
        self._chain_data = {
            strike: {
                "ce_oi": ce_oi, "pe_oi": pe_oi,
                "ce_vol": ce_vol, "pe_vol": pe_vol,
                "ce_price": ce_price, "pe_price": pe_price,
            }
        }
        self._compute_metrics(strike)

    def _compute_metrics(self, atm_strike: float):
        """Compute PCR, max pain, and directional bias."""
        if not self._chain_data:
            return

        total_ce_oi = sum(d["ce_oi"] for d in self._chain_data.values())
        total_pe_oi = sum(d["pe_oi"] for d in self._chain_data.values())

        # PCR = Total PE OI / Total CE OI
        self._pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

        # Max Pain: strike where total loss (CE writers + PE writers) is minimum
        if len(self._chain_data) >= 3:
            min_pain = float('inf')
            max_pain_strike = atm_strike
            for test_strike in self._chain_data:
                pain = 0
                for strike, data in self._chain_data.items():
                    # CE buyers lose when price < strike
                    if test_strike < strike:
                        pain += data["ce_oi"] * (strike - test_strike)
                    # PE buyers lose when price > strike
                    if test_strike > strike:
                        pain += data["pe_oi"] * (test_strike - strike)
                if pain < min_pain:
                    min_pain = pain
                    max_pain_strike = test_strike
            self._max_pain = max_pain_strike
        else:
            self._max_pain = atm_strike

    def analyze(self, nifty_price: float, side: str) -> dict:
        """
        Full option chain analysis for entry decision.

        Returns score (0-1) and intelligence.
        """
        if not self._chain_data:
            return {"score": 0.5, "pcr": 1.0, "max_pain": 0,
                    "oi_bias": "neutral", "recommendation": "no_data"}

        pcr = self._pcr or 1.0
        max_pain = self._max_pain or nifty_price

        # PCR interpretation
        # PCR > 1.2 = more puts being bought = bearish sentiment = contrarian bullish
        # PCR < 0.7 = more calls being bought = bullish sentiment = contrarian bearish
        # PCR 0.8-1.2 = neutral
        if pcr >= 1.5:
            oi_bias = "very_bullish"  # heavy put writing = support building
        elif pcr >= 1.2:
            oi_bias = "bullish"
        elif pcr >= 0.8:
            oi_bias = "neutral"
        elif pcr >= 0.5:
            oi_bias = "bearish"
        else:
            oi_bias = "very_bearish"  # heavy call writing = resistance

        # Score based on side alignment with OI bias
        if side == "CE":
            if oi_bias in ("very_bullish", "bullish"):
                score = 0.85  # CE aligns with bullish OI
            elif oi_bias == "neutral":
                score = 0.6
            else:
                score = 0.3   # CE against bearish OI
        else:  # PE
            if oi_bias in ("very_bearish", "bearish"):
                score = 0.85  # PE aligns with bearish OI
            elif oi_bias == "neutral":
                score = 0.6
            else:
                score = 0.3   # PE against bullish OI

        # Max pain proximity — price near max pain = likely to stay, avoid
        if max_pain > 0 and nifty_price > 0:
            dist_from_mp = abs(nifty_price - max_pain) / nifty_price * 100
            if dist_from_mp < 0.3:
                score *= 0.7  # too close to max pain — price gravitates here

        # Total OI concentration
        total_oi = sum(d["ce_oi"] + d["pe_oi"] for d in self._chain_data.values())

        return {
            "score": round(score, 3),
            "pcr": round(pcr, 3),
            "max_pain": max_pain,
            "max_pain_dist": round(abs(nifty_price - max_pain), 1) if max_pain else 0,
            "oi_bias": oi_bias,
            "total_oi": total_oi,
            "ce_oi_total": sum(d["ce_oi"] for d in self._chain_data.values()),
            "pe_oi_total": sum(d["pe_oi"] for d in self._chain_data.values()),
            "recommendation": "aligned" if score >= 0.7 else ("neutral" if score >= 0.5 else "against"),
        }

    @property
    def state(self) -> dict:
        return {
            "pcr": self._pcr,
            "max_pain": self._max_pain,
            "strikes_tracked": len(self._chain_data),
        }


class MultiTimeframeAnalyzer:
    """
    Builds candles on multiple timeframes from tick data and
    determines trend alignment across timeframes.

    Timeframes: 1min, 5min, 15min

    Entry is strongest when all 3 timeframes agree on direction.
    """

    def __init__(self):
        self._ticks = []  # (timestamp, price) tuples
        self._candles_1m = []
        self._candles_5m = []
        self._candles_15m = []

    def add_tick(self, price: float, ts: float = None):
        """Add a price tick with timestamp."""
        if ts is None:
            ts = datetime.now().timestamp()
        self._ticks.append((ts, price))
        # Keep last 2 hours of ticks
        cutoff = ts - 7200
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.pop(0)
        self._rebuild_candles()

    def _rebuild_candles(self):
        """Build 1m, 5m, 15m candles from ticks."""
        self._candles_1m = self._build(60)
        self._candles_5m = self._build(300)
        self._candles_15m = self._build(900)

    def _build(self, period: int) -> list:
        if not self._ticks:
            return []
        buckets = {}
        for ts, price in self._ticks:
            k = int(ts // period) * period
            if k not in buckets:
                buckets[k] = []
            buckets[k].append(price)
        candles = []
        for t in sorted(buckets):
            arr = buckets[t]
            candles.append({
                "t": t, "o": arr[0], "h": max(arr),
                "l": min(arr), "c": arr[-1]
            })
        return candles

    def _trend(self, candles: list, lookback: int = 5) -> str:
        """Determine trend from last N candles."""
        if len(candles) < lookback:
            return "unknown"
        recent = candles[-lookback:]
        closes = [c["c"] for c in recent]

        # Simple: count up vs down closes
        up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down = len(closes) - 1 - up

        # Also check slope
        slope = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0

        if up >= lookback - 1 and slope > 0.05:
            return "strong_up"
        elif up > down and slope > 0:
            return "up"
        elif down >= lookback - 1 and slope < -0.05:
            return "strong_down"
        elif down > up and slope < 0:
            return "down"
        return "sideways"

    def analyze(self, side: str) -> dict:
        """
        Multi-timeframe analysis.

        Returns alignment score (0-1) and per-TF trends.
        """
        t1m = self._trend(self._candles_1m)
        t5m = self._trend(self._candles_5m)
        t15m = self._trend(self._candles_15m)

        trends = {"1m": t1m, "5m": t5m, "15m": t15m}

        # Score based on alignment with entry side
        favorable = {"CE": ["up", "strong_up"], "PE": ["down", "strong_down"]}
        fav = favorable.get(side, [])

        aligned = 0
        total = 0
        for tf, trend in trends.items():
            if trend == "unknown":
                continue
            total += 1
            weight = {"1m": 1, "5m": 2, "15m": 3}.get(tf, 1)
            if trend in fav:
                aligned += weight
                if "strong" in trend:
                    aligned += 0.5  # bonus for strong trend
            elif trend == "sideways":
                aligned += weight * 0.3  # neutral, slight penalty
            # else: opposing trend, no credit

        max_possible = sum([1, 2, 3])  # weights
        score = aligned / max_possible if max_possible > 0 else 0.5

        # All 3 aligned = perfect
        all_aligned = all(trends[tf] in fav for tf in trends if trends[tf] != "unknown")

        return {
            "score": round(min(score, 1.0), 3),
            "trends": trends,
            "aligned": all_aligned,
            "recommendation": "strong" if all_aligned else ("ok" if score >= 0.5 else "weak"),
        }

    def reset(self):
        self._ticks = []
        self._candles_1m = []
        self._candles_5m = []
        self._candles_15m = []
