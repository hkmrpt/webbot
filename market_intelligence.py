"""
market_intelligence.py  ──  Option Buy Robot v9
════════════════════════════════════════════════

Historical + real-time market intelligence engine.

Responsibilities:
  1. Day classification — expiry day, pre-expiry, normal day
  2. DTE calculation — days to expiry, theta pressure level
  3. Historical volatility analysis — what did NIFTY do on similar days?
  4. Session phase intelligence — which hours are profitable historically?
  5. Volatility regime from historical data — is today's vol normal or extreme?
  6. Premium behavior modeling — how fast does premium decay at this DTE?
  7. Adaptive parameter recommendations — SL/trail/timeout tuned per day type

Provides a MarketContext object with all intelligence to the EntryAnalyzer
and ExitAnalyzer for informed decisions.
"""

import json
import math
import os
from datetime import datetime, date, timedelta
from collections import deque

INTEL_STATE_FILE = "market_intel_state.json"


# ── Day Type Classification ──────────────────────────────────────────────────

class DayType:
    EXPIRY      = "expiry"        # weekly/monthly expiry day
    PRE_EXPIRY  = "pre_expiry"    # 1 day before expiry
    POST_EXPIRY = "post_expiry"   # 1 day after expiry (new series)
    NORMAL      = "normal"        # regular trading day


def classify_day(expiry_date_str: str | None) -> dict:
    """
    Classify today relative to the option expiry date.

    Returns:
      day_type, dte (days to expiry), theta_pressure (0-1),
      premium_decay_rate, recommended adjustments
    """
    today = date.today()

    if not expiry_date_str:
        return {
            "day_type": DayType.NORMAL,
            "dte": 5,
            "theta_pressure": 0.3,
            "premium_decay_rate": 1.0,
            "is_weekly_expiry": False,
        }

    try:
        expiry = date.fromisoformat(expiry_date_str)
    except (ValueError, TypeError):
        return {
            "day_type": DayType.NORMAL,
            "dte": 5, "theta_pressure": 0.3,
            "premium_decay_rate": 1.0, "is_weekly_expiry": False,
        }

    dte = (expiry - today).days
    is_weekly = expiry.weekday() == 3  # Thursday = weekly NIFTY expiry

    # Classify
    if dte == 0:
        day_type = DayType.EXPIRY
    elif dte == 1:
        day_type = DayType.PRE_EXPIRY
    elif dte < 0:
        day_type = DayType.POST_EXPIRY
        dte = 0
    else:
        day_type = DayType.NORMAL

    # Theta pressure: exponentially increasing as expiry approaches
    # theta ∝ 1/√DTE — at DTE=0, pressure is maximum
    if dte <= 0:
        theta_pressure = 1.0
    elif dte == 1:
        theta_pressure = 0.85
    elif dte <= 3:
        theta_pressure = 0.6
    elif dte <= 5:
        theta_pressure = 0.4
    else:
        theta_pressure = 0.2

    # Premium decay rate: how fast premium bleeds per hour
    # On expiry day, ATM options can lose 5-10% per hour
    # On normal days, ~0.5-1% per hour
    if dte == 0:
        premium_decay_rate = 5.0   # 5x normal — very fast
    elif dte == 1:
        premium_decay_rate = 2.5   # 2.5x normal — accelerating
    elif dte <= 3:
        premium_decay_rate = 1.5
    else:
        premium_decay_rate = 1.0

    return {
        "day_type":           day_type,
        "dte":                dte,
        "theta_pressure":     round(theta_pressure, 2),
        "premium_decay_rate": round(premium_decay_rate, 1),
        "is_weekly_expiry":   is_weekly,
    }


# ── Historical Volatility Analyzer ──────────────────────────────────────────

class HistoricalAnalyzer:
    """
    Analyzes historical NIFTY candles to build intelligence about:
    - Average daily range for different day types
    - Best trading hours historically
    - Typical volatility patterns
    - Expected move size for the day
    """

    def __init__(self):
        self._daily_stats: list = []  # cached daily statistics
        self._loaded = False

    def analyze_candles(self, candles: list) -> dict:
        """
        Analyze historical 1-day candles to extract intelligence.

        candles: list of {t, o, h, l, c, v} — daily OHLC

        Returns intelligence dict with:
          avg_daily_range, avg_daily_range_pct, volatility_percentile,
          expected_move_today, trend_bias
        """
        if not candles or len(candles) < 3:
            return {
                "avg_daily_range": 0,
                "avg_daily_range_pct": 0,
                "volatility_percentile": 50,
                "expected_move": 0,
                "trend_bias": "neutral",
                "avg_close_vs_open_pct": 0,
                "historical_win_direction": "neutral",
            }

        # Daily ranges
        ranges = [c["h"] - c["l"] for c in candles if c.get("h") and c.get("l")]
        closes = [c["c"] for c in candles if c.get("c")]
        opens = [c["o"] for c in candles if c.get("o")]

        if not ranges or not closes:
            return {
                "avg_daily_range": 0, "avg_daily_range_pct": 0,
                "volatility_percentile": 50, "expected_move": 0,
                "trend_bias": "neutral", "avg_close_vs_open_pct": 0,
                "historical_win_direction": "neutral",
            }

        avg_range = sum(ranges) / len(ranges)
        last_close = closes[-1] if closes else 1
        avg_range_pct = (avg_range / last_close) * 100 if last_close else 0

        # Today's range vs historical — percentile
        sorted_ranges = sorted(ranges)
        today_range = ranges[-1] if ranges else 0

        if len(sorted_ranges) > 1:
            rank = sum(1 for r in sorted_ranges if r <= today_range)
            vol_percentile = int((rank / len(sorted_ranges)) * 100)
        else:
            vol_percentile = 50

        # Expected move = avg range × 0.7 (typical actual range is 70% of max)
        expected_move = avg_range * 0.7

        # Trend bias: are recent days mostly up or down?
        if len(closes) >= 5 and len(opens) >= 5:
            up_days = sum(1 for i in range(-5, 0) if closes[i] > opens[i])
            if up_days >= 4:
                trend_bias = "bullish"
            elif up_days <= 1:
                trend_bias = "bearish"
            else:
                trend_bias = "neutral"
        else:
            trend_bias = "neutral"

        # Close vs Open — are days closing higher or lower on average?
        if opens and closes and len(opens) == len(closes):
            co_pcts = [(closes[i] - opens[i]) / opens[i] * 100
                       for i in range(len(closes)) if opens[i] > 0]
            avg_co = sum(co_pcts) / len(co_pcts) if co_pcts else 0
        else:
            avg_co = 0

        # Historical direction winner
        if avg_co > 0.05:
            win_dir = "CE"    # historically closes higher → CE wins
        elif avg_co < -0.05:
            win_dir = "PE"    # historically closes lower → PE wins
        else:
            win_dir = "neutral"

        return {
            "avg_daily_range":     round(avg_range, 2),
            "avg_daily_range_pct": round(avg_range_pct, 3),
            "volatility_percentile": vol_percentile,
            "expected_move":       round(expected_move, 2),
            "trend_bias":          trend_bias,
            "avg_close_vs_open_pct": round(avg_co, 3),
            "historical_win_direction": win_dir,
        }

    def analyze_intraday(self, minute_candles: list) -> dict:
        """
        Analyze today's intraday minute candles for pattern intelligence.

        Returns:
          session_range, range_pct, vwap_approx, volume_profile,
          high_vol_hours, trend_phase
        """
        if not minute_candles or len(minute_candles) < 5:
            return {
                "session_range": 0, "range_pct": 0,
                "session_high": 0, "session_low": 0,
                "vwap_approx": 0, "volume_trend": "unknown",
                "trend_phase": "unknown",
                "candles_analyzed": 0,
            }

        highs = [c["h"] for c in minute_candles if c.get("h")]
        lows = [c["l"] for c in minute_candles if c.get("l")]
        closes = [c["c"] for c in minute_candles if c.get("c")]
        volumes = [c.get("v", 0) for c in minute_candles]

        if not highs or not lows or not closes:
            return {
                "session_range": 0, "range_pct": 0,
                "session_high": 0, "session_low": 0,
                "vwap_approx": 0, "volume_trend": "unknown",
                "trend_phase": "unknown", "candles_analyzed": 0,
            }

        session_high = max(highs)
        session_low = min(lows)
        session_range = session_high - session_low
        last_price = closes[-1]
        range_pct = (session_range / last_price * 100) if last_price else 0

        # VWAP approximation
        if any(v > 0 for v in volumes):
            typical_prices = [(minute_candles[i]["h"] + minute_candles[i]["l"] +
                               minute_candles[i]["c"]) / 3
                              for i in range(len(minute_candles))]
            cum_tp_vol = sum(tp * v for tp, v in zip(typical_prices, volumes) if v > 0)
            cum_vol = sum(v for v in volumes if v > 0)
            vwap = cum_tp_vol / cum_vol if cum_vol > 0 else last_price
        else:
            vwap = sum(closes) / len(closes)  # simple average as fallback

        # Volume trend: is volume increasing or decreasing through the day?
        if len(volumes) >= 10:
            first_half = volumes[:len(volumes)//2]
            second_half = volumes[len(volumes)//2:]
            avg_first = sum(first_half) / len(first_half) if first_half else 0
            avg_second = sum(second_half) / len(second_half) if second_half else 0
            if avg_second > avg_first * 1.3:
                vol_trend = "increasing"
            elif avg_second < avg_first * 0.7:
                vol_trend = "decreasing"
            else:
                vol_trend = "steady"
        else:
            vol_trend = "unknown"

        # Trend phase from price action
        if len(closes) >= 10:
            early = sum(closes[:5]) / 5
            recent = sum(closes[-5:]) / 5
            change_pct = (recent - early) / early * 100 if early else 0
            if change_pct > 0.15:
                trend_phase = "uptrend"
            elif change_pct < -0.15:
                trend_phase = "downtrend"
            else:
                trend_phase = "sideways"
        else:
            trend_phase = "unknown"

        return {
            "session_range":    round(session_range, 2),
            "range_pct":        round(range_pct, 3),
            "session_high":     round(session_high, 2),
            "session_low":      round(session_low, 2),
            "vwap_approx":      round(vwap, 2),
            "volume_trend":     vol_trend,
            "trend_phase":      trend_phase,
            "candles_analyzed":  len(minute_candles),
        }


# ── Parameter Recommendations ────────────────────────────────────────────────

def recommend_params(day_info: dict, hist_intel: dict, intraday_intel: dict) -> dict:
    """
    Generate adaptive trading parameters based on all intelligence.

    Returns recommended: sl_pct, trail_pct, timeout, max_lots_mult,
    entry_bias, avoid_entry (bool), reason
    """
    day_type = day_info.get("day_type", DayType.NORMAL)
    dte = day_info.get("dte", 5)
    theta_pressure = day_info.get("theta_pressure", 0.3)
    decay_rate = day_info.get("premium_decay_rate", 1.0)
    vol_pctile = hist_intel.get("volatility_percentile", 50)
    trend_bias = hist_intel.get("trend_bias", "neutral")
    trend_phase = intraday_intel.get("trend_phase", "unknown")

    # Base params
    sl_pct = 10.0
    trail_pct = 8.0
    timeout = 50
    max_lots_mult = 1.0  # multiplier on calculated lots
    entry_bias = "neutral"  # CE / PE / neutral
    avoid_entry = False
    reasons = []

    # ── Expiry day adjustments ───────────────────────────────────────
    if day_type == DayType.EXPIRY:
        sl_pct = 6.0          # tight SL — premium melts fast
        trail_pct = 4.0       # tight trail — capture quick moves
        timeout = 30          # short timeout — no time to wait
        max_lots_mult = 0.7   # smaller position — higher risk
        reasons.append("EXPIRY: tight SL/trail, fast timeout, smaller size")

        # On expiry, only trade if volatility is decent
        if vol_pctile < 30:
            avoid_entry = True
            reasons.append("EXPIRY+LOW_VOL: avoid — premium too cheap to profit")

    # ── Pre-expiry adjustments ───────────────────────────────────────
    elif day_type == DayType.PRE_EXPIRY:
        sl_pct = 8.0
        trail_pct = 5.0
        timeout = 40
        max_lots_mult = 0.85
        reasons.append("PRE-EXPIRY: moderately tight params, theta accelerating")

    # ── Post-expiry (new series) ─────────────────────────────────────
    elif day_type == DayType.POST_EXPIRY:
        sl_pct = 12.0         # wider SL — new series has more premium
        trail_pct = 10.0      # wider trail — let moves develop
        timeout = 60
        max_lots_mult = 1.0
        reasons.append("POST-EXPIRY: new series, wider params, more premium")

    # ── Normal day — adjust by volatility ────────────────────────────
    else:
        if vol_pctile >= 80:
            # High vol day — expect big moves
            sl_pct = 8.0
            trail_pct = 6.0
            timeout = 45
            max_lots_mult = 0.8   # slightly smaller — more risk
            reasons.append("HIGH_VOL_DAY: tighter params, smaller size")
        elif vol_pctile <= 20:
            # Low vol day — small moves, need patience
            sl_pct = 12.0
            trail_pct = 10.0
            timeout = 60
            max_lots_mult = 1.2   # can size up — less risk per move
            reasons.append("LOW_VOL_DAY: wider params, patient approach")
        else:
            reasons.append("NORMAL_DAY: standard params")

    # ── Theta pressure adjustments (applies to all day types) ────────
    if theta_pressure >= 0.8:
        timeout = min(timeout, 35)   # don't hold long when theta is crushing
        trail_pct = min(trail_pct, 5.0)  # tight trail
        reasons.append(f"HIGH_THETA({theta_pressure}): shortened timeout, tight trail")

    # ── Historical trend bias ────────────────────────────────────────
    if trend_bias == "bullish" and trend_phase == "uptrend":
        entry_bias = "CE"
        reasons.append("HIST+INTRADAY both bullish → CE bias")
    elif trend_bias == "bearish" and trend_phase == "downtrend":
        entry_bias = "PE"
        reasons.append("HIST+INTRADAY both bearish → PE bias")

    # ── Premium decay rate adjustment ────────────────────────────────
    if decay_rate >= 3.0:
        # Very fast decay — only enter if move is strong
        max_lots_mult *= 0.6
        reasons.append(f"FAST_DECAY({decay_rate}x): reduced size")

    return {
        "sl_pct":          round(sl_pct, 1),
        "trail_pct":       round(trail_pct, 1),
        "timeout":         timeout,
        "max_lots_mult":   round(max_lots_mult, 2),
        "entry_bias":      entry_bias,
        "avoid_entry":     avoid_entry,
        "reasons":         reasons,
        "day_type":        day_type,
        "dte":             dte,
        "theta_pressure":  theta_pressure,
        "decay_rate":      decay_rate,
    }


# ── Intelligent Trade Budget ─────────────────────────────────────────────────

def compute_trade_budget(
    day_info: dict,
    hist_intel: dict,
    intraday_intel: dict,
    session_stats: dict,
    trade_history: list,
) -> dict:
    """
    Decide how many more trades to allow today — fully adaptive.

    Inputs:
      session_stats: trades_today, wins, losses, session_pnl, loss_streak,
                     capital, day_start_capital
      trade_history: list of past trade outcomes (from market_intel_state)

    Logic:
      - Start with a base budget per day type
      - Increase if winning today (hot hand)
      - Decrease if losing today (protect capital)
      - Adjust for volatility (high vol = more opportunities = more trades)
      - Adjust for time remaining (less time = fewer trades left)
      - On loss streak: reduce aggressively
      - Near drawdown limit: cut to minimum

    Returns:
      max_trades: int — total allowed today
      remaining: int — how many more allowed
      reason: str
      confidence: str — "aggressive" / "normal" / "conservative" / "minimal"
    """
    day_type = day_info.get("day_type", DayType.NORMAL)
    dte = day_info.get("dte", 5)
    vol_pctile = hist_intel.get("volatility_percentile", 50)
    trend_phase = intraday_intel.get("trend_phase", "unknown")

    trades_today = session_stats.get("trades_today", 0)
    wins = session_stats.get("wins", 0)
    losses = session_stats.get("losses", 0)
    session_pnl = session_stats.get("session_pnl", 0)
    loss_streak = session_stats.get("loss_streak", 0)
    capital = session_stats.get("capital", 10000)
    day_start = session_stats.get("day_start_capital", capital)

    reasons = []

    # ── Base budget by day type ──────────────────────────────────────
    if day_type == DayType.EXPIRY:
        base = 8       # expiry = fast moves, many short trades
        reasons.append("EXPIRY: base=8 (quick scalps)")
    elif day_type == DayType.PRE_EXPIRY:
        base = 10
        reasons.append("PRE-EXPIRY: base=10")
    elif day_type == DayType.POST_EXPIRY:
        base = 12      # new series, fresh premium
        reasons.append("POST-EXPIRY: base=12 (fresh series)")
    else:
        base = 10
        reasons.append("NORMAL: base=10")

    # ── Volatility adjustment ────────────────────────────────────────
    if vol_pctile >= 75:
        base = int(base * 1.4)  # high vol = more opportunities
        reasons.append(f"HIGH_VOL({vol_pctile}p): +40% budget")
    elif vol_pctile <= 25:
        base = max(3, int(base * 0.6))  # low vol = fewer opportunities
        reasons.append(f"LOW_VOL({vol_pctile}p): -40% budget")

    # ── Trending market bonus ────────────────────────────────────────
    if trend_phase in ("uptrend", "downtrend"):
        base = int(base * 1.2)  # trending = more reliable signals
        reasons.append(f"TRENDING({trend_phase}): +20% budget")

    # ── Today's performance adjustment ───────────────────────────────
    if trades_today >= 3:
        today_wr = wins / trades_today
        if today_wr >= 0.7:
            # Hot hand — increase budget
            base = int(base * 1.3)
            reasons.append(f"HOT_HAND({today_wr:.0%}wr): +30% budget")
        elif today_wr <= 0.3 and trades_today >= 4:
            # Cold day — cut budget
            base = max(trades_today + 1, int(base * 0.5))
            reasons.append(f"COLD_DAY({today_wr:.0%}wr): -50% budget")

    # ── Session P&L adjustment ───────────────────────────────────────
    if day_start > 0:
        pnl_pct = session_pnl / day_start * 100
        if pnl_pct >= 3.0:
            # Already up 3%+ — can afford to be aggressive
            base = int(base * 1.2)
            reasons.append(f"UP_{pnl_pct:.1f}%: aggressive mode")
        elif pnl_pct <= -2.0:
            # Down 2%+ — protect capital, minimize trades
            base = max(trades_today + 1, int(base * 0.4))
            reasons.append(f"DOWN_{pnl_pct:.1f}%: protective mode")
        elif pnl_pct <= -1.0:
            # Down 1%+ — cautious
            base = max(trades_today + 1, int(base * 0.7))
            reasons.append(f"DOWN_{pnl_pct:.1f}%: cautious mode")

    # ── Loss streak protection ───────────────────────────────────────
    if loss_streak >= 4:
        base = trades_today + 1  # stop after current
        reasons.append(f"STREAK({loss_streak}): near-halt")
    elif loss_streak >= 3:
        base = max(trades_today + 1, int(base * 0.5))
        reasons.append(f"STREAK({loss_streak}): heavy reduction")
    elif loss_streak >= 2:
        base = max(trades_today + 1, int(base * 0.7))
        reasons.append(f"STREAK({loss_streak}): moderate reduction")

    # ── Time remaining adjustment ────────────────────────────────────
    now = datetime.now()
    mins_left = max(0, 15 * 60 + 20 - (now.hour * 60 + now.minute))
    if mins_left < 30:
        base = min(base, trades_today + 1)  # last 30 min: no new trades
        reasons.append("CLOSING: no new trades")
    elif mins_left < 90:
        base = min(base, trades_today + 2)
        reasons.append("LATE_SESSION: max 2 more")

    # ── Historical day-type performance ──────────────────────────────
    dt_trades = [t for t in trade_history if t.get("day_type") == day_type]
    if len(dt_trades) >= 10:
        dt_wr = sum(1 for t in dt_trades if t.get("won")) / len(dt_trades)
        if dt_wr < 0.35:
            base = max(3, int(base * 0.6))
            reasons.append(f"HIST_{day_type.upper()}_WR({dt_wr:.0%}): reduced budget")
        elif dt_wr >= 0.6:
            base = int(base * 1.15)
            reasons.append(f"HIST_{day_type.upper()}_WR({dt_wr:.0%}): boosted budget")

    # Floor and ceiling
    base = max(max(trades_today, 1), min(base, 25))
    remaining = max(0, base - trades_today)

    # Confidence label
    if remaining == 0:
        confidence = "halted"
    elif remaining <= 2:
        confidence = "minimal"
    elif base <= 5:
        confidence = "conservative"
    elif base >= 15:
        confidence = "aggressive"
    else:
        confidence = "normal"

    return {
        "max_trades":  base,
        "remaining":   remaining,
        "confidence":  confidence,
        "reasons":     reasons,
    }


# ── Main Market Intelligence Engine ─────────────────────────────────────────

class MarketIntelligence:
    """
    Central intelligence hub. Aggregates all market analysis.

    Usage:
      mi = MarketIntelligence()
      # At startup or when ATM resolves:
      mi.update_expiry("2025-06-26")
      mi.update_historical_candles(daily_candles)
      mi.update_intraday_candles(minute_candles)

      # Before each entry:
      context = mi.get_context()
      # context has: day_info, hist_intel, intraday_intel, recommendations
    """

    def __init__(self):
        self._expiry_str: str | None = None
        self._day_info: dict = {}
        self._hist_analyzer = HistoricalAnalyzer()
        self._hist_intel: dict = {}
        self._intraday_intel: dict = {}
        self._recommendations: dict = {}
        self._daily_candles: list = []
        self._intraday_candles: list = []
        self._trade_history: list = []  # outcomes for this day type
        self._load()

    def update_expiry(self, expiry_str: str | None):
        """Call when ATM options are resolved (expiry date known)."""
        self._expiry_str = expiry_str
        self._day_info = classify_day(expiry_str)
        self._rebuild_recommendations()

    def update_historical_candles(self, candles: list):
        """Call with daily OHLC candles (last 7-30 days)."""
        self._daily_candles = candles
        self._hist_intel = self._hist_analyzer.analyze_candles(candles)
        self._rebuild_recommendations()

    def update_intraday_candles(self, candles: list):
        """Call with today's minute candles."""
        self._intraday_candles = candles
        self._intraday_intel = self._hist_analyzer.analyze_intraday(candles)
        self._rebuild_recommendations()

    def _rebuild_recommendations(self):
        self._recommendations = recommend_params(
            self._day_info, self._hist_intel, self._intraday_intel
        )

    def get_context(self, session_stats: dict | None = None) -> dict:
        """
        Full market context for entry/exit decisions.
        If session_stats provided, includes intelligent trade budget.
        """
        ctx = {
            "day_info":        self._day_info,
            "hist_intel":      self._hist_intel,
            "intraday_intel":  self._intraday_intel,
            "recommendations": self._recommendations,
            "expiry":          self._expiry_str,
        }
        if session_stats is not None:
            ctx["trade_budget"] = compute_trade_budget(
                self._day_info, self._hist_intel, self._intraday_intel,
                session_stats, self._trade_history,
            )
        return ctx

    def on_trade_closed(self, result: dict):
        """Learn from trade outcome for this day type."""
        pnl_pct = result.get("pnl_pct", 0) or 0
        day_type = self._day_info.get("day_type", DayType.NORMAL)
        self._trade_history.append({
            "day_type": day_type,
            "dte":      self._day_info.get("dte", 5),
            "pnl_pct":  pnl_pct,
            "won":      pnl_pct >= 0.5,
        })
        # Keep last 100 trades
        if len(self._trade_history) > 100:
            self._trade_history = self._trade_history[-100:]
        self._save()

    @property
    def state(self) -> dict:
        # Win rates by day type
        by_type = {}
        for dt in [DayType.EXPIRY, DayType.PRE_EXPIRY, DayType.NORMAL]:
            trades = [t for t in self._trade_history if t["day_type"] == dt]
            wins = sum(1 for t in trades if t["won"])
            by_type[dt] = {
                "trades": len(trades),
                "win_rate": round(wins / len(trades), 2) if trades else 0,
            }

        return {
            "day_info":        self._day_info,
            "recommendations": self._recommendations,
            "hist_intel":      self._hist_intel,
            "intraday_intel":  self._intraday_intel,
            "performance_by_day_type": by_type,
            "total_learned_trades": len(self._trade_history),
        }

    def _save(self):
        try:
            with open(INTEL_STATE_FILE, "w") as f:
                json.dump({"trade_history": self._trade_history}, f)
        except Exception:
            pass

    def _load(self):
        if not os.path.exists(INTEL_STATE_FILE):
            return
        try:
            with open(INTEL_STATE_FILE) as f:
                data = json.load(f)
            self._trade_history = data.get("trade_history", [])
        except Exception:
            pass
