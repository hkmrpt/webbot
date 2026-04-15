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
_ENCTOKEN_FILE = _os.path.join(_runtime_dir(), "enctoken.dat")

import asyncio
import threading
import numpy as np
from collections import deque
from datetime import datetime, time as dtime, timedelta
from flask import Flask, send_file
from flask_socketio import SocketIO, emit

from config import (
    CAPITAL, BUY_QTY, MAX_TRADES_DAY, LOT_SIZE,
    JUMP_PCT, CONFIRM_SUSTAIN_PCT,
    MOMENTUM_WINDOW, MOMENTUM_MIN,
    REGRESSION_WINDOW, REGRESSION_SLOPE_MIN,
    OPTION_VOL_WINDOW, OPTION_VOL_FACTOR,
    BUY_TRAIL_PCT, SL_PHASE1_PCT, SL_PHASE1_SECS, SL_PHASE2_PCT,
    SL_COOLDOWN_SECS, MAX_DAILY_LOSS, DAILY_PROFIT_TARGET,
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
    JUMP_ADAPTIVE_MIN, JUMP_ADAPTIVE_MAX,
    SL_COOLDOWN_MAX_SECS,
    NIFTY_REVERSAL_EXIT,
    FAST_MOVE_VELOCITY,
    BREAKEVEN_TRIGGER_PCT,
    ZERODHA_CONFIG,
)
from buy_exit_strategy import BuyExitStrategy
from market_brain import MarketBrain
from self_tuner import SelfTuner
from volatility_detector import VolatilityDetector
from zerodha_websocket import connect_zerodha_websocket
from Zerodha_api import place_buy, place_sell

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

TRADE_START  = dtime(TRADE_START_H,  TRADE_START_M)
TRADE_END    = dtime(TRADE_END_H,    TRADE_END_M)
FORCE_EXIT_T = dtime(FORCE_EXIT_H,   FORCE_EXIT_M)
NIFTY_TOKEN  = 256265

app      = Flask(__name__)
app.config["SECRET_KEY"] = "buybot_v8_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# _state_lock guards S and RC mutations ONLY.
# Never call socketio.emit, place_buy, place_sell, or any blocking I/O
# while holding this lock.
_state_lock   = threading.Lock()
_market_brain = MarketBrain()        # AI entry brain — persists across trades via brain_state.json
_self_tuner   = SelfTuner()          # self-learning parameter tuner — persists via tuner_state.json
_vol_detector = VolatilityDetector() # pre-entry range filter — blocks entries when NIFTY swings too wide

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
    "sl_cooldown_max_secs":   SL_COOLDOWN_MAX_SECS,
    "jump_adaptive_min":      JUMP_ADAPTIVE_MIN,
    "jump_adaptive_max":      JUMP_ADAPTIVE_MAX,
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
    "slots":              {"CE": _make_opt_slot(), "PE": _make_opt_slot()},
    "trade_side":         None,

    "nifty_price":        None,
    "nifty_prev_tick":    None,
    "nifty_ref":          None,
    "nifty_move":         None,
    "nifty_ticks":        deque(maxlen=_buf),

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
    "trade_pnl":          0.0,
    "session_pnl":        0.0,
    "trades_today":       0,
    "wins":               0,
    "losses":             0,
    "trading_date":       datetime.now().date(),   # for auto daily reset
    "capital":            CAPITAL,

    "last_order_id":      None,
    "last_exit_order_id": None,

    "exit_engine":        BuyExitStrategy(capital=CAPITAL),

    "ws_connected":       False,
    "ws_send_queue":      None,
    "ws_loop":            None,
    "subscribed_tokens":  [],

    "logs":                   [],
    "live_trail_pct":         BUY_TRAIL_PCT,
    "option_atr":             None,
    "consecutive_sl_hits":    0,
    "effective_jump_mult":    JUMP_ATR_MULTIPLIER,
    "entry_params":           {},      # active param snapshot taken at trade entry
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




# ── Adaptive SL cooldown ──────────────────────────────────────────────────────
def _adaptive_sl_cooldown() -> int:
    """
    Doubles base cooldown for each consecutive SL hit, capped at sl_cooldown_max_secs.
    hits=1 → base, hits=2 → 2×base, hits=3 → 4×base, …
    """
    hits = S["consecutive_sl_hits"]
    secs = RC["sl_cooldown_secs"] * (2 ** max(0, hits - 1))
    return int(min(secs, RC["sl_cooldown_max_secs"]))


# ── Auto jump threshold ───────────────────────────────────────────────────────
def _update_jump_threshold(nifty_price):
    atr  = _compute_atr(list(S["nifty_atr_ticks"]))
    S["jump_atr"] = atr

    adaptive_floor           = _self_tuner.get("jump_floor")  # 4–7 pts, learned from trade outcomes
    S["effective_jump_mult"] = adaptive_floor

    if RC["auto_jump"] and atr is not None:
        raw = atr * RC["jump_atr_multiplier"]
        pts = round(max(adaptive_floor, min(RC["jump_max_pts"], raw)), 2)
    else:
        pts = round(nifty_price * RC["jump_pct"] / 100, 2) if nifty_price else adaptive_floor

    S["jump_threshold"]   = pts
    S["dynamic_jump_pts"] = pts
    return pts


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
        "last_order_id":        S["last_order_id"],
        "last_exit_order_id":   S["last_exit_order_id"],
        "active_sides":         list(S["active_sides"]),

        "auto_jump_active":     RC["auto_jump"],
        "jump_atr":             S["jump_atr"],
        "jump_threshold_pts":   S["jump_threshold"],

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
        "trade_pnl":            S["trade_pnl"],
        "trades_today":         S["trades_today"],
        "wins":                 S["wins"],
        "losses":               S["losses"],
        "capital":              S["exit_engine"].capital,
        "live_pnl":             live_pnl,

        "entry":                snap.get("entry"),
        "sl":                   snap.get("sl"),
        "sl_pct":               snap.get("sl_pct"),
        "qty":                  snap.get("qty"),
        "phase2":               snap.get("phase2", False),
        "peak_price":           snap.get("peak_price"),
        "peak_profit":          snap.get("peak_profit"),
        "peak_profit_pct":      snap.get("peak_profit_pct"),
        "trail_price":          snap.get("trail_price"),
        "trail_pct":            snap.get("trail_pct",     BUY_TRAIL_PCT),
        "option_atr":           snap.get("option_atr",   S["option_atr"]),
        "held_secs":            snap.get("held_secs",    0),
        "momentum_score":       snap.get("momentum_score"),
        "exit_score":           snap.get("exit_score",   0.0),
        "breakeven_moved":      snap.get("breakeven_moved", False),
        "exit_brain":           snap.get("brain",        {}),

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
        "consecutive_sl_hits":  S["consecutive_sl_hits"],
        "adaptive_min_pts":     S["effective_jump_mult"],
        "tuner":                _self_tuner.state,

        # New config constants for UI display
        "breakeven_trigger_pct": BREAKEVEN_TRIGGER_PCT,
        "nifty_reversal_exit":   NIFTY_REVERSAL_EXIT,
        "fast_move_velocity":    FAST_MOVE_VELOCITY,
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
    Patches the given payload dict with the real order_id.
    Returns the order_id or None.
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
        else:
            log(f"📡 REAL SELL — {symbol}  qty={qty}", "warning")
            oid, err = place_sell(symbol, qty)

        if oid:
            with _state_lock:
                if action == "buy":
                    S["last_order_id"] = oid
                else:
                    S["last_exit_order_id"] = oid
            log(f"✅ {'BUY' if action == 'buy' else 'SELL'} order confirmed — order_id={oid}", "success")
            if opened_payload and action == "buy":
                opened_payload["order_id"] = oid
            if closed_payload and action == "sell":
                closed_payload["exit_order_id"] = oid
        else:
            log(f"❌ {'BUY' if action == 'buy' else 'SELL'} order FAILED — {err}", "error")
        return oid
    else:
        log(f"🔵 DEMO — no real {'buy' if action == 'buy' else 'sell'} placed", "info")
        return None


# ── Tick processor ────────────────────────────────────────────────────────────
def process_ticks(ticks):
    if not S["running"] and not S["trade_open"]:
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
            if token == NIFTY_TOKEN:
                S["nifty_price"] = price
                S["nifty_ticks"].append(price)
                S["nifty_atr_ticks"].append(price)
                _vol_detector.add(price)
                S["regression_slope"] = _regression_slope(list(S["nifty_ticks"]))
                _update_jump_threshold(price)

                # ── Auto daily reset ──────────────────────────────────────
                today = datetime.now().date()
                if today != S["trading_date"]:
                    S["trading_date"]     = today
                    S["session_pnl"]      = 0.0
                    S["trades_today"]     = 0
                    S["wins"]             = 0
                    S["losses"]           = 0
                    S["trade_pnl"]        = 0.0
                    S["cooldown_until"]      = None
                    S["last_skip_reason"]    = None
                    S["consecutive_sl_hits"] = 0
                    with _log_lock:
                        S["logs"] = []   # clear log buffer for new day
                    daily_reset_fired = True

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

                    # NIFTY reversal exit: if NIFTY crosses back past entry level
                    # the spike has failed — exit immediately before option bleeds
                    if (NIFTY_REVERSAL_EXIT
                            and S["nifty_entry_price"] is not None
                            and S["nifty_price"] is not None
                            and not trade_closed_payload):
                        side      = S["trade_side"]
                        nifty_now = S["nifty_price"]
                        nifty_ref = S["nifty_entry_price"]
                        reversal  = (
                            (side == "CE" and nifty_now < nifty_ref) or
                            (side == "PE" and nifty_now > nifty_ref)
                        )
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
                        elapsed = (
                            (datetime.now() - S["trade_open_time"]).total_seconds()
                            if S["trade_open_time"] else 0.0
                        )
                        exit_result = S["exit_engine"].on_price(
                            aslot["price"],
                            elapsed=elapsed,
                            regime=S.get("ai_regime", "unknown"),
                        )
                        if exit_result:
                            closed = _on_trade_closed_state(exit_result)
                            trade_closed_payload = closed.get("trade_closed_payload")
                            order_intent         = closed.get("order_intent")
                            log_entries.extend(closed.get("logs", []))

    # ── All I/O after lock is fully released ──────────────────────────────────

    if daily_reset_fired:
        today_str = datetime.now().strftime("%d %b %Y")
        socketio.emit("daily_reset", {"date": today_str}, namespace="/")
        log(f"📅 New trading day {today_str} — session and logs cleared", "info")

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
        S["last_skip_reason"] = f"SL cooldown — {remaining}s remaining"
        return True
    S["cooldown_until"] = None
    return False


def _regression_confirms(side):
    slope = S["regression_slope"]
    if slope is None:
        return True
    mn = _self_tuner.get("slope_min")   # learned minimum slope (default 0.3)
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
        return False
    if S["session_pnl"] >= RC["daily_profit_target"]:
        S["last_skip_reason"] = f"Profit target ₹{RC['daily_profit_target']} hit"
        S["running"] = False
        return False
    return True


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
    if S["trades_today"] >= RC["max_trades_day"]:
        return None
    if not _check_daily_limits():
        return None

    move = current - prev
    S["nifty_move"] = round(move, 2)

    if S["pending_side"] is not None:
        S["confirm_count"] += 1
        return _continue_confirmation(current)

    jump_pts = S["jump_threshold"]
    if abs(move) < jump_pts:
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

    if not _vol_detector.is_ranging():
        info = _vol_detector.range_info()
        S["last_skip_reason"] = (
            f"Volatility filter: NIFTY range {info['spread']:.1f}pts "
            f"exceeds limit — spike likely to reverse"
        )
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

    # ── AI Entry Scoring ──────────────────────────────────────────────────
    ai_state = {
        "side":              side,
        "nifty_move":        S["nifty_move"],
        "jump_threshold":    S["jump_threshold"],
        "regression_slope":  S["regression_slope"],
        "nifty_ticks":       list(S["nifty_ticks"]),
        "nifty_price":       current,
        "jump_atr":          S.get("jump_atr"),
        "fast_entry":        S["fast_entry"],
    }
    ai_score, ai_allow, ai_reason = _market_brain.score_entry(ai_state)
    S["ai_entry_score"] = round(ai_score, 3)
    S["ai_regime"]      = _market_brain._last_regime

    if not ai_allow:
        S["last_skip_reason"] = ai_reason
        _reset_pending()
        return None

    log_entries_ai = [(ai_reason, "info")]   # surfaced in caller via logs list

    if S["fast_entry"]:
        result = _enter_trade_state(side, current)
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
def _enter_trade_state(side, nifty_price):
    """
    Called under _state_lock.
    Returns {trade_opened_payload, order_intent, logs} — no I/O performed here.
    """
    sl        = _slot(side)
    opt_price = sl["price"]
    if opt_price is None:
        return None

    seed = list(sl["price_history"])
    info = S["exit_engine"].open_leg(
        side, opt_price,
        sl_pct_override=RC["sl_phase1_pct"],
        seed_prices=seed,
    )

    # Snapshot active parameters so the tuner can learn from this trade's outcome
    S["entry_params"] = _self_tuner.snapshot_entry(S, RC)

    S["trade_open"]         = True
    S["trade_side"]         = side
    S["active_side"]        = side
    S["active_status"]      = "open"
    S["trades_today"]      += 1
    S["trade_open_time"]    = datetime.now()
    S["nifty_entry_price"]  = nifty_price   # record NIFTY level at entry
    S["option_atr"]         = _compute_option_atr(seed)
    S["last_order_id"]      = None

    mode_label = "FAST" if S["fast_entry"] else f"CONFIRMED({S['confirm_needed']}t)"
    jmp_label  = (f"ATR-auto={S['jump_threshold']:.2f}pts" if RC["auto_jump"]
                  else f"fixed={S['jump_threshold']:.2f}pts")

    logs = [
        (
            f"▲ BUY {side} [{mode_label}]  entry=₹{opt_price:.2f}  "
            f"Nifty={nifty_price:.2f}  spike={S['nifty_move']:+.2f}pts  "
            f"threshold={jmp_label}  slope={S['regression_slope']:+.3f}",
            "trade",
        ),
        (
            f"  SL=₹{info['sl']:.2f} ({RC['sl_phase1_pct']}%→{RC['sl_phase2_pct']}% "
            f"after {RC['sl_phase1_secs']}s)  "
            f"qty={info['qty']} lot ({info['qty'] * LOT_SIZE} units)  "
            f"trail={RC['trail_pct_low']}–{RC['trail_pct_high']}% [RATCHETED]",
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
        "action": "buy",
        "symbol": sl["symbol"],
        "qty":    info["qty"] * LOT_SIZE,
        "side":   side,
    } if sl["symbol"] else None

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
    sl_pct_used    = result.get("sl_pct",        RC["sl_phase1_pct"])
    trail_pct_used = result.get("trail_pct",     BUY_TRAIL_PCT)
    atr_at_exit    = result.get("option_atr")
    peak_pct       = result.get("peak_profit_pct", 0)

    reason_label = {
        "sl":             f"SL hit ({sl_pct_used}%)",
        "trail":          f"Trail exit (trail={trail_pct_used}%  atr={atr_at_exit})",
        "timeout":        "Timeout (hard safety — held too long, low profit)",
        "ai_exit":        "AI exit (ExitBrain policy triggered)",
        "nifty_reversal": "NIFTY reversal exit",
        "manual":         "Manual exit (button)",
        "force_exit":     "Force-exit (market close)",
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

    # Teach the AI brain from this trade's outcome
    _market_brain.on_trade_closed(result)
    brain = _market_brain.state
    logs.append((
        f"[AI] learned — trades={brain['n_trades']}  "
        f"win_rate={brain['win_rate']:.1%}  "
        f"regime={brain['last_regime']}  "
        f"model_ready={brain['model_ready']}",
        "info",
    ))

    # Self-tuner: learn entry gate parameters from this trade's outcome
    _self_tuner.on_trade_closed(result, S.get("entry_params", {}))
    _self_tuner.apply_to_rc(RC)
    if _self_tuner.ready:
        t = _self_tuner.state["learned"]
        logs.append((
            f"[Tuner] confirms={t['confirm_ticks']:.1f}  "
            f"jump_floor={t['jump_floor']}pts  slope_min={t['slope_min']}",
            "info",
        ))

    if pnl >= 0:
        S["wins"] += 1
        S["consecutive_sl_hits"] = 0   # winning trade resets the streak
    else:
        S["losses"] += 1
        if reason == "sl":
            S["consecutive_sl_hits"] += 1
            cd = _adaptive_sl_cooldown()
            S["cooldown_until"] = datetime.now() + timedelta(seconds=cd)
            logs.append((
                f"⏳ SL cooldown — {cd}s "
                f"(hit #{S['consecutive_sl_hits']}, base={RC['sl_cooldown_secs']}s)",
                "warning",
            ))
        else:
            S["consecutive_sl_hits"] = 0   # non-SL loss resets streak

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
        today = datetime.now().date()
        if today != S["trading_date"]:
            S["trading_date"]     = today
            S["session_pnl"]      = 0.0
            S["trades_today"]     = 0
            S["wins"]             = 0
            S["losses"]           = 0
            S["trade_pnl"]        = 0.0
            S["cooldown_until"]   = None
            S["last_skip_reason"] = None
        payload = _build_state_payload()
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
    ce_raw   = data.get("ce_input", "").strip()
    pe_raw   = data.get("pe_input", "").strip()
    trade_ce = bool(data.get("trade_ce", False))
    trade_pe = bool(data.get("trade_pe", False))

    if not trade_ce and not trade_pe:
        emit("error", {"msg": "Check at least CE or PE to trade"})
        return

    def _tok(s):
        try:
            return int(s.strip().split("/")[-1]) if s else None
        except Exception:
            return None

    def _sym(s):
        return s.split("/")[0].strip() if "/" in s else ""

    def _label(s):
        return s.split("/")[0].strip() if "/" in s else s

    active_sides = set()
    ce_slot_data = _make_opt_slot()
    pe_slot_data = _make_opt_slot()

    if trade_ce:
        tok = _tok(ce_raw)
        if not tok:
            emit("error", {"msg": "CE is checked but token is invalid"})
            return
        ce_slot_data["token"]  = tok
        ce_slot_data["symbol"] = _sym(ce_raw)
        ce_slot_data["strike"] = _label(ce_raw)
        active_sides.add("CE")

    if trade_pe:
        tok = _tok(pe_raw)
        if not tok:
            emit("error", {"msg": "PE is checked but token is invalid"})
            return
        pe_slot_data["token"]  = tok
        pe_slot_data["symbol"] = _sym(pe_raw)
        pe_slot_data["strike"] = _label(pe_raw)
        active_sides.add("PE")

    buf = max(REGRESSION_WINDOW, MOMENTUM_WINDOW + 2, OPTION_ATR_PERIOD + 2,
              CONFIRM_TICKS_SLOW + 5, RC["jump_atr_window"] + 2)

    with _state_lock:
        S["slots"] = {"CE": ce_slot_data, "PE": pe_slot_data}
        S.update({
            "running":            True,
            "trade_open":         False,
            "active_sides":       active_sides,
            "trade_side":         None,
            "nifty_price":        None,
            "nifty_prev_tick":    None,
            "nifty_ref":          None,
            "nifty_move":         None,
            "nifty_ticks":        deque(maxlen=buf),
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
            "last_skip_reason":   None,
            "last_order_id":      None,
            "last_exit_order_id": None,
            "exit_engine":        BuyExitStrategy(capital=S["capital"]),
        })
        sides_str = " + ".join(sorted(active_sides))
        mode_str  = "🔴 REAL TRADING" if S["trading_mode"] == "real" else "🔵 DEMO (paper)"
        jmp_str   = (f"AUTO ATR×{RC['jump_atr_multiplier']} [{RC['jump_min_pts']}–{RC['jump_max_pts']}pts]"
                     if RC["auto_jump"] else f"FIXED {RC['jump_pct']}% Nifty")

    tokens_to_sub = [NIFTY_TOKEN]
    if "CE" in active_sides:
        tokens_to_sub.append(S["slots"]["CE"]["token"])
    if "PE" in active_sides:
        tokens_to_sub.append(S["slots"]["PE"]["token"])
    _subscribe(tokens_to_sub)

    log(f"Buy Robot v8.2 started — sides={sides_str}  [{mode_str}]", "success")
    log(f"  Spike threshold: {jmp_str}", "info")
    for side in ("CE", "PE"):
        if side in active_sides:
            sl = S["slots"][side]
            log(f"  {side}: token={sl['token']}  symbol={sl['symbol'] or '(none)'}  strike={sl['strike']}", "info")
    if len(active_sides) == 2:
        log("  BOTH mode — whichever side spikes first gets the trade", "info")
    if S["trading_mode"] == "real":
        log("⚠ REAL MODE — check Zerodha for existing open positions!", "error")

    broadcast()


@socketio.on("stop_robot")
def on_stop():
    _close_active_trade(reason="manual")

    with _state_lock:
        tokens = [NIFTY_TOKEN]
        for side in ("CE", "PE"):
            t = S["slots"][side]["token"]
            if t:
                tokens.append(t)
        S["running"]      = False
        S["trade_open"]   = False
        S["active_sides"] = set()
        S["trade_side"]   = None
        S["slots"]        = {"CE": _make_opt_slot(), "PE": _make_opt_slot()}
        _reset_pending()

    _unsubscribe(tokens)
    log("Buy Robot v8.2 stopped.", "warning")
    broadcast()


@socketio.on("manual_exit_trade")
def on_manual_exit():
    closed, _ = _close_active_trade(reason="manual")
    if not closed:
        emit("error", {"msg": "No open trade to exit"})
        return
    broadcast()


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
    else:
        log("🔵 Switched to DEMO mode — paper trading only", "success")
    broadcast()


@socketio.on("get_state")
def on_get_state():
    broadcast()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("buy_index.html")


@app.route("/settings")
def settings():
    return send_file("buy_settings.html")



if __name__ == "__main__":
    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)