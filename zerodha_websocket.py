"""
zerodha_websocket.py
──────────────────────
Async WebSocket client for Zerodha.
• send_queue  : put {"a": "subscribe"|"unsubscribe", "v": [tokens]}
• recv_queue  : receives list of simplified tick dicts
              : or {"__event__": "connected"/"disconnected"}

Reconnection is handled by the caller (_ws_thread in buy_app.py).
This coroutine manages a single connection lifetime and raises on error.
"""

import asyncio
import json
from datetime import datetime

import websockets

from config import ZERODHA_CONFIG
from decoder import ZerodhaDataDecoder

_decoder = ZerodhaDataDecoder()


async def connect_zerodha_websocket(
    send_queue: asyncio.Queue,
    recv_queue: asyncio.Queue,
) -> None:
    params = {k: v for k, v in ZERODHA_CONFIG.items() if k != "websocket_url"}
    qs  = "&".join(f"{k}={v}" for k, v in params.items())
    uri = f"{ZERODHA_CONFIG['websocket_url']}?{qs}"

    _log("Connecting…")
    try:
        async with websockets.connect(uri, ping_interval=10, ping_timeout=300) as ws:
            _log("Connected ✓")
            await recv_queue.put({"__event__": "connected"})
            await asyncio.gather(_recv(ws, recv_queue), _send(ws, send_queue))
    finally:
        await recv_queue.put({"__event__": "disconnected"})


async def _recv(ws, recv_queue: asyncio.Queue) -> None:
    """Receive loop — raises on connection close so gather cancels _send."""
    while True:
        msg = await ws.recv()   # raises ConnectionClosed when connection ends
        if not msg:
            continue
        try:
            data = json.loads(msg)
            _log(f"JSON msg: {str(data)[:200]}")
            if isinstance(data, list):
                await recv_queue.put(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            ticks = _decoder.decode(msg)
            simplified = [
                {
                    "instrument_token": t["instrument_token"],
                    "last_price":       t["last_price"],
                }
                for t in ticks
                if "instrument_token" in t and "last_price" in t
            ]
            if simplified:
                tokens = [t["instrument_token"] for t in simplified]
                has_nifty = 256265 in tokens
                _log(f"BIN ticks={len(simplified)} tokens={tokens} NIFTY={'YES' if has_nifty else 'NO'}")
                await recv_queue.put(simplified)
            else:
                _log(f"BIN decode: 0 ticks from {len(msg)} bytes, raw_decoded={len(ticks)}")


async def _send(ws, send_queue: asyncio.Queue) -> None:
    """Send loop — puts message back on ConnectionClosed for resubscription after reconnect."""
    while True:
        msg = await send_queue.get()
        try:
            await ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            await send_queue.put(msg)   # preserve for resubscription after reconnect
            return


def _log(msg: str) -> None:
    print(f"[WS {datetime.now().strftime('%H:%M:%S')}] {msg}")