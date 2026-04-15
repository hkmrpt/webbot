# ═══════════════════════════════════════════════════════════════
# SEMI-AUTO OPTION BOT  ──  config.py
# ═══════════════════════════════════════════════════════════════

# ── Trading Mode ─────────────────────────────────────────────
TRADING_MODE = "demo"          # "demo" | "real"

# ── Zerodha Connection ────────────────────────────────────────
ZERODHA_CONFIG = {
    "api_key":       "kitefront",
    "user_id":       "GIU182",
    "enctoken":      "wCYp7YWu4%2B9rjprMLxpvKbzTY1xxBLLB7vZSAMfGVjc1jHBpH76kpUXvH0aD9CmA4ktRgM8F4%2Fk0m5CrfgKQrShzb5Qr36Kh1lbRmf1hpkqYKLwmpIx%2Bew%3D%3D",
    "kf_session":    "RxKs57L99OglA1HaT9ELOkkWuo25xPIj",
    "public_token":  "OZKli4yNG5C5be5rneCwHrKEQ7ZtmBCm",
    "uid":           "1774325659463",
    "user_agent":    "kite3-web",
    "version":       "3.0.0",
    "websocket_url": "wss://ws.zerodha.com/",
}

SUBSCRIBE_INSTRUMENTS = [256265]   # NIFTY 50 index token

# ── Position Sizing ───────────────────────────────────────────
LOT_SIZE = 75                  # NIFTY lot size (current)

# ── Trading Hours ─────────────────────────────────────────────
TRADE_START_H  = 9
TRADE_START_M  = 15
TRADE_END_H    = 15
TRADE_END_M    = 20
FORCE_EXIT_H   = 15
FORCE_EXIT_M   = 25

# ── Trade Log ─────────────────────────────────────────────────
TRADE_LOG = "trade_log.csv"
