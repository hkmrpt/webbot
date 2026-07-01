"""
buy_app.py  ──  Option BUYING Robot  v8.2
══════════════════════════════════════════

v8.2 fixes & optimisations over v8.1:
  1.  BUG FIX: _check_force_exit_state() now returns closed result so
      process_ticks can place the real sell order and emit trade_closed.
      Previously the result was silently discarded — force exit never
      placed a real order or notified clients.
  2.  BUG FIX: _subscribe/_unsubscribe now hold _state_lock when
      mutating S["subscribed_tokens"] — fixes a thread-safety race.
  3.  BUG FIX: _RESTART_PARAMS removed non-existent "option_atr_period"
      entry (it was never in RC so would never match).
  4.  REFACTOR: _run_order_intent() extracts the repeated exit-order
      placement + result patching pattern (was duplicated 3×).
  5.  REFACTOR: _build_state_payload() shared by broadcast() and
      on_connect to prevent payload drift between the two.
  6.  REFACTOR: Separate _log_lock for S["logs"] so logging never
      contends with trading state mutations.
  7.  SIMPLIFY: on_stop and on_manual_exit deduplicated via shared
      _close_active_trade() helper.

v8.1 thread-safety invariant (preserved):
  _state_lock covers state mutation ONLY.
  All I/O (socketio.emit, place_buy, place_sell, network) happens
  after the lock is released.
"""

import os as _os
import sys as _sys

# ── Writable runtime directory (next to exe when frozen, script dir otherwise) ─
def _runtime_dir() -> str:
    if getattr(_sys, "frozen", False):
        return _os.path.dirname(_os.path.abspath(_sys.executable))
    return _os.path.dirname(_os.path.abspath(__file__))

# Enctoken persisted in a tiny file — config.py is never modified at runtime
_ENCTOKEN_FILE  = _os.path.join(_runtime_dir(), "enctoken.dat")
_WATCHLIST_FILE = _os.path.join(_runtime_dir(), "watchlist.json")

import asyncio
import threading
import numpy as np
from collections import deque
from datetime import datetime, time as dtime, timedelta
import csv as _csv
import re as _re
import io as _io
import time as _time
import urllib.request as _urllib_request
from flask import Flask, send_file, request, jsonify
from flask_socketio import SocketIO, emit

from config import (
    CAPITAL, BUY_QTY, MAX_TRADES_DAY, LOT_SIZE, MAX_LOTS_PER_TRADE,
    JUMP_PCT, CONFIRM_SUSTAIN_PCT,
    MOMENTUM_WINDOW, MOMENTUM_MIN,
    REGRESSION_WINDOW, REGRESSION_SLOPE_MIN,
    OPTION_VOL_WINDOW, OPTION_VOL_FACTOR,
    BUY_TRAIL_PCT, SL_PHASE1_PCT, SL_PHASE1_SECS, SL_PHASE2_PCT,
    SL_COOLDOWN_SECS, MAX_DAILY_LOSS, DAILY_PROFIT_TARGET, DAILY_PROFIT_PCT,
    TRADE_START_H, TRADE_START_M, TRADE_END_H, TRADE_END_M,
    FORCE_EXIT_H, FORCE_EXIT_M,
    OPTION_ATR_PERIOD,
    CONFIRM_ATR_HIGH, CONFIRM_ATR_LOW,
    CONFIRM_TICKS_FAST, CONFIRM_TICKS_MID, CONFIRM_TICKS_SLOW,
    TRAIL_ATR_HIGH, TRAIL_ATR_LOW,
    TRAIL_PCT_HIGH, TRAIL_PCT_LOW,
    TRADE_TIMEOUT_SECS,
    PROFIT_TRAIL_THRESHOLD_PCT, PROFIT_TIER_TRAIL,
    BREAKEVEN_TRIGGER_PCT,
    TRADING_MODE,
    AUTO_JUMP, JUMP_ATR_WINDOW, JUMP_ATR_MULTIPLIER,
    JUMP_MIN_PTS, JUMP_MAX_PTS,
    HIST_ATR_DAYS, HIST_ATR_SPIKE_FRACTION,
    SPIKE_LOOKBACK_SECS, SPIKE_MAX_SECS, SPIKE_MIN_SPEED_PCT_SEC,
    NIFTY_REVERSAL_EXIT, NIFTY_REVERSAL_BUFFER_PCT, NIFTY_REVERSAL_PROFIT_SKIP_PCT,
    FAST_MOVE_VELOCITY,
    BREAKEVEN_TRIGGER_PCT,
    ZERODHA_CONFIG,
    BANKNIFTY_TOKEN,
    SMART_COOLDOWN_ENABLED,
    COOLDOWN_AFTER_SL, COOLDOWN_AFTER_TRAIL_WIN, COOLDOWN_AFTER_TRAIL_LOSS,
    COOLDOWN_AFTER_TIMEOUT_WIN, COOLDOWN_AFTER_TIMEOUT_LOSS,
    COOLDOWN_AFTER_AI_EXIT, COOLDOWN_AFTER_REVERSAL,
    TREND_ENABLED, TREND_WINDOW, TREND_MIN_MOVE_PCT,
    TREND_CONSISTENCY_PCT, TREND_COOLDOWN_TICKS,
    BREAKOUT_FILTER_ENABLED, BREAKOUT_RANGE_WINDOW,
    VOL_GATE_ENABLED, VOL_LOW_ATR_PTS, VOL_HIGH_ATR_PTS,
    ENSEMBLE_ENABLED, ENSEMBLE_MIN_SCORE,
    OPTION_MIN_PRICE, OPTION_SWEET_MIN, OPTION_SWEET_MAX, OPTION_MAX_PRICE,
    ATR_POSITION_SIZING, MAX_RISK_PER_TRADE,
    LOSS_STREAK_REDUCE_AFTER, LOSS_STREAK_SIZE_MULT,
    MAX_DRAWDOWN_PCT,
)
from buy_exit_strategy import BuyExitStrategy
from market_brain import MarketBrain
from volatility_detector import VolatilityDetector
from entry_analyzer import EntryAnalyzer
from market_intelligence import MarketIntelligence
from position_sync import detect_open_positions, build_adoption_params, detect_position_changes
from option_chain_intel import VIXTracker, OptionChainAnalyzer, MultiTimeframeAnalyzer, VIX_TOKEN
from zerodha_websocket import connect_zerodha_websocket
from Zerodha_api import place_buy, place_sell, place_limit_sell, cancel_order, fetch_balance, fetch_orders, fetch_trades, fetch_positions

# ── Load persisted enctoken into memory (overrides config.py value) ───────────
def _load_enctoken():
    try:
        if _os.path.exists(_ENCTOKEN_FILE):
            token = open(_ENCTOKEN_FILE, "r", encoding="utf-8").read().strip()
            if token:
                ZERODHA_CONFIG["enctoken"] = token
    except Exception:
        pass

_load_enctoken()

# ── Watchlist persistence ─────────────────────────────────────────────────────
import json as _json_mod

def _load_watchlist() -> list:
    try:
        with open(_WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return _json_mod.load(f)
    except Exception:
        return []

def _save_watchlist(items: list):
    try:
        with open(_WATCHLIST_FILE, "w", encoding="utf-8") as f:
            _json_mod.dump(items, f, indent=2)
    except Exception:
        pass

TRADE_START  = dtime(TRADE_START_H,  TRADE_START_M)
TRADE_END    = dtime(TRADE_END_H,    TRADE_END_M)
FORCE_EXIT_T = dtime(FORCE_EXIT_H,   FORCE_EXIT_M)
NIFTY_TOKEN  = 256265   # NIFTY 50

app      = Flask(__name__)
app.config["SECRET_KEY"] = "buybot_v8_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# _state_lock guards S and RC mutations ONLY.
# Never call socketio.emit, place_buy, place_sell, or any blocking I/O
# while holding this lock.
_state_lock      = threading.Lock()
_market_brain    = MarketBrain()          # Global NIFTY AI brain
_entry_analyzer  = EntryAnalyzer()       # 8-dimension entry analysis engine
_market_intel    = MarketIntelligence()  # historical + day-type intelligence
_vix_tracker     = VIXTracker()         # India VIX live tracking
_oi_analyzer     = OptionChainAnalyzer() # option chain OI/PCR analysis
_mtf_analyzer    = MultiTimeframeAnalyzer() # multi-timeframe trend
_vol_detector    = VolatilityDetector()

# Separate lock for the log buffer so logging never blocks trading state.
_log_lock = threading.Lock()

_buf = max(REGRESSION_WINDOW, MOMENTUM_WINDOW + 2, OPTION_ATR_PERIOD + 2,
           CONFIRM_TICKS_SLOW + 5, JUMP_ATR_WINDOW + 2)

# ── Runtime config (hot-reloadable subset) ────────────────────────────────────
RC = {
    "auto_jump":              AUTO_JUMP,
    "jump_atr_window":        JUMP_ATR_WINDOW,
    "jump_atr_multiplier":    JUMP_ATR_MULTIPLIER,
    "jump_min_pts":           JUMP_MIN_PTS,
    "jump_max_pts":           JUMP_MAX_PTS,
    "hist_atr_days":           HIST_ATR_DAYS,
    "hist_atr_spike_fraction": HIST_ATR_SPIKE_FRACTION,
    "spike_lookback_secs":     SPIKE_LOOKBACK_SECS,
    "spike_max_secs":          SPIKE_MAX_SECS,
    "spike_min_speed_pct_sec": SPIKE_MIN_SPEED_PCT_SEC,
    "jump_pct":               JUMP_PCT,
    "confirm_sustain_pct":    CONFIRM_SUSTAIN_PCT,
    "confirm_atr_high":       CONFIRM_ATR_HIGH,
    "confirm_atr_low":        CONFIRM_ATR_LOW,
    "confirm_ticks_fast":     CONFIRM_TICKS_FAST,
    "confirm_ticks_mid":      CONFIRM_TICKS_MID,
    "confirm_ticks_slow":     CONFIRM_TICKS_SLOW,
    "momentum_window":        MOMENTUM_WINDOW,
    "momentum_min":           MOMENTUM_MIN,
    "sl_phase1_pct":          SL_PHASE1_PCT,
    "sl_phase1_secs":         SL_PHASE1_SECS,
    "sl_phase2_pct":          SL_PHASE2_PCT,
    "trail_atr_high":         TRAIL_ATR_HIGH,
    "trail_atr_low":          TRAIL_ATR_LOW,
    "trail_pct_high":         TRAIL_PCT_HIGH,
    "trail_pct_low":          TRAIL_PCT_LOW,
    "buy_trail_pct":          BUY_TRAIL_PCT,
    "profit_trail_threshold": PROFIT_TRAIL_THRESHOLD_PCT,
    "sl_cooldown_secs":       SL_COOLDOWN_SECS,
    "max_trades_day":         MAX_TRADES_DAY,
    "max_daily_loss":         MAX_DAILY_LOSS,
    "daily_profit_target":    DAILY_PROFIT_TARGET,
    "max_lots_per_trade":     MAX_LOTS_PER_TRADE,
    "trade_timeout_secs":     TRADE_TIMEOUT_SECS,
    "regression_window":      REGRESSION_WINDOW,
    "regression_slope_min":   REGRESSION_SLOPE_MIN,
    "option_vol_factor":      OPTION_VOL_FACTOR,
    "trade_start_h":          TRADE_START_H,
    "trade_start_m":          TRADE_START_M,
    "trade_end_h":            TRADE_END_H,
    "trade_end_m":            TRADE_END_M,
    "force_exit_h":           FORCE_EXIT_H,
    "force_exit_m":           FORCE_EXIT_M,

    # ── Smart Cooldown ────────────────────────────────────────────────────
    "smart_cooldown_enabled":  SMART_COOLDOWN_ENABLED,
}

# Params whose deque sizes are fixed at startup — they require a restart to
# fully take effect (the live RC value still updates for logic calculations).
_RESTART_PARAMS = {
    "regression_window", "momentum_window", "confirm_ticks_slow",
    "jump_atr_window",
}


def _make_opt_slot():
    return {
        "token":         None,
        "symbol":        "",
        "strike":        "",
        "price":         None,
        "volume":        0,
        "vol_history":   deque(maxlen=OPTION_VOL_WINDOW),
        "price_history": deque(maxlen=OPTION_ATR_PERIOD + 1),
    }


S = {
    "running":            False,
    "trade_open":         False,
    "trading_mode":       TRADING_MODE,
    "active_sides":       set(),
    "nifty_atm":          None,    # current resolved ATM info
    "atm_pending":        False,   # True = waiting to resolve ATM
    "slots":              {"CE": _make_opt_slot(), "PE": _make_opt_slot()},
    "trade_side":         None,

    "nifty_price":        None,
    "nifty_prev_tick":    None,
    "nifty_ref":          None,
    "nifty_move":         None,
    "nifty_ticks":        deque(maxlen=_buf),
    "nifty_tick_times":   deque(maxlen=_buf),
    "trend_cooldown":     0,

    "nifty_atr_ticks":    deque(maxlen=JUMP_ATR_WINDOW + 2),
    "jump_atr":           None,
    "jump_threshold":     JUMP_MIN_PTS,

    "regression_slope":   None,
    "dynamic_jump_pts":   0.0,
    "pending_side":       None,
    "confirm_count":      0,
    "confirm_needed":     CONFIRM_TICKS_MID,
    "spike_ref_price":    None,
    "fast_entry":         False,
    "cooldown_until":     None,
    "last_skip_reason":   None,

    "active_side":        None,
    "active_status":      None,
    "trade_open_time":    None,
    "nifty_entry_price":  None,
    "ai_entry_score":     None,
    "ai_regime":          "unknown",
    "_last_trade_symbol": "",
    "trade_pnl":          0.0,
    "session_pnl":        0.0,
    "trades_today":       0,
    "wins":               0,
    "losses":             0,
    "loss_streak":        0,
    "adopted_position":   None,           # externally-opened position being managed
    "_last_positions":    [],             # last positions snapshot for change detection
    "_pos_sync_ts":       0,              # timestamp of last position sync
    "trading_date":       datetime.now().date(),   # for auto daily reset
    "capital":            CAPITAL,
    "day_start_capital":  CAPITAL,               # capital at start of this trading day

    "last_order_id":      None,
    "last_exit_order_id": None,
    "target_order_id":    None,   # standing LIMIT SELL at +2.5%

    "exit_engine":        BuyExitStrategy(capital=CAPITAL),

    "index_token":        NIFTY_TOKEN,    # switchable: NIFTY_TOKEN or BANKNIFTY_TOKEN
    "index_name":         "NIFTY",        # display name for the selected index

    "ws_connected":       False,
    "ws_send_queue":      None,
    "ws_loop":            None,
    "subscribed_tokens":  [],

    "logs":               [],
    "live_trail_pct":     BUY_TRAIL_PCT,
    "option_atr":         None,
}


# ── Slot helpers ──────────────────────────────────────────────────────────────
def _slot(side):
    return S["slots"][side]


def _active_slot():
    ts = S["trade_side"]
    return S["slots"][ts] if ts else None


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg, level="info"):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    with _log_lock:
        S["logs"].append(entry)
        if len(S["logs"]) > 300:
            S["logs"] = S["logs"][-300:]
    socketio.emit("log", entry, namespace="/")


# ── Maths ─────────────────────────────────────────────────────────────────────
def _regression_slope(prices):
    if len(prices) < RC["regression_window"]:
        return None
    x = np.arange(len(prices), dtype=float)
    y = np.array(prices, dtype=float)
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def _momentum_score(prices, side):
    w = RC["momentum_window"]
    if len(prices) < w + 1:
        return 0
    recent = list(prices)[-(w + 1):]
    if side == "CE":
        return sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    return sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])


def _compute_atr(prices):
    if len(prices) < 2:
        return None
    trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return round(sum(trs) / len(trs), 3)


def _compute_option_atr(prices):
    return _compute_atr(prices)


# ── Historical ATR (daily candles) ────────────────────────────────────────────
_hist_daily_atr: float | None = None    # avg daily range from last N days
_hist_spike_threshold: float | None = None  # historical ATR × fraction

def _fetch_historical_daily_atr():
    """
    Fetch last HIST_ATR_DAYS daily candles from Zerodha and compute
    average daily range. Sets _hist_daily_atr and _hist_spike_threshold.
    """
    global _hist_daily_atr, _hist_spike_threshold
    import urllib.parse as _up
    try:
        cfg = ZERODHA_CONFIG
        enctoken = _up.unquote(cfg.get("enctoken", ""))
        user_id = cfg.get("user_id", "")
        token = S.get("index_token", NIFTY_TOKEN)
        today = datetime.now()
        from_date = (today - timedelta(days=HIST_ATR_DAYS + 5)).strftime("%Y-%m-%d")
        to_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        url = (
            f"https://kite.zerodha.com/oms/instruments/historical/{token}/day"
            f"?user_id={user_id}&oi=0&from={from_date}&to={to_date}"
        )
        req = _urllib_request.Request(url, headers={
            "Authorization": f"enctoken {enctoken}",
            "Cookie": (
                f"kf_session={cfg.get('kf_session','')}; user_id={user_id}; "
                f"public_token={cfg.get('public_token','')}; enctoken={enctoken}"
            ),
            "Accept": "application/json, */*",
            "User-Agent": cfg.get("user_agent", "kite3-web"),
            "x-kite-userid": user_id,
            "x-kite-version": cfg.get("version", "3.0.0"),
        })
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json_mod.loads(resp.read().decode())

        raw = data.get("data", {}).get("candles", [])
        if not raw:
            return

        # Each candle: [timestamp, O, H, L, C, volume, oi]
        daily_ranges = [c[2] - c[3] for c in raw if len(c) >= 5]  # H - L
        if not daily_ranges:
            return

        # Take last HIST_ATR_DAYS
        daily_ranges = daily_ranges[-HIST_ATR_DAYS:]
        avg_range = sum(daily_ranges) / len(daily_ranges)
        threshold = avg_range * HIST_ATR_SPIKE_FRACTION

        _hist_daily_atr = round(avg_range, 2)
        _hist_spike_threshold = round(threshold, 2)

        # Feed daily candles to market intelligence
        daily_candles = [{
            "t": 0, "o": c[1], "h": c[2], "l": c[3], "c": c[4],
            "v": c[5] if len(c) > 5 else 0,
        } for c in raw]
        _market_intel.update_historical_candles(daily_candles)

        log(
            f"[HIST] {len(daily_ranges)}-day avg range: {avg_range:.1f} pts  "
            f"spike threshold: {threshold:.1f} pts  "
            f"(range x {HIST_ATR_SPIKE_FRACTION})",
            "info",
        )
    except Exception as exc:
        log(f"[HIST] Historical candle fetch failed: {exc}", "warning")


# ── Auto jump threshold ───────────────────────────────────────────────────────
def _update_jump_threshold(nifty_price):
    """
    Compute spike detection threshold.

    IMPORTANT: This threshold is compared to a SINGLE tick-to-tick move.
    NIFTY ticks move 1-15 pts typically. So threshold must be in that range.

    Primary: live tick-to-tick ATR × multiplier (e.g. 5 pts ATR × 1.2 = 6 pts)
    Historical daily range is used ONLY to set an intelligent ceiling.
    """
    atr = _compute_atr(list(S["nifty_atr_ticks"]))
    S["jump_atr"] = atr

    # PRIMARY: live tick ATR — this is what actually happens tick-to-tick
    if RC["auto_jump"] and atr is not None:
        raw = atr * RC["jump_atr_multiplier"]
        pts = round(max(RC["jump_min_pts"], min(RC["jump_max_pts"], raw)), 2)
        S["_threshold_source"] = "live_atr"
    else:
        pts = round(nifty_price * RC["jump_pct"] / 100, 2) if nifty_price else RC["jump_min_pts"]
        S["_threshold_source"] = "fixed"

    # Historical daily range sets an intelligent MAX CEILING only
    # Daily range 200pts → reasonable tick spike max ≈ 200 × 0.05 = 10pts
    # (a single tick can't move more than ~5% of the daily range)
    if _hist_daily_atr is not None and _hist_daily_atr > 0:
        hist_tick_ceiling = round(_hist_daily_atr * 0.05, 2)  # 5% of daily range
        hist_tick_ceiling = max(hist_tick_ceiling, 4.0)         # at least 4 pts
        if pts > hist_tick_ceiling:
            pts = hist_tick_ceiling
            S["_threshold_source"] = "hist_capped"

    # ── Intelligent multiplier ───────────────────────────────────────
    try:
        intel_ctx = _market_intel.get_context()
        day_info = intel_ctx.get("day_info", {})
        day_type = day_info.get("day_type", "normal")
        regime = S.get("ai_regime", "unknown")

        mult = 1.0

        # Day type
        if day_type == "expiry":
            mult *= 0.8
        elif day_type == "pre_expiry":
            mult *= 0.9

        # Regime
        if regime == "volatile":
            mult *= 1.2
        elif regime == "choppy":
            mult *= 1.15
        elif regime in ("trending_up", "trending_down"):
            mult *= 0.85

        # Loss streak
        streak = S.get("loss_streak", 0)
        if streak >= 3:
            mult *= 1.3
        elif streak >= 2:
            mult *= 1.15

        # Session time
        now = datetime.now()
        mins = now.hour * 60 + now.minute
        if mins < 9 * 60 + 30:
            mult *= 1.2
        elif mins > 15 * 60:
            mult *= 1.2

        # VIX
        vix_val = _vix_tracker.current
        if vix_val is not None:
            if vix_val < 12:
                mult *= 1.15
            elif vix_val > 20:
                mult *= 0.85
            elif vix_val > 25:
                mult *= 0.75

        pts = round(pts * mult, 2)
    except Exception:
        pass

    # Final clamp — tick-to-tick spike should be 2-20 pts max for NIFTY
    pts = max(RC["jump_min_pts"], min(20.0, pts))

    S["jump_threshold"]   = pts
    S["dynamic_jump_pts"] = pts
    return pts


def _compute_rsi(prices, period=14):
    """Compute RSI from tick prices."""
    if len(prices) < period + 1:
        return 50.0
    changes = [prices[i] - prices[i - 1] for i in range(len(prices) - period, len(prices))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _session_range_position(prices):
    """Where is current price in the session's range? 0=low, 1=high."""
    if len(prices) < 10:
        return 0.5
    hi  = max(prices)
    lo  = min(prices)
    rng = hi - lo
    if rng < 0.01:
        return 0.5
    return round((prices[-1] - lo) / rng, 3)


def _check_spike_speed(current_price):
    """Validate spike happened fast enough — not a slow drift."""
    times = list(S.get("nifty_tick_times", []))
    ticks = list(S["nifty_ticks"])
    if len(ticks) < 3 or len(times) < 3:
        return True, ""
    now = datetime.now()
    lookback = RC.get("spike_lookback_secs", SPIKE_LOOKBACK_SECS)
    cutoff = now - timedelta(seconds=lookback)
    start_idx = 0
    for i in range(len(times) - 1, -1, -1):
        if times[i] < cutoff:
            start_idx = i + 1
            break
    if start_idx >= len(ticks) - 1:
        return True, ""
    move_pts = abs(ticks[-1] - ticks[start_idx])
    elapsed  = (times[-1] - times[start_idx]).total_seconds()
    if elapsed <= 0:
        return True, ""
    max_secs  = RC.get("spike_max_secs", SPIKE_MAX_SECS)
    min_speed = RC.get("spike_min_speed_pct_sec", SPIKE_MIN_SPEED_PCT_SEC)
    if elapsed > max_secs:
        return False, f"Spike too slow ({elapsed:.1f}s > {max_secs}s) — trend, not spike"
    speed = (move_pts / current_price * 100) / elapsed
    if speed < min_speed:
        return False, f"Spike speed {speed:.4f}%/s < min {min_speed}%/s"
    return True, ""


def _check_trend(current_price):
    """Detect slow sustained trend for alternative entry. Returns side or None."""
    if not TREND_ENABLED:
        return None
    if S.get("trend_cooldown", 0) > 0:
        S["trend_cooldown"] -= 1
        return None
    ticks = list(S["nifty_ticks"])
    if len(ticks) < TREND_WINDOW:
        return None
    window  = ticks[-TREND_WINDOW:]
    changes = [window[i] - window[i - 1] for i in range(1, len(window))]
    total   = len(changes)
    if total == 0:
        return None
    up_count   = sum(1 for c in changes if c > 0)
    down_count = sum(1 for c in changes if c < 0)
    cumulative = window[-1] - window[0]
    move_pct   = abs(cumulative) / current_price * 100
    if move_pct < TREND_MIN_MOVE_PCT:
        return None
    if up_count / total >= TREND_CONSISTENCY_PCT:
        return "CE"
    if down_count / total >= TREND_CONSISTENCY_PCT:
        return "PE"
    return None


def _option_price_ok(side):
    """Check option price is in acceptable range. Returns (ok, reason)."""
    sl = _slot(side)
    opt_price = sl["price"]
    if opt_price is None:
        return False, "No option price"
    if opt_price < OPTION_MIN_PRICE:
        return False, f"Option ₹{opt_price:.2f} too cheap (< ₹{OPTION_MIN_PRICE})"
    if opt_price > OPTION_MAX_PRICE:
        return False, f"Option ₹{opt_price:.2f} too expensive (> ₹{OPTION_MAX_PRICE})"
    return True, ""


def _breakout_filter_ok(current_price):
    """Check if spike breaks out of recent consolidation range."""
    if not BREAKOUT_FILTER_ENABLED:
        return True, ""
    if not _vol_detector.is_ranging():
        return True, ""  # not in a range, allow
    info = _vol_detector.range_info()
    hi   = info.get("high", 0)
    lo   = info.get("low", 0)
    if current_price > hi or current_price < lo:
        return True, ""  # broke out of range
    return False, f"Price inside range [{lo:.1f}–{hi:.1f}] — no breakout"


def _adaptive_confirm_ticks(side):
    hist = list(_slot(side)["price_history"])
    atr  = _compute_option_atr(hist)
    if atr is None:
        ticks, label = RC["confirm_ticks_mid"], f"MID({RC['confirm_ticks_mid']}) — no ATR yet"
    elif atr >= RC["confirm_atr_high"]:
        ticks, label = RC["confirm_ticks_fast"], f"FAST({RC['confirm_ticks_fast']}) — ATR={atr:.2f} high"
    elif atr <= RC["confirm_atr_low"]:
        ticks, label = RC["confirm_ticks_slow"], f"SLOW({RC['confirm_ticks_slow']}) — ATR={atr:.2f} low"
    else:
        ticks, label = RC["confirm_ticks_mid"], f"MID({RC['confirm_ticks_mid']}) — ATR={atr:.2f} normal"
    log(f"Adaptive confirm: {label}", "info")
    return ticks


# ── State payload builder ─────────────────────────────────────────────────────
def _build_state_payload():
    """Build full state snapshot. Must be called under _state_lock."""
    snap      = S["exit_engine"].leg_snapshot() if S["active_status"] == "open" else {}
    live_pnl  = 0.0
    aslot     = _active_slot()
    opt_price = aslot["price"] if aslot else None

    if S["active_status"] == "open" and snap and opt_price:
        entry = snap.get("entry")
        qty   = snap.get("qty", BUY_QTY)
        if entry:
            live_pnl = round((opt_price - entry) * qty * LOT_SIZE, 2)

    cdrem = 0
    if S["cooldown_until"]:
        diff  = (S["cooldown_until"] - datetime.now()).total_seconds()
        cdrem = max(0, int(diff))

    ce_slot = S["slots"]["CE"]
    pe_slot = S["slots"]["PE"]

    return {
        "running":              S["running"],
        "trade_open":           S["trade_open"],
        "trading_mode":         S["trading_mode"],
        "index_name":           S["index_name"],
        "index_token":          S["index_token"],
        "last_order_id":        S["last_order_id"],
        "last_exit_order_id":   S["last_exit_order_id"],
        "target_order_id":      S["target_order_id"],
        "active_sides":         list(S["active_sides"]),

        "auto_jump_active":     RC["auto_jump"],
        "jump_atr":             S["jump_atr"],
        "jump_threshold_pts":   S["jump_threshold"],
        "hist_daily_atr":       _hist_daily_atr,
        "hist_spike_threshold": _hist_spike_threshold,
        "threshold_source":     S.get("_threshold_source", "live_atr"),
        "vix":                  _vix_tracker.score(),
        "option_chain":         _oi_analyzer.state,
        "multi_tf":             _mtf_analyzer.analyze("CE") if S.get("nifty_ticks") else {},

        "ce_token":             ce_slot["token"],
        "ce_strike":            ce_slot["strike"],
        "ce_price":             ce_slot["price"],
        "ce_volume":            ce_slot["volume"],
        "pe_token":             pe_slot["token"],
        "pe_strike":            pe_slot["strike"],
        "pe_price":             pe_slot["price"],
        "pe_volume":            pe_slot["volume"],

        "opt_side":             S["trade_side"],
        "opt_strike":           aslot["strike"] if aslot else "",
        "opt_price":            opt_price,

        "nifty_price":          S["nifty_price"],
        "nifty_prev":           S["nifty_prev_tick"],
        "nifty_ref":            S["nifty_ref"],
        "nifty_move":           S["nifty_move"],
        "nifty_slope":          round(S["regression_slope"], 3) if S["regression_slope"] is not None else None,
        "dynamic_jump_pts":     S["dynamic_jump_pts"],

        "pending_side":         S["pending_side"],
        "confirm_count":        S["confirm_count"],
        "confirm_needed":       S["confirm_needed"],
        "last_skip_reason":     S["last_skip_reason"],
        "fast_entry":           S["fast_entry"],
        "cooldown_active":      cdrem > 0,
        "cooldown_remaining":   cdrem,

        "active_side":          S["active_side"],
        "active_status":        S["active_status"],
        "session_pnl":          S["session_pnl"],
        "day_start_capital":    S["day_start_capital"],
        "trade_pnl":            S["trade_pnl"],
        "trades_today":         S["trades_today"],
        "wins":                 S["wins"],
        "losses":               S["losses"],
        "capital":              S["exit_engine"].capital,
        "live_pnl":             live_pnl,

        "entry":                snap.get("entry"),
        "target_price":         round(snap["entry"] * 1.025, 2) if snap.get("entry") and S["target_order_id"] else None,
        "sl":                   snap.get("sl"),
        "sl_pct":               snap.get("sl_pct"),
        "qty":                  snap.get("qty"),
        "phase2":               snap.get("phase2", False),
        "peak_price":           snap.get("peak_price"),
        "peak_profit":          snap.get("peak_profit"),
        "peak_profit_pct":      snap.get("peak_profit_pct"),
        "trail_price":          snap.get("trail_price"),
        "trail_pct":            snap.get("trail_pct",         BUY_TRAIL_PCT),
        "atr_trail_pct":        snap.get("atr_trail_pct",     BUY_TRAIL_PCT),
        "profit_trail_pct":     snap.get("profit_trail_pct"),
        "min_trail_pct_reached":snap.get("min_trail_pct_reached"),
        "option_atr":           snap.get("option_atr",        S["option_atr"]),
        "held_secs":            snap.get("held_secs", 0),
        "momentum_score":       snap.get("momentum_score"),
        "move_type":            snap.get("move_type"),
        "breakeven_moved":      snap.get("breakeven_moved", False),

        "jump_pct":             RC["jump_pct"],
        "sl_pct_p1":            RC["sl_phase1_pct"],
        "sl_pct_p2":            RC["sl_phase2_pct"],
        "sl_phase1_secs":       RC["sl_phase1_secs"],
        "trail_pct_high":       RC["trail_pct_high"],
        "trail_pct_low":        RC["trail_pct_low"],
        "trail_atr_high":       RC["trail_atr_high"],
        "trail_atr_low":        RC["trail_atr_low"],
        "profit_trail_threshold": RC["profit_trail_threshold"],
        "confirm_ticks_fast":   RC["confirm_ticks_fast"],
        "confirm_ticks_mid":    RC["confirm_ticks_mid"],
        "confirm_ticks_slow":   RC["confirm_ticks_slow"],
        "confirm_atr_high":     RC["confirm_atr_high"],
        "confirm_atr_low":      RC["confirm_atr_low"],
        "regression_window":    RC["regression_window"],
        "sl_cooldown_secs":     RC["sl_cooldown_secs"],
        "max_daily_loss":       RC["max_daily_loss"],
        "profit_target":        RC["daily_profit_target"],
        "ws_connected":         S["ws_connected"],
        "trade_start":          f"{RC['trade_start_h']:02d}:{RC['trade_start_m']:02d}",
        "trade_end":            f"{RC['trade_end_h']:02d}:{RC['trade_end_m']:02d}",
        "force_exit_time":      f"{RC['force_exit_h']:02d}:{RC['force_exit_m']:02d}",
        "ai_entry_score":       S.get("ai_entry_score"),
        "ai_regime":            S.get("ai_regime", "unknown"),
        "ai_brain":             _market_brain.state,
        "entry_analyzer":       _entry_analyzer.state,
        "entry_verdict":        S.get("entry_verdict"),
        "market_intel":         _market_intel.state,
        "adopted_position":     S.get("adopted_position"),
        "trade_budget":         _market_intel.get_context({
            "trades_today": S["trades_today"], "wins": S["wins"],
            "losses": S["losses"], "session_pnl": S["session_pnl"],
            "loss_streak": S.get("loss_streak", 0), "capital": S["capital"],
            "day_start_capital": S["day_start_capital"],
        }).get("trade_budget", {}),

        # New config constants for UI display
        "breakeven_trigger_pct": BREAKEVEN_TRIGGER_PCT,
        "nifty_reversal_exit":   NIFTY_REVERSAL_EXIT,
        "fast_move_velocity":    FAST_MOVE_VELOCITY,

        "nifty_atm":            S.get("nifty_atm"),
        "atm_pending":          S.get("atm_pending", False),
        "rsi":                  _compute_rsi(list(S["nifty_ticks"])),
        "range_position":       _session_range_position(list(S["nifty_ticks"])),
        "vol_detector":         _vol_detector.range_info(),
    }


# ── Broadcast ─────────────────────────────────────────────────────────────────
def broadcast():
    """Build state snapshot under the lock, emit outside it."""
    with _state_lock:
        payload = _build_state_payload()
    socketio.emit("state", payload, namespace="/")


# ── Order intent executor ─────────────────────────────────────────────────────
def _run_order_intent(intent, opened_payload=None, closed_payload=None):
    """
    Execute a pending order intent (buy or sell) outside _state_lock.

    BUY flow:
      1. Place MARKET BUY
      2. On success → immediately place LIMIT SELL at entry + 2.5% (profit target)
         The target order stands in Zerodha until either:
           a) Price hits +2.5%  → exchange fills it automatically
           b) Bot's own exit fires → we cancel it first, then MARKET SELL

    SELL flow (bot-triggered exit: SL / trail / timeout):
      1. Cancel standing target LIMIT SELL order (if any)
      2. Place MARKET SELL
    """
    if not intent:
        return None

    symbol = intent["symbol"]
    qty    = intent["qty"]
    action = intent["action"]

    if not symbol:
        log("⚠ No tradingsymbol for real order", "error")
        return None

    if S["trading_mode"] == "real":
        if action == "buy":
            log(f"📡 REAL BUY — {symbol}  qty={qty}", "warning")
            oid, err = place_buy(symbol, qty)

            if oid:
                with _state_lock:
                    S["last_order_id"]   = oid
                    S["target_order_id"] = None   # reset any stale target
                log(f"✅ BUY confirmed — order_id={oid}", "success")
                if opened_payload:
                    opened_payload["order_id"] = oid

                # ── Place standing LIMIT SELL at entry + 2.5% ─────────────
                entry_price = intent.get("entry_price")
                if entry_price and entry_price > 0:
                    target_price = round(entry_price * 1.025, 2)
                    t_oid, t_err = place_limit_sell(symbol, qty, target_price)
                    if t_oid:
                        with _state_lock:
                            S["target_order_id"] = t_oid
                        log(
                            f"🎯 Target SELL placed — ₹{target_price:.2f} (+2.5%)  "
                            f"order_id={t_oid}",
                            "success",
                        )
                    else:
                        log(f"⚠ Target SELL failed — {t_err}", "warning")
                else:
                    log("⚠ No entry price for target order — skipped", "warning")
            else:
                log(f"❌ BUY order FAILED — {err}", "error")
            return oid

        else:  # sell (bot-triggered exit)
            # Cancel standing target LIMIT SELL before placing MARKET SELL
            with _state_lock:
                target_oid = S.get("target_order_id")
                S["target_order_id"] = None

            if target_oid:
                ok, c_err = cancel_order(target_oid)
                if ok:
                    log(f"🗑 Target order cancelled — order_id={target_oid}", "info")
                else:
                    # May already be filled or expired — log but continue
                    log(f"⚠ Cancel target order failed ({c_err}) — may already be filled", "warning")

            log(f"📡 REAL SELL — {symbol}  qty={qty}", "warning")
            oid, err = place_sell(symbol, qty)

            if oid:
                with _state_lock:
                    S["last_exit_order_id"] = oid
                log(f"✅ SELL confirmed — order_id={oid}", "success")
                if closed_payload:
                    closed_payload["exit_order_id"] = oid
            else:
                log(f"❌ SELL order FAILED — {err}", "error")
            return oid

    else:
        log(f"🔵 DEMO — no real {'buy' if action == 'buy' else 'sell'} placed", "info")
        return None


# ── Tick processor ────────────────────────────────────────────────────────────
def process_ticks(ticks):
    trading_active = S["running"] or S["trade_open"]

    if not trading_active:
        # Robot stopped — still update prices for live dashboard display
        _need_atm = False
        for tick in ticks:
            token = tick.get("instrument_token")
            price = tick.get("last_price")
            if price is None:
                continue
            with _state_lock:
                if token == NIFTY_TOKEN or token == S.get("index_token"):
                    S["nifty_price"] = price
                    S["nifty_ticks"].append(price)
                    # Auto-resolve ATM if not yet resolved
                    if not S.get("nifty_atm") and not S.get("_idle_atm_pending"):
                        S["_idle_atm_pending"] = True
                        _need_atm = price
                # Update CE/PE slot prices even when stopped
                for side in ("CE", "PE"):
                    sl = S["slots"][side]
                    if token == sl["token"]:
                        sl["price"] = price
        if _need_atm:
            def _idle_resolve(p=_need_atm):
                atm = _resolve_nifty_atm(p)
                if atm:
                    _apply_nifty_atm(atm)
                    log(f"[ATM] Auto-resolved (idle): strike={atm['strike']}", "info")
                with _state_lock:
                    S["_idle_atm_pending"] = False
            threading.Thread(target=_idle_resolve, daemon=True).start()
        broadcast()
        return

    trade_opened_payload = None
    trade_closed_payload = None
    order_intent         = None
    log_entries          = []
    daily_reset_fired    = False

    for tick in ticks:
        token  = tick.get("instrument_token")
        price  = tick.get("last_price")
        volume = tick.get("volume_traded") or tick.get("volume") or 0
        if price is None:
            continue

        with _state_lock:
            if token == NIFTY_TOKEN or token == S["index_token"]:
                S["nifty_price"] = price
                S["nifty_ticks"].append(price)
                S["nifty_tick_times"].append(datetime.now())
                _vol_detector.add(price)
                _mtf_analyzer.add_tick(price)
                S["nifty_atr_ticks"].append(price)
                S["regression_slope"] = _regression_slope(list(S["nifty_ticks"]))
                _update_jump_threshold(price)

                # ── Auto-resolve ATM on first tick ────────────────────────────
                if S.get("atm_pending") and S["running"]:
                    _snap_price = price
                    def _do_resolve(p=_snap_price):
                        atm = _resolve_nifty_atm(p)
                        if atm:
                            _apply_nifty_atm(atm)
                        else:
                            log("⚠ ATM resolve failed — retry next tick", "warning")
                            with _state_lock:
                                S["atm_pending"] = True   # retry
                    S["atm_pending"] = False   # prevent re-triggering until thread finishes
                    threading.Thread(target=_do_resolve, daemon=True).start()

                # ── Auto-roll ATM when NIFTY drifts ≥ 50 pts from current strike ──
                atm_info = S.get("nifty_atm")
                if (S["running"] and not S["trade_open"]
                        and atm_info and not S.get("atm_pending")):
                    current_strike = atm_info.get("strike", 0)
                    if current_strike and abs(price - current_strike) >= 50:
                        _snap_price2 = price
                        def _do_roll(p=_snap_price2):
                            atm = _resolve_nifty_atm(p)
                            if atm:
                                _apply_nifty_atm(atm)
                        S["atm_pending"] = True   # block re-trigger during roll
                        threading.Thread(target=_do_roll, daemon=True).start()

                # ── Auto daily reset ──────────────────────────────────────
                today = datetime.now().date()
                if today != S["trading_date"]:
                    S["trading_date"]     = today
                    S["day_start_capital"] = S["capital"]  # snapshot for 2.5% target
                    S["session_pnl"]      = 0.0
                    S["trades_today"]     = 0
                    S["wins"]             = 0
                    S["losses"]           = 0
                    S["trade_pnl"]        = 0.0
                    S["cooldown_until"]   = None
                    S["last_skip_reason"] = None
                    with _log_lock:
                        S["logs"] = []   # clear log buffer for new day
                    daily_reset_fired = True

                # ── Periodic position sync (real mode, every 30s) ────────
                if (S["trading_mode"] == "real"
                        and S["running"]
                        and _time.time() - S.get("_pos_sync_ts", 0) > 30):
                    S["_pos_sync_ts"] = _time.time()
                    threading.Thread(target=_sync_zerodha_positions, daemon=True).start()

                if S["running"] and not S["trade_open"]:
                    result = _check_spike(price)
                    if result:
                        trade_opened_payload = result.get("trade_opened_payload")
                        order_intent         = result.get("order_intent")
                        log_entries.extend(result.get("logs", []))
                # Check force exit once per Nifty tick (trade must be open)
                if S["trade_open"]:
                    force_result = _check_force_exit_state()
                    if force_result and not trade_closed_payload:
                        trade_closed_payload = force_result.get("trade_closed_payload")
                        order_intent         = force_result.get("order_intent")
                        log_entries.extend(force_result.get("logs", []))

            # VIX tick
            if token == VIX_TOKEN:
                _vix_tracker.on_tick(price)

            for side in ("CE", "PE"):
                sl = _slot(side)
                if token == sl["token"]:
                    sl["price"]  = price
                    sl["volume"] = volume
                    if volume:
                        sl["vol_history"].append(volume)
                    sl["price_history"].append(price)
                    if S["trade_side"] == side:
                        S["option_atr"] = _compute_option_atr(list(sl["price_history"]))

            if S["active_status"] == "open":
                aslot = _active_slot()
                if aslot and aslot["price"] is not None:

                    # NIFTY reversal exit: spike has failed when NIFTY retraces
                    # meaningfully past the entry reference level.
                    # Requires a buffer (fraction of spike threshold) to avoid
                    # exiting on 1-tick noise. Also skips if option already in profit.
                    if (NIFTY_REVERSAL_EXIT
                            and S["nifty_entry_price"] is not None
                            and S["nifty_price"] is not None
                            and not trade_closed_payload):
                        side      = S["trade_side"]
                        nifty_now = S["nifty_price"]
                        nifty_ref = S["nifty_entry_price"]
                        # Buffer = fraction of the spike threshold (not just 0 pts)
                        jmp_thr   = S.get("jump_threshold") or RC["jump_min_pts"]
                        buffer    = jmp_thr * NIFTY_REVERSAL_BUFFER_PCT
                        reversal  = (
                            (side == "CE" and nifty_now < nifty_ref - buffer) or
                            (side == "PE" and nifty_now > nifty_ref + buffer)
                        )
                        # Skip reversal exit if option already in good profit
                        if reversal:
                            snap = S["exit_engine"].leg_snapshot()
                            if snap.get("peak_profit_pct", 0) >= NIFTY_REVERSAL_PROFIT_SKIP_PCT:
                                reversal = False
                        if reversal:
                            exit_result = S["exit_engine"].force_close(
                                aslot["price"], "nifty_reversal"
                            )
                            if exit_result:
                                closed = _on_trade_closed_state(exit_result)
                                trade_closed_payload = closed.get("trade_closed_payload")
                                order_intent         = closed.get("order_intent")
                                log_entries.extend(closed.get("logs", []))

                    if not trade_closed_payload:
                        exit_result = S["exit_engine"].on_price(aslot["price"])
                        if exit_result:
                            if exit_result.get("event_type") == "partial":
                                # Partial profit booking — trade stays open
                                partial = _on_partial_exit(exit_result)
                                log_entries.extend(partial.get("logs", []))
                                if partial.get("order_intent"):
                                    # Queue partial sell — will be executed after lock
                                    S["_partial_order"] = partial["order_intent"]
                            else:
                                closed = _on_trade_closed_state(exit_result)
                                trade_closed_payload = closed.get("trade_closed_payload")
                                order_intent         = closed.get("order_intent")
                                log_entries.extend(closed.get("logs", []))

    # ── All I/O after lock is fully released ──────────────────────────────────

    if daily_reset_fired:
        today_str = datetime.now().strftime("%d %b %Y")
        socketio.emit("daily_reset", {"date": today_str}, namespace="/")
        log(f"📅 New trading day {today_str} — session and logs cleared", "info")

    # ── Execute partial order if queued (outside lock) ───────────────────────
    partial_order = None
    with _state_lock:
        partial_order = S.pop("_partial_order", None)
    if partial_order and S.get("trading_mode") == "real":
        try:
            place_sell(partial_order["symbol"], partial_order["qty"])
            log(f"  ↳ partial sell placed {partial_order['qty']} units", "info")
        except Exception as e:
            log(f"  ↳ partial sell error: {e}", "error")

    for msg, level in log_entries:
        log(msg, level)

    if trade_opened_payload:
        socketio.emit("trade_opened", trade_opened_payload, namespace="/")

    _run_order_intent(order_intent, trade_opened_payload, trade_closed_payload)

    if trade_closed_payload:
        socketio.emit("trade_closed", trade_closed_payload, namespace="/")

    broadcast()


# ── Filters ───────────────────────────────────────────────────────────────────
def _is_valid_time():
    now   = datetime.now().time()
    start = dtime(RC["trade_start_h"], RC["trade_start_m"])
    end   = dtime(RC["trade_end_h"],   RC["trade_end_m"])
    return start <= now <= end


def _is_in_cooldown():
    if not S["cooldown_until"]:
        return False
    if datetime.now() < S["cooldown_until"]:
        remaining = int((S["cooldown_until"] - datetime.now()).total_seconds())
        S["last_skip_reason"] = f"Cooldown — {remaining}s remaining"
        return True
    S["cooldown_until"] = None
    return False


def _regression_confirms(side):
    slope = S["regression_slope"]
    if slope is None:
        return True
    mn = RC["regression_slope_min"]
    return slope >= mn if side == "CE" else slope <= -mn


def _option_volume_confirms(side):
    sl       = _slot(side)
    vol_hist = sl["vol_history"]
    cur_vol  = sl["volume"]
    if len(vol_hist) < 5 or cur_vol == 0:
        return True
    avg = sum(vol_hist) / len(vol_hist)
    return cur_vol >= avg * RC["option_vol_factor"]


def _check_daily_limits():
    """Must be called under _state_lock. Returns False and mutates S if limit hit."""
    if S["session_pnl"] <= -RC["max_daily_loss"]:
        S["last_skip_reason"] = f"Daily loss limit ₹{RC['max_daily_loss']} hit"
        S["running"] = False
        _write_daily_summary_async("Loss limit hit")
        return False
    # Max drawdown protection
    if MAX_DRAWDOWN_PCT > 0:
        drawdown_limit = S["day_start_capital"] * MAX_DRAWDOWN_PCT / 100
        if S["session_pnl"] <= -drawdown_limit:
            S["last_skip_reason"] = f"Max drawdown {MAX_DRAWDOWN_PCT}% hit — ₹{S['session_pnl']:+.2f}"
            S["running"] = False
            _write_daily_summary_async("Max drawdown hit")
            return False
    # Daily profit target (0 = disabled — no limit)
    if DAILY_PROFIT_PCT > 0:
        day_target = round(S["day_start_capital"] * DAILY_PROFIT_PCT, 2)
        if S["session_pnl"] >= day_target:
            S["last_skip_reason"] = (
                f"Daily target hit: ₹{S['session_pnl']:+.2f} "
                f"(target={DAILY_PROFIT_PCT*100}% of ₹{S['day_start_capital']:,.0f} = ₹{day_target:.0f})"
            )
            S["running"] = False
            _write_daily_summary_async("Target achieved")
            return False
    return True


def _write_daily_summary_async(reason: str):
    """Fire-and-forget: write daily summary to Excel outside the lock."""
    snap_pnl     = S["session_pnl"]
    snap_capital = S["day_start_capital"]
    snap_trades  = S["trades_today"]
    snap_wins    = S["wins"]
    snap_losses  = S["losses"]

    def _do():
        try:
            from excel_logger import write_daily_summary
            write_daily_summary(snap_pnl, snap_capital, snap_trades,
                                snap_wins, snap_losses, reason)
        except Exception as exc:
            app.logger.warning(f"Excel daily summary failed: {exc}")

    threading.Thread(target=_do, daemon=True).start()


def _check_force_exit_state():
    """
    Called under _state_lock.
    Returns closed result dict (trade_closed_payload, order_intent, logs) or None.
    """
    if not S["trade_open"]:
        return None
    now        = datetime.now().time()
    force_exit_t = dtime(RC["force_exit_h"], RC["force_exit_m"])
    if now >= force_exit_t:
        aslot = _active_slot()
        price = aslot["price"] if aslot else 0
        result = S["exit_engine"].force_close(price or 0, "force_exit")
        if result:
            return _on_trade_closed_state(result)
    return None


# ── Spike detector ────────────────────────────────────────────────────────────
def _check_spike(current):
    """
    Called under _state_lock.
    Returns a dict with keys: trade_opened_payload, order_intent, logs
    or None if no trade triggered.
    """
    prev = S["nifty_prev_tick"]
    S["nifty_prev_tick"] = current
    if prev is None:
        return None
    # ── Intelligent trade budget (replaces hard max_trades_day) ─────
    _session_stats = {
        "trades_today":      S["trades_today"],
        "wins":              S["wins"],
        "losses":            S["losses"],
        "session_pnl":       S["session_pnl"],
        "loss_streak":       S.get("loss_streak", 0),
        "capital":           S["capital"],
        "day_start_capital": S["day_start_capital"],
    }
    _budget = _market_intel.get_context(_session_stats).get("trade_budget", {})
    if _budget.get("remaining", 1) <= 0:
        S["last_skip_reason"] = (
            f"Trade budget exhausted: {_budget.get('max_trades',0)} trades  "
            f"[{_budget.get('confidence','?')}]  "
            f"{' | '.join(_budget.get('reasons', [])[-2:])}"
        )
        return None
    if not _check_daily_limits():
        return None

    move = current - prev
    S["nifty_move"] = round(move, 2)

    if S["pending_side"] is not None:
        S["confirm_count"] += 1
        return _continue_confirmation(current)

    jump_pts = S["jump_threshold"]
    _is_trend_entry = False
    if abs(move) < jump_pts:
        # No spike — check for slow trend instead
        trend_side = _check_trend(current)
        if trend_side is None:
            return None
        side = trend_side
        _is_trend_entry = True
        S["fast_entry"] = True   # trend IS the confirmation — enter now
    else:
        # Spike speed validation
        speed_ok, speed_reason = _check_spike_speed(current)
        if not speed_ok:
            S["last_skip_reason"] = speed_reason
            return None
        side = "CE" if move > 0 else "PE"

    if side not in S["active_sides"]:
        S["last_skip_reason"] = f"Spike is {side} but {side} not selected"
        return None

    if _slot(side)["token"] is None:
        S["last_skip_reason"] = f"{side} spike but no token configured"
        return None

    if not _is_valid_time():
        S["last_skip_reason"] = (
            f"Time filter: outside "
            f"{RC['trade_start_h']:02d}:{RC['trade_start_m']:02d}–"
            f"{RC['trade_end_h']:02d}:{RC['trade_end_m']:02d}"
        )
        return None

    if _is_in_cooldown():
        return None

    if not _option_volume_confirms(side):
        S["last_skip_reason"] = f"{side} option volume too low"
        return None

    if not _regression_confirms(side):
        slope = S["regression_slope"]
        S["last_skip_reason"] = f"Slope {slope:+.3f} — against trend"
        return None

    # Option price quality filter
    opt_ok, opt_reason = _option_price_ok(side)
    if not opt_ok:
        S["last_skip_reason"] = opt_reason
        return None

    # Breakout filter
    brk_ok, brk_reason = _breakout_filter_ok(current)
    if not brk_ok:
        S["last_skip_reason"] = brk_reason
        return None

    S["last_skip_reason"] = None

    adaptive_ticks      = _adaptive_confirm_ticks(side)
    S["confirm_needed"] = adaptive_ticks

    prices  = list(S["nifty_ticks"])
    m_score = _momentum_score(prices, side)

    if m_score >= RC["momentum_min"]:
        S["nifty_ref"]  = prev
        S["fast_entry"] = True
    else:
        S["pending_side"]    = side
        S["confirm_count"]   = 1
        S["spike_ref_price"] = prev
        S["nifty_ref"]       = prev
        S["fast_entry"]      = False

    # ── AI Entry Scoring (MarketBrain) ──────────────────────────────────
    nifty_list = list(S["nifty_ticks"])
    ai_state = {
        "side":              side,
        "nifty_move":        S["nifty_move"],
        "jump_threshold":    S["jump_threshold"],
        "regression_slope":  S["regression_slope"],
        "nifty_ticks":       nifty_list,
        "nifty_price":       current,
        "jump_atr":          S.get("jump_atr"),
        "fast_entry":        S["fast_entry"],
        "rsi":               _compute_rsi(nifty_list),
        "range_position":    _session_range_position(nifty_list),
    }
    ai_score, ai_allow, ai_reason = _market_brain.score_entry(ai_state)
    S["ai_entry_score"] = round(ai_score, 3)
    S["ai_regime"]      = _market_brain._last_regime

    if not ai_allow:
        S["last_skip_reason"] = ai_reason
        _reset_pending()
        return None

    # ── Market Intelligence Gate ────────────────────────────────────
    intel_ctx = _market_intel.get_context()
    intel_rec = intel_ctx.get("recommendations", {})

    # Block entries if intelligence says avoid
    if intel_rec.get("avoid_entry"):
        S["last_skip_reason"] = f"INTEL: {' | '.join(intel_rec.get('reasons', ['avoid']))}"
        _reset_pending()
        return None

    # Entry bias: if intelligence has a direction preference, enforce it
    entry_bias = intel_rec.get("entry_bias", "neutral")
    if entry_bias != "neutral" and entry_bias != side:
        S["last_skip_reason"] = (
            f"INTEL: bias={entry_bias} but signal is {side}  "
            f"({intel_rec.get('day_type','?')} DTE={intel_ctx.get('day_info',{}).get('dte','?')})"
        )
        _reset_pending()
        return None

    # ── 8-Dimension Entry Analysis ───────────────────────────────────
    opt_slot = _slot(side)
    opt_hist = list(opt_slot["price_history"]) if opt_slot else []
    atm_info = S.get("nifty_atm") or {}

    # Gather VIX, OI, and multi-TF intelligence
    vix_data = _vix_tracker.score()
    oi_data = _oi_analyzer.analyze(current, side)
    mtf_data = _mtf_analyzer.analyze(side)

    analyzer_state = {
        "side":              side,
        "nifty_price":       current,
        "nifty_ticks":       nifty_list,
        "opt_price":         opt_slot["price"] if opt_slot else 0,
        "opt_price_history": opt_hist,
        "strike":            atm_info.get("strike", current),
        "capital":           S["capital"],
        "lot_size":          opt_slot.get("lot_size") or LOT_SIZE,
        "ai_score":          ai_score,
        "regime":            S.get("ai_regime", "unknown"),
        "loss_streak":       S.get("loss_streak", 0),
        "fast_entry":        S["fast_entry"],
        "nifty_move":        S["nifty_move"],
        "market_intel":      intel_ctx,
        "vix":               vix_data,
        "option_chain":      oi_data,
        "multi_tf":          mtf_data,
    }
    verdict = _entry_analyzer.analyze(analyzer_state)
    S["entry_verdict"] = verdict.to_dict()

    # Override verdict params with intelligence recommendations
    if intel_rec.get("sl_pct"):
        verdict.recommended_sl = intel_rec["sl_pct"]
    if intel_rec.get("trail_pct"):
        verdict.recommended_trail = intel_rec["trail_pct"]
    if intel_rec.get("timeout"):
        verdict.recommended_timeout = intel_rec["timeout"]
    if intel_rec.get("max_lots_mult", 1.0) != 1.0:
        verdict.recommended_lots = max(1, int(verdict.recommended_lots * intel_rec["max_lots_mult"]))

    if not verdict.allow:
        S["last_skip_reason"] = verdict.reason
        _reset_pending()
        return None

    day_info = intel_ctx.get("day_info", {})
    log_entries_ai = [
        (ai_reason, "info"),
        (verdict.reason, "trade"),
        (f"  INTEL: {day_info.get('day_type','?').upper()} DTE={day_info.get('dte','?')} "
         f"theta={day_info.get('theta_pressure','?')} decay={day_info.get('premium_decay_rate','?')}x "
         f"bias={entry_bias}", "info"),
    ]
    # Build greeks log
    g = verdict.greeks
    if g:
        log_entries_ai.append((
            f"  Greeks: D={g.get('delta',0):+.3f}  G={g.get('gamma',0):.4f}  "
            f"T={g.get('theta',0):.2f}  V={g.get('vega',0):.2f}  "
            f"moneyness={g.get('moneyness',1):.3f}",
            "info",
        ))
    rr = verdict.risk_reward
    if rr:
        log_entries_ai.append((
            f"  R:R={rr.get('rr_ratio',0):.1f}x  EV=₹{rr.get('ev',0):.1f}  "
            f"Kelly={rr.get('kelly',0)*100:.1f}%  risk/lot=₹{rr.get('risk_per_lot',0):.0f}",
            "info",
        ))

    # Pass analyzer's recommended params to the trade
    override = {
        "sl_pct_p1":     verdict.recommended_sl,
        "trail_pct":     verdict.recommended_trail,
        "timeout_secs":  verdict.recommended_timeout,
    }

    if S["fast_entry"]:
        result = _enter_trade_state(side, current, override_params=override)
        if result:
            result["logs"] = log_entries_ai + result.get("logs", [])
        return result

    return None


def _continue_confirmation(current):
    """Called under _state_lock. Returns trade result dict or None."""
    side      = S["pending_side"]
    ref_price = S["spike_ref_price"]
    count     = S["confirm_count"]
    needed    = S["confirm_needed"]
    jump_pts  = S["jump_threshold"]
    net_move  = current - ref_price
    threshold = jump_pts * RC["confirm_sustain_pct"]

    sustained = (net_move >= threshold) if side == "CE" else (net_move <= -threshold)

    if not sustained:
        _reset_pending()
        return None

    if count >= needed:
        result = _enter_trade_state(side, current)
        _reset_pending()
        return result

    return None


def _reset_pending():
    S["pending_side"]    = None
    S["confirm_count"]   = 0
    S["spike_ref_price"] = None


# ── Tiered SL ─────────────────────────────────────────────────────────────────

# ── Enter trade (state mutation only) ────────────────────────────────────────
def _enter_trade_state(side, nifty_price, override_params: dict | None = None):
    """
    Called under _state_lock.
    override_params: optional entry params (sl_pct_p1, sl_pct_p2,
                     sl_phase1_secs, trail_pct, timeout_secs).
    Returns {trade_opened_payload, order_intent, logs} — no I/O performed here.
    """
    sl        = _slot(side)
    opt_price = sl["price"]
    if opt_price is None:
        return None

    p    = override_params or {}
    seed = list(sl["price_history"])

    sl_pct_for_mm = p.get("sl_pct_p1", RC["sl_phase1_pct"])
    mm_lot_size   = sl.get("lot_size") or LOT_SIZE

    # Position sizing — ATR-based risk management or max-capital
    capital      = S["capital"]
    cost_per_lot = opt_price * mm_lot_size

    if ATR_POSITION_SIZING:
        # ATR-based: risk MAX_RISK_PER_TRADE of capital per trade
        # lots = capital × risk% / (option_ATR × lot_size)
        opt_atr = _compute_option_atr(seed) or (opt_price * 0.05)  # fallback 5% of price
        risk_amount = capital * MAX_RISK_PER_TRADE
        atr_lots = max(1, int(risk_amount / (opt_atr * mm_lot_size))) if opt_atr > 0 else 1
        # Also cap by available capital
        max_afford = max(1, int(capital / cost_per_lot)) if cost_per_lot > 0 else 1
        auto_lots = min(atr_lots, max_afford)
    else:
        auto_lots = max(1, int(capital / cost_per_lot)) if cost_per_lot > 0 else 1

    # Loss streak reduction
    streak = S.get("loss_streak", 0)
    if streak >= LOSS_STREAK_REDUCE_AFTER:
        reductions = min(streak - LOSS_STREAK_REDUCE_AFTER + 1, 3)
        auto_lots = max(1, int(auto_lots * (LOSS_STREAK_SIZE_MULT ** reductions)))

    cap_limit  = RC.get("max_lots_per_trade", 0)
    qty_lots   = min(auto_lots, cap_limit) if cap_limit > 0 else auto_lots

    info = S["exit_engine"].open_leg(
        side, opt_price,
        sl_pct_override         = sl_pct_for_mm,
        sl_pct_p2_override      = p.get("sl_pct_p2",      None),
        sl_phase1_secs_override = p.get("sl_phase1_secs", None),
        trail_pct_override      = p.get("trail_pct",      None),
        timeout_secs_override   = p.get("timeout_secs",   None),
        seed_prices=seed,
        symbol=S.get("index_name", ""),
        option_symbol=sl.get("symbol", ""),
        qty_override=qty_lots,
    )

    S["trade_open"]         = True
    S["trade_side"]         = side
    S["active_side"]        = side
    S["active_status"]      = "open"
    S["trades_today"]      += 1
    S["trade_open_time"]    = datetime.now()
    S["nifty_entry_price"]  = nifty_price   # record NIFTY level at entry
    S["option_atr"]         = _compute_option_atr(seed)
    S["last_order_id"]      = None
    S["mm_lot_size"]        = mm_lot_size   # stored for partial exit handler

    mode_label = "FAST" if S["fast_entry"] else f"CONFIRMED({S['confirm_needed']}t)"
    jmp_label  = (f"ATR-auto={S['jump_threshold']:.2f}pts" if RC["auto_jump"]
                  else f"fixed={S['jump_threshold']:.2f}pts")

    total_cost = round(opt_price * info["qty"] * mm_lot_size, 2)
    logs = [
        (
            f"▲ BUY {side} [{mode_label}]  entry=₹{opt_price:.2f}  "
            f"Nifty={nifty_price:.2f}  spike={S['nifty_move']:+.2f}pts  "
            f"threshold={jmp_label}  slope={S['regression_slope']:+.3f}",
            "trade",
        ),
        (
            f"  SL=₹{info['sl']:.2f} "
            f"({p.get('sl_pct_p1', RC['sl_phase1_pct'])}%"
            f"→{p.get('sl_pct_p2', RC['sl_phase2_pct'])}% "
            f"after {p.get('sl_phase1_secs', RC['sl_phase1_secs'])}s)  "
            f"qty={info['qty']} lot ({info['qty'] * mm_lot_size} units)  "
            f"capital=₹{capital:,.0f}  deployed=₹{total_cost:,.0f}  "
            f"trail={p.get('trail_pct', RC['trail_pct_low'])}% "
            f"timeout={p.get('timeout_secs', RC.get('trade_timeout_secs', 60))}s",
            "info",
        ),
    ]

    trade_opened_payload = {
        "side":         side,
        "entry":        opt_price,
        "sl":           info["sl"],
        "qty":          info["qty"],
        "nifty_ref":    S["nifty_ref"],
        "nifty_now":    nifty_price,
        "move":         S["nifty_move"],
        "slope":        S["regression_slope"],
        "fast_entry":   S["fast_entry"],
        "confirm_used": S["confirm_needed"],
        "trading_mode": S["trading_mode"],
        "order_id":     None,   # filled in after real order placed
    }

    order_intent = {
        "action":      "buy",
        "symbol":      sl["symbol"],
        "qty":         info["qty"] * mm_lot_size,
        "side":        side,
        "entry_price": opt_price,   # used for +2.5% limit sell target
    } if sl["symbol"] else None

    # Set trend cooldown to prevent re-firing
    S["trend_cooldown"] = TREND_COOLDOWN_TICKS

    return {
        "trade_opened_payload": trade_opened_payload,
        "order_intent":         order_intent,
        "logs":                 logs,
    }


# ── Trade closed (state mutation only) ───────────────────────────────────────
def _on_trade_closed_state(result):
    """
    Called under _state_lock.
    Returns {trade_closed_payload, order_intent, logs} — no I/O performed here.
    """
    side            = result["side"]
    pnl             = result["pnl"]
    reason          = result["reason"]
    sl_pct_used     = result.get("sl_pct",        RC["sl_phase1_pct"])
    trail_pct_used  = result.get("trail_pct",      BUY_TRAIL_PCT)
    atr_trail_used  = result.get("atr_trail_pct",  BUY_TRAIL_PCT)
    prof_trail_used = result.get("profit_trail_pct")
    min_trail       = result.get("min_trail_pct_reached")
    atr_at_exit     = result.get("option_atr")
    peak_pct        = result.get("peak_profit_pct", 0)

    trail_label = f"trail={trail_pct_used}% (min_reached={min_trail}%)"
    if prof_trail_used is not None:
        trail_label += f" [ATR={atr_trail_used}% / Profit={prof_trail_used}%]"

    reason_label = {
        "sl":         f"SL hit ({sl_pct_used}%)",
        "trail":      f"Trail exit ({trail_label}  atr={atr_at_exit})",
        "timeout":    f"Timeout (>{RC['trade_timeout_secs']}s)",
        "manual":     "Manual exit (button)",
        "force_exit": "Force-exit (market close)",
        "momentum_stall": "Momentum stalled (no new high)",
        "volume_dryup":   "Volume dried up (activity collapsed)",
        "ai_analyzer":    "AI Analyzer exit (multi-signal)",
    }.get(reason, reason)

    color = "success" if pnl >= 0 else "error"

    logs = [
        (
            f"{'▲' if pnl >= 0 else '▼'} EXIT {side}  "
            f"₹{result['entry']:.2f}→₹{result['exit_price']:.2f}  "
            f"P&L=₹{pnl:+.2f} ({result.get('pnl_pct', 0):+.1f}%)  "
            f"peak={peak_pct:+.1f}%  held={result.get('held_secs', 0)}s  [{reason_label}]",
            color,
        ),
    ]

    sl_sym   = _slot(side)["symbol"]
    real_qty = (result.get("qty") or BUY_QTY) * LOT_SIZE

    S["trade_pnl"]          = pnl
    S["session_pnl"]        = round(S["session_pnl"] + pnl, 2)
    S["active_status"]      = f"closed_{reason}"
    S["capital"]            = result["equity_after"]
    S["trade_open_time"]    = None
    S["nifty_entry_price"]  = None
    S["last_exit_order_id"] = None

    # Teach the global market brain
    _market_brain.on_trade_closed(result)
    brain = _market_brain.state

    # Teach the entry analyzer
    _entry_analyzer.on_trade_closed(result)
    _market_intel.on_trade_closed(result)
    ea = _entry_analyzer.state

    logs.append((
        f"[AI] learned — brain: {brain['n_trades']}t/{brain['win_rate']:.0%}wr  "
        f"analyzer: {ea['n_trades']}t/{ea['win_rate']:.0%}wr  "
        f"regime={brain['last_regime']}",
        "info",
    ))

    if pnl >= 0:
        S["wins"] += 1
        S["loss_streak"] = 0  # reset streak on win
    else:
        S["losses"] += 1
        S["loss_streak"] = S.get("loss_streak", 0) + 1

    # ── Smart reason-aware cooldown ───────────────────────────────────────────
    if SMART_COOLDOWN_ENABLED:
        _cd = {
            "sl":              COOLDOWN_AFTER_SL,
            "trail":           COOLDOWN_AFTER_TRAIL_WIN  if pnl >= 0 else COOLDOWN_AFTER_TRAIL_LOSS,
            "timeout":         COOLDOWN_AFTER_TIMEOUT_WIN if pnl >= 0 else COOLDOWN_AFTER_TIMEOUT_LOSS,
            "ai_exit":         COOLDOWN_AFTER_AI_EXIT,
            "nifty_reversal":  COOLDOWN_AFTER_REVERSAL,
            "momentum_stall":  COOLDOWN_AFTER_TRAIL_WIN,   # quick re-entry
            "volume_dryup":    COOLDOWN_AFTER_TRAIL_WIN,   # quick re-entry
            "ai_analyzer":     COOLDOWN_AFTER_AI_EXIT,     # AI-driven exit
            "manual":          0,
            "force_exit":      0,
        }.get(reason, 60)
    else:
        # Legacy flat cooldown on SL only
        _cd = RC["sl_cooldown_secs"] if reason == "sl" else 0

    if _cd > 0:
        S["cooldown_until"] = datetime.now() + timedelta(seconds=_cd)
        logs.append((f"⏳ Cooldown {_cd}s [{reason}]", "warning"))

    logs.append((
        f"Detector reset ₹{S['nifty_price']:.2f}  |  "
        f"Session ₹{S['session_pnl']:+.2f}  "
        f"Trades {S['trades_today']}  ({S['wins']}W/{S['losses']}L)",
        "info",
    ))

    _check_daily_limits()

    S["nifty_prev_tick"] = S["nifty_price"]
    S["nifty_move"]      = None
    S["trade_open"]      = False
    S["trade_side"]      = None
    S["active_side"]     = None
    S["option_atr"]      = None
    S["fast_entry"]      = False
    S["exit_engine"].reset()
    _reset_pending()

    trade_closed_payload = {
        **result,
        "trading_mode":  S["trading_mode"],
        "exit_order_id": None,   # filled in after real order placed
    }

    order_intent = {
        "action": "sell",
        "symbol": sl_sym,
        "qty":    real_qty,
        "side":   side,
    } if sl_sym else None

    return {
        "trade_closed_payload": trade_closed_payload,
        "order_intent":         order_intent,
        "logs":                 logs,
    }


# ── Partial profit booking (state mutation only) ─────────────────────────────
def _on_partial_exit(partial_result: dict) -> dict:
    """
    Called under _state_lock when exit_engine fires a partial booking event.
    Trade stays open — only a portion of the position is sold.
    Returns {logs, order_intent} — no I/O performed here.
    """
    side        = partial_result.get("side", S.get("trade_side", "CE"))
    price       = partial_result["price"]
    partial_qty = partial_result["partial_qty"]
    remaining   = partial_result["remaining_qty"]
    peak_pct    = partial_result["peak_profit_pct"]
    reason_lbl  = partial_result["reason"].replace("_", " ").upper()
    sl_sym      = _active_slot()["symbol"] if _active_slot() else ""
    lot_sz      = _active_slot().get("lot_size") or S.get("mm_lot_size") or LOT_SIZE

    pnl_per_lot  = round((price - S["exit_engine"]._leg["entry"]) * lot_sz, 2)
    partial_pnl  = round(pnl_per_lot * partial_qty, 2)

    logs = [
        (
            f"📊 {reason_lbl}  sell {partial_qty} lot @ ₹{price:.2f}  "
            f"P&L≈₹{partial_pnl:+.2f}  peak={peak_pct:+.1f}%  "
            f"remaining={remaining} lot",
            "success",
        ),
        (f"  SL locked to ₹{partial_result['new_sl']:.2f}", "info"),
    ]

    order_intent = {
        "action": "sell",
        "symbol": sl_sym,
        "qty":    partial_qty * lot_sz,
        "side":   side,
    } if sl_sym else None

    return {"logs": logs, "order_intent": order_intent}


# ── Close active trade helper (shared by stop and manual exit) ────────────────
def _close_active_trade(reason="manual"):
    """
    Close the currently open trade and return (closed_result, order_intent).
    Must be called from a SocketIO event handler (outside _state_lock).
    """
    trade_closed_payload = None
    order_intent         = None

    with _state_lock:
        if S["active_status"] != "open":
            return None, None
        aslot = _active_slot()
        price = aslot["price"] if aslot else 0
        result = S["exit_engine"].force_close(price or 0, reason)
        if result:
            closed = _on_trade_closed_state(result)
            trade_closed_payload = closed.get("trade_closed_payload")
            order_intent         = closed.get("order_intent")
            for msg, level in closed.get("logs", []):
                log(msg, level)

    _run_order_intent(order_intent, closed_payload=trade_closed_payload)

    if trade_closed_payload:
        socketio.emit("trade_closed", trade_closed_payload, namespace="/")

    return trade_closed_payload, order_intent


# ── WebSocket with auto-reconnect ─────────────────────────────────────────────
def _ws_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    S["ws_loop"] = loop
    send_q = asyncio.Queue()
    recv_q = asyncio.Queue()
    S["ws_send_queue"] = send_q

    async def _forward():
        while True:
            msg = await recv_q.get()
            if isinstance(msg, dict) and "__event__" in msg:
                ev        = msg["__event__"]
                connected = (ev == "connected")
                with _state_lock:
                    S["ws_connected"] = connected
                socketio.emit("ws_status", {"connected": connected}, namespace="/")
                log(f"WebSocket {'connected ✔' if connected else 'disconnected ✖'}",
                    "success" if connected else "error")
                if connected and S["subscribed_tokens"]:
                    await send_q.put({"a": "subscribe", "v": S["subscribed_tokens"]})
                    log(f"Subscribed: {S['subscribed_tokens']}", "info")
            elif isinstance(msg, list):
                process_ticks(msg)

    async def _main_with_reconnect():
        delay = 2
        while True:
            try:
                await asyncio.gather(
                    connect_zerodha_websocket(send_q, recv_q),
                    _forward(),
                )
            except Exception as e:
                log(f"WS error: {e} — reconnecting in {delay}s", "error")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                delay = 2

    loop.run_until_complete(_main_with_reconnect())


def _subscribe(tokens):
    with _state_lock:
        existing = set(S["subscribed_tokens"])
        existing.update(tokens)
        S["subscribed_tokens"] = list(existing)
    if S["ws_send_queue"] and S["ws_loop"]:
        asyncio.run_coroutine_threadsafe(
            S["ws_send_queue"].put({"a": "subscribe", "v": tokens}),
            S["ws_loop"],
        )


def _unsubscribe(tokens):
    with _state_lock:
        stored = set(S["subscribed_tokens"])
        for t in tokens:
            stored.discard(t)
        S["subscribed_tokens"] = list(stored)
    if S["ws_send_queue"] and S["ws_loop"] and tokens:
        asyncio.run_coroutine_threadsafe(
            S["ws_send_queue"].put({"a": "unsubscribe", "v": tokens}),
            S["ws_loop"],
        )


# ── Config persistence helpers ────────────────────────────────────────────────
import os as _os
# When frozen (exe), write next to the exe. When running as .py, use script dir.
CONFIG_FILE = _os.path.join(
    _os.path.dirname(_os.path.abspath(_sys.executable))
    if getattr(_sys, "frozen", False)
    else _os.path.dirname(_os.path.abspath(__file__)),
    "config.py",
)


def _read_config_source():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_config_key(key_upper, new_value):
    src   = _read_config_source()
    lines = src.splitlines()
    out   = []
    found = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(key_upper + " ") or stripped.startswith(key_upper + "="):
            indent = len(line) - len(stripped)
            if isinstance(new_value, bool):
                out.append(" " * indent + f'{key_upper} = {new_value}')
            elif isinstance(new_value, str):
                out.append(" " * indent + f'{key_upper} = "{new_value}"')
            else:
                out.append(" " * indent + f'{key_upper} = {new_value}')
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f'{key_upper} = {repr(new_value)}')
    with open(CONFIG_FILE, "w") as f:
        f.write("\n".join(out) + "\n")


def _write_enctoken(new_enctoken: str):
    """Persist enctoken to enctoken.dat and update in-memory ZERODHA_CONFIG immediately."""
    with open(_ENCTOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(new_enctoken)
    ZERODHA_CONFIG["enctoken"] = new_enctoken


# ── SocketIO events ───────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    with _state_lock:
        payload = _build_state_payload()
        with _log_lock:
            payload["logs"] = list(S["logs"])
    emit("state", payload)


@socketio.on("get_config")
def on_get_config():
    import config as cfg_mod
    zc = getattr(cfg_mod, "ZERODHA_CONFIG", {})
    with _state_lock:
        payload = {
            "enctoken":         zc.get("enctoken", ""),
            "auto_jump":            RC["auto_jump"],
            "jump_atr_window":      RC["jump_atr_window"],
            "jump_atr_multiplier":  RC["jump_atr_multiplier"],
            "jump_min_pts":         RC["jump_min_pts"],
            "jump_max_pts":         RC["jump_max_pts"],
            "jump_pct":             RC["jump_pct"],
            "confirm_sustain_pct":  RC["confirm_sustain_pct"],
            "confirm_atr_high":     RC["confirm_atr_high"],
            "confirm_atr_low":      RC["confirm_atr_low"],
            "confirm_ticks_fast":   RC["confirm_ticks_fast"],
            "confirm_ticks_mid":    RC["confirm_ticks_mid"],
            "confirm_ticks_slow":   RC["confirm_ticks_slow"],
            "momentum_window":      RC["momentum_window"],
            "momentum_min":         RC["momentum_min"],
            "sl_phase1_pct":        RC["sl_phase1_pct"],
            "sl_phase1_secs":       RC["sl_phase1_secs"],
            "sl_phase2_pct":        RC["sl_phase2_pct"],
            "trail_atr_high":       RC["trail_atr_high"],
            "trail_atr_low":        RC["trail_atr_low"],
            "trail_pct_high":       RC["trail_pct_high"],
            "trail_pct_low":        RC["trail_pct_low"],
            "buy_trail_pct":        RC["buy_trail_pct"],
            "profit_trail_threshold": RC["profit_trail_threshold"],
            "sl_cooldown_secs":     RC["sl_cooldown_secs"],
            "max_trades_day":       RC["max_trades_day"],
            "max_daily_loss":       RC["max_daily_loss"],
            "daily_profit_target":  RC["daily_profit_target"],
            "trade_timeout_secs":   RC.get("trade_timeout_secs", TRADE_TIMEOUT_SECS),
            "regression_window":    RC["regression_window"],
            "regression_slope_min": RC["regression_slope_min"],
            "option_vol_factor":    RC["option_vol_factor"],
            "capital":              S["exit_engine"].capital,
            "lot_size":             LOT_SIZE,
            "buy_qty":              BUY_QTY,
            "trade_start_h":    TRADE_START_H, "trade_start_m":  TRADE_START_M,
            "trade_end_h":      TRADE_END_H,   "trade_end_m":    TRADE_END_M,
            "force_exit_h":     FORCE_EXIT_H,  "force_exit_m":   FORCE_EXIT_M,
        }
    emit("config_snapshot", payload)


@socketio.on("update_config")
def on_update_config(data):
    _KEY_MAP = {
        "auto_jump":            "AUTO_JUMP",
        "jump_atr_window":      "JUMP_ATR_WINDOW",
        "jump_atr_multiplier":  "JUMP_ATR_MULTIPLIER",
        "jump_min_pts":         "JUMP_MIN_PTS",
        "jump_max_pts":         "JUMP_MAX_PTS",
        "jump_pct":             "JUMP_PCT",
        "confirm_sustain_pct":  "CONFIRM_SUSTAIN_PCT",
        "confirm_atr_high":     "CONFIRM_ATR_HIGH",
        "confirm_atr_low":      "CONFIRM_ATR_LOW",
        "confirm_ticks_fast":   "CONFIRM_TICKS_FAST",
        "confirm_ticks_mid":    "CONFIRM_TICKS_MID",
        "confirm_ticks_slow":   "CONFIRM_TICKS_SLOW",
        "momentum_window":      "MOMENTUM_WINDOW",
        "momentum_min":         "MOMENTUM_MIN",
        "sl_phase1_pct":        "SL_PHASE1_PCT",
        "sl_phase1_secs":       "SL_PHASE1_SECS",
        "sl_phase2_pct":        "SL_PHASE2_PCT",
        "trail_atr_high":       "TRAIL_ATR_HIGH",
        "trail_atr_low":        "TRAIL_ATR_LOW",
        "trail_pct_high":       "TRAIL_PCT_HIGH",
        "trail_pct_low":        "TRAIL_PCT_LOW",
        "buy_trail_pct":        "BUY_TRAIL_PCT",
        "profit_trail_threshold":"PROFIT_TRAIL_THRESHOLD_PCT",
        "sl_cooldown_secs":     "SL_COOLDOWN_SECS",
        "max_trades_day":       "MAX_TRADES_DAY",
        "max_daily_loss":       "MAX_DAILY_LOSS",
        "daily_profit_target":  "DAILY_PROFIT_TARGET",
        "trade_timeout_secs":   "TRADE_TIMEOUT_SECS",
        "regression_window":    "REGRESSION_WINDOW",
        "regression_slope_min": "REGRESSION_SLOPE_MIN",
        "option_vol_factor":    "OPTION_VOL_FACTOR",
        "trade_start_h":        "TRADE_START_H",
        "trade_start_m":        "TRADE_START_M",
        "trade_end_h":          "TRADE_END_H",
        "trade_end_m":          "TRADE_END_M",
        "force_exit_h":         "FORCE_EXIT_H",
        "force_exit_m":         "FORCE_EXIT_M",
    }

    restarted    = []
    live_updated = []

    for k, v in data.items():
        if k == "_config_py_content" or k == "zerodha_config":
            continue  # legacy keys — ignored

        if k == "enctoken":
            try:
                _write_enctoken(str(v))
                log("Enctoken updated in config.py (restart WS to apply)", "success")
                live_updated.append("enctoken")
            except Exception as e:
                log(f"Enctoken write failed: {e}", "error")
            continue

        upper = _KEY_MAP.get(k)
        if upper is None:
            continue

        try:
            if k == "auto_jump":
                v = bool(v)
            elif isinstance(v, str):
                v = float(v) if "." in v else int(v)
        except Exception:
            pass

        try:
            _write_config_key(upper, v)
        except Exception as e:
            log(f"⚙ Disk write failed for {upper}: {e}", "error")

        with _state_lock:
            if k in RC:
                RC[k] = v
                if k in _RESTART_PARAMS:
                    restarted.append(k)
                else:
                    live_updated.append(k)

    if live_updated:
        log(f"⚙ Live config updated: {', '.join(live_updated)}", "success")
    if restarted:
        log(f"⚙ Restart-needed params saved: {', '.join(restarted)}", "warning")

    emit("config_saved", {
        "live_updated":   live_updated,
        "restart_needed": restarted,
    })
    broadcast()


@socketio.on("start_robot")
def on_start(data):
    """
    Simplified start — multi_bot handles all watchlist stocks.
    NIFTY/BANKNIFTY token subscribed for ATR reference only.
    """
    idx_sel        = data.get("index", "NIFTY").upper()
    idx_token      = BANKNIFTY_TOKEN if idx_sel == "BANKNIFTY" else NIFTY_TOKEN
    idx_name       = "BANKNIFTY" if idx_sel == "BANKNIFTY" else "NIFTY"

    buf = max(REGRESSION_WINDOW, MOMENTUM_WINDOW + 2, OPTION_ATR_PERIOD + 2,
              CONFIRM_TICKS_SLOW + 5, RC["jump_atr_window"] + 2)

    with _state_lock:
        S["slots"] = {"CE": _make_opt_slot(), "PE": _make_opt_slot()}
        S.update({
            "running":            True,
            "trade_open":         False,
            "active_sides":       {"CE", "PE"},
            "trade_side":         None,
            "nifty_price":        None,
            "nifty_prev_tick":    None,
            "nifty_ref":          None,
            "nifty_move":         None,
            "nifty_ticks":        deque(maxlen=buf),
            "nifty_tick_times":   deque(maxlen=buf),
            "trend_cooldown":     0,
            "nifty_atr_ticks":    deque(maxlen=RC["jump_atr_window"] + 2),
            "jump_atr":           None,
            "jump_threshold":     RC["jump_min_pts"],
            "regression_slope":   None,
            "dynamic_jump_pts":   0.0,
            "option_atr":         None,
            "live_trail_pct":     RC["buy_trail_pct"],
            "pending_side":       None,
            "confirm_count":      0,
            "confirm_needed":     RC["confirm_ticks_mid"],
            "spike_ref_price":    None,
            "fast_entry":         False,
            "cooldown_until":     None,
            "active_side":        None,
            "active_status":      None,
            "trade_open_time":    None,
            "nifty_entry_price":  None,
            "ai_entry_score":     None,
            "ai_regime":          "unknown",
            "trade_pnl":          0.0,
            "session_pnl":        0.0,
            "trades_today":       0,
            "wins":               0,
            "losses":             0,
            "loss_streak":        0,
            "adopted_position":   None,
            "_last_positions":    [],
            "_pos_sync_ts":       0,
            "last_skip_reason":   None,
            "last_order_id":      None,
            "last_exit_order_id": None,
            "exit_engine":        BuyExitStrategy(capital=S["capital"]),
            "index_token":        idx_token,
            "index_name":         idx_name,
            "atm_pending":        False,  # startup thread resolves immediately
        })
        mode_str = "🔴 REAL TRADING" if S["trading_mode"] == "real" else "🔵 DEMO (paper)"
        jmp_str  = (f"AUTO ATR×{RC['jump_atr_multiplier']} [{RC['jump_min_pts']}–{RC['jump_max_pts']}pts]"
                    if RC["auto_jump"] else f"FIXED {RC['jump_pct']}% Nifty")

    _subscribe([idx_token, VIX_TOKEN])
    _vol_detector.reset()
    _vix_tracker.reset()
    _mtf_analyzer.reset()

    log(f"Buy Robot v8.2 started — {mode_str}", "success")
    log(f"  Spike threshold: {jmp_str}  |  index ref={idx_name}", "info")
    if S["trading_mode"] == "real":
        log("⚠ REAL MODE — check Zerodha for existing open positions!", "error")

    # ── Resolve ATM + load capital immediately via REST ─────────────────────────
    def _startup_atm_resolve():
        # Load capital based on mode
        if S["trading_mode"] == "real":
            bal = fetch_balance()
            if bal and bal.get("available", 0) > 0:
                live_cap = round(bal["available"], 2)
                with _state_lock:
                    S["capital"]           = live_cap
                    S["day_start_capital"] = live_cap
                    S["exit_engine"].capital = live_cap
                log(
                    f"💰 Capital loaded from Kite — available=₹{live_cap:,.2f}  "
                    f"(used=₹{bal.get('used',0):,.2f}  net=₹{bal.get('net',0):,.2f})",
                    "success",
                )
            else:
                log("⚠ Could not fetch live balance — using config capital", "warning")
        else:
            # Demo mode: flat initial capital from config
            with _state_lock:
                S["capital"]             = CAPITAL
                S["day_start_capital"]   = CAPITAL
                S["exit_engine"].capital  = CAPITAL

        # Fetch historical daily candles for intelligent spike threshold
        log("  Fetching historical candles for spike intelligence…", "info")
        _fetch_historical_daily_atr()

        log("  Fetching NIFTY price for ATM resolution…", "info")
        price = _fetch_nifty_ltp()
        if price:
            atm = _resolve_nifty_atm(price)
            if atm:
                _apply_nifty_atm(atm)
                return
            log("⚠ ATM resolve failed — will retry on first tick", "warning")
        else:
            log("⚠ NIFTY price fetch failed — will resolve on first tick", "warning")
        # Fallback: let first incoming NIFTY tick trigger resolution
        with _state_lock:
            S["atm_pending"] = True

    threading.Thread(target=_startup_atm_resolve, daemon=True, name="atm-startup").start()

    # ── Position sync: detect & adopt existing Zerodha positions ──────────
    if S["trading_mode"] == "real":
        def _startup_position_sync():
            import time as _t
            _t.sleep(3)  # wait for ATM to resolve first
            _sync_zerodha_positions()
        threading.Thread(target=_startup_position_sync, daemon=True, name="pos-sync").start()

    broadcast()


def _sync_zerodha_positions():
    """
    Fetch positions from Zerodha and adopt any open NFO positions.
    Called at startup and periodically during trading.
    """
    try:
        positions_data = fetch_positions()
        if not positions_data:
            return

        with _state_lock:
            known_sym = ""
            active_slot = _active_slot()
            if active_slot:
                known_sym = active_slot.get("symbol", "")
            atm_info = S.get("nifty_atm") or {}

        open_pos = detect_open_positions(positions_data, known_sym)
        prev_pos = S.get("_last_positions", [])

        # Detect changes from last sync
        if prev_pos:
            changes = detect_position_changes(prev_pos, open_pos, known_sym)
            if changes.get("externally_closed"):
                # Someone closed our position on Kite web!
                log("[SYNC] Position closed externally on Zerodha — resetting bot state", "warning")
                with _state_lock:
                    if S["trade_open"]:
                        S["trade_open"]    = False
                        S["trade_side"]    = None
                        S["active_side"]   = None
                        S["active_status"] = "closed_external"
                        S["exit_engine"].reset()
                        _reset_pending()
                broadcast()

            for new_pos in changes.get("new_positions", []):
                log(f"[SYNC] New position detected: {new_pos['symbol']} "
                    f"qty={new_pos['qty']} avg=₹{new_pos['avg_price']:.2f}", "info")

            for chg in changes.get("qty_changed", []):
                log(f"[SYNC] Position qty changed: {chg['symbol']} "
                    f"{chg['prev_qty']}→{chg['curr_qty']}", "info")

        # Store current snapshot for next comparison
        with _state_lock:
            S["_last_positions"] = open_pos
            S["_pos_sync_ts"]    = _time.time()

        # Adopt untracked positions
        if not S.get("trade_open") and open_pos:
            # Find the best position to adopt (prefer ATM match, then largest)
            best = None
            for pos in open_pos:
                if not pos["is_long"]:
                    continue  # only adopt long option positions
                if atm_info:
                    from position_sync import match_position_to_atm
                    if match_position_to_atm(pos, atm_info):
                        best = pos
                        break
                if best is None or pos["qty"] > best["qty"]:
                    best = pos

            if best:
                _adopt_external_position(best)

    except Exception as exc:
        log(f"[SYNC] Position sync error: {exc}", "error")


def _adopt_external_position(position: dict):
    """
    Adopt an externally-opened position into the bot's exit engine.
    """
    params = build_adoption_params(position)
    side = params["side"]
    entry_price = params["entry_price"]
    symbol = params["symbol"]

    if entry_price <= 0:
        log(f"[SYNC] Cannot adopt {symbol} — no entry price", "warning")
        return

    with _state_lock:
        sl = _slot(side)
        # Set the slot to track this position
        if not sl["token"] and position.get("instrument_token"):
            sl["token"] = position["instrument_token"]
        sl["symbol"] = symbol
        sl["price"]  = params["current_price"]

        # Open the exit engine with adaptive params
        info = S["exit_engine"].open_leg(
            side, entry_price,
            sl_pct_override         = params["sl_pct_p1"],
            sl_pct_p2_override      = params["sl_pct_p2"],
            trail_pct_override      = params["trail_pct"],
            timeout_secs_override   = params["timeout_secs"],
            seed_prices=[entry_price, params["current_price"]],
            symbol=S.get("index_name", ""),
            option_symbol=symbol,
            qty_override=params["qty_lots"],
        )

        S["trade_open"]         = True
        S["trade_side"]         = side
        S["active_side"]        = side
        S["active_status"]      = "open"
        S["nifty_entry_price"]  = S.get("nifty_price")
        S["adopted_position"]   = params
        S["last_order_id"]      = None

        # Subscribe to the instrument for live ticks
        token = position.get("instrument_token")

    if token:
        _subscribe([token])

    log(
        f"[SYNC] ADOPTED external position: {side} {symbol}  "
        f"entry=₹{entry_price:.2f}  current=₹{params['current_price']:.2f}  "
        f"P&L={params['pnl_pct']:+.1f}%  qty={params['qty_lots']}  "
        f"SL={params['sl_pct_p1']}%→{params['sl_pct_p2']}%  trail={params['trail_pct']}%",
        "trade",
    )
    log(
        f"  Bot will manage this position with intelligent exits "
        f"(trail/SL/timeout/reversal/AI)",
        "success",
    )

    socketio.emit("trade_opened", {
        "side":         side,
        "entry":        entry_price,
        "sl":           info["sl"],
        "qty":          info["qty"],
        "trading_mode": "real",
        "adopted":      True,
        "symbol":       symbol,
    }, namespace="/")
    broadcast()


@socketio.on("stop_robot")
def on_stop():
    _close_active_trade(reason="manual")

    with _state_lock:
        unsub_tokens = []
        for side in ("CE", "PE"):
            t = S["slots"][side]["token"]
            if t:
                unsub_tokens.append(t)
        S["running"]      = False
        S["trade_open"]   = False
        S["active_sides"] = set()
        S["trade_side"]   = None
        S["slots"]        = {"CE": _make_opt_slot(), "PE": _make_opt_slot()}
        _reset_pending()

    # Only unsubscribe option tokens — keep index token subscribed for live dashboard
    if unsub_tokens:
        _unsubscribe(unsub_tokens)
    log("Buy Robot v8.2 stopped.", "warning")
    broadcast()


@socketio.on("manual_exit_trade")
def on_manual_exit():
    closed, _ = _close_active_trade(reason="manual")
    if not closed:
        emit("error", {"msg": "No open trade to exit"})
        return
    broadcast()


@socketio.on("refresh_nifty_atm")
def on_refresh_atm():
    """Force re-resolve NIFTY ATM options."""
    with _state_lock:
        price = S.get("nifty_price")
        if not price:
            emit("error", {"msg": "No NIFTY price yet — wait for ticks"})
            return
        S["atm_pending"] = False   # will be set by _do_resolve
    def _do():
        atm = _resolve_nifty_atm(price)
        if atm:
            _apply_nifty_atm(atm)
        else:
            log("⚠ ATM resolve failed", "warning")
    threading.Thread(target=_do, daemon=True).start()
    log("ATM refresh requested…", "info")


@socketio.on("adopt_positions")
def on_adopt_positions(data):
    """Toggle position adoption — scan and adopt open Zerodha positions."""
    enabled = data.get("enabled", False)

    if not enabled:
        emit("adopt_status", {"adopted": False, "positions": 0})
        return

    if S["trading_mode"] != "real":
        emit("adopt_status", {"adopted": False, "positions": 0, "error": "Demo mode"})
        return

    def _do_adopt():
        try:
            positions_data = fetch_positions()
            if not positions_data:
                socketio.emit("adopt_status", {"adopted": False, "positions": 0}, namespace="/")
                return

            known_sym = ""
            with _state_lock:
                active_slot = _active_slot()
                if active_slot:
                    known_sym = active_slot.get("symbol", "")

            open_pos = detect_open_positions(positions_data, known_sym)

            if S.get("trade_open") and S.get("adopted_position"):
                # Already managing an adopted position
                ap = S["adopted_position"]
                socketio.emit("adopt_status", {
                    "adopted": True,
                    "symbol": ap.get("symbol", ""),
                    "side": ap.get("side", ""),
                    "pnl_pct": ap.get("pnl_pct", 0),
                    "positions": len(open_pos),
                }, namespace="/")
                return

            if not S.get("trade_open") and open_pos:
                # Find best position to adopt
                best = None
                atm_info = S.get("nifty_atm") or {}
                for pos in open_pos:
                    if not pos["is_long"]:
                        continue
                    from position_sync import match_position_to_atm
                    if atm_info and match_position_to_atm(pos, atm_info):
                        best = pos
                        break
                    if best is None or pos["qty"] > best["qty"]:
                        best = pos

                if best:
                    _adopt_external_position(best)
                    socketio.emit("adopt_status", {
                        "adopted": True,
                        "symbol": best["symbol"],
                        "side": best["side"],
                        "pnl_pct": round((best["last_price"] - best["avg_price"]) / best["avg_price"] * 100, 1) if best["avg_price"] else 0,
                        "positions": len(open_pos),
                    }, namespace="/")
                    return

            socketio.emit("adopt_status", {
                "adopted": False,
                "positions": len(open_pos),
            }, namespace="/")

        except Exception as exc:
            socketio.emit("adopt_status", {"adopted": False, "positions": 0, "error": str(exc)}, namespace="/")

    threading.Thread(target=_do_adopt, daemon=True).start()


@socketio.on("set_trading_mode")
def on_set_mode(data):
    new_mode = data.get("mode", "demo")
    if new_mode not in ("demo", "real"):
        emit("error", {"msg": f"Invalid mode: {new_mode}"})
        return
    with _state_lock:
        if S["trade_open"]:
            emit("error", {"msg": "Cannot change mode while a trade is open"})
            return
        S["trading_mode"] = new_mode

    if new_mode == "real":
        log("🔴 Switched to REAL TRADING — live orders will be placed!", "error")
        # Fetch live balance and update capital + day target immediately
        def _sync_capital():
            bal = fetch_balance()
            if bal and bal.get("available", 0) > 0:
                live_cap = round(bal["available"], 2)
                with _state_lock:
                    S["capital"]             = live_cap
                    S["day_start_capital"]   = live_cap
                    S["exit_engine"].capital  = live_cap
                log(
                    f"💰 Capital synced from Kite — ₹{live_cap:,.2f}  "
                    f"(day target = ₹{round(live_cap * DAILY_PROFIT_PCT, 2):,.2f})",
                    "success",
                )
                broadcast()
            else:
                log("⚠ Could not fetch live balance — capital unchanged", "warning")
        threading.Thread(target=_sync_capital, daemon=True).start()
    else:
        with _state_lock:
            S["capital"]             = CAPITAL
            S["day_start_capital"]   = CAPITAL
            S["exit_engine"].capital  = CAPITAL
        log("🔵 Switched to DEMO mode — paper trading only", "success")
    broadcast()


@socketio.on("get_state")
def on_get_state():
    broadcast()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/account_balance")
def api_account_balance():
    """Fetch live equity balance from Zerodha (only meaningful in real mode)."""
    bal = fetch_balance()
    if bal is None:
        return jsonify({"error": "Failed to fetch balance — check credentials"}), 502
    return jsonify(bal)


# ── Instrument search ─────────────────────────────────────────────────────────
_instruments_cache: list = []
_instruments_cache_ts: float = 0.0


def _fetch_nifty_ltp() -> float | None:
    """Fetch current NIFTY 50 spot price from Zerodha quote API (no WebSocket needed)."""
    import urllib.parse as _up
    cfg      = ZERODHA_CONFIG
    enctoken = _up.unquote(cfg.get("enctoken", ""))
    user_id  = cfg.get("user_id", "")
    url      = "https://kite.zerodha.com/oms/quote?i=NSE:NIFTY+50"
    req = _urllib_request.Request(url, headers={
        "Authorization":  f"enctoken {enctoken}",
        "Cookie":         (
            f"kf_session={cfg.get('kf_session','')}; user_id={user_id}; "
            f"public_token={cfg.get('public_token','')}; enctoken={enctoken}"
        ),
        "Accept":         "application/json, */*",
        "Referer":        "https://kite.zerodha.com/",
        "User-Agent":     cfg.get("user_agent", "kite3-web"),
        "x-kite-userid":  user_id,
        "x-kite-version": cfg.get("version", "3.0.0"),
    })
    try:
        with _urllib_request.urlopen(req, timeout=8) as resp:
            data = _json_mod.loads(resp.read().decode())
        return float(data["data"]["NSE:NIFTY 50"]["last_price"])
    except Exception:
        return None


def _fetch_nfo_instruments() -> list:
    """Fetch NFO instruments from Zerodha public API. Cached 1 hour."""
    global _instruments_cache, _instruments_cache_ts
    if _instruments_cache and (_time.time() - _instruments_cache_ts) < 3600:
        return _instruments_cache
    req = _urllib_request.Request(
        "https://api.kite.trade/instruments/NFO",
        headers={"Accept": "text/csv", "User-Agent": "Mozilla/5.0"},
    )
    with _urllib_request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
    reader = _csv.DictReader(_io.StringIO(raw))
    result = []
    for row in reader:
        sym = row.get("tradingsymbol", "")
        result.append({
            "token":  row.get("instrument_token", ""),
            "symbol": sym,
            "expiry": row.get("expiry", ""),
            "strike": row.get("strike", ""),
            "type":   row.get("instrument_type", ""),
            "name":   row.get("name", ""),
        })
    _instruments_cache = result
    _instruments_cache_ts = _time.time()
    return result


def _resolve_nifty_atm(nifty_price: float) -> dict | None:
    """
    Find nearest-expiry ATM NIFTY CE and PE tokens from live NFO instruments.
    Strike step = 50 pts. Returns dict or None on failure.
    """
    try:
        from datetime import date as _date
        today_str = _date.today().isoformat()
        instruments = _fetch_nfo_instruments()

        nifty_opts = [
            i for i in instruments
            if i.get("name", "").upper() == "NIFTY"
            and i.get("type") in ("CE", "PE")
            and i.get("expiry", "") >= today_str
            and i.get("strike")
        ]
        if not nifty_opts:
            return None

        # Nearest expiry
        nearest_expiry = sorted(set(i["expiry"] for i in nifty_opts))[0]

        # ATM strike = round to nearest 50
        atm_strike = round(nifty_price / 50) * 50

        # Try ATM, then ATM±50, ATM±100 until both CE and PE found
        for offset in (0, 50, -50, 100, -100):
            strike = atm_strike + offset
            ce = next((i for i in nifty_opts
                       if i["expiry"] == nearest_expiry
                       and float(i["strike"]) == strike
                       and i["type"] == "CE"), None)
            pe = next((i for i in nifty_opts
                       if i["expiry"] == nearest_expiry
                       and float(i["strike"]) == strike
                       and i["type"] == "PE"), None)
            if ce and pe:
                return {
                    "ce_token":  int(ce["token"]),
                    "ce_symbol": ce["symbol"],
                    "pe_token":  int(pe["token"]),
                    "pe_symbol": pe["symbol"],
                    "strike":    strike,
                    "expiry":    nearest_expiry,
                }
        return None
    except Exception as exc:
        app.logger.warning(f"ATM resolve failed: {exc}")
        return None


def _apply_nifty_atm(atm: dict):
    """
    Apply a resolved ATM dict: set slot tokens, re-subscribe, update state.
    Called from background thread — no locks held.
    """
    ce_token = atm.get("ce_token")
    pe_token = atm.get("pe_token")

    # Unsubscribe old option tokens
    old_tokens = []
    with _state_lock:
        for side in ("CE", "PE"):
            t = S["slots"][side]["token"]
            if t:
                old_tokens.append(t)

    if old_tokens:
        _unsubscribe(old_tokens)

    # Set new tokens and mark ATM resolved
    with _state_lock:
        if ce_token:
            S["slots"]["CE"]["token"]  = ce_token
            S["slots"]["CE"]["symbol"] = atm["ce_symbol"]
        if pe_token:
            S["slots"]["PE"]["token"]  = pe_token
            S["slots"]["PE"]["symbol"] = atm["pe_symbol"]
        S["nifty_atm"] = atm
        S["atm_pending"] = False

    new_tokens = [t for t in (ce_token, pe_token) if t]
    if new_tokens:
        _subscribe(new_tokens)

    # Update market intelligence with expiry info
    _market_intel.update_expiry(atm.get("expiry"))
    ctx = _market_intel.get_context()
    day_info = ctx.get("day_info", {})
    rec = ctx.get("recommendations", {})

    log(
        f"[ATM] Resolved NIFTY  strike={atm['strike']}  expiry={atm['expiry']}  "
        f"CE={atm['ce_symbol']}  PE={atm['pe_symbol']}",
        "success",
    )
    log(
        f"[INTEL] {day_info.get('day_type','?').upper()}  DTE={day_info.get('dte','?')}  "
        f"theta={day_info.get('theta_pressure','?')}  decay={day_info.get('premium_decay_rate','?')}x  "
        f"SL={rec.get('sl_pct','?')}%  trail={rec.get('trail_pct','?')}%  "
        f"timeout={rec.get('timeout','?')}s",
        "info",
    )
    if rec.get("avoid_entry"):
        log(f"[INTEL] AVOID ENTRIES: {' | '.join(rec.get('reasons', []))}", "warning")
    socketio.emit("nifty_atm_resolved", atm, namespace="/")
    broadcast()


@app.route("/api/search_instruments")
def api_search_instruments():
    q = request.args.get("q", "").upper().strip()
    if len(q) < 2:
        return jsonify([])
    try:
        instruments = _fetch_nfo_instruments()
        results = [i for i in instruments if q in i["symbol"]][:40]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade_history")
def api_trade_history():
    rows = []
    try:
        log_path = _os.path.join(_runtime_dir(), "trade_log.csv")
        with open(log_path, "r", newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
        rows.reverse()
    except FileNotFoundError:
        pass
    return jsonify(rows)


@app.route("/api/nifty_historical")
def api_nifty_historical():
    """Fetch NIFTY 50 historical candles from Zerodha for chart."""
    import urllib.parse as _up
    interval = request.args.get("interval", "minute")
    cfg      = ZERODHA_CONFIG
    enctoken = _up.unquote(cfg.get("enctoken", ""))
    user_id  = cfg.get("user_id", "")
    today    = datetime.now().strftime("%Y-%m-%d")
    token    = S.get("index_token", NIFTY_TOKEN)
    url = (
        f"/oms/instruments/historical/{token}/{interval}"
        f"?user_id={user_id}&oi=0&from={today}&to={today}"
    )
    req = _urllib_request.Request(
        f"https://kite.zerodha.com{url}",
        headers={
            "Authorization":  f"enctoken {enctoken}",
            "Cookie":         (
                f"kf_session={cfg.get('kf_session','')}; user_id={user_id}; "
                f"public_token={cfg.get('public_token','')}; enctoken={enctoken}"
            ),
            "Accept":         "application/json, */*",
            "Referer":        "https://kite.zerodha.com/",
            "User-Agent":     cfg.get("user_agent", "kite3-web"),
            "x-kite-userid":  user_id,
            "x-kite-version": cfg.get("version", "3.0.0"),
        },
    )
    try:
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json_mod.loads(resp.read().decode())
        raw_candles = data.get("data", {}).get("candles", [])
        # Convert to [{t, o, h, l, c}] format for the chart
        candles = []
        for c in raw_candles:
            # c = [timestamp_str, O, H, L, C, volume, oi]
            ts_str = c[0]
            # Parse ISO timestamp to epoch seconds
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(ts_str)
            epoch = dt.timestamp()
            rec = {"t": epoch, "o": c[1], "h": c[2], "l": c[3], "c": c[4]}
            if len(c) > 5 and c[5]:
                rec["v"] = c[5]
            candles.append(rec)
        return jsonify(candles)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/zerodha_orders")
def api_zerodha_orders():
    """Fetch today's orders from Zerodha Kite."""
    orders = fetch_orders()
    if orders is None:
        return jsonify({"error": "Failed to fetch orders"}), 502
    return jsonify(orders)


@app.route("/api/zerodha_trades")
def api_zerodha_trades():
    """Fetch today's executed trades from Zerodha Kite."""
    trades = fetch_trades()
    if trades is None:
        return jsonify({"error": "Failed to fetch trades"}), 502
    return jsonify(trades)


@app.route("/api/zerodha_positions")
def api_zerodha_positions():
    """Fetch current open positions from Zerodha Kite."""
    from Zerodha_api import fetch_positions
    positions = fetch_positions()
    if positions is None:
        return jsonify({"error": "Failed to fetch positions"}), 502
    return jsonify(positions)


@app.route("/api/order_book")
def api_order_book():
    """Combined view: positions + orders + trades for the dashboard."""
    from Zerodha_api import fetch_positions
    result = {"positions": [], "orders": [], "trades": [], "summary": {}}

    # Fetch all data
    positions = fetch_positions()
    orders = fetch_orders()
    trades = fetch_trades()

    if positions:
        # Filter to only NFO positions with non-zero quantity
        day_pos = positions.get("day", [])
        net_pos = positions.get("net", [])
        result["positions"] = [
            p for p in net_pos
            if p.get("quantity", 0) != 0 or p.get("day_buy_quantity", 0) > 0 or p.get("day_sell_quantity", 0) > 0
        ]

    if orders:
        # Sort by timestamp descending
        orders.sort(key=lambda o: o.get("order_timestamp", ""), reverse=True)
        result["orders"] = orders

    if trades:
        trades.sort(key=lambda t: t.get("fill_timestamp", t.get("order_timestamp", "")), reverse=True)
        result["trades"] = trades

    # Summary
    open_positions = [p for p in result["positions"] if p.get("quantity", 0) != 0]
    total_pnl = sum(p.get("pnl", 0) for p in result["positions"])
    total_m2m = sum(p.get("m2m", 0) for p in result["positions"])
    completed_orders = [o for o in result.get("orders", []) if o.get("status") == "COMPLETE"]
    open_orders = [o for o in result.get("orders", []) if o.get("status") in ("OPEN", "TRIGGER PENDING")]

    result["summary"] = {
        "open_positions": len(open_positions),
        "total_pnl": round(total_pnl, 2),
        "total_m2m": round(total_m2m, 2),
        "completed_orders": len(completed_orders),
        "open_orders": len(open_orders),
        "total_orders": len(result.get("orders", [])),
        "total_trades": len(result.get("trades", [])),
    }

    return jsonify(result)


@app.route("/api/analytics")
def api_analytics():
    """Performance analytics computed from trade log."""
    from performance_analytics import load_trades, compute_analytics
    trades = load_trades()
    capital = S.get("day_start_capital", CAPITAL)
    return jsonify(compute_analytics(trades, capital))


@app.route("/")
def index():
    return send_file("buy_index.html")


@app.route("/settings")
def settings():
    return send_file("buy_settings.html")



# ── Watchlist API ─────────────────────────────────────────────────────────────

@app.route("/api/watchlist")
def api_get_watchlist():
    return jsonify(_load_watchlist())


# ── Zerodha Watchlist API ─────────────────────────────────────────────────────

@app.route("/api/add_to_watchlist", methods=["POST"])
def api_add_to_watchlist():
    """
    Add a symbol to Zerodha watchlist via Kite web API.
    JSON body: {"exchange": "NFO", "tradingsymbol": "RELIANCE25JUN1420CE", "watchlist_id": 2}
    Requires csrf_token and app_uuid in ZERODHA_CONFIG.
    """
    import http.client as _hc
    import urllib.parse as _up

    data = request.get_json(force=True) or {}
    exchange    = data.get("exchange", "NFO")
    tsymbol     = data.get("tradingsymbol", "")
    wid         = data.get("watchlist_id", 2)

    if not tsymbol:
        return jsonify({"error": "tradingsymbol required"}), 400

    cfg         = ZERODHA_CONFIG
    enctoken    = _up.unquote(cfg.get("enctoken", ""))
    csrf_token  = cfg.get("csrf_token", "")
    app_uuid    = cfg.get("app_uuid", "")
    user_id     = cfg.get("user_id", "")

    body_str = _up.urlencode({
        "exchange":      exchange,
        "tradingsymbol": tsymbol,
        "weight":        "0.5",
        "group":         "",
    }).encode("utf-8")

    cookie = (
        f"kf_session={cfg.get('kf_session', '')}; "
        f"user_id={user_id}; "
        f"public_token={cfg.get('public_token', '')}; "
        f"enctoken={enctoken}"
    )
    headers = {
        "Host":              "kite.zerodha.com",
        "Accept":            "application/json, text/plain, */*",
        "Authorization":     f"enctoken {enctoken}",
        "Content-Type":      "application/x-www-form-urlencoded",
        "Content-Length":    str(len(body_str)),
        "Cookie":            cookie,
        "Origin":            "https://kite.zerodha.com",
        "Referer":           "https://kite.zerodha.com/",
        "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "x-kite-userid":     user_id,
        "x-kite-version":    cfg.get("version", "3.0.0"),
        "x-csrftoken":       csrf_token,
        "x-kite-app-uuid":   app_uuid,
    }

    try:
        conn = _hc.HTTPSConnection("kite.zerodha.com", timeout=10)
        conn.request("POST", f"/api/marketwatch/{wid}/items",
                     body=body_str, headers=headers)
        resp = conn.getresponse()
        raw  = resp.read().decode("utf-8")
        conn.close()
        import json as _json
        result = _json.loads(raw)
        if result.get("status") == "success":
            log(f"Watchlist: added {tsymbol}", "success")
            return jsonify({"ok": True, "data": result.get("data")})
        return jsonify({"error": result.get("message", raw)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _periodic_broadcast():
    """Broadcast state every second so UI always has fresh prices."""
    import time as _t
    while True:
        _t.sleep(1)
        try:
            broadcast()
        except Exception:
            pass


def _nifty_ltp_poller():
    """Poll NIFTY spot price via REST API every 2s — always keeps nifty_price fresh."""
    import time as _t
    while True:
        _t.sleep(2)
        try:
            price = _fetch_nifty_ltp()
            if price is None:
                continue
            with _state_lock:
                S["nifty_price"] = price
                S["nifty_ticks"].append(price)
                S["nifty_tick_times"].append(datetime.now())
                _vol_detector.add(price)
                _mtf_analyzer.add_tick(price)
                S["nifty_atr_ticks"].append(price)
                S["regression_slope"] = _regression_slope(list(S["nifty_ticks"]))
                _update_jump_threshold(price)
        except Exception:
            pass


if __name__ == "__main__":
    _subscribe([NIFTY_TOKEN])
    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    pb = threading.Thread(target=_periodic_broadcast, daemon=True)
    pb.start()
    # NIFTY spot price REST poller — fills in when WS doesn't send index ticks
    nifty_poll = threading.Thread(target=_nifty_ltp_poller, daemon=True)
    nifty_poll.start()
    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)