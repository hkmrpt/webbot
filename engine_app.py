"""
engine_app.py  ──  Complete Trading Engine  v1.0
════════════════════════════════════════════════
Multi-strategy NIFTY option scalping engine.

Start:  python engine_app.py
Dashboard: http://localhost:5002

Architecture:
  Tick stream (zerodha_websocket) → MarketContext (all indicators)
      → 6 strategies run per tick → SignalHub aggregates
      → RiskManager sizes position → TradeManager manages exit

Thread-safety invariant (same as buy_app.py v8.2):
  _state_lock covers S / RC mutations ONLY.
  All I/O (socketio.emit, place_buy/sell, any network) happens
  AFTER the lock is fully released.
"""

import os as _os
import sys as _sys


def _runtime_dir() -> str:
    if getattr(_sys, "frozen", False):
        return _os.path.dirname(_os.path.abspath(_sys.executable))
    return _os.path.dirname(_os.path.abspath(__file__))


_ENCTOKEN_FILE = _os.path.join(_runtime_dir(), "enctoken.dat")

import asyncio
import threading
from collections import deque
from datetime import datetime, time as dtime, timedelta

from flask import Flask, send_file, jsonify
from flask_socketio import SocketIO, emit

from config import (
    CAPITAL, LOT_SIZE, BUY_QTY,
    TRADING_MODE, ZERODHA_CONFIG,
    TRADE_START_H, TRADE_START_M,
    TRADE_END_H,   TRADE_END_M,
    FORCE_EXIT_H,  FORCE_EXIT_M,
    AUTO_JUMP, JUMP_ATR_MULTIPLIER,
    JUMP_MIN_PTS, JUMP_MAX_PTS,
    CONFIRM_SUSTAIN_PCT,
    SL_PHASE1_PCT, SL_PHASE1_SECS, SL_PHASE2_PCT,
    SL_COOLDOWN_SECS, SL_COOLDOWN_MAX_SECS,
    MAX_TRADES_DAY, MAX_DAILY_LOSS, DAILY_PROFIT_TARGET,
    JUMP_ADAPTIVE_MIN, JUMP_ADAPTIVE_MAX,
)
from engine.market_context import MarketContext
from engine.strategies import build_strategies, Signal
from engine.signal_hub import SignalHub
from engine.risk_manager import RiskManager
from engine.trade_manager import TradeManager
from zerodha_websocket import connect_zerodha_websocket
from Zerodha_api import place_buy, place_sell

# ── Load enctoken ──────────────────────────────────────────────────────────────
def _load_enctoken():
    try:
        if _os.path.exists(_ENCTOKEN_FILE):
            t = open(_ENCTOKEN_FILE, "r", encoding="utf-8").read().strip()
            if t:
                ZERODHA_CONFIG["enctoken"] = t
    except Exception:
        pass


_load_enctoken()

NIFTY_TOKEN  = 256265
TRADE_START  = dtime(TRADE_START_H, TRADE_START_M)
TRADE_END    = dtime(TRADE_END_H,   TRADE_END_M)
FORCE_EXIT_T = dtime(FORCE_EXIT_H,  FORCE_EXIT_M)

app      = Flask(__name__)
app.config["SECRET_KEY"] = "engine_v1_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_state_lock = threading.Lock()
_log_lock   = threading.Lock()

# ── Hot-reloadable config ──────────────────────────────────────────────────────
RC = {
    "auto_jump":            AUTO_JUMP,
    "jump_atr_multiplier":  JUMP_ATR_MULTIPLIER,
    "jump_min_pts":         JUMP_MIN_PTS,
    "jump_max_pts":         JUMP_MAX_PTS,
    "jump_adaptive_min":    JUMP_ADAPTIVE_MIN,
    "jump_adaptive_max":    JUMP_ADAPTIVE_MAX,
    "confirm_sustain_pct":  CONFIRM_SUSTAIN_PCT,
    "sl_phase1_pct":        SL_PHASE1_PCT,
    "sl_phase1_secs":       SL_PHASE1_SECS,
    "sl_phase2_pct":        SL_PHASE2_PCT,
    "sl_cooldown_secs":     SL_COOLDOWN_SECS,
    "sl_cooldown_max_secs": SL_COOLDOWN_MAX_SECS,
    "max_trades_day":       MAX_TRADES_DAY,
    "max_daily_loss":       MAX_DAILY_LOSS,
    "daily_profit_target":  DAILY_PROFIT_TARGET,
    "trade_start_h":        TRADE_START_H,
    "trade_start_m":        TRADE_START_M,
    "trade_end_h":          TRADE_END_H,
    "trade_end_m":          TRADE_END_M,
    "force_exit_h":         FORCE_EXIT_H,
    "force_exit_m":         FORCE_EXIT_M,
    "risk_pct":             1.0,
    "min_confidence":       0.65,
    # Per-strategy enable flags
    "strat_spike":      True,
    "strat_orb":        True,
    "strat_vwap_bounce": True,
    "strat_vwap_band":  True,
    "strat_trend_pb":   True,
    "strat_rsi_burst":  True,
}

# ── Global engine objects (recreated on start) ─────────────────────────────────
_ctx        = MarketContext()
_strategies = build_strategies(RC)
_hub        = SignalHub(min_confidence=RC["min_confidence"])
_risk       = RiskManager(capital=CAPITAL, risk_pct=RC["risk_pct"],
                           lot_size=LOT_SIZE, max_trades=RC["max_trades_day"])


def _make_opt_slot():
    return {
        "token":         None,
        "symbol":        "",
        "strike":        "",
        "price":         None,
        "volume":        0,
        "price_history": deque(maxlen=30),
    }


S = {
    "running":              False,
    "trade_open":           False,
    "trading_mode":         TRADING_MODE,
    "active_sides":         set(),
    "slots":                {"CE": _make_opt_slot(), "PE": _make_opt_slot()},
    "trade_side":           None,

    "nifty_price":          None,
    "nifty_entry_price":    None,

    "session_pnl":          0.0,
    "trade_pnl":            0.0,
    "trades_today":         0,
    "wins":                 0,
    "losses":               0,
    "trading_date":         datetime.now().date(),
    "capital":              CAPITAL,

    "last_order_id":        None,
    "last_exit_order_id":   None,
    "last_skip_reason":     None,
    "last_signal":          None,     # last Signal that fired

    "cooldown_until":       None,
    "consecutive_sl_hits":  0,

    "active_status":        None,     # None | "open" | "closed_<reason>"
    "active_side":          None,
    "trade_open_time":      None,

    "trade_manager":        None,     # TradeManager instance

    "ws_connected":         False,
    "ws_send_queue":        None,
    "ws_loop":              None,
    "subscribed_tokens":    [],

    "logs":                 [],
}


# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "info"):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    with _log_lock:
        S["logs"].append(entry)
        if len(S["logs"]) > 300:
            S["logs"] = S["logs"][-300:]
    socketio.emit("log", entry, namespace="/")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _slot(side: str) -> dict:
    return S["slots"][side]


def _active_slot() -> dict | None:
    ts = S["trade_side"]
    return S["slots"][ts] if ts else None


def _is_valid_time() -> bool:
    now   = datetime.now().time()
    start = dtime(RC["trade_start_h"], RC["trade_start_m"])
    end   = dtime(RC["trade_end_h"],   RC["trade_end_m"])
    return start <= now <= end


def _in_cooldown() -> bool:
    if not S["cooldown_until"]:
        return False
    if datetime.now() < S["cooldown_until"]:
        rem = int((S["cooldown_until"] - datetime.now()).total_seconds())
        S["last_skip_reason"] = f"SL cooldown — {rem}s remaining"
        return True
    S["cooldown_until"] = None
    return False


def _adaptive_cooldown() -> int:
    hits = S["consecutive_sl_hits"]
    secs = RC["sl_cooldown_secs"] * (2 ** max(0, hits - 1))
    return int(min(secs, RC["sl_cooldown_max_secs"]))


# ── State payload ──────────────────────────────────────────────────────────────
def _build_state_payload() -> dict:
    """Build full dashboard snapshot. Call under _state_lock."""
    tm    = S["trade_manager"]
    snap  = tm.snapshot() if (tm and not tm.closed) else {}
    aslot = _active_slot()
    opt_price = aslot["price"] if aslot else None

    live_pnl = 0.0
    if snap and opt_price and snap.get("entry"):
        live_pnl = round((opt_price - snap["entry"]) * snap.get("qty", 1) * LOT_SIZE, 2)

    cdrem = 0
    if S["cooldown_until"]:
        cdrem = max(0, int((S["cooldown_until"] - datetime.now()).total_seconds()))

    ce = S["slots"]["CE"]
    pe = S["slots"]["PE"]

    sig = S.get("last_signal")

    return {
        # Bot state
        "running":            S["running"],
        "trade_open":         S["trade_open"],
        "trading_mode":       S["trading_mode"],
        "ws_connected":       S["ws_connected"],

        # NIFTY
        "nifty_price":        S["nifty_price"],
        "nifty_entry_price":  S["nifty_entry_price"],

        # Option slots
        "ce_token":   ce["token"],  "ce_strike": ce["strike"],
        "ce_price":   ce["price"],  "ce_volume": ce["volume"],
        "pe_token":   pe["token"],  "pe_strike": pe["strike"],
        "pe_price":   pe["price"],  "pe_volume": pe["volume"],

        # Active trade
        "active_sides":         list(S["active_sides"]),
        "opt_side":             S["trade_side"],
        "opt_strike":           aslot["strike"] if aslot else "",
        "opt_price":            opt_price,
        "active_status":        S["active_status"],
        "active_side":          S["active_side"],
        "live_pnl":             live_pnl,

        # Trade details from TradeManager
        "entry":                snap.get("entry"),
        "sl":                   snap.get("sl"),
        "sl_pct":               snap.get("sl_pct"),
        "target":               snap.get("target"),
        "trail_price":          snap.get("trail_price"),
        "trail_pct":            snap.get("trail_pct"),
        "peak_price":           snap.get("peak_price"),
        "peak_profit_pct":      snap.get("peak_profit_pct"),
        "profit_pct":           snap.get("profit_pct"),
        "held_secs":            snap.get("held_secs", 0),
        "phase2":               snap.get("phase2", False),
        "breakeven":            snap.get("breakeven", False),
        "velocity":             snap.get("velocity", 0.0),
        "trade_strategy":       snap.get("strategy"),

        # Session stats
        "session_pnl":          S["session_pnl"],
        "trade_pnl":            S["trade_pnl"],
        "trades_today":         S["trades_today"],
        "wins":                 S["wins"],
        "losses":               S["losses"],
        "capital":              S["capital"],
        "consecutive_sl_hits":  S["consecutive_sl_hits"],

        # Cooldown
        "cooldown_active":      cdrem > 0,
        "cooldown_remaining":   cdrem,
        "last_skip_reason":     S["last_skip_reason"],

        # Orders
        "last_order_id":        S["last_order_id"],
        "last_exit_order_id":   S["last_exit_order_id"],

        # Last signal
        "last_signal": {
            "strategy":   sig.strategy,
            "side":       sig.side,
            "confidence": sig.confidence,
            "reason":     sig.reason,
            "urgency":    sig.urgency,
        } if sig else None,

        # Market context
        "ctx": _ctx.snapshot(),

        # Strategy stats
        "strategy_stats": {
            s.name: {"enabled": s.enabled, **s.stats}
            for s in _strategies
        },

        # Signal history (last 10)
        "signal_history": _hub.last_signals_snapshot(10),

        # Config
        "trade_start":    f"{RC['trade_start_h']:02d}:{RC['trade_start_m']:02d}",
        "trade_end":      f"{RC['trade_end_h']:02d}:{RC['trade_end_m']:02d}",
        "force_exit_time": f"{RC['force_exit_h']:02d}:{RC['force_exit_m']:02d}",
        "risk_pct":        RC["risk_pct"],
        "min_confidence":  RC["min_confidence"],
    }


def broadcast():
    with _state_lock:
        payload = _build_state_payload()
    socketio.emit("state", payload, namespace="/")


# ── Order execution ────────────────────────────────────────────────────────────
def _place_order(action: str, symbol: str, qty: int) -> str | None:
    """Places real or demo order. Returns order_id or None."""
    if S["trading_mode"] != "real":
        log(f"  DEMO {action.upper()} {symbol} qty={qty}", "info")
        return None

    if not symbol:
        log("  No symbol configured — order skipped", "error")
        return None

    log(f"  REAL {action.upper()} {symbol} qty={qty}", "warning")
    if action == "buy":
        oid, err = place_buy(symbol, qty)
    else:
        oid, err = place_sell(symbol, qty)

    if oid:
        log(f"  Order confirmed — id={oid}", "success")
    else:
        log(f"  Order FAILED — {err}", "error")
    return oid


# ── Trade entry ────────────────────────────────────────────────────────────────
def _enter_trade(signal: Signal, nifty_price: float):
    """
    Called OUTSIDE _state_lock.
    Opens trade state, sends order, broadcasts.
    """
    side  = signal.side
    sl    = _slot(side)
    price = sl["price"]

    if price is None:
        log(f"  Skip entry: {side} price not available", "warning")
        return

    # Risk-based sizing
    lots, risk_inr = _risk.size_trade(price, signal.sl_pct)

    # Dynamic target from risk manager if signal target is 0
    tgt_pct = signal.target_pct if signal.target_pct > 0 else _risk.target_pct(
        signal.sl_pct, signal.confidence, _ctx.regime
    )

    tm = TradeManager(
        side=side, entry_price=price,
        qty_lots=lots, strategy_name=signal.strategy,
        sl_pct=signal.sl_pct, target_pct=tgt_pct,
        capital=S["capital"],
    )

    with _state_lock:
        S["trade_open"]       = True
        S["trade_side"]       = side
        S["active_side"]      = side
        S["active_status"]    = "open"
        S["trades_today"]    += 1
        S["trade_open_time"]  = datetime.now()
        S["nifty_entry_price"] = nifty_price
        S["trade_manager"]    = tm
        S["last_order_id"]    = None
        S["last_signal"]      = signal

    log(
        f"▲ ENTRY [{signal.strategy.upper()}] {side}  "
        f"price=₹{price:.2f}  lots={lots}  "
        f"SL={signal.sl_pct}%  target={tgt_pct:.1f}%  "
        f"conf={signal.confidence:.2f}  urgency={signal.urgency}",
        "trade",
    )
    log(f"  {signal.reason}", "info")
    log(f"  Risk=₹{risk_inr:.0f}  NIFTY@{nifty_price:.2f}", "info")

    # Place order
    oid = _place_order("buy", sl["symbol"], lots * LOT_SIZE)
    if oid:
        with _state_lock:
            S["last_order_id"] = oid

    opened = {
        "side":         side,
        "entry":        price,
        "sl_pct":       signal.sl_pct,
        "target_pct":   tgt_pct,
        "lots":         lots,
        "strategy":     signal.strategy,
        "confidence":   signal.confidence,
        "nifty":        nifty_price,
        "trading_mode": S["trading_mode"],
        "order_id":     oid,
    }
    socketio.emit("trade_opened", opened, namespace="/")
    broadcast()


# ── Trade exit ─────────────────────────────────────────────────────────────────
def _close_trade(result: dict, from_lock: bool = False):
    """
    Finalises trade state and sends exit order.
    If called from within _state_lock pass from_lock=True (won't re-acquire).
    Call _do_close_io() separately for I/O if from_lock is True.
    """
    side    = result["side"]
    pnl     = result["pnl"]
    reason  = result["reason"]
    sl_sym  = _slot(side)["symbol"]
    qty_lot = (result.get("qty") or 1) * LOT_SIZE

    def _mutate():
        S["trade_pnl"]        = pnl
        S["session_pnl"]      = round(S["session_pnl"] + pnl, 2)
        S["capital"]          = result.get("equity_after", S["capital"])
        S["active_status"]    = f"closed_{reason}"
        S["trade_open"]       = False
        S["trade_side"]       = None
        S["active_side"]      = None
        S["trade_open_time"]  = None
        S["nifty_entry_price"] = None
        _risk.update_capital(S["capital"])

        if pnl >= 0:
            S["wins"]                += 1
            S["consecutive_sl_hits"]  = 0
        else:
            S["losses"] += 1
            if reason == "sl":
                S["consecutive_sl_hits"] += 1
                cd = _adaptive_cooldown()
                S["cooldown_until"] = datetime.now() + timedelta(seconds=cd)

    if from_lock:
        _mutate()
    else:
        with _state_lock:
            _mutate()

    # Log
    color = "success" if pnl >= 0 else "error"
    tm    = S.get("trade_manager")
    strat = tm.strategy_name if tm else "?"
    log(
        f"{'▲' if pnl >= 0 else '▼'} EXIT [{strat}] {side}  "
        f"₹{result['entry']:.2f}→₹{result['exit_price']:.2f}  "
        f"P&L=₹{pnl:+.2f} ({result.get('pnl_pct',0):+.1f}%)  "
        f"peak={result.get('peak_profit_pct',0):+.1f}%  "
        f"held={result.get('held_secs',0)}s  [{reason}]",
        color,
    )
    if pnl < 0 and reason == "sl":
        hits = S["consecutive_sl_hits"]
        cd   = int((S["cooldown_until"] - datetime.now()).total_seconds()) if S["cooldown_until"] else 0
        log(f"  SL cooldown {cd}s  (hit #{hits})", "warning")

    # Notify strategy
    sig = S.get("last_signal")
    if sig:
        for s in _strategies:
            if s.name == sig.strategy:
                s.on_trade_closed(result)

    # Place exit order
    oid = _place_order("sell", sl_sym, qty_lot)
    if oid:
        with _state_lock:
            S["last_exit_order_id"] = oid
    result["exit_order_id"] = oid

    socketio.emit("trade_closed", {**result, "trading_mode": S["trading_mode"]}, namespace="/")
    broadcast()


# ── Tick processor ─────────────────────────────────────────────────────────────
def process_ticks(ticks: list):
    if not S["running"] and not S["trade_open"]:
        return

    for tick in ticks:
        token  = tick.get("instrument_token")
        price  = tick.get("last_price")
        volume = tick.get("volume_traded") or tick.get("volume") or 0
        if price is None:
            continue

        # Update option slot prices
        with _state_lock:
            for side in ("CE", "PE"):
                sl = _slot(side)
                if token == sl["token"]:
                    sl["price"]  = price
                    sl["volume"] = volume
                    sl["price_history"].append(price)

        # NIFTY tick
        if token != NIFTY_TOKEN:
            continue

        with _state_lock:
            S["nifty_price"] = price

            # Auto daily reset
            today = datetime.now().date()
            if today != S["trading_date"]:
                S["trading_date"]        = today
                S["session_pnl"]         = 0.0
                S["trades_today"]        = 0
                S["wins"]                = 0
                S["losses"]              = 0
                S["trade_pnl"]           = 0.0
                S["cooldown_until"]      = None
                S["consecutive_sl_hits"] = 0
                S["last_skip_reason"]    = None
                with _log_lock:
                    S["logs"] = []
                _ctx.session_reset()
                socketio.emit("daily_reset",
                              {"date": today.strftime("%d %b %Y")}, namespace="/")

        # Update market context (indicators)
        _ctx.on_tick(price, volume)

        # Force exit check
        if S["trade_open"]:
            now_t = datetime.now().time()
            force_t = dtime(RC["force_exit_h"], RC["force_exit_m"])
            if now_t >= force_t:
                tm = S.get("trade_manager")
                aslot = _active_slot()
                if tm and aslot and not tm.closed:
                    opt_p = aslot["price"] or 0.0
                    result = tm.force_close(opt_p, "force_exit")
                    _close_trade(result)
                    continue

        # NIFTY reversal exit — with ATR buffer so normal scalp oscillations
        # don't trigger it. Only fires when NIFTY moves meaningfully past entry.
        if S["trade_open"]:
            tm    = S.get("trade_manager")
            aslot = _active_slot()
            if tm and aslot and not tm.closed and S["nifty_entry_price"]:
                side_  = S["trade_side"]
                nifty_ = S["nifty_price"]
                ref_   = S["nifty_entry_price"]
                # Buffer = max(3pts, 0.5× short ATR) — absorbs normal noise
                buf    = max(3.0, (_ctx.spike_atr or 3.0) * 0.5)
                rev    = (
                    (side_ == "CE" and nifty_ < ref_ - buf) or
                    (side_ == "PE" and nifty_ > ref_ + buf)
                )
                if rev:
                    opt_p  = aslot["price"] or 0.0
                    result = tm.force_close(opt_p, "nifty_reversal")
                    _close_trade(result)
                    continue

        # Per-tick exit check for open trade
        if S["trade_open"]:
            tm    = S.get("trade_manager")
            aslot = _active_slot()
            if tm and aslot and not tm.closed:
                opt_p   = aslot["price"]
                opt_atr = None
                hist    = list(aslot["price_history"])
                if len(hist) >= 2:
                    trs = [abs(hist[i] - hist[i-1]) for i in range(1, len(hist))]
                    opt_atr = sum(trs) / len(trs)

                elapsed = (datetime.now() - S["trade_open_time"]).total_seconds() if S["trade_open_time"] else 0
                result  = tm.on_price(opt_p, opt_atr=opt_atr, elapsed=elapsed) if opt_p else None
                if result:
                    _close_trade(result)
                    continue

        # Entry signal processing (only if not in trade)
        if not S["trade_open"] and S["running"]:
            if not _is_valid_time():
                continue
            if _in_cooldown():
                continue
            ok, reason = _risk.can_trade(S["session_pnl"], S["trades_today"])
            if not ok:
                S["last_skip_reason"] = reason
                S["running"] = False
                log(f"  Stopped: {reason}", "warning")
                continue

            # Gather signals from all strategies
            state_snap = {
                "regime":      _ctx.regime,
                "nifty_price": price,
                "trade_open":  S["trade_open"],
            }
            signals = []
            for strat in _strategies:
                if strat.enabled and RC.get(f"strat_{strat.name}", True):
                    try:
                        sig = strat.on_tick(price, _ctx, state_snap)
                        signals.append(sig)
                    except Exception as e:
                        signals.append(None)

            best = _hub.process(signals, S["active_sides"])
            if best:
                _enter_trade(best, price)

    # Periodic broadcast (every NIFTY tick that went through)
    broadcast()


# ── WebSocket thread ───────────────────────────────────────────────────────────
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
                ev = msg["__event__"]
                connected = (ev == "connected")
                with _state_lock:
                    S["ws_connected"] = connected
                socketio.emit("ws_status", {"connected": connected}, namespace="/")
                log(f"WebSocket {'connected ✔' if connected else 'disconnected ✖'}",
                    "success" if connected else "error")
                if connected and S["subscribed_tokens"]:
                    await send_q.put({"a": "subscribe", "v": S["subscribed_tokens"]})
            elif isinstance(msg, list):
                process_ticks(msg)

    async def _main():
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

    loop.run_until_complete(_main())


def _subscribe(tokens: list):
    with _state_lock:
        existing = set(S["subscribed_tokens"])
        existing.update(tokens)
        S["subscribed_tokens"] = list(existing)
    if S["ws_send_queue"] and S["ws_loop"]:
        asyncio.run_coroutine_threadsafe(
            S["ws_send_queue"].put({"a": "subscribe", "v": tokens}),
            S["ws_loop"],
        )


def _unsubscribe(tokens: list):
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


# ── Manual close helper ────────────────────────────────────────────────────────
def _do_manual_close(reason: str = "manual") -> bool:
    if S["active_status"] != "open":
        return False
    aslot = _active_slot()
    tm    = S.get("trade_manager")
    if not tm or tm.closed:
        return False
    price  = aslot["price"] if aslot else 0.0
    result = tm.force_close(price or 0.0, reason)
    _close_trade(result)
    return True


# ── SocketIO events ────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    with _state_lock:
        payload = _build_state_payload()
    emit("state", payload)


@socketio.on("start_engine")
def on_start(data):
    global _strategies, _hub, _risk, _ctx

    ce_raw   = data.get("ce_input", "").strip()
    pe_raw   = data.get("pe_input", "").strip()
    trade_ce = bool(data.get("trade_ce", False))
    trade_pe = bool(data.get("trade_pe", False))

    if not trade_ce and not trade_pe:
        emit("error", {"msg": "Select at least CE or PE"})
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

    active = set()
    ce_slot = _make_opt_slot()
    pe_slot = _make_opt_slot()

    if trade_ce:
        tok = _tok(ce_raw)
        if not tok:
            emit("error", {"msg": "CE token invalid"})
            return
        ce_slot["token"]  = tok
        ce_slot["symbol"] = _sym(ce_raw)
        ce_slot["strike"] = _label(ce_raw)
        active.add("CE")

    if trade_pe:
        tok = _tok(pe_raw)
        if not tok:
            emit("error", {"msg": "PE token invalid"})
            return
        pe_slot["token"]  = tok
        pe_slot["symbol"] = _sym(pe_raw)
        pe_slot["strike"] = _label(pe_raw)
        active.add("PE")

    # Rebuild engine components fresh
    _ctx        = MarketContext()
    _strategies = build_strategies(RC)
    _hub        = SignalHub(min_confidence=RC["min_confidence"])
    _risk       = RiskManager(
        capital=S["capital"], risk_pct=RC["risk_pct"],
        lot_size=LOT_SIZE, max_trades=RC["max_trades_day"],
    )

    with _state_lock:
        S["slots"]               = {"CE": ce_slot, "PE": pe_slot}
        S["running"]             = True
        S["trade_open"]          = False
        S["active_sides"]        = active
        S["trade_side"]          = None
        S["nifty_price"]         = None
        S["nifty_entry_price"]   = None
        S["session_pnl"]         = 0.0
        S["trade_pnl"]           = 0.0
        S["trades_today"]        = 0
        S["wins"]                = 0
        S["losses"]              = 0
        S["cooldown_until"]      = None
        S["consecutive_sl_hits"] = 0
        S["last_skip_reason"]    = None
        S["last_signal"]         = None
        S["active_status"]       = None
        S["active_side"]         = None
        S["trade_open_time"]     = None
        S["trade_manager"]       = None
        S["last_order_id"]       = None
        S["last_exit_order_id"]  = None

    tokens = [NIFTY_TOKEN]
    if "CE" in active: tokens.append(ce_slot["token"])
    if "PE" in active: tokens.append(pe_slot["token"])
    _subscribe(tokens)

    sides_str = " + ".join(sorted(active))
    mode_str  = "REAL" if S["trading_mode"] == "real" else "DEMO"
    log(f"Engine v1.0 started — {sides_str}  [{mode_str}]  risk={RC['risk_pct']}%", "success")
    log(f"  Strategies: {', '.join(s.name for s in _strategies)}", "info")
    log(f"  Min confidence: {RC['min_confidence']}", "info")
    if S["trading_mode"] == "real":
        log("⚠ REAL MODE — live orders active!", "error")

    broadcast()


@socketio.on("stop_engine")
def on_stop():
    _do_manual_close("manual")
    tokens = [NIFTY_TOKEN]
    with _state_lock:
        for side in ("CE", "PE"):
            t = S["slots"][side]["token"]
            if t:
                tokens.append(t)
        S["running"]      = False
        S["trade_open"]   = False
        S["active_sides"] = set()
        S["trade_side"]   = None
        S["slots"]        = {"CE": _make_opt_slot(), "PE": _make_opt_slot()}
    _unsubscribe(tokens)
    log("Engine stopped.", "warning")
    broadcast()


@socketio.on("manual_exit")
def on_manual_exit():
    if not _do_manual_close("manual"):
        emit("error", {"msg": "No open trade"})


@socketio.on("set_trading_mode")
def on_set_mode(data):
    mode = data.get("mode", "demo")
    if mode not in ("demo", "real"):
        emit("error", {"msg": "Invalid mode"})
        return
    with _state_lock:
        if S["trade_open"]:
            emit("error", {"msg": "Cannot switch mode while trade is open"})
            return
        S["trading_mode"] = mode
    log(f"{'REAL' if mode == 'real' else 'DEMO'} mode active", "error" if mode == "real" else "success")
    broadcast()


@socketio.on("update_rc")
def on_update_rc(data):
    """Hot-update RC params at runtime. Also updates strategy enable flags."""
    updated = []
    with _state_lock:
        for k, v in data.items():
            if k in RC:
                try:
                    if isinstance(RC[k], bool):
                        RC[k] = bool(v)
                    elif isinstance(RC[k], float):
                        RC[k] = float(v)
                    elif isinstance(RC[k], int):
                        RC[k] = int(v)
                    else:
                        RC[k] = v
                    updated.append(k)
                except Exception:
                    pass
    if updated:
        log(f"RC updated: {', '.join(updated)}", "success")
    emit("rc_updated", {"keys": updated})
    broadcast()


@socketio.on("update_enctoken")
def on_update_enctoken(data):
    token = data.get("enctoken", "").strip()
    if not token:
        emit("error", {"msg": "Empty enctoken"})
        return
    try:
        with open(_ENCTOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
        ZERODHA_CONFIG["enctoken"] = token
        log("Enctoken updated", "success")
    except Exception as e:
        log(f"Enctoken write failed: {e}", "error")


@socketio.on("get_state")
def on_get_state():
    broadcast()


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("engine_dashboard.html")


@app.route("/api/rc")
def api_rc():
    return jsonify(RC)


@app.route("/api/signals")
def api_signals():
    return jsonify(_hub.history[-50:])


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    log("Engine v1.0 starting on http://0.0.0.0:5002", "success")
    socketio.run(app, host="0.0.0.0", port=5002, debug=False,
                 allow_unsafe_werkzeug=True)
