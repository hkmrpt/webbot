"""
Semi-Auto Option Bot — app.py
Flask + SocketIO server.
- Streams NIFTY ticks from Zerodha WebSocket
- Arms on user command; auto-enters when NIFTY crosses entry line
- Auto-exits when NIFTY crosses SL or TP line
- Lines set via drag-and-drop on chart or sidebar inputs

State machine: idle → armed → in_trade → idle
"""

import asyncio
import csv
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import config
from flask import Flask, send_file
from flask_socketio import SocketIO
import Zerodha_api
from zerodha_websocket import connect_zerodha_websocket

app = Flask(__name__, static_folder="static")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_lock = threading.Lock()

# ── Global State ───────────────────────────────────────────────────────────────
S = {
    "nifty_price":       0.0,
    "nifty_ticks":       deque(maxlen=12000),   # (timestamp_ms, price)
    "ws_connected":      False,
    "status":            "idle",    # idle | armed | in_trade
    "instrument":        "",        # e.g. NIFTY25APR2624000CE
    "side":              "",        # CE | PE
    "qty":               1,
    "entry_line":        0.0,       # NIFTY price to trigger entry
    "sl_line":           0.0,       # NIFTY price for stop loss
    "tp_line":           0.0,       # NIFTY price for take profit
    "order_id":          None,
    "exit_order_id":     None,
    "trade_entry_nifty": 0.0,
    "trade_entry_time":  None,
    "prev_nifty":        0.0,       # for edge-cross detection
    "session_pnl_pts":   0.0,
    "trade_count":       0,
    "wins":              0,
    "mode":              "demo",
    "logs":              deque(maxlen=300),
    "last_trade":        None,
}


# ── Logging ────────────────────────────────────────────────────────────────────
def _log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    with _lock:
        S["logs"].appendleft(entry)
    socketio.emit("log", entry)
    print(f"[{ts}] [{level.upper()}] {msg}")


# ── State payload (excludes large tick array) ──────────────────────────────────
def _build_payload() -> dict:
    with _lock:
        return {
            "nifty_price":       S["nifty_price"],
            "ws_connected":      S["ws_connected"],
            "status":            S["status"],
            "instrument":        S["instrument"],
            "side":              S["side"],
            "qty":               S["qty"],
            "entry_line":        S["entry_line"],
            "sl_line":           S["sl_line"],
            "tp_line":           S["tp_line"],
            "order_id":          S["order_id"],
            "exit_order_id":     S["exit_order_id"],
            "trade_entry_nifty": S["trade_entry_nifty"],
            "trade_entry_time":  S["trade_entry_time"],
            "session_pnl_pts":   round(S["session_pnl_pts"], 2),
            "trade_count":       S["trade_count"],
            "wins":              S["wins"],
            "mode":              S["mode"],
            "logs":              list(S["logs"]),
            "last_trade":        S["last_trade"],
        }


def broadcast():
    socketio.emit("state", _build_payload())


# ── Tick Processing ────────────────────────────────────────────────────────────
def _process_tick(price: float):
    ts_ms = int(time.time() * 1000)

    with _lock:
        S["nifty_ticks"].append((ts_ms, price))
        prev = S["prev_nifty"]
        S["nifty_price"] = price
        status = S["status"]
        entry_line = S["entry_line"]
        sl_line = S["sl_line"]
        tp_line = S["tp_line"]
        side = S["side"]

    # Emit individual tick to client (lightweight)
    socketio.emit("tick", [ts_ms, price])

    if prev == 0.0:
        with _lock:
            S["prev_nifty"] = price
        broadcast()
        return

    # ── Armed: watch for entry line cross ─────────────────────────────────────
    if status == "armed" and entry_line > 0:
        triggered = False
        if side == "CE" and prev < entry_line <= price:
            triggered = True
        elif side == "PE" and prev > entry_line >= price:
            triggered = True
        if triggered:
            _enter_trade()

    # ── In trade: watch for SL / TP cross ─────────────────────────────────────
    elif status == "in_trade":
        exit_reason = None
        if side == "CE":
            if sl_line > 0 and price <= sl_line:
                exit_reason = "SL Hit"
            elif tp_line > 0 and price >= tp_line:
                exit_reason = "TP Hit"
        else:  # PE
            if sl_line > 0 and price >= sl_line:
                exit_reason = "SL Hit"
            elif tp_line > 0 and price <= tp_line:
                exit_reason = "TP Hit"
        if exit_reason:
            _exit_trade(exit_reason, price)

    with _lock:
        S["prev_nifty"] = price

    broadcast()


# ── Trade Entry ────────────────────────────────────────────────────────────────
def _enter_trade():
    with _lock:
        instrument = S["instrument"]
        qty = S["qty"]
        mode = S["mode"]
        price = S["nifty_price"]
        S["status"] = "in_trade"
        S["trade_entry_nifty"] = price
        S["trade_entry_time"] = datetime.now().strftime("%H:%M:%S")

    if mode == "real":
        order_id, err = Zerodha_api.place_buy(instrument, qty)
        if err:
            _log(f"Buy order failed: {err}", "error")
            with _lock:
                S["status"] = "armed"
            return
        with _lock:
            S["order_id"] = order_id
    else:
        with _lock:
            S["order_id"] = f"DEMO_{uuid.uuid4().hex[:8].upper()}"

    with _lock:
        oid = S["order_id"]

    _log(f"ENTRY: {instrument} @ NIFTY {price:.2f} | order={oid}", "trade")
    socketio.emit("trade_opened", {
        "nifty_price": price,
        "order_id":    oid,
        "instrument":  instrument,
    })


# ── Trade Exit ─────────────────────────────────────────────────────────────────
def _exit_trade(reason: str, nifty_price: float):
    with _lock:
        instrument = S["instrument"]
        qty = S["qty"]
        mode = S["mode"]
        entry_nifty = S["trade_entry_nifty"]
        side = S["side"]
        entry_time = S["trade_entry_time"]
        S["status"] = "idle"

    pnl_pts = (nifty_price - entry_nifty) if side == "CE" else (entry_nifty - nifty_price)

    if mode == "real":
        exit_oid, err = Zerodha_api.place_sell(instrument, qty)
        if err:
            _log(f"Exit order failed: {err}", "error")
            with _lock:
                S["status"] = "in_trade"
            return
        with _lock:
            S["exit_order_id"] = exit_oid
    else:
        with _lock:
            S["exit_order_id"] = f"DEMO_{uuid.uuid4().hex[:8].upper()}"

    with _lock:
        exit_oid = S["exit_order_id"]
        S["trade_count"] += 1
        S["session_pnl_pts"] += pnl_pts
        if pnl_pts > 0:
            S["wins"] += 1
        last_trade = {
            "instrument":   instrument,
            "side":         side,
            "entry_nifty":  round(entry_nifty, 2),
            "exit_nifty":   round(nifty_price, 2),
            "pnl_pts":      round(pnl_pts, 2),
            "reason":       reason,
            "entry_time":   entry_time,
            "exit_time":    datetime.now().strftime("%H:%M:%S"),
        }
        S["last_trade"] = last_trade

    level = "trade" if pnl_pts > 0 else "warn"
    _log(f"EXIT ({reason}): NIFTY {nifty_price:.2f} | P&L: {pnl_pts:+.2f} pts | exit={exit_oid}", level)
    _write_trade_log(S["last_trade"])
    socketio.emit("trade_closed", S["last_trade"])


# ── Trade Log CSV ──────────────────────────────────────────────────────────────
def _write_trade_log(trade: dict):
    file_exists = os.path.exists(config.TRADE_LOG)
    try:
        with open(config.TRADE_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(trade.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(trade)
    except Exception as e:
        print(f"CSV write error: {e}")


# ── Force Exit at market close ─────────────────────────────────────────────────
def _force_exit_checker():
    while True:
        time.sleep(30)
        now = datetime.now()
        with _lock:
            status = S["status"]
            price = S["nifty_price"]
        if (status == "in_trade" and
                now.hour == config.FORCE_EXIT_H and
                now.minute >= config.FORCE_EXIT_M):
            _log("Force exit: market closing", "warn")
            _exit_trade("Force Exit", price)
            broadcast()


# ── WebSocket Thread ───────────────────────────────────────────────────────────
def _ws_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    send_q = asyncio.Queue()
    recv_q = asyncio.Queue()

    async def _connect_loop():
        while True:
            try:
                await connect_zerodha_websocket(send_q, recv_q)
            except Exception as e:
                print(f"[WS] Error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _process_loop():
        while True:
            msg = await recv_q.get()
            if isinstance(msg, dict):
                event = msg.get("__event__")
                if event == "connected":
                    with _lock:
                        S["ws_connected"] = True
                    await send_q.put({"a": "subscribe", "v": config.SUBSCRIBE_INSTRUMENTS})
                    await send_q.put({"a": "mode", "v": ["full", config.SUBSCRIBE_INSTRUMENTS]})
                    _log("WebSocket connected", "info")
                    broadcast()
                elif event == "disconnected":
                    with _lock:
                        S["ws_connected"] = False
                    _log("WebSocket disconnected — reconnecting…", "warn")
                    broadcast()
            elif isinstance(msg, list):
                for tick in msg:
                    token = tick.get("instrument_token")
                    price = tick.get("last_price")
                    if token in config.SUBSCRIBE_INSTRUMENTS and price:
                        _process_tick(price)

    loop.run_until_complete(asyncio.gather(_connect_loop(), _process_loop()))


# ── SocketIO Events ────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    # Send full tick history first, then state
    with _lock:
        ticks = list(S["nifty_ticks"])
    socketio.emit("ticks", ticks)
    socketio.emit("state", _build_payload())


@socketio.on("arm")
def on_arm(data):
    raw = (data.get("instrument") or "").strip().upper()
    if not raw:
        socketio.emit("log", {"ts": "", "msg": "Instrument is required", "level": "error"})
        return

    # Accept "SYMBOL" or "SYMBOL/TOKEN" formats
    instrument = raw.split("/")[0].strip()

    side = "CE" if instrument.endswith("CE") else "PE" if instrument.endswith("PE") else ""
    if not side:
        socketio.emit("log", {"ts": "", "msg": "Symbol must end in CE or PE", "level": "error"})
        return

    try:
        entry_line = float(data.get("entry_line") or 0)
        sl_line    = float(data.get("sl_line")    or 0)
        tp_line    = float(data.get("tp_line")    or 0)
        qty        = max(1, int(data.get("qty")   or 1))
    except (ValueError, TypeError) as e:
        socketio.emit("log", {"ts": "", "msg": f"Invalid values: {e}", "level": "error"})
        return

    with _lock:
        if S["status"] == "in_trade":
            return
        S.update({
            "instrument": instrument,
            "side":       side,
            "qty":        qty,
            "entry_line": entry_line,
            "sl_line":    sl_line,
            "tp_line":    tp_line,
            "status":     "armed",
            "order_id":   None,
            "exit_order_id": None,
        })

    _log(f"ARMED: {instrument}  Entry={entry_line}  SL={sl_line}  TP={tp_line}", "info")
    broadcast()


@socketio.on("disarm")
def on_disarm(data=None):
    with _lock:
        if S["status"] != "armed":
            return
        S["status"] = "idle"
    _log("Disarmed", "info")
    broadcast()


@socketio.on("manual_buy")
def on_manual_buy(data=None):
    with _lock:
        status = S["status"]
        instrument = S["instrument"]
    if status == "in_trade" or not instrument:
        return
    with _lock:
        S["status"] = "armed"  # ensure in armed state so _enter_trade works
    _enter_trade()
    broadcast()


@socketio.on("manual_exit")
def on_manual_exit(data=None):
    with _lock:
        status = S["status"]
        price = S["nifty_price"]
    if status != "in_trade":
        return
    _exit_trade("Manual Exit", price)
    broadcast()


@socketio.on("update_lines")
def on_update_lines(data):
    with _lock:
        if "entry_line" in data and data["entry_line"] is not None:
            S["entry_line"] = float(data["entry_line"])
        if "sl_line" in data and data["sl_line"] is not None:
            S["sl_line"]    = float(data["sl_line"])
        if "tp_line" in data and data["tp_line"] is not None:
            S["tp_line"]    = float(data["tp_line"])
    broadcast()


@socketio.on("set_mode")
def on_set_mode(data):
    mode = data.get("mode", "demo")
    with _lock:
        if S["status"] != "idle":
            socketio.emit("log", {"ts": "", "msg": "Cannot change mode while armed or in trade", "level": "warn"})
            return
        S["mode"] = mode
    _log(f"Mode set to {mode.upper()}", "info")
    broadcast()


@socketio.on("update_enctoken")
def on_update_enctoken(data):
    enctoken = (data.get("enctoken") or "").strip()
    if not enctoken:
        return
    config.ZERODHA_CONFIG["enctoken"] = enctoken
    try:
        with open("enctoken.dat", "w") as f:
            f.write(enctoken)
        _log("Enctoken updated (reconnect WebSocket to apply)", "info")
    except Exception as e:
        _log(f"Failed to save enctoken: {e}", "error")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")


# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Load saved enctoken
    if os.path.exists("enctoken.dat"):
        with open("enctoken.dat") as f:
            enc = f.read().strip()
            if enc:
                config.ZERODHA_CONFIG["enctoken"] = enc

    threading.Thread(target=_ws_thread, daemon=True).start()
    threading.Thread(target=_force_exit_checker, daemon=True).start()

    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)
