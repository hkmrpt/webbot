# ═══════════════════════════════════════════════════════════════
# OPTION BUY ROBOT  v8  ──  config.py
# ═══════════════════════════════════════════════════════════════

# ── Trading Mode ─────────────────────────────────────────────
TRADING_MODE = "demo"          # "demo" | "real"

# ── Zerodha Connection ────────────────────────────────────────
ZERODHA_CONFIG = {
    "api_key":       "kitefront",
    "user_id":       "GIU182",
    "enctoken":      "2%2B6mrjlX42SB0FaWJws%2BmA3Cbreo38PUHR%2FeLYuWhdVIbGmxZH3GdjrG0kshgfJB9itLFNYKTjN4kxPXYq8VsfFVZbRDbo7LonBnYhlI4dL%2B9YRXLMopzg%3D%3D",
    "kf_session":    "RxKs57L99OglA1HaT9ELOkkWuo25xPIj",
    "public_token":  "OZKli4yNG5C5be5rneCwHrKEQ7ZtmBCm",
    "uid":           "1774325659463",
    "user_agent":    "kite3-web",
    "version":       "3.0.0",
    "websocket_url": "wss://ws.zerodha.com/",
}

SUBSCRIBE_INSTRUMENTS = [256265]   # NIFTY 50 index token

# ── Position Sizing ───────────────────────────────────────────
LOT_SIZE            = 65
CAPITAL             = 100_000.0
MAX_RISK_PER_TRADE  = 0.01

# ── AUTO Spike Detection (v8: ATR-based dynamic threshold) ────
# When AUTO_JUMP is True, JUMP_PCT is IGNORED.
# dynamic_jump_pts = Nifty tick ATR  × JUMP_ATR_MULTIPLIER
# Result is clamped to [JUMP_MIN_PTS, JUMP_MAX_PTS].
AUTO_JUMP           = True         # True = ATR-based | False = fixed JUMP_PCT
JUMP_ATR_WINDOW     = 30           # rolling tick window for Nifty ATR
JUMP_ATR_MULTIPLIER = 1.2          # ATR × this = spike threshold
JUMP_MIN_PTS        = 4.0          # minimum spike threshold (pts)
JUMP_MAX_PTS        = 20.0         # maximum spike threshold (pts)

# Legacy fixed threshold (used when AUTO_JUMP = False)
JUMP_PCT            = 0.02         # % of Nifty price

# ── Spike Confirmation ────────────────────────────────────────
CONFIRM_SUSTAIN_PCT = 0.70         # spike must hold this fraction of jump_pts

CONFIRM_ATR_HIGH    = 5.0
CONFIRM_ATR_LOW     = 2.0
CONFIRM_TICKS_FAST  = 2
CONFIRM_TICKS_MID   = 3
CONFIRM_TICKS_SLOW  = 4

MOMENTUM_WINDOW     = 5
MOMENTUM_MIN        = 4

# ── Stop Loss ─────────────────────────────────────────────────
BUY_SL_PCT          = 5.0          # initial SL (fallback)
SL_PHASE1_PCT       = 15.0         # wide SL for first N seconds
SL_PHASE1_SECS      = 30           # seconds before tightening
SL_PHASE2_PCT       = 8.0          # tightened SL after phase 1

# ── NIFTY Reversal Exit ───────────────────────────────────────
# If NIFTY drops back below entry price (CE) or rises above (PE),
# the spike has failed — exit the option immediately.
NIFTY_REVERSAL_EXIT = True

# ── Trail ─────────────────────────────────────────────────────
OPTION_ATR_PERIOD   = 10
TRAIL_ATR_HIGH      = 5.0
TRAIL_ATR_LOW       = 2.0
TRAIL_PCT_HIGH      = 35.0
TRAIL_PCT_LOW       = 15.0
BUY_TRAIL_PCT       = 25.0         # default trail %

# ── Profit-Tier Trail ─────────────────────────────────────────
# Proportional: small profit → tight trail (protect it)
#               large profit → wider trail (let it run)
PROFIT_TRAIL_THRESHOLD_PCT = 5.0

PROFIT_TIER_TRAIL = [
    (5.0,   4.0),   # profit ≥ 5%  → trail 4%  (locks ~1% profit)
    (10.0,  6.0),   # profit ≥ 10% → trail 6%  (locks ~4% profit)
    (20.0, 10.0),   # profit ≥ 20% → trail 10% (locks ~10% profit)
    (30.0, 15.0),   # profit ≥ 30% → trail 15% (locks ~15% profit)
]

# ── Profit-Lock SL tiers ──────────────────────────────────────
# Moves SL above entry to lock in profit at key milestones.
# Format: (peak_profit_pct_trigger, sl_lock_pct_above_entry)
PROFIT_LOCK_TIERS = [
    (10.0,  3.0),   # peak ≥ 10% → SL at entry + 3%
    (20.0,  8.0),   # peak ≥ 20% → SL at entry + 8%
    (30.0, 15.0),   # peak ≥ 30% → SL at entry + 15%
]

# ── Trail ratchet (never widen once tightened) ────────────────
# Disabled: proportional trail intentionally widens with profit growth
TRAIL_RATCHET_ENABLED = False

# ── Breakeven + Time-based trail ─────────────────────────────
BREAKEVEN_TRIGGER_PCT     = 3.0    # move SL to entry once peak profit hits this %
TRAIL_TIME_START_PCT      = 15.0   # trail % when time-trail first activates
TRAIL_TIME_TIGHTEN_SECS   = 60     # tighten trail every N seconds after activation
TRAIL_TIME_TIGHTEN_STEP   = 2.0    # tighten by this % each interval
TRAIL_TIME_MIN_PCT        = 5.0    # minimum trail floor

# ── Move Type Detection ───────────────────────────────────────
# Velocity = avg absolute pts/tick over last N option price ticks.
# Fast move = market spiking quickly  → wider trail, more time
# Slow move = market creeping slowly  → tight trail, exit sooner
MOVE_VELOCITY_WINDOW   = 5      # ticks to measure velocity
FAST_MOVE_VELOCITY     = 2.0    # avg pts/tick — above this = fast move

SLOW_MOVE_TRAIL_CAP    = 5.0    # trail capped here on slow moves (protect small gains)
FAST_MOVE_TRAIL_FLOOR  = 12.0   # trail floored here on fast moves (let it run)

SLOW_MOVE_TIMEOUT_SECS = 45     # exit sooner if slow — don't hold losers
SLOW_MOVE_MIN_PROFIT   = 1.0    # min profit % to stay in after slow timeout
FAST_MOVE_TIMEOUT_SECS = 120    # more time for fast moves
FAST_MOVE_MIN_PROFIT   = 3.0    # min profit % to stay in after fast timeout

# ── Timeout (legacy fallback) ─────────────────────────────────
TRADE_TIMEOUT_SECS        = 60     # seconds before timeout check
TRADE_TIMEOUT_MIN_PROFIT  = 1.0    # % — exit if profit below this after timeout

# ── Trade Controls ────────────────────────────────────────────
SL_COOLDOWN_SECS    = 180
BUY_QTY             = 1
MAX_TRADES_DAY      = 30
MAX_DAILY_LOSS      = 3000.0
DAILY_PROFIT_TARGET = 60000.0

# ── Regression / Momentum ─────────────────────────────────────
REGRESSION_WINDOW    = 20
REGRESSION_SLOPE_MIN = 0.3

OPTION_VOL_WINDOW   = 20
OPTION_VOL_FACTOR   = 1.5

# ── Trading Hours ─────────────────────────────────────────────
TRADE_START_H       = 9
TRADE_START_M       = 15
TRADE_END_H         = 15
TRADE_END_M         = 20

# ── Force exit before close ───────────────────────────────────
FORCE_EXIT_H        = 15
FORCE_EXIT_M        = 25

# ── Trade Log ─────────────────────────────────────────────────
TRADE_LOG           = "trade_log.csv"