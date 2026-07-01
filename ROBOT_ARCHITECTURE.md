# Option Buy Robot v9 — Complete Architecture

## Overview

AI/ML-powered automated option buying robot for NIFTY 50 index options on Zerodha.
Monitors NIFTY in real-time, detects price spikes and trends, buys the nearest-strike
Call or Put option, and manages exits via 10-signal AI exit engine with adaptive trailing.

4 self-learning ML engines persist to disk and improve with every trade.

---

## ENTRY PIPELINE

Every NIFTY tick passes through 11 sequential gates. ALL must pass for a trade to open.

### Gate 1: Spike Detection

- Compares tick-to-tick price move against dynamic threshold
- Threshold = live tick ATR x 1.2 (typically 4-8 pts for NIFTY)
- Capped by historical daily range x 5% (prevents impossible thresholds)
- Hard max: 20 pts (config JUMP_MAX_PTS)
- Intelligent multipliers adjust threshold based on conditions:
  - Expiry day: x0.8 (lower bar, smaller moves matter)
  - Trending market: x0.85 (catch continuation)
  - Volatile/choppy: x1.15-1.2 (filter noise)
  - Loss streak 2+: x1.15-1.3 (be selective)
  - Opening/closing 15 min: x1.2 (avoid noise)
  - VIX > 20: x0.85 (real moves happening)
  - VIX < 12: x1.15 (dead calm, filter noise)

### Gate 2: Trend Detection (alternative to spike)

- If no spike detected, checks for slow sustained trend
- 12-tick window, 70%+ directional consistency
- Minimum 0.25% cumulative move
- 25-tick cooldown between trend entries

### Gate 3: Spike Speed Validation

- Measures how fast the move happened using timestamped ticks
- Minimum speed: 0.012% per second
- Moves taking >20 seconds classified as trend, not spike
- Prevents entering on slow drifts that look like spikes

### Gate 4: Option Price Filter

- Blocks options below Rs.8 (gamma traps, huge spread)
- Blocks options above Rs.700 (slow delta, poor R:R)
- Sweet spot: Rs.18-350

### Gate 5: Breakout Filter

- Uses VolatilityDetector to track consolidation range
- Requires price to break out of the range
- If price is inside range (no breakout), entry blocked

### Gate 6: Regression + Volume Confirmation

- Linear regression slope must agree with trade direction
- CE needs positive slope, PE needs negative slope
- Option volume must be above average (volume factor 1.2x)

### Gate 7: MarketBrain (9-Feature ML)

- Online logistic regression trained one sample at a time
- 9 features: spike_strength, slope, velocity, time_of_day, atr_pct,
  fast_entry, regime, RSI, range_position
- Normalizes features with Welford's online algorithm
- Blocks: CE in downtrend, PE in uptrend, score < 0.52
- Market regime detection: trending_up, trending_down, choppy, volatile
- Persists learned weights to brain_state.json
- After 15+ trades, model becomes discriminative

### Gate 8: Market Intelligence

- Day type classification from option expiry date:
  - Expiry (DTE=0): tight params, small position
  - Pre-expiry (DTE=1): moderate tightening
  - Post-expiry (new series): wider params, full position
  - Normal (DTE 2+): standard
- Theta pressure scoring (exponential as expiry approaches)
- Premium decay rate (5x on expiry, 1x normal)
- Historical daily candle analysis (avg range, trend bias, vol percentile)
- Entry bias enforcement (blocks wrong-direction entries)
- Blocks all entries on expiry + low vol conditions

### Gate 9: Trade Budget Check

- Dynamic max trades per day (replaces fixed limit)
- Computed from 7 factors:
  - Day type base: expiry=8, normal=10, post-expiry=12
  - Volatility: high vol +40%, low vol -40%
  - Trending market: +20%
  - Today's win rate: hot hand (70%+) +30%, cold (<30%) -50%
  - Session P&L: up 3%+ aggressive, down 2%+ protective
  - Loss streak: 2 losses -30%, 3 -50%, 4+ near-halt
  - Time remaining: last 30 min no new trades
- Updates in real-time every tick

### Gate 10: Entry Analyzer (11-Dimension ML)

Comprehensive scoring across 11 dimensions:

1. Price Action: momentum (10-tick directional %), trend strength (R-squared),
   support/resistance quality, candle body quality, RSI signal
2. Volatility: ATR regime (dead/low/normal/high), NIFTY and option ATR
3. Greeks: delta (moneyness-based), gamma (ATM peak), theta (DTE-aware),
   vega (ATR-derived). Uses actual days-to-expiry from market intelligence
4. Risk/Reward: expected value, Kelly criterion fraction, R:R ratio,
   risk per lot in rupees, Kelly-optimal lot count
5. Timing: session phase (opening/early/prime/late/closing),
   minutes remaining, prime hours = 90% score
6. Microstructure: spread proxy (min tick movement), liquidity
   (price change frequency), tight spread + high liquidity = 90%
7. AI Brain: MarketBrain logistic regression score passthrough
8. Capital: utilization %, affordable lots, loss streak penalty
9. VIX: India VIX fear regime, trend direction, option buying suitability
10. Option Chain: PCR from OI, OI directional bias, max pain proximity
11. Multi-Timeframe: 1m/5m/15m candle trends, alignment scoring

- Composite score 0-100 with grade A/B/C/D/F
- Block if score < 45 (first 20 trades) or < 52 (after training)
- Self-learning: dimension weights adapt after each trade
- Persists to entry_analyzer_state.json

### Gate 11: Position Sizing

- ATR-based: lots = capital x 2% / (option_ATR x lot_size)
- Kelly criterion optimal lots (from win rate + R:R)
- Loss streak reduction: 2 consecutive losses = halve lots
- Day-type multiplier: 0.42x on expiry, 1.2x post-expiry
- Capped by available capital and MAX_LOTS_PER_TRADE

Output: Trade opens with adaptive SL%, trail%, timeout, qty.

---

## EXIT ENGINE

Every option price tick during an open trade runs through:

### 10-Signal AI Exit Analyzer

Runs 10 parallel signals every tick:

1. Volatility Spike: ATR expanded 3x+ suddenly. In profit = take profit
   before reversal (urgency 0.85). Losing = panic exit (0.9).
2. Reversal Pattern: bearish/bullish engulfing detection. Last 6 ticks
   form opposing pattern with 1.3x magnitude (urgency 0.7).
3. Momentum Collapse: velocity + acceleration + consistency all declining.
   Velocity ratio <0.4, favorable direction <35%, negative acceleration (0.75).
4. Profit Decay: lost 30%+ of peak profit in 6 consecutive declining ticks (0.8).
   Or profit halved from peak at any point (0.7).
5. Volume Dry-up: price movement at <30% of average activity.
   Only exits if at least 1% profit (0.55).
6. Gamma Trap: 4+ entry crossings with <15% movement efficiency.
   Price oscillating around entry = bleeding from spread (0.65).
7. Time Pressure: theta eating premium. Held 60s+ with <1% profit.
   Held 90s+ while losing = stronger signal. Cheap options increase urgency.
8. Trend Exhaustion: RSI divergence + slope flattening.
   CE with RSI>75 and flat slope = exhaustion (0.6).
9. Cascade Risk: 4+ consecutive accelerating drops, >2% total drop.
   Urgency 0.95 = nearly always exits immediately.
10. Adaptive Trail: handled by the trailing stop system below.

Decision logic:
- Critical signal (urgency >= 0.85) = immediate exit
- 3+ signals firing with combined weighted urgency >= 1.5 = exit
- Single strong signal above learned threshold = exit
- Otherwise hold and let trail/SL manage

Self-learning: learns from each trade whether exit was "good" (didn't give
back too much peak profit). Good exits boost signal weights, premature exits
penalize them. Persists to exit_analyzer_state.json.

### Stop Loss (Phased)

- Phase 1: 10% SL for first 20 seconds (give trade room to develop)
- Phase 2: 4% SL after 20 seconds (tighten aggressively)
- Breakeven: SL moves to entry price at 2% peak profit
- Profit lock tiers:
  - Peak >= 5%: SL at entry + 1% (guaranteed small win)
  - Peak >= 10%: SL at entry + 4%
  - Peak >= 20%: SL at entry + 10%
  - Peak >= 35%: SL at entry + 20%

### Trailing Stop (6-Tier + ATR + Time + AI)

Three independent trail sources, combined with min():

ATR Trail: linear interpolation from option tick ATR
- ATR <= 2.0 -> trail 5%, ATR >= 5.0 -> trail 18%

Profit-Tier Trail (6 levels):
- Peak >= 2% -> trail 3.5% (lock small gains early)
- Peak >= 4% -> trail 3.0% (tighten as profit grows)
- Peak >= 8% -> trail 4.0% (room for continuation)
- Peak >= 15% -> trail 5.5% (wide for runners)
- Peak >= 25% -> trail 7.0% (very wide for big moves)
- Peak >= 40% -> trail 10% (max room for monster moves)

Time Trail: starts at 10%, tightens by 1% every 45 seconds
Floor at 4% minimum.

AI Brain (ExitBrain): momentum score adjusts the combined trail.
High momentum = wider trail (let it run).
Low momentum = tight trail (protect gains).
Self-tuning multiplier learned from trade outcomes.

Ratchet: trail only gets tighter, never widens.
Trail price: high-water mark, never goes backward.
Floor: trail price always >= entry price.

### NIFTY Reversal Exit

- Monitors NIFTY index price during trade
- If NIFTY reverses past 55% of the spike threshold = exit
- Skipped if option already 2%+ in profit (trust the trail)

### Partial Profit Booking

- Peak >= 8%: sell 50% of lots, lock SL above entry
- Peak >= 18%: sell 25% of remaining, tighten SL further
- Continue trailing the remaining position

### Timeout

- Slow moves: 40 seconds, exit if < 0.5% profit
- Fast moves: 90 seconds, exit if < 2% profit
- General timeout: 50 seconds

### Smart Cooldown (after exit)

Reason-aware cooldown before next trade:
- SL hit: 120 seconds
- Profitable trail: 15 seconds (momentum may continue)
- Losing trail: 60 seconds
- Profitable timeout: 5 seconds (almost instant)
- Losing timeout: 45 seconds
- AI exit: 45 seconds
- NIFTY reversal: 60 seconds

---

## SELF-LEARNING ENGINES

4 independent ML engines that persist and improve:

### 1. MarketBrain (brain_state.json)

- 9-feature online logistic regression
- Features: spike_strength, slope, nifty_velocity, time_of_day, atr_pct,
  fast_entry, regime_enc, RSI, range_position
- Welford's algorithm for online normalization
- SGD with L2 regularization, learning rate 0.05
- Label: 1 if trade profitable >= 0.8%, else 0
- Ready after 15 trades

### 2. EntryAnalyzer (entry_analyzer_state.json)

- 11-dimension weighted scoring
- Dimensions: price_action, volatility, greeks, risk_reward, timing,
  microstructure, ai_brain, capital, vix, option_chain, multi_tf
- After each trade: boost weights of dimensions that predicted correctly,
  penalize dimensions that predicted wrongly
- EWMA weight update, normalized to sum=1

### 3. ExitAnalyzer (exit_analyzer_state.json)

- 10-signal reliability weights
- After each trade: if exit was good (didn't give back too much),
  boost signals that fired. If premature, penalize.
- Learning rate 0.06

### 4. MarketIntelligence (market_intel_state.json)

- Tracks win rate per day type (expiry, pre-expiry, normal)
- Last 100 trade outcomes stored
- If historically bad on a day type, reduces trade budget

---

## RISK MANAGEMENT

### Per-Trade Risk

- ATR-based position sizing: 1 ATR move = 2% of capital
- Kelly criterion optimal lots from win rate and R:R ratio
- Maximum capped by available capital

### Loss Streak Protection

- After 2 consecutive losses: halve lot size
- After 3: quarter lot size
- After 4+: trade budget near-halt (only 1 more allowed)

### Drawdown Protection

- Max drawdown: 5% from day-start capital -> stop trading
- Daily loss limit: Rs.300

### Trade Budget

- Dynamic, not fixed. Typically 8-18 trades per day
- Increases on winning days (hot hand +30%)
- Decreases on losing days (protective mode -50%)
- Near-halt on 4+ loss streak

---

## MARKET CONDITION HANDLING

### Silent market -> sudden breakout

- Spike detector catches the sudden move
- Breakout filter confirms it broke consolidation range
- Low vol regime in entry analyzer boosts the score
- Historical daily range provides intelligent ceiling

### Sideways / ranging

- MarketRegimeDetector classifies as "choppy" (regime_enc = -1.0)
- MarketBrain penalizes entries (low AI score)
- Breakout filter blocks trades inside range
- Entry analyzer price action composite is low

### Strong directional trend

- Trend detector catches sustained moves over 12 ticks
- MarketBrain allows same-direction entries only
- Lower spike threshold (x0.85) catches continuation moves
- Wide trail for fast moves, MTF alignment confirms
- ExitBrain widens trail when momentum is high

### High volatility / wild swings

- VIX elevated (>20): lower spike threshold, tighter SL/trail
- Entry analyzer volatility dimension: "high_vol" = 0.6 score
- ExitAnalyzer cascade risk signal catches panic drops
- Smaller position size (day-type multiplier)

### Expiry day

- Day type = "expiry": SL 6%, trail 4%, timeout 30s
- Position size 0.42x (tiny)
- Premium decay rate 5x normal
- Theta pressure maximum (1.0)
- Low vol + expiry = block all entries
- Spike threshold lowered (x0.8) to catch small moves

### Pre-expiry

- SL 8%, trail 5%, timeout 35-40s
- Position size 0.85x
- Theta accelerating but still tradeable

### Post-expiry (new series)

- Fresh premium, wider params
- SL 12%, trail 10%, timeout 60s
- Full position size

### Sudden reversal mid-trade

- NIFTY reversal exit (index reverses past buffer)
- ExitAnalyzer reversal pattern detection (bearish engulfing)
- Profit decay signal (lost 30%+ of peak)
- Cascade risk signal (accelerating drops, urgency 0.95)
- Breakeven SL protects from 2% profit onwards
- Profit lock tiers guarantee win from 5% peak

---

## POSITION SYNC (Real Mode)

### Startup Detection

- Fetches all Zerodha positions (GET /oms/portfolio/positions)
- Detects any open NFO long option positions
- Adopts them into the exit engine with adaptive SL/trail
- Subscribes to instrument token for live ticks

### Periodic Sync (every 30 seconds)

- Re-fetches positions, compares with previous snapshot
- Detects: new positions, closed positions, qty changes
- External close on Kite web -> resets bot state
- New external position -> adopts it

### Adoption Intelligence

SL/trail adapts to current P&L of the adopted position:
- Already 5%+ profit: SL 4%, trail 4% (tight protection)
- 2-5% profit: SL 6%, trail 5%
- Near breakeven: SL 8%, trail 8%
- Losing: SL 6-12% (adaptive), trail 8%

### Manual Toggle

"Manage Open Positions" switch in UI (only visible in real mode).
Scans Zerodha, adopts positions, shows status with live P&L.

---

## DATA FEEDS

| Source | Token | Purpose |
|--------|-------|---------|
| NIFTY 50 | 256265 | Spike detection, trend, ATR, regression |
| India VIX | 264969 | Fear regime, vol expectation, threshold adj |
| ATM CE option | Dynamic | Entry/exit price, trail management |
| ATM PE option | Dynamic | Entry/exit price, trail management |
| Historical daily | Zerodha REST | 7-day avg range, trend bias, vol percentile |
| Historical intraday | Zerodha REST | Chart candles, session analysis |
| Positions | Zerodha REST | Position sync, adoption |
| Orders | Zerodha REST | Order book display |

---

## DASHBOARD

### Chart (IQ Option style)

- Deep navy background (#0b0e18)
- Solid filled candles with gradients and rounded corners
- Clean grid (ultra-subtle 0.5px lines)
- Current price tag on right axis (green/red)
- Historical candles from Zerodha API on page load

### 22 Indicators

Trend: EMA, SMA, WMA, DEMA, TEMA, Parabolic SAR, Ichimoku Cloud
Oscillators: RSI, Stochastic, MACD, CCI, Williams %R, MFI
Volatility: Bollinger Bands, ATR, Keltner Channel, Donchian Channel
Volume: Volume, VWAP, OBV, CMF

- IQ Option style floating panel with categories
- Active indicators as floating pills on chart
- Per-indicator settings (period, color, multiplier)
- Sub-panel indicators (RSI, MACD, etc) below main chart

### 12 Drawing Tools

Cursor, Horizontal Line, Trend Line, Rectangle, Vertical Line,
Ray, Parallel Channel, Fibonacci Retracement, Arrow, Ellipse,
Measure (price diff + % + bars), Text Label

### Right Panel (4 tabs)

- Trade: active trade card, session stats, trade budget, intelligence
- Positions: live Zerodha positions with P&L (real mode only)
- Orders: order book with filters (all/open/executed/rejected)
- Log: activity log

### Performance Analytics API

GET /api/analytics returns:
- Equity curve, Sharpe ratio, Sortino ratio
- Max drawdown, profit factor, expectancy
- Win rate by hour, by exit reason, by date, by side
- Average winner vs loser, hold times
- Capture ratio, streak analysis

---

## FILES

| File | Purpose |
|------|---------|
| buy_app.py | Main Flask/SocketIO server, tick processor, entry pipeline |
| buy_exit_strategy.py | Exit engine: trailing, SL phases, partial booking |
| exit_brain.py | MomentumScorer + AdaptiveTrailEngine |
| exit_analyzer.py | 10-signal AI exit analysis engine |
| entry_analyzer.py | 11-dimension ML entry scoring |
| market_brain.py | 9-feature online logistic regression |
| market_intelligence.py | Day type, DTE, historical analysis, trade budget |
| option_chain_intel.py | VIX tracker, OI/PCR analyzer, multi-TF |
| performance_analytics.py | Sharpe, drawdown, equity curve from trade log |
| position_sync.py | Zerodha position detection and adoption |
| volatility_detector.py | Consolidation range detection |
| config.py | All trading parameters |
| Zerodha_api.py | HTTP order placement, balance, positions, orders |
| zerodha_websocket.py | WebSocket tick streaming |
| decoder.py | Binary WebSocket protocol decoder |
| excel_logger.py | Trade journal in Excel format |
| buy_index.html | Dashboard UI |
| static/js/buy_index.js | Chart engine, indicators, drawing tools, UI logic |
| static/css/buy_index.css | Dashboard styles |

### Persistence Files (auto-created)

| File | Contents |
|------|----------|
| brain_state.json | MarketBrain learned weights (9 features) |
| entry_analyzer_state.json | EntryAnalyzer dimension weights (11 dims) |
| exit_analyzer_state.json | ExitAnalyzer signal weights (10 signals) |
| market_intel_state.json | Day-type win rates, trade history |
| trade_log.csv | All trades with full metadata |
| trade_log.xlsx | Excel trade journal with formatting |
| enctoken.dat | Persisted Zerodha auth token |
| watchlist.json | Saved watchlist items |
