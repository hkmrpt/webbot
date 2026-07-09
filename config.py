# ═══════════════════════════════════════════════════════════════
# OPTION BUY ROBOT  v8.5  ──  config.py
# ═══════════════════════════════════════════════════════════════

# ── Trading Mode ─────────────────────────────────────────────
TRADING_MODE = "demo"          # "demo" | "real"

# ── Zerodha Connection ────────────────────────────────────────
ZERODHA_CONFIG = {
    "api_key":       "kitefront",
    "user_id":       "GIU182",
    "enctoken":      "TBKikBEtNyq981tzCtY2XdWOcXJ5FURnKefyVGvefMUlQXtasWfDuUeUxmzZzmhIIl3DJaerLbqewoU%2F6qGzo41cCOo0B%2F1KgO5PrukbW3HizJ8Xpr%2BgqA%3D%3D",
    "kf_session":    "RxKs57L99OglA1HaT9ELOkkWuo25xPIj",
    "public_token":  "OZKli4yNG5C5be5rneCwHrKEQ7ZtmBCm",
    "uid":           "1774325659463",
    "user_agent":    "kite3-web",
    "version":       "3.0.0",
    "websocket_url": "wss://ws.zerodha.com/",
    # Optional — needed for Zerodha watchlist API (/api/marketwatch)
    "csrf_token":    "",
    "app_uuid":      "",
}

SUBSCRIBE_INSTRUMENTS = [256265]   # NIFTY 50 index token

# ── Index Tokens ──────────────────────────────────────────────
BANKNIFTY_TOKEN = 260105           # BANKNIFTY index token

# ── Position Sizing ───────────────────────────────────────────
LOT_SIZE            = 65
CAPITAL             = 100_000.0
MAX_RISK_PER_TRADE  = 0.02         # risk 2% of capital per trade (ATR-based sizing)

# Max-capital position sizing
# Bot buys as many lots as capital allows: lots = floor(capital / (option_price × LOT_SIZE))
# MAX_LOTS_PER_TRADE = 0  → no cap (pure max-capital)
# MAX_LOTS_PER_TRADE = N  → hard ceiling at N lots (safety guard)
MAX_LOTS_PER_TRADE  = 0            # 0 = use all available capital

# ── AUTO Spike Detection (v8: ATR-based dynamic threshold) ────
# When AUTO_JUMP is True, JUMP_PCT is IGNORED.
# dynamic_jump_pts = Nifty tick ATR  × JUMP_ATR_MULTIPLIER
# Result is clamped to [JUMP_MIN_PTS, JUMP_MAX_PTS].
AUTO_JUMP           = True         # True = ATR-based | False = fixed JUMP_PCT
JUMP_ATR_WINDOW     = 30           # rolling tick window for live ATR (fallback only)
JUMP_ATR_MULTIPLIER = 1.2          # ATR × this = spike threshold (live fallback)
JUMP_MIN_PTS        = 2.0          # absolute minimum spike threshold (pts)
JUMP_MAX_PTS        = 20.0         # max spike threshold (tick-to-tick moves, not daily range)

# ── Historical ATR-based spike threshold (primary method) ─────
# Uses last HIST_ATR_DAYS daily candles to measure typical intraday range.
# Spike threshold = avg_daily_range × HIST_ATR_SPIKE_FRACTION
# e.g. RELIANCE avg daily range = 40pts  →  threshold = 40 × 0.18 = 7.2pts
#      NIFTY    avg daily range = 200pts →  threshold = 200 × 0.18 = 36pts
HIST_ATR_DAYS           = 7        # days of history to measure daily range
HIST_ATR_SPIKE_FRACTION = 0.18     # fraction of daily range = spike threshold

# ── Spike speed (time-based validation) ──────────────────────
# A real spike must be fast — big move in a short time.
# Speed  = pts moved / seconds elapsed (pts/sec)
# Window = how many seconds back to measure the spike move
# A move that takes longer than SPIKE_MAX_SECS is treated as a trend, not spike.
SPIKE_LOOKBACK_SECS     = 10       # window (seconds) to measure cumulative move
SPIKE_MAX_SECS          = 20       # move taking longer than this = trend, not spike
SPIKE_MIN_SPEED_PCT_SEC = 0.012    # min speed as % of price per second
                                   # e.g. NIFTY@22000 → 0.012% = 2.6 pts/sec min
                                   #      RELIANCE@1500 → 0.012% = 0.18 pts/sec min

# Legacy fixed threshold (used when AUTO_JUMP = False)
JUMP_PCT            = 0.02         # % of Nifty price

# ── Trend Detection (slow uptrend / downtrend) ────────────────
# Detects sustained directional movement over multiple ticks.
# Works alongside spike detection — three modes total:
#   1. Sudden spike  — single tick > ATR threshold
#   2. Slow uptrend  — consistent up-ticks → buy CE
#   3. Downtrend     — consistent down-ticks → buy PE
TREND_ENABLED           = True    # enable slow-trend entries
TREND_WINDOW            = 12      # ticks to evaluate (last N ticks)
TREND_MIN_MOVE_PCT      = 0.25    # min cumulative move % to qualify
TREND_CONSISTENCY_PCT   = 0.70    # % of ticks that must go in same dir
TREND_COOLDOWN_TICKS    = 25      # ticks to wait before re-firing trend

# ── Spike Confirmation ────────────────────────────────────────
CONFIRM_SUSTAIN_PCT = 0.80         # spike must hold 80% — filters noise without over-filtering

CONFIRM_ATR_HIGH    = 5.0
CONFIRM_ATR_LOW     = 2.0
CONFIRM_TICKS_FAST  = 1
CONFIRM_TICKS_MID   = 2
CONFIRM_TICKS_SLOW  = 3

MOMENTUM_WINDOW     = 20           # was 5 — max score was 5, min was 15 (impossible)
MOMENTUM_MIN        = 14           # 14/20 ticks directional = very strong momentum

# ── Stop Loss ─────────────────────────────────────────────────
BUY_SL_PCT          = 5.0          # initial SL (fallback)
SL_PHASE1_PCT       = 10.0         # initial SL — generous enough for entry noise
SL_PHASE1_SECS      = 20           # tighten faster — 20s is enough for the move to show
SL_PHASE2_PCT       = 4.0          # tight phase-2 — protect capital aggressively

# ── NIFTY Reversal Exit ───────────────────────────────────────
NIFTY_REVERSAL_EXIT = True
# Reversal must exceed this fraction of the spike threshold before triggering.
# e.g. spike=36pts, buffer=0.35 → NIFTY must move back 12.6pts before exit.
# Prevents hair-trigger exits on 1-tick noise bounces.
NIFTY_REVERSAL_BUFFER_PCT = 0.55   # fraction of jump_threshold — less hair-trigger
# Don't exit on reversal if option is already this far in profit (trust the trail).
NIFTY_REVERSAL_PROFIT_SKIP_PCT = 2.0  # skip reversal exit once trade is 2%+ in profit

# ── Trail ─────────────────────────────────────────────────────
OPTION_ATR_PERIOD   = 10
TRAIL_ATR_HIGH      = 5.0
TRAIL_ATR_LOW       = 2.0
TRAIL_PCT_HIGH      = 18.0         # cap trail — don't let it get too wide
TRAIL_PCT_LOW       = 5.0          # tight floor — protect gains faster
BUY_TRAIL_PCT       = 8.0          # tighter default trail

# ── Profit-Tier Trail ─────────────────────────────────────────
PROFIT_TRAIL_THRESHOLD_PCT = 2.0   # activate profit-tier trail earlier

# Monotonic non-decreasing: small profit = tightest trail, large profit = widest.
PROFIT_TIER_TRAIL = [
    (2.0,   3.0),   # peak ≥ 2%  → trail 3.0% — lock small gains early
    (4.0,   3.5),   # peak ≥ 4%  → trail 3.5% — a bit more room as profit grows
    (8.0,   4.0),   # peak ≥ 8%  → trail 4.0% — give room for continuation
    (15.0,  5.5),   # peak ≥ 15% → trail 5.5% — wide trail for runners
    (25.0,  7.0),   # peak ≥ 25% → trail 7.0% — very wide for big moves
    (40.0, 10.0),   # peak ≥ 40% → trail 10%  — max room for monster moves
]

# ── Profit-Lock SL tiers ──────────────────────────────────────
PROFIT_LOCK_TIERS = [
    (5.0,   1.0),   # peak ≥ 5%  → SL at entry + 1% (guaranteed small win)
    (10.0,  4.0),   # peak ≥ 10% → SL at entry + 4%
    (20.0, 10.0),   # peak ≥ 20% → SL at entry + 10%
    (35.0, 20.0),   # peak ≥ 35% → SL at entry + 20%
]

# ── Trail ratchet ─────────────────────────────────────────────
TRAIL_RATCHET_ENABLED = True          # was False — ratchet locks in tighter trail as profit grows

# ── Breakeven ─────────────────────────────────────────────────
BREAKEVEN_TRIGGER_PCT     = 2.0    # move SL to breakeven earlier — protect capital

# ── Time-based trail tightening ───────────────────────────────
TRAIL_TIME_START_PCT      = 10.0   # was 15.0
TRAIL_TIME_TIGHTEN_SECS   = 45     # was 20 — tighten every 45s, not 20s
TRAIL_TIME_TIGHTEN_STEP   = 1.0    # was 1.5 — gentler tightening per interval
TRAIL_TIME_MIN_PCT        = 4.0    # was 3.0 — floor raised to avoid spread stops

# ── Move Type Detection (display-only chip on the dashboard) ──
MOVE_VELOCITY_WINDOW   = 5         # ticks to measure velocity
FAST_MOVE_VELOCITY     = 2.0       # avg pts/tick — above = fast move

# ── Timeout ───────────────────────────────────────────────────
TRADE_TIMEOUT_SECS        = 50     # general timeout reduced
TRADE_TIMEOUT_MIN_PROFIT  = 0.5    # exit if barely profitable after timeout

# ── Consolidation Range Detection ─────────────────────────────
RANGE_WINDOW     = 20              # ticks for range detection
RANGE_MIN_TICKS  = 10              # minimum ticks before range is valid
RANGE_MAX_POINTS = 50.0            # max range width in points

# ── Trade Controls ────────────────────────────────────────────
SL_COOLDOWN_SECS    = 300          # 5-min cooldown after SL hit
BUY_QTY             = 1
MAX_TRADES_DAY      = 15           # allow more trades when signals are good
MAX_DAILY_LOSS      = 300.0        # 3% of ₹10k — one SL at 12% on ₹20 option = ₹156
DAILY_PROFIT_TARGET = 0            # 0 = no daily profit cap (run all day)
DAILY_PROFIT_PCT    = 0.0          # 0 = disabled — no daily profit limit

# ── Regression / Momentum ─────────────────────────────────────
REGRESSION_WINDOW    = 20
REGRESSION_SLOPE_MIN = 0.2         # was 0.5 — spikes happen in sideways markets too

OPTION_VOL_WINDOW   = 20
OPTION_VOL_FACTOR   = 1.2

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

# ── Tick Recording (v9 redesign — backtest data foundation) ───
TICK_RECORDING_ENABLED = True      # record every WS tick to ticks/ticks_YYYYMMDD.jsonl.gz
TICK_DIR               = "ticks"

# ── Expiry Day (NSE moved NIFTY weekly expiry Thursday → Tuesday in 2025)
NIFTY_EXPIRY_WEEKDAY   = 1         # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri

# ── Standing profit-target order (real mode) ─────────────────
# After a real BUY fills, a standing LIMIT SELL is placed at
# entry × (1 + pct/100). Scalp mode uses SCALP_TARGET_PCT instead.
TARGET_LIMIT_PCT       = 2.5

# ── Advanced Entry Filters (v8.5) ─────────────────────────────
# Breakout filter — spike must clear the prior consolidation range
BREAKOUT_FILTER_ENABLED = True
BREAKOUT_RANGE_WINDOW   = 15       # was 30 — shorter window suits trending markets

# Volatility gate — block choppy / very-high-vol markets
VOL_GATE_ENABLED    = True
VOL_LOW_ATR_PTS     = 2.0          # below this = low-vol breakout setup (boost)
VOL_HIGH_ATR_PTS    = 10.0         # above this = chaotic (caution)

# ── Ensemble Entry Brain (v8.5+) ──────────────────────────────────────────────
# Master confidence score that combines all signals. Replaces standalone AI gate.
ENSEMBLE_ENABLED    = True
ENSEMBLE_MIN_SCORE  = 0.52         # below this → block (after MIN_TRAIN_TRADES)

# ── Options Quality Filter ────────────────────────────────────────────────────
# Cheap options = gamma traps. Expensive options = slow movers. Sweet spot wins.
OPTION_MIN_PRICE    = 8.0          # below this → hard block
OPTION_SWEET_MIN    = 18.0         # sweet spot start (score = 1.0)
OPTION_SWEET_MAX    = 350.0        # sweet spot end
OPTION_MAX_PRICE    = 700.0        # above this → strong caution

# ── Partial Profit Booking ────────────────────────────────────────────────────
# At each profit milestone, sell a fraction of the position. Trail the rest.
PARTIAL_BOOKING_ENABLED = True
PARTIAL_BOOKING_TARGETS = [
    (8.0,  0.50),   # peak ≥ 8%  → sell 50% of remaining lots
    (18.0, 0.25),   # peak ≥ 18% → sell 25% of remaining lots
]

# ── Smart Cooldown (reason-aware, replaces flat SL_COOLDOWN_SECS) ─────────────
SMART_COOLDOWN_ENABLED    = True
COOLDOWN_AFTER_SL         = 120    # reduced from 180 — don't miss opportunities
COOLDOWN_AFTER_TRAIL_WIN  = 15     # quick re-entry after winning trail — momentum may continue
COOLDOWN_AFTER_TRAIL_LOSS = 60     # moderate caution after losing trail
COOLDOWN_AFTER_TIMEOUT_WIN  = 5    # almost instant re-entry after profitable timeout
COOLDOWN_AFTER_TIMEOUT_LOSS = 45   # moderate cooldown after losing timeout
COOLDOWN_AFTER_AI_EXIT    = 45     # AI-triggered exit — moderate caution
COOLDOWN_AFTER_REVERSAL   = 60     # NIFTY reversal exit — wait for re-establishment

# ── Advanced Risk Management (v9) ─────────────────────────────────────────────
# Consecutive loss protection — reduce position size after losses
LOSS_STREAK_REDUCE_AFTER  = 2      # after 2 consecutive losses, reduce lot size
LOSS_STREAK_SIZE_MULT     = 0.5    # multiply lots by 0.5 after streak (halve position)
LOSS_STREAK_MAX_REDUCE    = 3      # max reductions before stopping for the day

# Max drawdown — stop trading if capital drops too much
MAX_DRAWDOWN_PCT          = 5.0    # stop trading if down 5% from day start

# ATR-based position sizing
# lots = floor(capital × MAX_RISK_PER_TRADE / (option_ATR × LOT_SIZE))
# This sizes the position so that 1 ATR move = MAX_RISK_PER_TRADE of capital
ATR_POSITION_SIZING       = True   # True = ATR-based, False = max-capital

# ── AI Exit Safety (v9 redesign) ─────────────────────────────────────────────
# The AI exit analyzer / exit brain may only force exits after learning from
# this many closed trades. Below the threshold, only the mechanical
# cascade_risk crash protector stays live. Hard SL / trail / timeout always run.
AI_EXIT_MIN_TRADES   = 10

# ExitBrain influence on the combined trail % is clamped to this factor range.
# (Previously momentum_factor × learned multiplier could scale 0.25×–3.0×,
#  silently defeating the trail ratchet and the tier table.)
BRAIN_TRAIL_MIN_FACTOR = 0.7
BRAIN_TRAIL_MAX_FACTOR = 1.3

# ── Scalping Mode ────────────────────────────────────────────────────────────
# Tight parameters for quick in-and-out trades capturing small movements.
# Toggled from dashboard UI — overrides normal params when active.
SCALP_SL_PHASE1_PCT       = 3.0    # tight initial SL
SCALP_SL_PHASE2_PCT       = 2.0    # very tight phase-2 SL
SCALP_SL_PHASE1_SECS      = 10     # tighten SL quickly
SCALP_TRAIL_PCT           = 2.5    # tight trail to lock small gains
SCALP_TIMEOUT_SECS        = 20     # exit fast if move doesn't materialise
SCALP_COOLDOWN_SECS       = 5      # minimal cooldown — re-enter quickly
SCALP_JUMP_MULTIPLIER     = 0.6    # lower spike threshold (60% of normal)
SCALP_BREAKEVEN_PCT       = 1.0    # move SL to breakeven at just 1% profit
SCALP_PROFIT_TRAIL_THRESHOLD = 1.0 # activate profit trail earlier
SCALP_TARGET_PCT          = 2.0   # take profit target % for scalp trades
SCALP_MICRO_MOVE_PCT      = 0.05  # min cumulative move % to qualify as micro-move
SCALP_MICRO_CONSISTENCY   = 0.60  # 60% of ticks must go in same direction
SCALP_MICRO_WINDOW        = 8     # ticks to evaluate micro-momentum

# ── Manual trades: NIFTY-level exits ─────────────────────────────────────────
# Manual (scalp-manual) trades exit on NIFTY SPOT levels, not option premium.
# On a manual buy, SL/TP lines are placed at these distances from the current
# NIFTY level (adjustable live from the dashboard). Premium-based SL / trail /
# timeout / AI exits are DISABLED for manual trades — only the NIFTY levels,
# the manual exit button, the 3:25 force-exit, and a catastrophic premium
# floor can close them.
MANUAL_NIFTY_SL_PTS        = 15.0  # default SL distance (NIFTY points)
MANUAL_NIFTY_TP_PTS        = 30.0  # default target distance (NIFTY points)
MANUAL_MAX_PREMIUM_LOSS_PCT = 50.0 # safety net: premium down this % → exit anyway
