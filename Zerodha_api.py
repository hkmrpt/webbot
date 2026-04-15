"""
Zerodha_api.py
══════════════════════════════════════════════════════════════════
Handles real order placement via Zerodha Kite HTTP API.

  place_buy(tradingsymbol, quantity)  → BUY  MARKET MIS
  place_sell(tradingsymbol, quantity) → SELL MARKET MIS
  Both return (order_id: str | None, error: str | None)

enctoken rule (single source of truth):
  config.py stores ONE URL-encoded enctoken.
  ├── WebSocket  → uses it encoded, as-is
  └── HTTP API   → _raw_enctoken() calls unquote() before use
  Never store two copies.
"""

import http.client
import json
import logging
import urllib.parse

from config import LOT_SIZE, ZERODHA_CONFIG

logger = logging.getLogger(__name__)

KITE_HOST  = "kite.zerodha.com"
ORDER_PATH = "/oms/orders/regular"


def _raw_enctoken() -> str:
    return urllib.parse.unquote(ZERODHA_CONFIG["enctoken"])


def _make_headers(body: bytes) -> dict:
    enctoken = _raw_enctoken()
    cookie = (
        f"kf_session={ZERODHA_CONFIG['kf_session']}; "
        f"user_id={ZERODHA_CONFIG['user_id']}; "
        f"public_token={ZERODHA_CONFIG['public_token']}; "
        f"enctoken={enctoken}"
    )
    return {
        "Host":            KITE_HOST,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "Authorization":   f"enctoken {enctoken}",
        "Content-Type":    "application/x-www-form-urlencoded",
        "Content-Length":  str(len(body)),
        "Cookie":          cookie,
        "Origin":          "https://kite.zerodha.com",
        "Referer":         "https://kite.zerodha.com/orders",
        "User-Agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "x-kite-userid":  ZERODHA_CONFIG["user_id"],
        "x-kite-version": ZERODHA_CONFIG.get("version", "3.0.0"),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _post_order(payload: dict) -> tuple[str | None, str | None]:
    body    = urllib.parse.urlencode(payload).encode("utf-8")
    headers = _make_headers(body)
    try:
        conn = http.client.HTTPSConnection(KITE_HOST, timeout=10)
        conn.request("POST", ORDER_PATH, body=body, headers=headers)
        resp = conn.getresponse()
        raw  = resp.read().decode("utf-8")
        conn.close()
        logger.debug("Zerodha HTTP [%d]: %s", resp.status, raw)
        data = json.loads(raw)
        if data.get("status") == "success":
            oid = data["data"]["order_id"]
            logger.info("Order accepted — order_id=%s", oid)
            return oid, None
        err = data.get("message") or data.get("error_type") or raw
        logger.error("Order rejected: %s", err)
        return None, err
    except Exception as exc:
        logger.exception("HTTP order error: %s", exc)
        return None, str(exc)


def _order_payload(tradingsymbol: str, transaction_type: str, quantity: int) -> dict:
    return {
        "variety":            "regular",
        "exchange":           "NFO",
        "tradingsymbol":      tradingsymbol,
        "transaction_type":   transaction_type,
        "order_type":         "MARKET",
        "quantity":           quantity,
        "price":              0,
        "product":            "MIS",
        "validity":           "DAY",
        "disclosed_quantity": 0,
        "trigger_price":      0,
        "squareoff":          0,
        "stoploss":           0,
        "trailing_stoploss":  0,
        "user_id":            ZERODHA_CONFIG["user_id"],
    }


def place_buy(tradingsymbol: str, quantity: int | None = None) -> tuple[str | None, str | None]:
    """Place a real BUY MARKET MIS order on NFO."""
    qty = quantity if quantity is not None else LOT_SIZE
    payload = _order_payload(tradingsymbol, "BUY", qty)
    payload["tag"] = "buybot"
    logger.info("REAL BUY  %s  qty=%d", tradingsymbol, qty)
    return _post_order(payload)


def place_sell(tradingsymbol: str, quantity: int | None = None) -> tuple[str | None, str | None]:
    """Place a real SELL MARKET MIS order on NFO (square-off)."""
    qty = quantity if quantity is not None else LOT_SIZE
    logger.info("REAL SELL %s  qty=%d", tradingsymbol, qty)
    return _post_order(_order_payload(tradingsymbol, "SELL", qty))


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 3:
        print("Usage: python Zerodha_api.py <buy|sell> <tradingsymbol> [qty]")
        sys.exit(1)
    action = sys.argv[1].lower()
    symbol = sys.argv[2]
    qty    = int(sys.argv[3]) if len(sys.argv) > 3 else None
    fn     = place_buy if action == "buy" else place_sell
    oid, err = fn(symbol, qty)
    print(f"✅ order_id={oid}" if oid else f"❌ {err}")