# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Option Buying Robot v8.2** — an automated algorithmic trading bot for index options on Zerodha (Indian NFO exchange). It monitors NIFTY 50 in real-time, detects price spikes via ATR-based thresholds, buys the nearest-strike Call or Put option, and manages exits via trailing stops, profit-tier trails, and timeout mechanisms. Includes a real-time Flask/SocketIO web dashboard and an AI layer for self-learning entry/exit decisions.

## Running the Application

```bash
pip install -r requirements.txt
python buy_app.py           # starts server at http://0.0.0.0:5001
```

- Main dashboard: `/` (buy_index.html)
- Settings panel: `/settings` (buy_settings.html)
- Static assets (CSS/JS) are served from `static/`

**Test order placement directly:**
```bash
python Zerodha_api.py buy NIFTY25APR26200CE 1
python Zerodha_api.py sell NIFTY25APR26200CE 1
```

**Build a standalone Windows executable:**
```bash
pyinstaller BuyRobot.spec
```

There are no automated tests. Manual testing requires Zerodha credentials and market hours (9:15 AM–3:20 PM IST).

## Architecture

```
Browser (buy_index.html / buy_settings.html)
    │  SocketIO
    ▼
buy_app.py  ─── BuyExitStrategy (buy_exit_strategy.py)
    │                   │── ExitBrain (exit_brain.py)   ← AI exit layer
    │                   └── trade_log.csv
    ├── MarketBrain (market_brain.py)                   ← AI entry layer
    │       └── brain_state.json                        ← persisted model weights
    ├── zerodha_websocket.py  (real-time tick stream, async thread)
    ├── Zerodha_api.py        (HTTP order placement)
    └── decoder.py            (binary WebSocket message decoder)
```

**Global state dicts:**
- `S` — runtime trading state (prices, flags, trade slot, stats, exit engine instance). Protected by `_state_lock`.
- `RC` — hot-reloadable config parameters (thresholds, percentages, timing). Updated via `POST /api/update_rc` without restart.

## Key Files

| File | Role |
|------|------|
| `buy_app.py` | Main app: Flask/SocketIO server, tick processor, spike detector, trade lifecycle state machine |
| `buy_exit_strategy.py` | Exit engine: trailing stops, profit-tier trails, phase-based SL, CSV trade logging |
| `exit_brain.py` | AI exit layer: `MomentumScorer`, `AdaptiveTrailEngine`, `ExitBrain` — adaptive trail% and profit-decay exit |
| `market_brain.py` | AI entry layer: `MarketBrain` — online logistic regression that learns which entries win; persists to `brain_state.json` |
| `config.py` | All configuration — credentials, trading params, thresholds. Single source of truth. |
| `Zerodha_api.py` | HTTP wrapper for Zerodha Kite API (MARKET MIS orders, NFO exchange) |
| `zerodha_websocket.py` | Async WebSocket client for Zerodha tick streaming |
| `decoder.py` | Struct-unpacking decoder for Zerodha's binary WebSocket protocol |
| `volatility_detector.py` | **Orphaned module** — not imported by `buy_app.py`. Also requires `RANGE_WINDOW`, `RANGE_MAX_POINTS`, `RANGE_MIN_TICKS` in config.py which are not defined. |

## Configuration (`config.py`)

All trading parameters live here. Key settings:
- **`TRADING_MODE`**: `"demo"` (paper) or `"real"` (live orders)
- **Credentials**: All in `ZERODHA_CONFIG` dict (`api_key`, `enctoken`, `kf_session`, `public_token`, etc.). At startup, `enctoken.dat` (if present) overrides `ZERODHA_CONFIG["enctoken"]` in memory — `config.py` is never modified at runtime.
- **Spike detection**: `AUTO_JUMP=True` uses ATR × `JUMP_ATR_MULTIPLIER` (1.2), clamped to `[JUMP_MIN_PTS, JUMP_MAX_PTS]`
- **Stop loss phases**: Phase 1 (0–30s) = 15% SL (`SL_PHASE1_PCT`), Phase 2 (30s+) = 8% SL (`SL_PHASE2_PCT`)
- **Trailing stop**: ATR-interpolated trail%, ratcheting profit-tier trails (`PROFIT_TIER_TRAIL`) activate at 5% profit; `PROFIT_LOCK_TIERS` moves SL above entry at profit milestones
- **Move type detection**: Fast vs slow option-price velocity (`FAST_MOVE_VELOCITY=2.0 pts/tick`) affects trail width, timeout duration, and minimum profit thresholds
- **NIFTY reversal exit**: `NIFTY_REVERSAL_EXIT=True` — exits immediately if NIFTY retraces past entry price (CE: drops below, PE: rises above)
- **Daily limits**: `MAX_TRADES_DAY=30`, `MAX_DAILY_LOSS=₹3000`, `DAILY_PROFIT_TARGET=₹60000`
- **Trading hours**: 9:15 AM–3:20 PM IST; **Force exit**: 3:25 PM IST

## AI Layer (v8.2)

### Entry AI — `MarketBrain` (`market_brain.py`)
Online logistic regression (no external ML libraries) that scores each spike 0–1 and blocks low-confidence entries once it has seen ≥15 trades. Features: spike strength, NIFTY slope, velocity, time-of-day, ATR%, fast-entry flag, market regime. Weights persist across restarts in `brain_state.json`. Also runs `MarketRegimeDetector` to block direction-conflicting entries (e.g. CE in downtrend).

### Exit AI — `ExitBrain` (`exit_brain.py`)
Three components used by `BuyExitStrategy`:
- `MomentumScorer` — per-tick 0–1 momentum score (velocity + consistency + acceleration)
- `AdaptiveTrailEngine` — maps momentum score to trail%, self-tunes its width multiplier after each trade via EWMA
- `ExitBrain.check_ai_exit()` — triggers early exit when profit is declining for `DECAY_WINDOW` consecutive ticks with low momentum

## Thread Safety (v8.1/v8.2 Critical Pattern)

The v8.1 refactor fixed deadlocks. The invariant must be preserved:

1. **`_state_lock` covers state mutation ONLY** — acquire lock, read/mutate `S`/`RC`, collect order intent, release lock
2. **All I/O happens after lock release** — `socketio.emit`, `place_buy()`, `place_sell()`, any network call
3. **Write order results back** under a fresh lock acquire

Violating this pattern (doing I/O inside the lock) will cause deadlocks. `_csv_lock` is a separate lock for CSV writes. `_log_lock` is a separate lock for `S["logs"]`.

## Broadcast System

`broadcast()` in `buy_app.py` builds a full state snapshot via `_build_state_payload()` under lock, then emits via SocketIO outside the lock. `on_connect` reuses the same payload builder to prevent drift. SocketIO events:
- `state` — full dashboard state snapshot
- `log` — message to UI log panel
- `trade_opened` / `trade_closed` — trade lifecycle events
- `error` — error notifications

## Trade Lifecycle

**State machine flow:**
```
Idle → MarketBrain score_entry()
    → (spike detected + tick confirmations) → Entry
    → Trade Open → ExitBrain per-tick checks → Exit
    → MarketBrain.on_trade_closed() + ExitBrain.on_trade_closed() (learning)
```

Exit triggers: trailing stop breach, profit-lock SL hit, AI profit-decay exit, NIFTY reversal, move-type timeout, daily loss/profit limit, manual exit button, 3:25 PM force exit.

Trades logged to `trade_log.csv` with full metadata (entry/exit price, PnL, reason, SL%, trail%, ATR, held seconds, equity).
