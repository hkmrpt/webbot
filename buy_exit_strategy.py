"""
buy_exit_strategy.py  ──  Option Buy Robot v8.2
════════════════════════════════════════════════

Trail logic:
  - ATR trail:      linear interpolation ATR_LOW/HIGH → PCT_LOW/HIGH
  - Profit tier:    PROPORTIONAL — small profit = tight trail, large profit = wider trail
                    activates once PEAK profit ≥ threshold
  - Time trail:     starts at TRAIL_TIME_START_PCT, tightens by TRAIL_TIME_TIGHTEN_STEP
                    every TRAIL_TIME_TIGHTEN_SECS after profit threshold hit
  - Combined:       min(atr_trail, tier_trail, time_trail), then ExitBrain
                    nudges it within [BRAIN_TRAIL_MIN_FACTOR, BRAIN_TRAIL_MAX_FACTOR]
  - Ratchet:        applied AFTER the brain — once tightened, never widens
                    (TRAIL_RATCHET_ENABLED)
  - Trail price:    high-water mark, never goes backward
  - Floor:          trail price always ≥ entry price
  - Breakeven:      SL moved to entry once peak profit ≥ BREAKEVEN_TRIGGER_PCT
  - Profit-lock SL: SL stepped above entry at key profit milestones (PROFIT_LOCK_TIERS)
"""

import csv
import json
import os
import sys
import threading
from collections import deque
from datetime import datetime

from core.clock import now as _now

from exit_brain import ExitBrain
from exit_analyzer import ExitAnalyzer
from config import (
    BUY_QTY, BUY_SL_PCT, BUY_TRAIL_PCT, LOT_SIZE,
    OPTION_ATR_PERIOD,
    PROFIT_TIER_TRAIL, PROFIT_TRAIL_THRESHOLD_PCT,
    PROFIT_LOCK_TIERS,
    TRADE_LOG, TRADE_TIMEOUT_MIN_PROFIT, TRADE_TIMEOUT_SECS,
    TRAIL_ATR_HIGH, TRAIL_ATR_LOW,
    TRAIL_PCT_HIGH, TRAIL_PCT_LOW,
    TRAIL_RATCHET_ENABLED,
    BREAKEVEN_TRIGGER_PCT,
    TRAIL_TIME_START_PCT, TRAIL_TIME_TIGHTEN_SECS,
    TRAIL_TIME_TIGHTEN_STEP, TRAIL_TIME_MIN_PCT,
    SL_PHASE1_PCT, SL_PHASE1_SECS, SL_PHASE2_PCT,
    MOVE_VELOCITY_WINDOW, FAST_MOVE_VELOCITY,
    PARTIAL_BOOKING_ENABLED, PARTIAL_BOOKING_TARGETS,
    MANUAL_MAX_PREMIUM_LOSS_PCT,
    ADAPTIVE_TIMEOUT_ENABLED,
    TIMEOUT_STALL_MOMENTUM, TIMEOUT_EXTEND_MOMENTUM,
    TIMEOUT_MIN_FACTOR, TIMEOUT_MAX_FACTOR,
    OCO_EXIT_ENABLED, OCO_CANDLE_TICKS, OCO_LOOKBACK_TICKS,
    OCO_SUPPORT_CANDLES, OCO_GRACE_SECS, OCO_MIN_TARGET_ATR,
    TARGET_LIMIT_PCT,
    AI_TP_ENABLED, AI_TP_HOLD_MOMENTUM, AI_TP_LOCK_FRACTION,
)

_csv_lock = threading.Lock()

# Log dir anchored next to the module (or exe when frozen). A bare relative
# TRADE_LOG silently wrote to whatever the process cwd happened to be — the
# root cause of the near-empty trade_log.csv. Tests may override LOG_DIR.
LOG_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))

CSV_FIELDS = [
    "date", "time", "symbol", "option_symbol", "side", "entry", "exit_price",
    "qty", "lot_qty",
    "pnl", "pnl_pct",
    "reason",
    "sl_pct", "trail_pct",
    "atr_trail_pct", "profit_trail_pct",
    "min_trail_pct_reached",
    "option_atr",
    "peak_profit",
    "peak_profit_pct",
    "held_secs",
    "equity_after",
    # ── v2 columns (ML-joinable metadata) ──
    "mode",                  # demo | real  (at entry)
    "row_type",              # full | partial
    "partial_realized_pnl",  # cumulative partial P&L booked before final close
    "pnl_total",             # pnl + partial_realized_pnl (full rows only)
    "ai_entry_score",        # MarketBrain score at entry
    "entry_analyzer_score",  # EntryAnalyzer composite at entry
    "entry_grade",           # A/B/C/D/F
    "regime",                # market regime at entry
    "scalp_mode",            # off | auto | manual (at entry)
    "nifty_entry",           # NIFTY level at entry
    "nifty_exit",            # NIFTY level at exit
    "entry_verdict_json",    # compact entry verdict snapshot
    "exit_signals_json",     # last-fired AI exit signals snapshot
]


def _log_path() -> str:
    return TRADE_LOG if os.path.isabs(TRADE_LOG) else os.path.join(LOG_DIR, TRADE_LOG)


def _rotate_legacy_log(path: str):
    """If an existing log has an old (different) header, rename it aside so
    the new schema starts clean instead of appending mismatched rows."""
    try:
        with open(path, newline="") as f:
            first = f.readline().strip()
        if first and first != ",".join(CSV_FIELDS):
            base, ext = os.path.splitext(path)
            n = 1
            while os.path.exists(f"{base}_legacy{n}{ext}"):
                n += 1
            os.rename(path, f"{base}_legacy{n}{ext}")
    except OSError:
        pass


def write_trade_csv(result: dict):
    """Append one trade row to the anchored trade log. Thread-safe."""
    path = _log_path()
    now  = _now()
    row  = {
        "date":                 now.strftime("%Y-%m-%d"),
        "time":                 now.strftime("%H:%M:%S"),
        "symbol":               result.get("symbol",           ""),
        "option_symbol":        result.get("option_symbol",    ""),
        "side":                 result.get("side",             ""),
        "entry":                result.get("entry",            ""),
        "exit_price":           result.get("exit_price",       ""),
        "qty":                  result.get("qty",              ""),
        "lot_qty":              (result.get("qty", 0) or 0) * result.get("lot_size", LOT_SIZE),
        "pnl":                  result.get("pnl",              ""),
        "pnl_pct":              result.get("pnl_pct",          ""),
        "reason":               result.get("reason",           ""),
        "sl_pct":               result.get("sl_pct",           ""),
        "trail_pct":            result.get("trail_pct",        ""),
        "atr_trail_pct":        result.get("atr_trail_pct",    ""),
        "profit_trail_pct":     result.get("profit_trail_pct", ""),
        "min_trail_pct_reached":result.get("min_trail_pct_reached", ""),
        "option_atr":           result.get("option_atr",       ""),
        "peak_profit":          result.get("peak_profit",      ""),
        "peak_profit_pct":      result.get("peak_profit_pct",  ""),
        "held_secs":            result.get("held_secs",        ""),
        "equity_after":         result.get("equity_after",     ""),
        "mode":                 result.get("mode",             ""),
        "row_type":             result.get("row_type",         "full"),
        "partial_realized_pnl": result.get("partial_realized_pnl", ""),
        "pnl_total":            result.get("pnl_total",        ""),
        "ai_entry_score":       result.get("ai_entry_score",   ""),
        "entry_analyzer_score": result.get("entry_analyzer_score", ""),
        "entry_grade":          result.get("entry_grade",      ""),
        "regime":               result.get("regime",           ""),
        "scalp_mode":           result.get("scalp_mode",       ""),
        "nifty_entry":          result.get("nifty_entry",      ""),
        "nifty_exit":           result.get("nifty_exit",       ""),
        "entry_verdict_json":   result.get("entry_verdict_json", ""),
        "exit_signals_json":    result.get("exit_signals_json", ""),
    }
    with _csv_lock:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            _rotate_legacy_log(path)
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)


# ── ATR helpers ───────────────────────────────────────────────────────────────
def _compute_option_atr(prices: list) -> float | None:
    if len(prices) < 2:
        return None
    trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return round(sum(trs) / len(trs), 3)


def _atr_to_trail_pct(atr: float | None, override: float | None = None) -> float:
    """Linear interpolation: ATR → trail %. Override sets a floor."""
    if atr is None:
        return round(override if override is not None else BUY_TRAIL_PCT, 1)

    if atr >= TRAIL_ATR_HIGH:
        atr_pct = TRAIL_PCT_HIGH
    elif atr <= TRAIL_ATR_LOW:
        atr_pct = TRAIL_PCT_LOW
    else:
        t       = (atr - TRAIL_ATR_LOW) / (TRAIL_ATR_HIGH - TRAIL_ATR_LOW)
        atr_pct = TRAIL_PCT_LOW + t * (TRAIL_PCT_HIGH - TRAIL_PCT_LOW)

    if override is not None:
        return round(max(atr_pct, override), 1)
    return round(atr_pct, 1)


def _time_to_trail_pct(held_secs: float, peak_profit_pct: float) -> float | None:
    """
    Time-based trail: activates once peak profit >= threshold.
    Starts at TRAIL_TIME_START_PCT, tightens by TRAIL_TIME_TIGHTEN_STEP
    every TRAIL_TIME_TIGHTEN_SECS. Floor at TRAIL_TIME_MIN_PCT.
    Returns None if profit below threshold (not yet active).
    """
    if peak_profit_pct < PROFIT_TRAIL_THRESHOLD_PCT:
        return None
    intervals = int(held_secs // TRAIL_TIME_TIGHTEN_SECS)
    trail     = TRAIL_TIME_START_PCT - (intervals * TRAIL_TIME_TIGHTEN_STEP)
    return round(max(trail, TRAIL_TIME_MIN_PCT), 1)


def _profit_to_trail_pct(peak_profit_pct: float) -> float | None:
    """
    Uses PEAK profit%, not current.
    Returns None if profit below threshold; otherwise returns tier trail%.
    """
    if peak_profit_pct < PROFIT_TRAIL_THRESHOLD_PCT:
        return None

    tiers = sorted(PROFIT_TIER_TRAIL, key=lambda x: x[0], reverse=True)
    for (min_pct, trail) in tiers:
        if peak_profit_pct >= min_pct:
            return float(trail)

    if tiers:
        return float(min(tiers, key=lambda x: x[0])[1])
    return None


# ── Move classifier ───────────────────────────────────────────────────────────
def _classify_move(prices: deque, window: int) -> str:
    """
    Returns 'fast' if avg absolute pts/tick over last `window` ticks
    is >= FAST_MOVE_VELOCITY, else 'slow'.
    """
    lst = list(prices)
    if len(lst) < 2:
        return "slow"
    recent  = lst[-min(window + 1, len(lst)):]
    changes = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
    avg_vel = sum(changes) / len(changes)
    return "fast" if avg_vel >= FAST_MOVE_VELOCITY else "slow"


# ── Adaptive timeout + OCO bracket (v9.6) ─────────────────────────────────────
def _adaptive_timeout_factor(momentum_score: float | None) -> float:
    """
    Stretch/shrink factor for the trade timeout, decided by the tick-derived
    momentum score (ExitBrain, 0–1, ~0.34 on a dead tape):
      score ≤ STALL   → TIMEOUT_MIN_FACTOR (tape is dead — stop waiting)
      score ≥ EXTEND  → linear ramp 1.0 → TIMEOUT_MAX_FACTOR (move developing)
      in between      → 1.0 (base timeout unchanged)
    """
    if not ADAPTIVE_TIMEOUT_ENABLED or momentum_score is None:
        return 1.0
    if momentum_score <= TIMEOUT_STALL_MOMENTUM:
        return TIMEOUT_MIN_FACTOR
    if momentum_score >= TIMEOUT_EXTEND_MOMENTUM:
        t = (momentum_score - TIMEOUT_EXTEND_MOMENTUM) / max(
            1e-9, 1.0 - TIMEOUT_EXTEND_MOMENTUM)
        return round(1.0 + min(t, 1.0) * (TIMEOUT_MAX_FACTOR - 1.0), 2)
    return 1.0


def _build_pseudo_candles(prices: list, ticks_per_candle: int) -> list:
    """Group ticks into OHLC pseudo-candles, oldest → newest."""
    candles = []
    for i in range(0, len(prices), ticks_per_candle):
        chunk = prices[i:i + ticks_per_candle]
        if len(chunk) < 2:
            continue
        candles.append({"open": chunk[0], "high": max(chunk),
                        "low": min(chunk), "close": chunk[-1]})
    return candles


def _compute_oco_levels(prices: list, current_price: float,
                        atr: float | None, hard_sl: float):
    """
    Derive the timeout OCO bracket from recent tick/candle structure:
      target — swing high of the lookback candles (resistance the move must
               reclaim to justify holding), at least OCO_MIN_TARGET_ATR
               option-ATRs above the current price
      floor  — swing low of the newest OCO_SUPPORT_CANDLES candles (support),
               clamped so it can never sit below the hard SL
    Returns (target, floor), or None when the structure is degenerate —
    too little history, or price already below its own support — in which
    case the caller falls back to the plain timeout exit.
    """
    if len(prices) < OCO_CANDLE_TICKS * 3:
        return None
    candles = _build_pseudo_candles(prices[-OCO_LOOKBACK_TICKS:], OCO_CANDLE_TICKS)
    if len(candles) < 3:
        return None

    swing_high = max(c["high"] for c in candles)
    recent     = candles[-OCO_SUPPORT_CANDLES:]
    swing_low  = min(c["low"] for c in recent)

    min_room = (atr or 0.0) * OCO_MIN_TARGET_ATR
    if min_room <= 0:
        min_room = current_price * 0.001   # 0.1% fallback when ATR unknown

    target = round(max(swing_high, current_price + min_room), 2)
    floor  = round(max(swing_low, hard_sl), 2)
    if floor >= current_price or target <= current_price:
        return None
    return target, floor


# ── Strategy class ────────────────────────────────────────────────────────────
class BuyExitStrategy:

    def __init__(self, capital: float):
        self.capital             = capital
        self._leg                = None
        self._open_time          = None
        self._opt_price_hist     = deque(maxlen=OPTION_ATR_PERIOD + 1)
        self._tick_hist          = deque(maxlen=OCO_LOOKBACK_TICKS)   # OCO swing levels
        self._trail_pct_override = None
        self._brain              = ExitBrain()          # AI brain — persists across trades
        self._exit_analyzer      = ExitAnalyzer()      # 10-signal exit analysis engine
        self._partial_done: list = []                   # indices of partial targets already booked
        self._original_qty: int  = 1                    # qty at open (before any partial sells)
        self._profit_history: list = []                 # profit % each tick for decay detection
        # Market context the app keeps updated (e.g. live NIFTY level) so
        # close results can be enriched without the engine doing any I/O.
        self.market_ctx: dict    = {}

    def open_leg(
        self,
        side:                    str,
        entry_price:             float,
        sl_pct_override:         float | None = None,
        trail_pct_override:      float | None = None,
        sl_pct_p2_override:      float | None = None,
        sl_phase1_secs_override: float | None = None,
        timeout_secs_override:   float | None = None,
        tp_pct_override:         float | None = None,
        seed_prices:             list  | None = None,
        symbol:                  str         = "",
        option_symbol:           str         = "",
        qty_override:            int  | None = None,
        lot_size:                int  | None = None,
        meta:                    dict | None = None,
        manual_exit_mode:        bool        = False,
        **_kwargs,
    ) -> dict:
        sl_pct = sl_pct_override if sl_pct_override is not None else SL_PHASE1_PCT
        sl     = round(entry_price * (1 - sl_pct / 100), 2)

        self._trail_pct_override = trail_pct_override
        init_trail = trail_pct_override if trail_pct_override is not None else BUY_TRAIL_PCT

        self._opt_price_hist = deque(maxlen=OPTION_ATR_PERIOD + 1)
        if seed_prices:
            for p in seed_prices[-(OPTION_ATR_PERIOD + 1):]:
                self._opt_price_hist.append(p)
        self._opt_price_hist.append(entry_price)

        self._tick_hist = deque(maxlen=OCO_LOOKBACK_TICKS)
        if seed_prices:
            for p in seed_prices[-OCO_LOOKBACK_TICKS:]:
                self._tick_hist.append(p)
        self._tick_hist.append(entry_price)

        qty = qty_override if (qty_override is not None and qty_override >= 1) else BUY_QTY
        self._partial_done  = []
        self._original_qty  = qty
        self._profit_history = []
        # Defensive: momentum/profit history must never leak from a previous
        # trade into this one (contaminates the adaptive trail at open).
        self._brain.reset_trade_state()

        self._leg = {
            "side":                 side,
            "symbol":               symbol,
            "option_symbol":        option_symbol,
            "entry":                entry_price,
            "qty":                  qty,
            "lot_size":             int(lot_size) if lot_size else LOT_SIZE,
            "partial_realized_pnl": 0.0,
            "meta":                 dict(meta) if meta else {},
            "manual_mode":          bool(manual_exit_mode),
            "sl":                   sl,
            "sl_pct":               sl_pct,
            "trail_pct":            init_trail,
            "atr_trail_pct":        init_trail,
            "profit_trail_pct":     None,
            "time_trail_pct":       None,
            "min_trail_pct_reached":init_trail,
            "option_atr":           None,
            "peak_price":           entry_price,
            "peak_profit":          0.0,
            "peak_profit_pct":      0.0,
            "trail_price":          None,
            "phase2":               False,
            "breakeven_moved":      False,
            "move_type":            "slow",
            # Per-stock AI overrides (None = use config defaults)
            "_sl_pct_p2":           sl_pct_p2_override,
            "_sl_phase1_secs":      sl_phase1_secs_override,
            "_timeout_secs":        timeout_secs_override,
            # AI take-profit reference (None = config TARGET_LIMIT_PCT)
            "_tp_pct":              tp_pct_override,
            "tp_riding":            False,
            # Adaptive-timeout / OCO bracket state
            "timeout_eff_secs":     None,
            "oco_armed":            False,
            "oco_target":           None,
            "oco_floor":            None,
            "oco_deadline_secs":    None,
            "open":                 True,
        }
        self._open_time = _now()

        return {"sl": sl, "qty": qty, "sl_pct": sl_pct, "trail_pct": init_trail}

    def on_price(self, price: float):
        if not self._leg or not self._leg["open"]:
            return None

        leg      = self._leg
        qty_full = leg["qty"] * leg["lot_size"]
        elapsed  = (_now() - self._open_time).total_seconds() if self._open_time else 0

        self._opt_price_hist.append(price)
        self._tick_hist.append(price)

        atr               = _compute_option_atr(list(self._opt_price_hist))
        leg["option_atr"] = atr

        atr_trail            = _atr_to_trail_pct(atr, self._trail_pct_override)
        leg["atr_trail_pct"] = atr_trail

        if price > leg["peak_price"]:
            leg["peak_price"]      = price
            leg["peak_profit"]     = round((price - leg["entry"]) * qty_full, 2)
            leg["peak_profit_pct"] = round((price - leg["entry"]) / leg["entry"] * 100, 2)

        if not leg["phase2"] and price > leg["entry"]:
            leg["phase2"] = True

        # Tighten SL after phase 1 period (use per-stock overrides if set)
        _phase1_secs = leg["_sl_phase1_secs"] if leg["_sl_phase1_secs"] is not None else SL_PHASE1_SECS
        _sl_p2       = leg["_sl_pct_p2"]      if leg["_sl_pct_p2"]      is not None else SL_PHASE2_PCT
        if elapsed >= _phase1_secs and leg["sl_pct"] > _sl_p2:
            new_sl = round(leg["entry"] * (1 - _sl_p2 / 100), 2)
            if new_sl > leg["sl"]:
                leg["sl"]     = new_sl
                leg["sl_pct"] = _sl_p2

        peak_pct = leg["peak_profit_pct"]

        # Breakeven: move SL to entry once peak profit hits threshold
        if not leg["breakeven_moved"] and peak_pct >= BREAKEVEN_TRIGGER_PCT:
            if leg["entry"] > leg["sl"]:
                leg["sl"] = leg["entry"]
            leg["breakeven_moved"] = True

        # Profit-lock SL: step SL above entry to lock in profit at key milestones
        if PROFIT_LOCK_TIERS:
            tiers = sorted(PROFIT_LOCK_TIERS, key=lambda x: x[0], reverse=True)
            for (min_pct, lock_pct) in tiers:
                if peak_pct >= min_pct:
                    locked_sl = round(leg["entry"] * (1 + lock_pct / 100), 2)
                    if locked_sl > leg["sl"]:
                        leg["sl"] = locked_sl
                    break

        # ── AI Brain: momentum score ────────────────────────────────────────
        momentum_score       = self._brain.score(self._opt_price_hist)
        leg["momentum_score"] = momentum_score

        current_pct = (price - leg["entry"]) / leg["entry"] * 100
        self._brain.update_profit(current_pct)

        # Legacy move-type label (informational — brain drives the actual trail)
        move_type        = _classify_move(self._opt_price_hist, MOVE_VELOCITY_WINDOW)
        leg["move_type"] = move_type

        # Three trail sources
        p_trail                 = _profit_to_trail_pct(peak_pct)
        t_trail                 = _time_to_trail_pct(elapsed, peak_pct)
        leg["profit_trail_pct"] = p_trail
        leg["time_trail_pct"]   = t_trail

        candidates = [atr_trail]
        if p_trail is not None:
            candidates.append(p_trail)
        if t_trail is not None:
            candidates.append(t_trail)
        combined = round(min(candidates), 1)

        # ── AI Brain: adaptive trail ────────────────────────────────────────
        # Brain uses momentum score + self-learned multiplier (clamped to
        # [BRAIN_TRAIL_MIN_FACTOR, BRAIN_TRAIL_MAX_FACTOR]) to nudge the trail.
        # High momentum → wider trail (let it run).
        # Low momentum  → tight trail (protect gains fast).
        combined = self._brain.adaptive_trail(combined, momentum_score)

        # Ratchet AFTER the brain so the guarantee holds: once the trail has
        # tightened, no brain widening can loosen it again.
        if TRAIL_RATCHET_ENABLED:
            combined = round(min(combined, leg["min_trail_pct_reached"]), 1)
            leg["min_trail_pct_reached"] = combined

        leg["trail_pct"] = combined

        if leg["phase2"]:
            raw_trail  = leg["peak_price"] * (1 - leg["trail_pct"] / 100)
            new_trail  = round(max(raw_trail, leg["entry"]), 2)
            prev_trail = leg["trail_price"] or leg["entry"]
            leg["trail_price"] = max(new_trail, prev_trail)

        # Timeout: per-trade override (the entry verdict always supplies one),
        # config default as safety net. The base timeout is then stretched or
        # shrunk each tick by the momentum of the option's own recent ticks —
        # a developing move gets more room, a dead tape gets cut short.
        timeout_secs   = leg["_timeout_secs"] if leg["_timeout_secs"] is not None else TRADE_TIMEOUT_SECS
        eff_timeout    = round(timeout_secs * _adaptive_timeout_factor(momentum_score), 1)
        leg["timeout_eff_secs"] = eff_timeout
        min_profit_pct = TRADE_TIMEOUT_MIN_PROFIT

        # Track profit history for decay detection
        self._profit_history.append(current_pct)
        if len(self._profit_history) > 30:
            self._profit_history = self._profit_history[-30:]

        # ═════════════════════════════════════════════════════════════════
        # MANUAL MODE — premium-based exits are DISABLED. The trade exits on
        # NIFTY spot levels (checked in buy_app on index ticks), the manual
        # exit button, or the force-exit — plus one catastrophic premium
        # floor so a frozen index feed can never let the option bleed to 0.
        # ═════════════════════════════════════════════════════════════════
        if leg.get("manual_mode"):
            if current_pct <= -MANUAL_MAX_PREMIUM_LOSS_PCT:
                return self._close(price, "sl")
            return None

        # ═════════════════════════════════════════════════════════════════
        # EXIT PRECEDENCE — hard risk rules FIRST, learned signals LAST:
        #   1. hard SL          (never outranked by anything)
        #   2. trailing stop
        #   3. partial booking  (only reached if no hard exit this tick)
        #   3.5 AI take-profit  (target% is a reference — momentum decides
        #                        book-now vs ride-the-trail)
        #   4. timeout          (adaptive) / OCO bracket
        #   5. AI analyzer      (warmup-gated inside ExitAnalyzer)
        #   6. brain decay      (warmup-gated inside ExitBrain)
        # ═════════════════════════════════════════════════════════════════

        # 1. Hard stop-loss
        if price <= leg["sl"]:
            return self._close(price, "sl")

        # 2. Trailing stop
        if leg["phase2"] and leg["trail_price"] and price <= leg["trail_price"]:
            return self._close(price, "trail")

        # 3. Partial profit booking — book a fraction at profit milestones,
        #    credit the realized P&L, then continue trailing the rest.
        if PARTIAL_BOOKING_ENABLED and leg["qty"] > 1:
            for i, (target_pct, fraction) in enumerate(PARTIAL_BOOKING_TARGETS):
                if i not in self._partial_done and peak_pct >= target_pct:
                    sell_qty = max(1, round(leg["qty"] * fraction))
                    # Keep at least 1 lot running
                    if sell_qty < leg["qty"]:
                        self._partial_done.append(i)
                        leg["qty"] -= sell_qty
                        # Realized P&L of the sold portion — credited NOW so
                        # equity and the final close never lose it.
                        realized = round((price - leg["entry"]) * sell_qty * leg["lot_size"], 2)
                        leg["partial_realized_pnl"] = round(
                            leg["partial_realized_pnl"] + realized, 2)
                        self.capital = round(self.capital + realized, 2)
                        # After partial, tighten SL to lock in the gained profit
                        lock_pct     = max(BREAKEVEN_TRIGGER_PCT, target_pct * 0.40)
                        locked_sl    = round(leg["entry"] * (1 + lock_pct / 100), 2)
                        leg["sl"]    = max(leg["sl"], locked_sl)
                        event = {
                            "event_type":      "partial",
                            "partial_qty":     sell_qty,
                            "remaining_qty":   leg["qty"],
                            "price":           price,
                            "partial_pnl":     realized,
                            "peak_profit_pct": peak_pct,
                            "reason":          f"partial_{i + 1}",
                            "side":            leg["side"],
                            "symbol":          leg.get("symbol", ""),
                            "option_symbol":   leg.get("option_symbol", ""),
                            "entry":           leg["entry"],
                            "lot_size":        leg["lot_size"],
                            "new_sl":          leg["sl"],
                            "equity_after":    self.capital,
                        }
                        try:
                            write_trade_csv({
                                **{k: leg.get(k, "") for k in
                                   ("side", "symbol", "option_symbol", "entry", "lot_size")},
                                "exit_price":   price,
                                "qty":          sell_qty,
                                "pnl":          realized,
                                "pnl_pct":      round((price - leg["entry"]) / leg["entry"] * 100, 2),
                                "reason":       event["reason"],
                                "row_type":     "partial",
                                "equity_after": self.capital,
                                "peak_profit_pct": peak_pct,
                                "held_secs":    int(elapsed),
                                **leg.get("meta", {}),
                            })
                        except Exception as e:
                            print(f"[CSV] partial write error: {e}")
                        return event

        # 3.5 AI take-profit — the configured target% is a REFERENCE, not an
        #     order. At/above it the tick-derived momentum decides every tick:
        #     still pushing → ride the trail, but first ratchet the SL to lock
        #     most of the reached target (the ride can never give it back
        #     below the lock); fading → book the profit now.
        tp_pct = leg["_tp_pct"] if leg["_tp_pct"] is not None else TARGET_LIMIT_PCT
        if AI_TP_ENABLED and tp_pct > 0 and current_pct >= tp_pct:
            if momentum_score < AI_TP_HOLD_MOMENTUM:
                return self._close(price, "ai_tp")
            lock_sl = round(leg["entry"] * (1 + tp_pct * AI_TP_LOCK_FRACTION / 100), 2)
            if lock_sl > leg["sl"]:
                leg["sl"] = lock_sl
            leg["tp_riding"] = True

        # 4. Timeout — adaptive deadline, then a virtual OCO bracket instead of
        #    an instant market exit. Target = recent swing high, floor = recent
        #    swing low (never below the hard SL, which slots 1–2 still enforce
        #    first every tick). First level touched wins; OCO_GRACE_SECS caps
        #    the extra hold and falls back to the plain timeout exit.
        if leg["oco_armed"]:
            if price >= leg["oco_target"]:
                return self._close(price, "oco_target")
            if price <= leg["oco_floor"]:
                return self._close(price, "oco_floor")
            if elapsed >= leg["oco_deadline_secs"]:
                return self._close(price, "timeout")
        elif elapsed >= eff_timeout and current_pct < min_profit_pct:
            levels = (_compute_oco_levels(list(self._tick_hist), price,
                                          atr, leg["sl"])
                      if OCO_EXIT_ENABLED else None)
            if levels is None:
                return self._close(price, "timeout")
            leg["oco_armed"]         = True
            leg["oco_target"], leg["oco_floor"] = levels
            leg["oco_deadline_secs"] = elapsed + OCO_GRACE_SECS
            return {
                "event_type": "oco_armed",
                "target":     leg["oco_target"],
                "floor":      leg["oco_floor"],
                "grace_secs": OCO_GRACE_SECS,
                "price":      price,
                "side":       leg["side"],
            }

        # 5. 10-Signal AI Exit Analyzer (no authority during warmup — only the
        #    mechanical cascade_risk crash protector stays live)
        ai_exit, ai_exit_reason = self._exit_analyzer.check(
            prices=list(self._opt_price_hist),
            entry=leg["entry"],
            side=leg["side"],
            current_pct=current_pct,
            peak_pct=peak_pct,
            held_secs=elapsed,
            profit_history=self._profit_history,
            opt_price=price,
        )
        if ai_exit:
            leg["_ai_exit_detail"] = ai_exit_reason
            return self._close(price, "ai_analyzer")

        # 6. Brain profit-decay exit (warmup-gated)
        if self._brain.check_ai_exit(current_pct, momentum_score):
            return self._close(price, "ai_exit")

        return None

    def force_close(self, price: float, reason: str = "manual"):
        if not self._leg or not self._leg["open"]:
            return None
        return self._close(price, reason)

    def _close(self, exit_price: float, reason: str) -> dict:
        leg      = self._leg
        qty_full = leg["qty"] * leg["lot_size"]
        pnl      = round((exit_price - leg["entry"]) * qty_full, 2)
        pnl_pct  = round((exit_price - leg["entry"]) / leg["entry"] * 100, 2)
        partial  = leg.get("partial_realized_pnl", 0.0) or 0.0

        # Partial P&L was already credited to capital at booking time —
        # only the remaining quantity's P&L is credited here.
        self.capital = round(self.capital + pnl, 2)
        leg["open"]  = False

        held_secs = 0
        if self._open_time:
            held_secs = int((_now() - self._open_time).total_seconds())

        # Compact snapshot of the AI exit signals at the final tick
        try:
            sig_json = json.dumps({
                k: round(v["urgency"], 2)
                for k, v in (self._exit_analyzer._last_signals or {}).items()
                if v["fire"]
            })
        except Exception:
            sig_json = ""

        result = {
            "side":                 leg["side"],
            "symbol":               leg.get("symbol", ""),
            "option_symbol":        leg.get("option_symbol", ""),
            "entry":                leg["entry"],
            "exit_price":           exit_price,
            "qty":                  leg["qty"],
            "lot_size":             leg["lot_size"],
            "pnl":                  pnl,
            "pnl_pct":              pnl_pct,
            "partial_realized_pnl": partial,
            "pnl_total":            round(pnl + partial, 2),
            "reason":               reason,
            "equity_after":         self.capital,
            "sl_pct":               leg["sl_pct"],
            "trail_pct":            leg["trail_pct"],
            "atr_trail_pct":        leg["atr_trail_pct"],
            "profit_trail_pct":     leg["profit_trail_pct"],
            "min_trail_pct_reached":leg["min_trail_pct_reached"],
            "option_atr":           leg["option_atr"],
            "peak_profit":          leg["peak_profit"],
            "peak_profit_pct":      leg["peak_profit_pct"],
            "held_secs":            held_secs,
            "row_type":             "full",
            "nifty_exit":           self.market_ctx.get("nifty", ""),
            "exit_signals_json":    sig_json,
            **leg.get("meta", {}),
        }

        try:
            write_trade_csv(result)
        except Exception as e:
            print(f"[CSV] write error: {e}")

        try:
            from excel_logger import write_trade_excel
            write_trade_excel(result)
        except Exception as e:
            print(f"[XL] write error: {e}")

        # Teach the brains from this trade's outcome
        self._brain.on_trade_closed(result)
        self._exit_analyzer.on_trade_closed(result)

        return result

    def leg_snapshot(self) -> dict:
        if not self._leg:
            return {}
        held = 0
        if self._open_time:
            held = int((_now() - self._open_time).total_seconds())
        return {
            "side":                 self._leg["side"],
            "entry":                self._leg["entry"],
            "sl":                   self._leg["sl"],
            "sl_pct":               self._leg["sl_pct"],
            "trail_pct":            self._leg["trail_pct"],
            "atr_trail_pct":        self._leg["atr_trail_pct"],
            "profit_trail_pct":     self._leg["profit_trail_pct"],
            "time_trail_pct":       self._leg["time_trail_pct"],
            "min_trail_pct_reached":self._leg["min_trail_pct_reached"],
            "breakeven_moved":      self._leg["breakeven_moved"],
            "option_atr":           self._leg["option_atr"],
            "qty":                  self._leg["qty"],
            "peak_price":           self._leg["peak_price"],
            "peak_profit":          self._leg["peak_profit"],
            "peak_profit_pct":      self._leg["peak_profit_pct"],
            "trail_price":          self._leg["trail_price"],
            "phase2":               self._leg["phase2"],
            "move_type":            self._leg["move_type"],
            "partial_realized_pnl": self._leg.get("partial_realized_pnl", 0.0),
            "momentum_score":       self._leg.get("momentum_score", 0.5),
            "timeout_eff_secs":     self._leg.get("timeout_eff_secs"),
            "tp_riding":            self._leg.get("tp_riding", False),
            "tp_ref_pct":           (self._leg.get("_tp_pct")
                                     if self._leg.get("_tp_pct") is not None
                                     else TARGET_LIMIT_PCT),
            "oco_armed":            self._leg.get("oco_armed", False),
            "oco_target":           self._leg.get("oco_target"),
            "oco_floor":            self._leg.get("oco_floor"),
            "brain":                self._brain.state,
            "exit_analyzer":        self._exit_analyzer.state,
            "open":                 self._leg["open"],
            "held_secs":            held,
        }

    def reset(self):
        self._leg                = None
        self._open_time          = None
        self._opt_price_hist     = deque(maxlen=OPTION_ATR_PERIOD + 1)
        self._tick_hist          = deque(maxlen=OCO_LOOKBACK_TICKS)
        self._trail_pct_override = None
        self._partial_done       = []
        self._original_qty       = 1
        self._profit_history     = []
