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
    HIST_ATR_DAYS, HIST_ATR_SPIKE_FRACTION,
    SPIKE_LOOKBACK_SECS, SPIKE_MAX_SECS, SPIKE_MIN_SPEED_PCT_SEC,
    NIFTY_REVERSAL_EXIT, NIFTY_REVERSAL_BUFFER_PCT, NIFTY_REVERSAL_PROFIT_SKIP_PCT,
    FAST_MOVE_VELOCITY,
    BREAKEVEN_TRIGGER_PCT,
    ZERODHA_CONFIG,
    BREAKOUT_FILTER_ENABLED, BREAKOUT_RANGE_WINDOW,
    VOL_GATE_ENABLED, VOL_LOW_ATR_PTS, VOL_HIGH_ATR_PTS,
    BANKNIFTY_TOKEN,
    TREND_ENABLED, TREND_WINDOW, TREND_MIN_MOVE_PCT,
    TREND_CONSISTENCY_PCT, TREND_COOLDOWN_TICKS,
    ENSEMBLE_ENABLED,
    SMART_COOLDOWN_ENABLED,
    COOLDOWN_AFTER_SL, COOLDOWN_AFTER_TRAIL_WIN, COOLDOWN_AFTER_TRAIL_LOSS,
    COOLDOWN_AFTER_TIMEOUT_WIN, COOLDOWN_AFTER_TIMEOUT_LOSS,
    COOLDOWN_AFTER_AI_EXIT, COOLDOWN_AFTER_REVERSAL,
)
from buy_exit_strategy import BuyExitStrategy
from market_brain import MarketBrain
from money_manager import MoneyManager
from ensemble_brain import EnsembleBrain
from per_stock_strategy import StockStrategyManager
from advanced_filters import BreakoutFilter, VolatilityGate
from zerodha_websocket import connect_zerodha_websocket
from Zerodha_api import place_buy, place_sell, fetch_balance
from stock_screener import StockScreener
from option_resolver import OptionResolver
from multi_stock_bot import MultiStockBot

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

# ── LTP fetch via kite.zerodha.com (enctoken / cookie auth) ──────────────────
def _fetch_ltp(symbol: str, exchange: str = "NSE") -> float | None:
    """
    Fetch last traded price using Kite web session (enctoken + cookies).
    Uses kite.zerodha.com/oms/quote — works with web-session tokens.
    Falls back to None on any error.
    """
    import urllib.parse as _up
    cfg      = ZERODHA_CONFIG
    enctoken = _up.unquote(cfg.get("enctoken", ""))
    user_id  = cfg.get("user_id", "")
    cookie   = (
        f"kf_session={cfg.get('kf_session', '')}; "
        f"user_id={user_id}; "
        f"public_token={cfg.get('public_token', '')}; "
        f"enctoken={enctoken}"
    )
    inst_key = f"{exchange}:{symbol}"
    url = "https://kite.zerodha.com/oms/quote?i=" + _up.quote(inst_key, safe=":")
    req = _urllib_request.Request(url, headers={
        "Authorization": f"enctoken {enctoken}",
        "Cookie":        cookie,
        "Accept":        "application/json, text/plain, */*",
        "Referer":       "https://kite.zerodha.com/",
        "User-Agent":    cfg.get("user_agent", "kite3-web"),
        "x-kite-userid": user_id,
        "x-kite-version": cfg.get("version", "3.0.0"),
    })
    try:
        with _urllib_request.urlopen(req, timeout=8) as resp:
            data = _json_mod.loads(resp.read().decode())
        return float(data["data"][inst_key]["last_price"])
    except Exception:
        return None


def _estimate_spot_from_strikes(symbol: str) -> float | None:
    """
    Fallback: estimate spot price as the median of available NFO option strikes.
    Good enough to pick an approximately ATM strike when live LTP is unavailable.
    """
    try:
        instruments = _fetch_nfo_instruments()
        sym_upper = symbol.upper()
        strikes = sorted(set(
            float(r["strike"])
            for r in instruments
            if r.get("name", "").upper() == sym_upper
            and r.get("type") in ("CE", "PE")   # _fetch_nfo_instruments uses "type" not "instrument_type"
            and r.get("strike")
        ))
        if not strikes:
            return None
        return strikes[len(strikes) // 2]   # median strike ≈ ATM
    except Exception:
        return None

# ── Historical candles from Zerodha (intraday OHLC) ──────────────────────────
def _fetch_historical_candles(token: int, interval: str = "minute", days: int = 1) -> list:
    """
    Fetch OHLC candles from Zerodha historical API.
    interval: "minute", "day", "3minute", "5minute", etc.
    days: how many calendar days back to fetch from (1 = today only, 30 = last 30 days)
    Returns [{t, o, h, l, c}, ...] with t as Unix timestamp.
    """
    import urllib.parse as _up
    from datetime import date, timedelta, datetime as _dt
    cfg         = ZERODHA_CONFIG
    enctoken    = _up.unquote(cfg.get("enctoken", ""))
    user_id     = cfg.get("user_id", "")
    kf_session  = cfg.get("kf_session", "")
    public_token = cfg.get("public_token", "")
    today       = date.today().strftime("%Y-%m-%d")
    from_date   = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (
        f"https://kite.zerodha.com/oms/instruments/historical/{token}/{interval}"
        f"?user_id={_up.quote(user_id)}&oi=1&from={from_date}&to={today}"
    )
    cookie = (
        f"kf_session={kf_session}; "
        f"user_id={user_id}; "
        f"public_token={public_token}; "
        f"enctoken={enctoken}"
    )
    req = _urllib_request.Request(url, headers={
        "Authorization":  f"enctoken {enctoken}",
        "Cookie":         cookie,
        "Accept":         "application/json, */*",
        "Referer":        "https://kite.zerodha.com/",
        "User-Agent":     cfg.get("user_agent", "kite3-web"),
        "x-kite-userid":  user_id,
        "x-kite-version": cfg.get("version", "3.0.0"),
    })
    try:
        with _urllib_request.urlopen(req, timeout=10) as resp:
            data = _json_mod.loads(resp.read().decode())
        raw = data.get("data", {}).get("candles", [])
        result = []
        for c in raw:
            try:
                t = _dt.fromisoformat(c[0]).timestamp()
                result.append({"t": t, "o": c[1], "h": c[2], "l": c[3], "c": c[4]})
            except Exception:
                pass
        return result
    except Exception as e:
        app.logger.warning(f"Historical candles fetch failed for token {token}: {e}")
        return []


# ── Multi-factor stock scorer ────────────────────────────────────────────────
def _compute_stock_score(
    daily_candles: list,
    current_price: float,
    ce_price: float | None = None,
    pe_price: float | None = None,
) -> dict:
    """
    Compute a 0-100 composite score for a stock using multiple historical factors.

    Factors (with weights):
      25% — ATR%          14-day avg true range as % of price (volatility = option payoff potential)
      20% — 3-day momentum  abs % change over last 3 trading days
      20% — Intraday range  today's H-L as % of price (already in motion)
      15% — Week position   price near 5-day high or low (breakout/breakdown setup)
      10% — 30-day trend    consistent directional move over past month
      10% — Premium quality combined CE+PE premium as % of price (liquidity proxy)
    """
    if not daily_candles or current_price <= 0:
        return {"score": 0.0, "breakdown": {}}

    closes = [c["c"] for c in daily_candles]
    highs  = [c["h"] for c in daily_candles]
    lows   = [c["l"] for c in daily_candles]
    n = len(closes)

    # 1. ATR% — 14-day average true range as % of current price
    atr_s = 0.0
    if n >= 2:
        window = min(14, n)
        trs    = [highs[i] - lows[i] for i in range(n - window, n)]
        atr_pct = (np.mean(trs) / current_price) * 100 if trs else 0
        atr_s   = min(atr_pct / 4.0, 1.0)   # 4% ATR → full score

    # 2. 3-day momentum
    mom3_s = 0.0
    if n >= 4:
        ret3 = abs((closes[-1] - closes[-4]) / closes[-4]) * 100
        mom3_s = min(ret3 / 6.0, 1.0)        # 6% move → full score

    # 3. Intraday range (today)
    intra_s = 0.0
    if n >= 1:
        rng_pct = (highs[-1] - lows[-1]) / current_price * 100
        intra_s = min(rng_pct / 4.0, 1.0)    # 4% intraday range → full score

    # 4. Week position — near 5-day H/L extremes = breakout setup
    week_s = 0.0
    if n >= 5:
        wh  = max(highs[-5:])
        wl  = min(lows[-5:])
        rng = wh - wl
        if rng > 0:
            pos    = (current_price - wl) / rng   # 0=at low, 1=at high
            # Score peaks at extremes (near 0 or 1), zero at centre (0.5)
            week_s = max(0.0, min(1.0, (abs(pos - 0.5) - 0.1) / 0.4))

    # 5. 30-day trend consistency
    trend_s = 0.0
    if n >= 20:
        ret30  = abs((closes[-1] - closes[-min(20, n)]) / closes[-min(20, n)]) * 100
        trend_s = min(ret30 / 12.0, 1.0)     # 12% monthly move → full score

    # 6. Option premium quality (CE + PE combined ATM premium as % of spot)
    prem_s = 0.0
    if ce_price and pe_price and current_price > 0:
        prem_pct = (ce_price + pe_price) / current_price * 100
        prem_s   = min(prem_pct / 3.0, 1.0)  # 3% combined premium → full score

    composite = (
        0.25 * atr_s   +
        0.20 * mom3_s  +
        0.20 * intra_s +
        0.15 * week_s  +
        0.10 * trend_s +
        0.10 * prem_s
    ) * 100.0

    return {
        "score": round(composite, 1),
        "breakdown": {
            "atr":      round(atr_s   * 100, 0),
            "mom_3d":   round(mom3_s  * 100, 0),
            "intraday": round(intra_s * 100, 0),
            "week_pos": round(week_s  * 100, 0),
            "trend_30": round(trend_s * 100, 0),
            "premium":  round(prem_s  * 100, 0),
        },
    }


def _compute_historical_atr(candles: list, days: int = 7) -> float | None:
    """
    Compute average True Range over the last `days` daily candles.
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    Returns ATR in absolute price points, or None if insufficient data.
    """
    if len(candles) < 2:
        return None
    window = candles[-min(days + 1, len(candles)):]   # +1 for prev_close
    trs = []
    for i in range(1, len(window)):
        h  = window[i]["h"]
        l  = window[i]["l"]
        pc = window[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return sum(trs[-days:]) / len(trs[-days:])


def _parse_strike_from_symbol(opt_symbol: str) -> float | None:
    """Extract strike price from an option symbol like 'RELIANCE25JUN2900CE'."""
    m = _re.search(r'(\d+(?:\.\d+)?)(CE|PE)$', (opt_symbol or "").upper())
    return float(m.group(1)) if m else None


def _update_stock_atm(symbol: str, result: dict):
    """
    Apply a fresh ATM re-resolution: update multi_bot CE/PE tokens,
    re-subscribe, and persist the updated watchlist.
    Called from background threads — no locks held.
    """
    ce_row    = result.get("CE") or {}
    pe_row    = result.get("PE") or {}
    new_ce    = int(ce_row["token"])  if ce_row.get("token")  else None
    new_pe    = int(pe_row["token"])  if pe_row.get("token")  else None
    ce_sym    = ce_row.get("symbol")
    pe_sym    = pe_row.get("symbol")

    old_toks, new_toks = _multi_bot.update_option_tokens(
        symbol, ce_token=new_ce, pe_token=new_pe,
        ce_symbol=ce_sym, pe_symbol=pe_sym,
    )
    if old_toks:
        _unsubscribe(old_toks)
    if new_toks:
        _subscribe(new_toks)

    # Persist updated watchlist so the new strike survives a restart
    wl = _load_watchlist()
    for item in wl:
        if item.get("symbol", "").upper() == symbol.upper():
            item["ce_token"]  = new_ce
            item["pe_token"]  = new_pe
            item["ce_symbol"] = ce_sym
            item["pe_symbol"] = pe_sym
            item["strike"]    = result.get("strike")
            item["expiry"]    = result.get("expiry")
            break
    _save_watchlist(wl)

    log(
        f"[ATM-ROLL] {symbol}  strike={result['strike']}  expiry={result['expiry']}  "
        f"CE={ce_sym}  PE={pe_sym}",
        "info",
    )


def _refresh_all_stock_scores():
    """
    Fetch daily candles for every watchlist stock, recompute composite scores
    and push historical ATR-based spike threshold to each slot.
    Also re-locks ATM options when the stock price has drifted > 1 step.
    Called from background thread every 5 minutes during market hours.
    """
    status = _multi_bot.get_status()
    for symbol, data in status.items():
        token = data.get("token")
        if not token:
            continue
        try:
            candles = _fetch_historical_candles(token, "day", days=40)
            if not candles:
                continue
            score_data = _compute_stock_score(
                daily_candles=candles,
                current_price=data.get("price") or 0,
                ce_price=data.get("ce_price"),
                pe_price=data.get("pe_price"),
            )
            _multi_bot.update_stock_score(symbol, score_data)

            # Compute and push historical ATR spike threshold
            hist_atr = _compute_historical_atr(candles, days=RC.get("hist_atr_days", 7))
            if hist_atr:
                fraction  = RC.get("hist_atr_spike_fraction", 0.18)
                threshold = round(hist_atr * fraction, 2)
                threshold = max(RC["jump_min_pts"], min(RC["jump_max_pts"], threshold))
                _multi_bot.set_historical_threshold(symbol, round(threshold, 2), round(hist_atr, 2))
                app.logger.debug(
                    "Hist ATR %s: 7d_atr=%.2f  spike_thr=%.2f", symbol, hist_atr, threshold
                )

            # ── ATM re-lock: roll to nearest strike if price drifted ──────────
            current_price = data.get("price") or 0
            if current_price <= 0:
                continue

            ce_sym = data.get("ce_symbol") or ""
            pe_sym = data.get("pe_symbol") or ""
            current_strike = (
                _parse_strike_from_symbol(ce_sym) or
                _parse_strike_from_symbol(pe_sym)
            )

            if current_strike:
                # Estimate strike step: use known table, else 1% of price as proxy
                from option_resolver import KNOWN_STEPS
                step = float(KNOWN_STEPS.get(symbol.upper(), max(current_price * 0.01, 5.0)))

                drift = abs(current_price - current_strike)
                if drift >= step * 1.0:   # price moved ≥ 1 full step from ATM → roll
                    app.logger.info(
                        "ATM roll needed for %s: spot=%.1f  strike=%.1f  drift=%.1f  step=%.1f",
                        symbol, current_price, current_strike, drift, step,
                    )
                    new_result = _resolver.resolve(symbol, current_price)
                    if new_result and new_result.get("strike") != current_strike:
                        _update_stock_atm(symbol, new_result)

        except Exception as exc:
            app.logger.warning(f"Score refresh failed for {symbol}: {exc}")


def _score_refresh_loop():
    """Background daemon: refresh composite scores every 5 min during market hours."""
    # _init_watchlist already fires an immediate one-off refresh at startup.
    # This loop handles the periodic 5-min updates during market hours.
    _time.sleep(60)   # short delay — let the one-off init refresh finish first
    while True:
        try:
            now = datetime.now().time()
            if dtime(9, 0) <= now <= dtime(15, 35):
                _refresh_all_stock_scores()
        except Exception as exc:
            app.logger.warning(f"Score refresh loop error: {exc}")
        _time.sleep(300)   # 5-minute interval


# ── NSE equity instruments cache ─────────────────────────────────────────────
_nse_instruments_cache: list = []
_nse_instruments_cache_ts: float = 0.0

def _fetch_nse_instruments() -> list:
    global _nse_instruments_cache, _nse_instruments_cache_ts
    if _nse_instruments_cache and (_time.time() - _nse_instruments_cache_ts) < 3600:
        return _nse_instruments_cache
    req = _urllib_request.Request(
        "https://api.kite.trade/instruments/NSE",
        headers={"Accept": "text/csv", "User-Agent": "Mozilla/5.0"},
    )
    with _urllib_request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
    reader = _csv.DictReader(_io.StringIO(raw))
    _nse_instruments_cache = list(reader)
    _nse_instruments_cache_ts = _time.time()
    return _nse_instruments_cache

# Known index tokens (not in NSE equity instruments list)
_INDEX_TOKENS = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "FINNIFTY":   257801,
    "MIDCPNIFTY": 288009,
    "SENSEX":     265,
}

# Index symbols that trade on BSE exchange (not NSE)
_BSE_INDEX_SYMBOLS = {"SENSEX", "BANKEX"}

# Searchable index entries shown in watchlist search
_SEARCHABLE_INDICES = [
    {"symbol": "NIFTY",      "name": "NIFTY 50"},
    {"symbol": "BANKNIFTY",  "name": "BANK NIFTY"},
    {"symbol": "SENSEX",     "name": "BSE SENSEX"},
    {"symbol": "FINNIFTY",   "name": "NIFTY FIN SERVICE"},
    {"symbol": "MIDCPNIFTY", "name": "NIFTY MIDCAP SELECT"},
]

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
_stock_strategy  = StockStrategyManager() # Per-stock AI brain (one model per watchlist stock)
_money_mgr       = MoneyManager(capital=CAPITAL)  # AI dynamic lot sizing
_ensemble_brain  = EnsembleBrain()        # Master entry confidence scorer
_breakout_filter = BreakoutFilter()       # consolidation-range breakout gate
_vol_gate        = VolatilityGate()       # choppy/low-vol/high-vol regime gate
_screener        = StockScreener()        # multi-strategy F&O stock screener
_resolver        = OptionResolver()       # ATM option token resolver

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

    # ── Advanced entry filters (v8.5) ─────────────────────────────────────
    "breakout_filter_enabled": BREAKOUT_FILTER_ENABLED,
    "breakout_range_window":   BREAKOUT_RANGE_WINDOW,
    "vol_gate_enabled":        VOL_GATE_ENABLED,
    "vol_low_atr_pts":         VOL_LOW_ATR_PTS,
    "vol_high_atr_pts":        VOL_HIGH_ATR_PTS,

    # ── Trend detection ───────────────────────────────────────────────────
    "trend_enabled":           TREND_ENABLED,
    "trend_window":            TREND_WINDOW,
    "trend_min_move_pct":      TREND_MIN_MOVE_PCT,
    "trend_consistency_pct":   TREND_CONSISTENCY_PCT,
    "trend_cooldown_ticks":    TREND_COOLDOWN_TICKS,

    # ── Ensemble + Smart Cooldown ─────────────────────────────────────────
    "ensemble_enabled":        ENSEMBLE_ENABLED,
    "smart_cooldown_enabled":  SMART_COOLDOWN_ENABLED,
}

# Multi-stock bot — instantiated after RC is defined (takes RC as live reference)
def _on_multi_stock_spike(spike_info: dict):
    """
    Called by MultiStockBot (no locks held) when any tracked stock spikes.
    Per-stock AI brain scores the entry and derives custom SL/trail/timeout.
    Initiates a trade using the existing _enter_trade_state machinery.
    """
    symbol = spike_info.get("symbol", "").upper()

    # ── Filter: only trade stocks the user ticked ─────────────────────────────
    with _state_lock:
        active_syms = S.get("active_symbols", set())
    if active_syms and symbol not in active_syms:
        return

    # ── Per-stock AI entry scoring (outside lock — read-only on slot data) ────
    slot_status  = _multi_bot.get_status()
    slot_data    = slot_status.get(symbol, {})
    nifty_regime = _market_brain._last_regime   # last classified regime (safe to read)

    ai_score, ai_allow, ai_reason, ai_params = _stock_strategy.score_entry(
        symbol, spike_info, slot_data
    )

    trigger_type  = spike_info.get("trigger_type", "spike")
    trigger_label = {"spike": "⚡ SPIKE", "trend_up": "📈 UPTREND", "trend_down": "📉 DOWNTREND"}.get(trigger_type, trigger_type.upper())
    if trigger_type == "spike":
        speed_str = (f"  {spike_info['elapsed_secs']:.1f}s @ {spike_info['speed_pts_sec']:.2f}pts/s"
                     if spike_info.get("elapsed_secs") else "")
        trigger_label += speed_str

    trade_opened_payload = None
    order_intent         = None
    log_entries          = [(f"[{trigger_label}] {symbol}  {ai_reason}", "info")]

    if not ai_allow:
        # Per-stock brain blocked this entry — log and skip
        log(ai_reason, "skip")
        return

    # ── Ensemble check for multi-stock path ───────────────────────────────────
    if RC.get("ensemble_enabled", ENSEMBLE_ENABLED):
        ens_signals = {
            "market_ai_score": _market_brain.state.get("win_rate") or 0.5,
            "stock_ai_score":  ai_score,
            "regime":          nifty_regime,
            "side":            spike_info.get("side", "CE"),
            "breakout_passed": True,
            "vol_mult":        slot_data.get("vol_mult", 1.0),
            "option_price":    spike_info.get("option_price"),
        }
        ens_score, ens_allow, ens_reason = _ensemble_brain.score_entry(ens_signals)
        log_entries.append((ens_reason, "info"))
        if not ens_allow:
            log(ens_reason, "skip")
            return

    with _state_lock:
        if not S["running"] or S["trade_open"]:
            return
        if S["trades_today"] >= RC["max_trades_day"]:
            return
        if not _check_daily_limits():
            return
        if not _is_valid_time():
            return
        if _is_in_cooldown():
            return

        side = spike_info["side"]

        # Side-bias override: if AI strongly prefers opposite side, skip this spike
        if ai_params.get("side_bias") and ai_params["side_bias"] != side and ai_score < 0.55:
            return

        opt_slot   = _make_opt_slot()
        opt_slot["token"]  = int(spike_info["option_token"])
        opt_slot["symbol"] = spike_info["option_symbol"]
        opt_slot["price"]  = spike_info["option_price"]
        opt_slot["strike"] = spike_info["option_symbol"]

        if side == "CE":
            S["slots"] = {"CE": opt_slot, "PE": _make_opt_slot()}
        else:
            S["slots"] = {"PE": opt_slot, "CE": _make_opt_slot()}
        S["active_sides"]       = {side}
        S["nifty_price"]        = spike_info["underlying_price"]
        S["nifty_move"]         = spike_info["move"]
        S["nifty_ref"]          = spike_info["underlying_price"] - spike_info["move"]
        S["nifty_entry_price"]  = None   # disable NIFTY reversal-exit for stock trades
        S["fast_entry"]         = True
        S["index_name"]         = symbol
        S["index_token"]        = int(spike_info["token"])
        S["_last_trade_symbol"] = symbol   # tracked for on_trade_closed callback

        result = _enter_trade_state(side, spike_info["underlying_price"],
                                    override_params=ai_params)
        if result:
            trade_opened_payload = result.get("trade_opened_payload")
            order_intent         = result.get("order_intent")
            log_entries.extend(result.get("logs", []))

    for msg, level in log_entries:
        log(msg, level)

    if trade_opened_payload:
        _run_order_intent(order_intent, trade_opened_payload, None)
        socketio.emit("trade_opened", trade_opened_payload, namespace="/")
        broadcast()


_multi_bot = MultiStockBot(RC, _on_multi_stock_spike)

# Background thread: refresh multi-factor scores every 5 min during market hours
_score_thread = threading.Thread(target=_score_refresh_loop, daemon=True, name="score-refresh")
_score_thread.start()


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
    "ensemble_score":     None,
    "_last_trade_symbol": "",
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

    "index_token":        NIFTY_TOKEN,    # switchable: NIFTY_TOKEN or BANKNIFTY_TOKEN
    "index_name":         "NIFTY",        # display name for the selected index

    "ws_connected":       False,
    "ws_send_queue":      None,
    "ws_loop":            None,
    "subscribed_tokens":  [],

    "logs":               [],
    "live_trail_pct":     BUY_TRAIL_PCT,
    "option_atr":         None,

    # Advanced filter runtime state
    "adv_vol_mult":       1.0,
    "adv_filter_log":     [],
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


# ── Auto jump threshold ───────────────────────────────────────────────────────
def _update_jump_threshold(nifty_price):
    atr = _compute_atr(list(S["nifty_atr_ticks"]))
    S["jump_atr"] = atr

    if RC["auto_jump"] and atr is not None:
        raw = atr * RC["jump_atr_multiplier"]
        pts = round(max(RC["jump_min_pts"], min(RC["jump_max_pts"], raw)), 2)
    else:
        pts = round(nifty_price * RC["jump_pct"] / 100, 2) if nifty_price else RC["jump_min_pts"]

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
        "index_name":           S["index_name"],
        "index_token":          S["index_token"],
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
        "ensemble_brain":       _ensemble_brain.state,
        "ensemble_score":       S.get("ensemble_score"),
        "money_manager":        _money_mgr.state,

        # New config constants for UI display
        "breakeven_trigger_pct": BREAKEVEN_TRIGGER_PCT,
        "nifty_reversal_exit":   NIFTY_REVERSAL_EXIT,
        "fast_move_velocity":    FAST_MOVE_VELOCITY,

        # Advanced filter runtime state (v8.5)
        "adv_vol_atr":          _vol_gate.atr,
        "adv_vol_rev_rate":     _vol_gate.rev_rate,
        "adv_vol_mult":         S.get("adv_vol_mult", 1.0),
        "adv_breakout_range":   _breakout_filter.range_info(RC["breakout_range_window"]),
        "adv_breakout_enabled": RC["breakout_filter_enabled"],
        "adv_vol_gate_enabled": RC["vol_gate_enabled"],
    }


# ── Broadcast ─────────────────────────────────────────────────────────────────
def broadcast():
    """Build state snapshot under the lock, emit outside it."""
    with _state_lock:
        payload = _build_state_payload()
    ms = _multi_bot.get_status()   # own lock, safe outside _state_lock
    # Merge per-stock AI state into each stock's entry in multi_stocks
    for sym, stock_data in ms.items():
        stock_data["ai_strategy"] = _stock_strategy.get_state(sym)
    payload["multi_stocks"] = ms
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
    trading_active = S["running"] or S["trade_open"]

    if not trading_active:
        # Robot stopped — still update prices for live dashboard display
        for tick in ticks:
            token = tick.get("instrument_token")
            price = tick.get("last_price")
            vol   = tick.get("volume_traded") or tick.get("volume") or 0
            if price is None:
                continue
            with _state_lock:
                if token == S["index_token"]:
                    S["nifty_price"] = price
                    S["nifty_ticks"].append(price)
            _multi_bot.process_tick(token, price, vol)
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
            if token == S["index_token"]:
                S["nifty_price"] = price
                S["nifty_ticks"].append(price)
                S["nifty_atr_ticks"].append(price)
                S["regression_slope"] = _regression_slope(list(S["nifty_ticks"]))
                _update_jump_threshold(price)
                # Advanced filter history — must update before _check_spike
                _breakout_filter.update(price)
                _vol_gate.update(price)

                # ── Auto daily reset ──────────────────────────────────────
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

        # ── Multi-stock bot: route tick to any tracked stock ─────────────────
        # This may fire _on_multi_stock_spike which acquires _state_lock
        # independently — safe because we don't hold _state_lock here.
        _multi_bot.process_tick(token, price, volume)

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

    # ── Advanced filters (v8.5) ───────────────────────────────────────────

    # 1. Volatility Gate — block choppy oscillating markets
    if RC["vol_gate_enabled"]:
        vol_allow, vol_mult, vol_msg = _vol_gate.check(
            RC["vol_low_atr_pts"], RC["vol_high_atr_pts"]
        )
        if not vol_allow:
            S["last_skip_reason"] = vol_msg
            return None
    else:
        vol_mult = 1.0
        vol_msg  = "vol_gate: disabled"

    # 2. Breakout Filter — spike must break out of consolidation range
    if RC["breakout_filter_enabled"]:
        bo_allow, bo_msg = _breakout_filter.is_breakout(
            side, window=RC["breakout_range_window"]
        )
        if not bo_allow:
            S["last_skip_reason"] = bo_msg
            return None
    else:
        bo_msg = "breakout: disabled"

    S["adv_vol_mult"]   = vol_mult
    S["adv_filter_log"] = [vol_msg, bo_msg]

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

    # ── Ensemble Brain: master confidence score ───────────────────────────────
    if RC.get("ensemble_enabled", ENSEMBLE_ENABLED):
        ens_signals = {
            "market_ai_score": ai_score,
            "stock_ai_score":  ai_score,   # same source for NIFTY path
            "regime":          S["ai_regime"],
            "side":            side,
            "breakout_passed": True,        # already passed breakout gate above
            "vol_mult":        S.get("adv_vol_mult", 1.0),
            "option_price":    _slot(side).get("price"),
        }
        ens_score, ens_allow, ens_reason = _ensemble_brain.score_entry(ens_signals)
        S["ensemble_score"] = round(ens_score, 3)
        if not ens_allow:
            S["last_skip_reason"] = ens_reason
            _reset_pending()
            return None
    else:
        ens_reason = "ensemble: disabled"
        S["ensemble_score"] = round(ai_score, 3)

    log_entries_ai = [(ai_reason, "info"), (ens_reason, "info")]
    # Log advanced filter verdicts
    for adv_msg in S.get("adv_filter_log", []):
        log_entries_ai.append((f"  ↳ {adv_msg}", "info"))

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
def _enter_trade_state(side, nifty_price, override_params: dict | None = None):
    """
    Called under _state_lock.
    override_params: optional per-stock AI params (sl_pct_p1, sl_pct_p2,
                     sl_phase1_secs, trail_pct, timeout_secs) from StockStrategyManager.
    Returns {trade_opened_payload, order_intent, logs} — no I/O performed here.
    """
    sl        = _slot(side)
    opt_price = sl["price"]
    if opt_price is None:
        return None

    p    = override_params or {}
    seed = list(sl["price_history"])

    # ── AI money management: compute dynamic lot size ─────────────────────
    sl_pct_for_mm = p.get("sl_pct_p1", RC["sl_phase1_pct"])
    mm_lot_size   = sl.get("lot_size") or LOT_SIZE
    mm_lots, mm_reason = _money_mgr.compute_lots(
        capital      = S.get("capital", CAPITAL),
        option_price = opt_price,
        sl_pct       = sl_pct_for_mm,
        lot_size     = mm_lot_size,
    )

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
        qty_override=mm_lots,
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
            f"trail={p.get('trail_pct', RC['trail_pct_low'])}% "
            f"timeout={p.get('timeout_secs', RC.get('trade_timeout_secs', 60))}s",
            "info",
        ),
        (mm_reason, "info"),
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

    # Update money manager with trade outcome (for Kelly + streak tracking)
    _money_mgr.record_trade(
        pnl_pct = result.get("pnl_pct", 0.0) or 0.0,
        pnl     = pnl,
    )
    mm_st = _money_mgr.state
    logs.append((
        f"[MM] kelly={mm_st['kelly_mult']}  "
        f"wr={mm_st['win_rate']:.1%}  "
        f"streak={mm_st['loss_streak']}  "
        f"peak=₹{mm_st['peak_equity']:,.0f}  "
        f"last_lots={mm_st['last_lots']}",
        "info",
    ))

    # Teach the ensemble brain and global market brain
    _ensemble_brain.on_trade_closed(result)
    _market_brain.on_trade_closed(result)
    brain = _market_brain.state
    logs.append((
        f"[AI] global learned — trades={brain['n_trades']}  "
        f"win_rate={brain['win_rate']:.1%}  "
        f"regime={brain['last_regime']}  "
        f"model_ready={brain['model_ready']}",
        "info",
    ))

    # Teach the per-stock brain — uses the symbol tracked at entry time
    _last_sym = S.get("_last_trade_symbol", "")
    if _last_sym:
        _stock_strategy.on_trade_closed(_last_sym, result)
        stock_state = _stock_strategy.get_state(_last_sym)
        logs.append((
            f"[AI] {_last_sym} learned — "
            f"trades={stock_state['n_trades']}  "
            f"win_rate={stock_state['win_rate']:.1%}  "
            f"trail_mult={stock_state['trail_mult']:.2f}  "
            f"avg_pnl={stock_state['avg_pnl']:+.2f}%",
            "info",
        ))
        S["_last_trade_symbol"] = ""

    if pnl >= 0:
        S["wins"] += 1
    else:
        S["losses"] += 1

    # ── Smart reason-aware cooldown ───────────────────────────────────────────
    if SMART_COOLDOWN_ENABLED:
        _cd = {
            "sl":              COOLDOWN_AFTER_SL,
            "trail":           COOLDOWN_AFTER_TRAIL_WIN  if pnl >= 0 else COOLDOWN_AFTER_TRAIL_LOSS,
            "timeout":         COOLDOWN_AFTER_TIMEOUT_WIN if pnl >= 0 else COOLDOWN_AFTER_TIMEOUT_LOSS,
            "ai_exit":         COOLDOWN_AFTER_AI_EXIT,
            "nifty_reversal":  COOLDOWN_AFTER_REVERSAL,
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
            payload["logs"] = list(S["logs"])[-50:]
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
        # Advanced filters
        "breakout_filter_enabled": "BREAKOUT_FILTER_ENABLED",
        "breakout_range_window":   "BREAKOUT_RANGE_WINDOW",
        "vol_gate_enabled":        "VOL_GATE_ENABLED",
        "vol_low_atr_pts":         "VOL_LOW_ATR_PTS",
        "vol_high_atr_pts":        "VOL_HIGH_ATR_PTS",
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
    active_symbols = set(s.upper() for s in data.get("symbols", []))

    buf = max(REGRESSION_WINDOW, MOMENTUM_WINDOW + 2, OPTION_ATR_PERIOD + 2,
              CONFIRM_TICKS_SLOW + 5, RC["jump_atr_window"] + 2)

    with _state_lock:
        S["slots"] = {"CE": _make_opt_slot(), "PE": _make_opt_slot()}
        S.update({
            "running":            True,
            "trade_open":         False,
            "active_sides":       set(),   # multi_bot fires on_spike; NIFTY spike detection disabled
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
            "index_token":        idx_token,
            "index_name":         idx_name,
            "active_symbols":     active_symbols,
        })
        mode_str = "🔴 REAL TRADING" if S["trading_mode"] == "real" else "🔵 DEMO (paper)"
        jmp_str  = (f"AUTO ATR×{RC['jump_atr_multiplier']} [{RC['jump_min_pts']}–{RC['jump_max_pts']}pts]"
                    if RC["auto_jump"] else f"FIXED {RC['jump_pct']}% Nifty")

    _breakout_filter.reset()
    _vol_gate.reset()

    # Subscribe NIFTY + all watchlist stock tokens
    tokens_to_sub = [idx_token]
    multi_status = _multi_bot.get_status()
    for st in multi_status.values():
        for k in ("token", "ce_token", "pe_token"):
            t = st.get(k)
            if t:
                tokens_to_sub.append(int(t))
    _subscribe(tokens_to_sub)

    stock_count = len(multi_status)
    active_ct   = len(active_symbols) if active_symbols else stock_count
    log(f"Buy Robot v8.5 started — {active_ct}/{stock_count} stocks active  [{mode_str}]", "success")
    log(f"  Spike threshold: {jmp_str}  |  index ref={idx_name}", "info")
    if not stock_count:
        log("⚠ Watchlist is empty — add stocks via the search box", "warning")
    for sym, st in multi_status.items():
        active_tag = "" if (not active_symbols or sym in active_symbols) else " [INACTIVE]"
        log(f"  {sym}{active_tag}: CE={st.get('ce_symbol','?')}  PE={st.get('pe_symbol','?')}", "info")
    if S["trading_mode"] == "real":
        log("⚠ REAL MODE — check Zerodha for existing open positions!", "error")

    broadcast()


@socketio.on("stop_robot")
def on_stop():
    _close_active_trade(reason="manual")

    with _state_lock:
        tokens = [S["index_token"]]
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


@app.route("/")
def index():
    return send_file("buy_index.html")


@app.route("/settings")
def settings():
    return send_file("buy_settings.html")


@app.route("/screener")
def screener_page():
    return send_file("screener.html")


# ── Screener API ──────────────────────────────────────────────────────────────

import threading as _threading_mod

_screen_lock    = _threading_mod.Lock()
_screen_running = False


@app.route("/api/screen_stocks")
def api_screen_stocks():
    """
    Run the multi-strategy stock screener.
    Query params:
        top_n  — number of results (default 20, max 50)
        force  — "1" to bypass 30-min cache
    Returns JSON list of scored stocks.
    """
    global _screen_running
    top_n = min(int(request.args.get("top_n", 20)), 50)
    force = request.args.get("force", "0") == "1"

    with _screen_lock:
        if _screen_running:
            return jsonify({"error": "Screener already running"}), 429
        _screen_running = True

    try:
        results = _screener.screen(top_n=top_n, force=force)
        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        with _screen_lock:
            _screen_running = False


@app.route("/api/screen_cache_age")
def api_screen_cache_age():
    return jsonify({"age_secs": round(_screener.cache_age_secs(), 1)})


# ── Option resolver API ───────────────────────────────────────────────────────

@app.route("/api/resolve_options")
def api_resolve_options():
    """
    Find ATM CE/PE tokens for any NSE stock.
    Query params:
        symbol  — stock symbol, e.g. RELIANCE
        price   — current spot price
        expiry  — "nearest" (default), "monthly", "next"
    Returns {"CE": {...}, "PE": {...}, "lot_size": ..., "expiry": ...}
    """
    symbol = request.args.get("symbol", "").upper().strip()
    price  = float(request.args.get("price", 0) or 0)
    expiry = request.args.get("expiry", "nearest")

    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if price <= 0:
        return jsonify({"error": "price must be > 0"}), 400

    try:
        result = _resolver.resolve(symbol, price, expiry)
        if not result:
            return jsonify({"error": f"No options found for {symbol}"}), 404
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Multi-stock bot API (REST) ────────────────────────────────────────────────

@app.route("/api/tracked_stocks")
def api_tracked_stocks():
    """Get status of all tracked stocks in the multi-stock bot."""
    return jsonify(_multi_bot.get_status())


@app.route("/api/add_stock", methods=["POST"])
def api_add_stock():
    """
    Add a stock to the multi-stock bot.
    JSON body:
    {
      "symbol":     "RELIANCE",
      "token":      738561,
      "ce_token":   12345678,
      "pe_token":   12345679,
      "ce_symbol":  "RELIANCE25JUN1420CE",
      "pe_symbol":  "RELIANCE25JUN1420PE",
      "lot_size":   250,
      "active_sides": ["CE", "PE"]   // optional
    }
    """
    data = request.get_json(force=True) or {}

    required = ("symbol", "token", "lot_size")
    if any(k not in data for k in required):
        return jsonify({"error": f"Required fields: {required}"}), 400
    if not data.get("ce_token") and not data.get("pe_token"):
        return jsonify({"error": "At least one of ce_token / pe_token required"}), 400

    sides = set(data.get("active_sides") or ["CE", "PE"])
    tokens = _multi_bot.add_stock(
        symbol       = data["symbol"].upper(),
        token        = int(data["token"]),
        ce_token     = int(data["ce_token"]) if data.get("ce_token") else None,
        pe_token     = int(data["pe_token"]) if data.get("pe_token") else None,
        ce_symbol    = data.get("ce_symbol"),
        pe_symbol    = data.get("pe_symbol"),
        lot_size     = int(data["lot_size"]),
        active_sides = sides,
    )
    _subscribe(tokens)
    log(f"MultiBot: added {data['symbol']} — tokens={tokens}", "info")
    return jsonify({"ok": True, "subscribed_tokens": tokens})


@app.route("/api/remove_stock/<symbol>", methods=["DELETE"])
def api_remove_stock(symbol):
    """Remove a stock from the multi-stock bot."""
    tokens = _multi_bot.remove_stock(symbol.upper())
    _unsubscribe(tokens)
    log(f"MultiBot: removed {symbol} — unsubscribed {tokens}", "info")
    return jsonify({"ok": True, "unsubscribed_tokens": tokens})


@app.route("/api/clear_stocks", methods=["POST"])
def api_clear_stocks():
    """Remove all tracked stocks."""
    tokens = _multi_bot.clear()
    _unsubscribe(tokens)
    log(f"MultiBot: cleared all stocks — unsubscribed {len(tokens)} tokens", "info")
    return jsonify({"ok": True})


# ── Watchlist API ─────────────────────────────────────────────────────────────

@app.route("/api/search_fo_stocks")
def api_search_fo_stocks():
    """
    Search NSE F&O stocks by symbol/name.
    Sources from NSE equity instruments (EQ segment) filtered to those
    that also appear in NFO — so only tradeable F&O underlyings are returned.
    """
    q = request.args.get("q", "").upper().strip()
    if len(q) < 1:
        return jsonify([])
    try:
        # Always prepend matching index entries first (NIFTY, BANKNIFTY, SENSEX, …)
        index_matches = [e for e in _SEARCHABLE_INDICES if q in e["symbol"]]
        index_syms    = {e["symbol"] for e in index_matches}

        # Build F&O underlying set from NFO instruments
        nfo = _fetch_nfo_instruments()
        fo_names = {r.get("name", "").upper() for r in nfo if r.get("name")}

        # Fetch NSE equity instruments (EQ only) and filter to F&O underlyings
        nse = _fetch_nse_instruments()
        seen = set(index_syms)   # skip index symbols already in the prepend list
        prefix_results   = []
        contains_results = []
        for r in nse:
            seg  = r.get("segment", "")
            name = r.get("tradingsymbol", "").upper()
            if seg != "NSE" or not name:
                continue
            # Only include stocks that have F&O options
            if name not in fo_names and r.get("name", "").upper() not in fo_names:
                continue
            if name in seen:
                continue
            if q in name:
                seen.add(name)
                entry = {"symbol": name, "name": r.get("name", name)}
                if name.startswith(q):
                    prefix_results.append(entry)
                else:
                    contains_results.append(entry)
        results = (index_matches + prefix_results + contains_results)[:20]
        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/watchlist")
def api_get_watchlist():
    return jsonify(_load_watchlist())


@app.route("/api/option_chain/<symbol>")
def api_option_chain(symbol):
    """
    Return option chain data for a symbol: spot, expiry, nearby strikes (ATM ±3).
    Used by the UI to show the option chain panel before adding to watchlist.
    """
    symbol = symbol.upper()
    _exch  = "BSE" if symbol in _BSE_INDEX_SYMBOLS else "NSE"
    spot   = _fetch_ltp(symbol, _exch) or _fetch_ltp(symbol, "NSE") or _estimate_spot_from_strikes(symbol) or 0
    if spot <= 0:
        return jsonify({"error": f"Could not determine spot for {symbol}"}), 400

    result = _resolver.resolve(symbol, spot)
    if not result:
        return jsonify({"error": f"No F&O options found for {symbol}"}), 404

    # Resolve NSE/BSE token
    nse_token = _INDEX_TOKENS.get(symbol, 0)
    if not nse_token:
        try:
            nse_insts = _fetch_nse_instruments()
            row = next((r for r in nse_insts if r.get("tradingsymbol", "").upper() == symbol), None)
            if row:
                nse_token = int(row["instrument_token"])
        except Exception:
            pass

    return jsonify({**result, "nse_token": nse_token})


@app.route("/api/add_watchlist_stock", methods=["POST"])
def api_add_watchlist_stock():
    """
    Add a stock to watchlist + multi_bot.
    Mode 1 (direct tokens from option chain UI):
      {"symbol":"RELIANCE","nse_token":738561,"strike":1420,"expiry":"2025-06-26",
       "lot_size":250,"ce_token":123,"ce_symbol":"...CE","pe_token":456,"pe_symbol":"...PE","spot":1418}
    Mode 2 (auto-resolve):
      {"symbol": "RELIANCE"}  or  {"symbol": "RELIANCE", "spot_price": 1420.0}
    """
    data   = request.get_json(force=True) or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    # ── Mode 1: caller provides full token data ───────────────────────────────
    if data.get("nse_token") and data.get("strike"):
        nse_token  = int(data["nse_token"])
        strike     = float(data["strike"])
        expiry     = data.get("expiry", "")
        lot_size   = int(data.get("lot_size") or 1)
        ce_token   = int(data["ce_token"])   if data.get("ce_token")   else None
        pe_token   = int(data["pe_token"])   if data.get("pe_token")   else None
        ce_symbol  = data.get("ce_symbol")   or None
        pe_symbol  = data.get("pe_symbol")   or None
        spot       = float(data.get("spot") or 0)
        sides_req  = (data.get("sides") or "both").lower()   # "ce" | "pe" | "both"
        if sides_req == "ce":
            pe_token = pe_symbol = None
        elif sides_req == "pe":
            ce_token = ce_symbol = None
    # ── Mode 2: auto-resolve ──────────────────────────────────────────────────
    else:
        spot = float(data.get("spot_price") or data.get("price") or 0)
        if spot <= 0:
            _exch = "BSE" if symbol in _BSE_INDEX_SYMBOLS else "NSE"
            spot  = _fetch_ltp(symbol, _exch) or _fetch_ltp(symbol, "NSE") or 0
        if spot <= 0:
            spot = _estimate_spot_from_strikes(symbol) or 0
        if spot <= 0:
            return jsonify({"error": f"Could not determine spot price for {symbol}"}), 400

        result = _resolver.resolve(symbol, spot)
        if not result:
            return jsonify({"error": f"No F&O options found for {symbol}"}), 404

        ce_row    = result.get("CE") or {}
        pe_row    = result.get("PE") or {}
        strike    = result["strike"]
        expiry    = result["expiry"]
        lot_size  = result["lot_size"]
        ce_token  = int(ce_row["token"])  if ce_row.get("token")  else None
        pe_token  = int(pe_row["token"])  if pe_row.get("token")  else None
        ce_symbol = ce_row.get("symbol")
        pe_symbol = pe_row.get("symbol")

        nse_token = _INDEX_TOKENS.get(symbol, 0)
        if not nse_token:
            try:
                nse_insts = _fetch_nse_instruments()
                row = next((r for r in nse_insts if r.get("tradingsymbol", "").upper() == symbol), None)
                if row:
                    nse_token = int(row["instrument_token"])
            except Exception:
                pass

        if not nse_token:
            return jsonify({"error": f"NSE token not found for {symbol}"}), 404

    tokens = _multi_bot.add_stock(
        symbol    = symbol,
        token     = nse_token,
        ce_token  = ce_token,
        pe_token  = pe_token,
        ce_symbol = ce_symbol,
        pe_symbol = pe_symbol,
        lot_size  = lot_size,
    )
    _subscribe(tokens)

    # Persist
    wl = _load_watchlist()
    if symbol not in {w["symbol"] for w in wl}:
        wl.append({
            "symbol":    symbol,
            "token":     nse_token,
            "ce_token":  ce_token,
            "pe_token":  pe_token,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
            "lot_size":  lot_size,
            "expiry":    expiry,
            "strike":    strike,
            "spot":      spot,
        })
        _save_watchlist(wl)

    # Trigger an immediate score computation for the newly added stock (background)
    threading.Thread(target=_refresh_all_stock_scores, daemon=True, name=f"score-{symbol}").start()

    log(f"Watchlist: added {symbol} strike={strike} expiry={expiry}", "success")
    return jsonify({
        "ok":        True,
        "symbol":    symbol,
        "strike":    strike,
        "expiry":    expiry,
        "ce_symbol": ce_symbol,
        "pe_symbol": pe_symbol,
        "lot_size":  lot_size,
        "spot":      spot,
        "subscribed_tokens": tokens,
    })


@app.route("/api/remove_watchlist_stock/<symbol>", methods=["DELETE"])
def api_remove_watchlist_stock(symbol):
    symbol = symbol.upper()
    tokens = _multi_bot.remove_stock(symbol)
    _unsubscribe(tokens)
    wl = [w for w in _load_watchlist() if w["symbol"] != symbol]
    _save_watchlist(wl)
    log(f"Watchlist: removed {symbol}", "info")
    return jsonify({"ok": True})


@app.route("/api/stock_detail/<symbol>")
def api_stock_detail(symbol):
    """
    Return live tick history + trade history for a watchlist stock.
    Used by the stock detail chart modal.
    """
    symbol = symbol.upper()
    detail = _multi_bot.get_stock_detail(symbol)
    if detail is None:
        # Return empty structure so UI still opens
        detail = {"symbol": symbol, "spot_ticks": [], "ce_ticks": [], "pe_ticks": []}

    # Load trade history filtered to this symbol (or all if no symbol column)
    trades = []
    try:
        log_path = _os.path.join(_runtime_dir(), "trade_log.csv")
        with open(log_path, "r", newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                row_sym = row.get("symbol", "").upper()
                opt_sym = row.get("option_symbol", "").upper()
                if (not row_sym and not opt_sym) or row_sym == symbol or opt_sym.startswith(symbol):
                    trades.append(dict(row))
        trades.reverse()
        trades = trades[:50]   # last 50 trades
    except FileNotFoundError:
        pass

    # TF → Zerodha interval + days mapping
    _TF_MAP = {
        "minute":   1,   # 1m  → today only
        "3minute":  3,   # 3m  → 3 days
        "5minute":  5,   # 5m  → 5 days
        "15minute": 10,  # 15m → 10 days
        "30minute": 20,  # 30m → 20 days
        "60minute": 30,  # 1H  → 30 days
    }
    raw_interval = request.args.get("interval", "minute")
    ze_interval  = raw_interval if raw_interval in _TF_MAP else "minute"
    ze_days      = _TF_MAP[ze_interval]

    # Fetch historical OHLC candles at the requested interval
    spot_candles, ce_candles, pe_candles = [], [], []
    if detail:
        if detail.get("token"):
            spot_candles = _fetch_historical_candles(detail["token"], ze_interval, days=ze_days)
        if detail.get("ce_token"):
            ce_candles = _fetch_historical_candles(detail["ce_token"], ze_interval, days=ze_days)
        if detail.get("pe_token"):
            pe_candles = _fetch_historical_candles(detail["pe_token"], ze_interval, days=ze_days)

    return jsonify({
        **detail,
        "trades":       trades,
        "spot_candles": spot_candles,
        "ce_candles":   ce_candles,
        "pe_candles":   pe_candles,
    })


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


# ── Multi-stock SocketIO events ───────────────────────────────────────────────

@socketio.on("add_tracked_stock")
def on_add_tracked_stock(data):
    """
    Add a stock to the multi-stock bot.
    data keys: symbol, token, ce_token, pe_token, ce_symbol, pe_symbol, lot_size, active_sides
    """
    required = ("symbol", "token", "lot_size")
    if any(k not in data for k in required):
        emit("error", {"msg": f"add_tracked_stock: missing {required}"})
        return

    sides = set(data.get("active_sides") or ["CE", "PE"])
    tokens = _multi_bot.add_stock(
        symbol       = str(data["symbol"]).upper(),
        token        = int(data["token"]),
        ce_token     = int(data["ce_token"]) if data.get("ce_token") else None,
        pe_token     = int(data["pe_token"]) if data.get("pe_token") else None,
        ce_symbol    = data.get("ce_symbol"),
        pe_symbol    = data.get("pe_symbol"),
        lot_size     = int(data["lot_size"]),
        active_sides = sides,
    )
    _subscribe(tokens)
    log(f"MultiBot: added {data['symbol']} via WS — tokens={tokens}", "info")
    emit("multi_stock_status", _multi_bot.get_status())
    broadcast()


@socketio.on("remove_tracked_stock")
def on_remove_tracked_stock(data):
    symbol = data.get("symbol", "").upper()
    if not symbol:
        return
    tokens = _multi_bot.remove_stock(symbol)
    _unsubscribe(tokens)
    log(f"MultiBot: removed {symbol}", "info")
    emit("multi_stock_status", _multi_bot.get_status())
    broadcast()


@socketio.on("get_multi_stock_status")
def on_get_multi_stock_status():
    emit("multi_stock_status", _multi_bot.get_status())


@socketio.on("clear_tracked_stocks")
def on_clear_tracked_stocks():
    tokens = _multi_bot.clear()
    _unsubscribe(tokens)
    log("MultiBot: all tracked stocks cleared", "warning")
    emit("multi_stock_status", {})
    broadcast()


def _init_watchlist():
    """Restore persisted watchlist into multi_bot on startup and subscribe all tokens."""
    wl = _load_watchlist()

    # Always subscribe NIFTY so live price shows before robot is started
    _subscribe([NIFTY_TOKEN])

    if not wl:
        return
    for item in wl:
        sym = item.get("symbol", "")
        if not sym:
            continue
        try:
            tokens = _multi_bot.add_stock(
                symbol    = sym,
                token     = int(item.get("token", 0)),
                ce_token  = int(item["ce_token"]) if item.get("ce_token") else None,
                pe_token  = int(item["pe_token"]) if item.get("pe_token") else None,
                ce_symbol = item.get("ce_symbol"),
                pe_symbol = item.get("pe_symbol"),
                lot_size  = int(item.get("lot_size", 1)),
            )
            _subscribe(tokens)
        except Exception as exc:
            pass   # log after Flask starts

    # Kick off historical ATR computation immediately so spike thresholds are correct from the start
    threading.Thread(target=_refresh_all_stock_scores, daemon=True, name="init-atr-refresh").start()


if __name__ == "__main__":
    _init_watchlist()
    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)