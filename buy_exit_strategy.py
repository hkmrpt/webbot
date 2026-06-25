"""
buy_exit_strategy.py  ──  Option Buy Robot v8.2
════════════════════════════════════════════════

Trail logic:
  - ATR trail:      linear interpolation ATR_LOW/HIGH → PCT_LOW/HIGH
  - Profit tier:    PROPORTIONAL — small profit = tight trail, large profit = wider trail
                    activates once PEAK profit ≥ threshold
  - Time trail:     starts at TRAIL_TIME_START_PCT, tightens by TRAIL_TIME_TIGHTEN_STEP
                    every TRAIL_TIME_TIGHTEN_SECS after profit threshold hit
  - Combined:       min(atr_trail, tier_trail, time_trail)
  - Ratchet:        disabled — trail widens intentionally as profit grows
  - Trail price:    high-water mark, never goes backward
  - Floor:          trail price always ≥ entry price
  - Breakeven:      SL moved to entry once peak profit ≥ BREAKEVEN_TRIGGER_PCT
  - Profit-lock SL: SL stepped above entry at key profit milestones (PROFIT_LOCK_TIERS)
"""

import csv
import os
import threading
from collections import deque
from datetime import datetime

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
    SLOW_MOVE_TRAIL_CAP, FAST_MOVE_TRAIL_FLOOR,
    SLOW_MOVE_TIMEOUT_SECS, SLOW_MOVE_MIN_PROFIT,
    FAST_MOVE_TIMEOUT_SECS, FAST_MOVE_MIN_PROFIT,
    PARTIAL_BOOKING_ENABLED, PARTIAL_BOOKING_TARGETS,
    VOLUME_DRYUP_EXIT, VOLUME_DRYUP_RATIO, VOLUME_DRYUP_MIN_PROFIT,
    MOMENTUM_STALL_EXIT, MOMENTUM_STALL_TICKS, MOMENTUM_STALL_MIN_PROFIT,
)

_csv_lock = threading.Lock()

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
]


def write_trade_csv(result: dict):
    """Append one trade row to TRADE_LOG. Thread-safe."""
    path = TRADE_LOG
    now  = datetime.now()
    row  = {
        "date":                 now.strftime("%Y-%m-%d"),
        "time":                 now.strftime("%H:%M:%S"),
        "symbol":               result.get("symbol",           ""),
        "option_symbol":        result.get("option_symbol",    ""),
        "side":                 result.get("side",             ""),
        "entry":                result.get("entry",            ""),
        "exit_price":           result.get("exit_price",       ""),
        "qty":                  result.get("qty",              ""),
        "lot_qty":              (result.get("qty", 0) or 0) * LOT_SIZE,
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
    }
    with _csv_lock:
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


# ── Strategy class ────────────────────────────────────────────────────────────
class BuyExitStrategy:

    def __init__(self, capital: float):
        self.capital             = capital
        self._leg                = None
        self._open_time          = None
        self._opt_price_hist     = deque(maxlen=OPTION_ATR_PERIOD + 1)
        self._trail_pct_override = None
        self._brain              = ExitBrain()          # AI brain — persists across trades
        self._exit_analyzer      = ExitAnalyzer()      # 10-signal exit analysis engine
        self._partial_done: list = []                   # indices of partial targets already booked
        self._original_qty: int  = 1                    # qty at open (before any partial sells)
        self._no_new_high_count: int = 0                # ticks since last new high
        self._profit_history: list = []                 # profit % each tick for decay detection

    def open_leg(
        self,
        side:                    str,
        entry_price:             float,
        sl_pct_override:         float | None = None,
        trail_pct_override:      float | None = None,
        sl_pct_p2_override:      float | None = None,
        sl_phase1_secs_override: float | None = None,
        timeout_secs_override:   float | None = None,
        seed_prices:             list  | None = None,
        symbol:                  str         = "",
        option_symbol:           str         = "",
        qty_override:            int  | None = None,
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

        qty = qty_override if (qty_override is not None and qty_override >= 1) else BUY_QTY
        self._partial_done  = []
        self._original_qty  = qty
        self._no_new_high_count = 0
        self._profit_history = []

        self._leg = {
            "side":                 side,
            "symbol":               symbol,
            "option_symbol":        option_symbol,
            "entry":                entry_price,
            "qty":                  qty,
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
            "open":                 True,
        }
        self._open_time = datetime.now()

        return {"sl": sl, "qty": qty, "sl_pct": sl_pct, "trail_pct": init_trail}

    def on_price(self, price: float):
        if not self._leg or not self._leg["open"]:
            return None

        leg      = self._leg
        qty_full = leg["qty"] * LOT_SIZE
        elapsed  = (datetime.now() - self._open_time).total_seconds() if self._open_time else 0

        self._opt_price_hist.append(price)

        atr               = _compute_option_atr(list(self._opt_price_hist))
        leg["option_atr"] = atr

        atr_trail            = _atr_to_trail_pct(atr, self._trail_pct_override)
        leg["atr_trail_pct"] = atr_trail

        if price > leg["peak_price"]:
            leg["peak_price"]      = price
            leg["peak_profit"]     = round((price - leg["entry"]) * qty_full, 2)
            leg["peak_profit_pct"] = round((price - leg["entry"]) / leg["entry"] * 100, 2)
            self._no_new_high_count = 0  # reset on new high
        else:
            self._no_new_high_count += 1

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

        if TRAIL_RATCHET_ENABLED:
            combined = round(min(combined, leg["min_trail_pct_reached"]), 1)
            leg["min_trail_pct_reached"] = combined

        # ── AI Brain: adaptive trail ────────────────────────────────────────
        # Brain uses momentum score + self-learned multiplier to widen/tighten.
        # High momentum → wider trail (let it run).
        # Low momentum  → tight trail (protect gains fast).
        combined         = self._brain.adaptive_trail(combined, momentum_score)
        leg["trail_pct"] = combined

        if leg["phase2"]:
            raw_trail  = leg["peak_price"] * (1 - leg["trail_pct"] / 100)
            new_trail  = round(max(raw_trail, leg["entry"]), 2)
            prev_trail = leg["trail_price"] or leg["entry"]
            leg["trail_price"] = max(new_trail, prev_trail)

        # Exit checks
        # Per-stock timeout override, or default move-type based timeout
        if leg["_timeout_secs"] is not None:
            timeout_secs   = leg["_timeout_secs"]
            min_profit_pct = SLOW_MOVE_MIN_PROFIT    # keep profit floor
        else:
            timeout_secs   = FAST_MOVE_TIMEOUT_SECS if move_type == "fast" else SLOW_MOVE_TIMEOUT_SECS
            min_profit_pct = FAST_MOVE_MIN_PROFIT    if move_type == "fast" else SLOW_MOVE_MIN_PROFIT

        # ── Partial profit booking ──────────────────────────────────────────────
        # Check before full exit — book a fraction at profit milestones,
        # then continue trailing the rest with a tightened SL.
        if PARTIAL_BOOKING_ENABLED and leg["qty"] > 1:
            for i, (target_pct, fraction) in enumerate(PARTIAL_BOOKING_TARGETS):
                if i not in self._partial_done and peak_pct >= target_pct:
                    sell_qty = max(1, round(leg["qty"] * fraction))
                    # Keep at least 1 lot running
                    if sell_qty < leg["qty"]:
                        self._partial_done.append(i)
                        leg["qty"] -= sell_qty
                        # After partial, tighten SL to lock in the gained profit
                        lock_pct     = max(BREAKEVEN_TRIGGER_PCT, target_pct * 0.40)
                        locked_sl    = round(leg["entry"] * (1 + lock_pct / 100), 2)
                        leg["sl"]    = max(leg["sl"], locked_sl)
                        return {
                            "event_type":      "partial",
                            "partial_qty":     sell_qty,
                            "remaining_qty":   leg["qty"],
                            "price":           price,
                            "peak_profit_pct": peak_pct,
                            "reason":          f"partial_{i + 1}",
                            "side":            leg["side"],
                            "symbol":          leg.get("symbol", ""),
                            "option_symbol":   leg.get("option_symbol", ""),
                            "entry":           leg["entry"],
                            "new_sl":          leg["sl"],
                        }

        reason = None

        # Track profit history for decay detection
        self._profit_history.append(current_pct)
        if len(self._profit_history) > 30:
            self._profit_history = self._profit_history[-30:]

        # ── 10-Signal AI Exit Analyzer ──────────────────────────────────
        # Runs all advanced exit signals: volatility spike, reversal patterns,
        # momentum collapse, profit decay, gamma trap, cascade risk, etc.
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
            reason = "ai_analyzer"
            leg["_ai_exit_detail"] = ai_exit_reason

        # ── Core exit checks (SL, trail, timeout) ──────────────────────
        if reason is None and price <= leg["sl"]:
            reason = "sl"
        elif reason is None and leg["phase2"] and leg["trail_price"] and price <= leg["trail_price"]:
            reason = "trail"
        elif reason is None and elapsed >= timeout_secs:
            if current_pct < min_profit_pct:
                reason = "timeout"
        elif reason is None and self._brain.check_ai_exit(current_pct, momentum_score):
            reason = "ai_exit"

        if reason:
            return self._close(price, reason)
        return None

    def force_close(self, price: float, reason: str = "manual"):
        if not self._leg or not self._leg["open"]:
            return None
        return self._close(price, reason)

    def _close(self, exit_price: float, reason: str) -> dict:
        leg      = self._leg
        qty_full = leg["qty"] * LOT_SIZE
        pnl      = round((exit_price - leg["entry"]) * qty_full, 2)
        pnl_pct  = round((exit_price - leg["entry"]) / leg["entry"] * 100, 2)

        self.capital = round(self.capital + pnl, 2)
        leg["open"]  = False

        held_secs = 0
        if self._open_time:
            held_secs = int((datetime.now() - self._open_time).total_seconds())

        result = {
            "side":                 leg["side"],
            "symbol":               leg.get("symbol", ""),
            "option_symbol":        leg.get("option_symbol", ""),
            "entry":                leg["entry"],
            "exit_price":           exit_price,
            "qty":                  leg["qty"],
            "pnl":                  pnl,
            "pnl_pct":              pnl_pct,
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
            held = int((datetime.now() - self._open_time).total_seconds())
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
            "momentum_score":       self._leg.get("momentum_score", 0.5),
            "brain":                self._brain.state,
            "exit_analyzer":        self._exit_analyzer.state,
            "open":                 self._leg["open"],
            "held_secs":            held,
        }

    def reset(self):
        self._leg                = None
        self._open_time          = None
        self._opt_price_hist     = deque(maxlen=OPTION_ATR_PERIOD + 1)
        self._trail_pct_override = None
        self._partial_done       = []
        self._original_qty       = 1
        self._no_new_high_count  = 0
        self._profit_history     = []
