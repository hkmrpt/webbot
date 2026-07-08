# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Option Buying Robot v8.2** — an automated algorithmic trading bot for index options on Zerodha (Indian NFO exchange). It monitors NIFTY 50 in real-time, detects price spikes via ATR-based thresholds, buys the nearest-strike Call or Put option, and manages exits via trailing stops, profit-tier trails, and timeout mechanisms. Includes a real-time Flask/SocketIO web dashboard.

## Running the Application

```bash
pip install -r requirements.txt
python buy_app.py           # starts server at http://0.0.0.0:5001
```

- Main dashboard: `/` (buy_index.html)
- Settings panel: `/settings` (buy_settings.html)

**Automated tests** (pure logic — exit engine, gates, indicators, ML math, replay):
```bash
python -m pytest tests/
```

**Replay/backtest** — runs the exact strategy code over recorded ticks
(`ticks/ticks_YYYYMMDD.jsonl.gz`, recorded automatically while the app runs):
```bash
python -m replay.replay --date 20260708 [--config overrides.json] [--slippage-bps 5]
```
Output (isolated in `replay_out/run_*/`): trade_log.csv + report.json (win rate,
profit factor, max drawdown, per-reason/per-regime breakdowns). Replay never
touches live ML state or the live trade log.

**Test order placement directly:**
```bash
python Zerodha_api.py buy NIFTY25APR26200CE 1
python Zerodha_api.py sell NIFTY25APR26200CE 1
```

Live manual testing requires Zerodha credentials and market hours (9:15 AM–3:30 PM IST).

## Architecture

```
Browser (buy_index.html / buy_settings.html)
    │  SocketIO
    ▼
buy_app.py  ─── BuyExitStrategy (buy_exit_strategy.py)
    │                   │
    │                   └── trade_log.csv
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
| `config.py` | All configuration — credentials, trading params, thresholds. Single source of truth. |
| `Zerodha_api.py` | HTTP wrapper for Zerodha Kite API (MARKET MIS orders, NFO exchange) |
| `zerodha_websocket.py` | Async WebSocket client for Zerodha tick streaming |
| `decoder.py` | Struct-unpacking decoder for Zerodha's binary WebSocket protocol |

## Configuration (`config.py`)

All trading parameters live here. Key settings:
- **`MODE`**: `"demo"` (paper) or `"real"` (live orders)
- **Credentials**: `API_KEY`, `ENCTOKEN`, `KF_SESSION`, `PUBLIC_TOKEN` — hardcoded (no .env)
- **Spike detection**: `AUTO_JUMP=True` uses ATR × `JUMP_ATR_MULTIPLIER` (1.2), clamped to `[JUMP_MIN_PTS, JUMP_MAX_PTS]`
- **Stop loss phases**: Phase 1 (0–30s) = 15% SL, Phase 2 (30s+) = 8% SL
- **Trailing stop**: ATR-interpolated trail%, ratcheting profit-tier trails activate at 5% profit
- **Daily limits**: `MAX_TRADES_DAY=10`, `MAX_DAILY_LOSS=₹3000`, `DAILY_PROFIT_TARGET=₹6000`
- **Force exit**: 3:15 PM IST

## Thread Safety (v8.1 Critical Pattern)

The v8.1 refactor fixed deadlocks. The invariant must be preserved:

1. **`_state_lock` covers state mutation ONLY** — acquire lock, read/mutate `S`/`RC`, collect order intent, release lock
2. **All I/O happens after lock release** — `_emitter.emit`, `place_buy()`, `place_sell()`, any network call. Functions called under the lock return deferred log lists (or append to `S["_deferred_logs"]`) instead of calling `log()`.
3. **Write order results back** under a fresh lock acquire

Violating this pattern (doing I/O inside the lock) will cause deadlocks. `_csv_lock` is a separate lock for CSV writes.

## Core Invariants (v9.5 redesign)

- **Exit precedence** (`buy_exit_strategy.py`): hard SL → trail → partial booking → timeout → AI analyzer → brain decay. Learned signals can NEVER outrank the hard SL. AI exits are warmup-gated (`AI_EXIT_MIN_TRADES`); only the mechanical `cascade_risk` crash protector is live untrained.
- **Trail ratchet** is applied AFTER the ExitBrain adjustment (clamped 0.7–1.3×) — once tightened, the trail never widens. `PROFIT_TIER_TRAIL` must stay monotonic (tested).
- **Every entry path passes `_entry_gates_ok()`** — including manual scalp buys.
- **Time** comes from `core/clock.py` (naive IST, injectable) — never `datetime.now()`.
- **Seams**: `core/broker.py` (Real/Paper), `core/emitter.py` (SocketIO/Null), `core/clock.py`. The replay harness (`replay/replay.py`) drives the real `process_ticks` through these.
- **ML state is mode-tagged** (`*_demo.json` / `*_real.json`) — demo fills never train real-money weights. Engines skip learning when `result["mode"]` mismatches.
- **Trade log v2**: every close AND partial booking writes an ML-joinable row (entry scores, regime, mode, NIFTY levels) to the anchored `trade_log.csv`.
- **Tick recording** is always on (`TICK_RECORDING_ENABLED`) — `ticks/ticks_YYYYMMDD.jsonl.gz` is the backtest data foundation.

## Broadcast System

`broadcast()` in `buy_app.py` builds a full state snapshot under lock, then emits via SocketIO outside the lock. SocketIO events:
- `state` — full dashboard state snapshot
- `log` — message to UI log panel
- `trade_opened` / `trade_closed` — trade lifecycle events
- `error` — error notifications

## Trade Lifecycle

**State machine flow:**
```
Idle → (spike detected + tick confirmations) → Entry → Trade Open → Exit
```

Exit triggers: trailing stop breach, 60s timeout, daily loss/profit limit, manual exit button, 3:15 PM force exit.

Trades logged to `trade_log.csv` with full metadata (entry/exit price, PnL, reason, SL%, trail%, ATR, held seconds, equity).