"""
buy_exit_strategy.py  ──  Option Buy Robot v8.3
════════════════════════════════════════════════

Simplified exit engine driven by self-learning ExitBrain.

Three exit layers (in priority order):
  1. Hard SL     — ATR-phase stop loss (safety net, never removed)
  2. Trail stop  — ATR-based high-water mark trail (profit protection)
  3. AI exit     — ExitBrain per-tick policy (self-learning, data-driven)

Hard timeout (120s with < 1% profit) kept as absolute fallback only.
All complex rule-based trails (time trail, profit tier, move-type logic)
removed — the ExitBrain learns these patterns from data instead.
"""

import csv
import os
import threading
from collections import deque
from datetime import datetime

from exit_brain import ExitBrain, MAX_HOLD_SECS
from config import (
    BUY_QTY, BUY_TRAIL_PCT, LOT_SIZE,
    OPTION_ATR_PERIOD,
    PROFIT_LOCK_TIERS,
    TRADE_LOG,
    TRAIL_ATR_HIGH, TRAIL_ATR_LOW,
    TRAIL_PCT_HIGH, TRAIL_PCT_LOW,
    BREAKEVEN_TRIGGER_PCT,
    SL_PHASE1_PCT, SL_PHASE1_SECS, SL_PHASE2_PCT,
)

_csv_lock = threading.Lock()

CSV_FIELDS = [
    "date", "time", "side", "entry", "exit_price",
    "qty", "lot_qty",
    "pnl", "pnl_pct",
    "reason",
    "sl_pct", "trail_pct",
    "option_atr",
    "peak_profit", "peak_profit_pct",
    "held_secs",
    "equity_after",
]


def write_trade_csv(result: dict):
    """Append one trade row to TRADE_LOG. Thread-safe."""
    now = datetime.now()
    row = {
        "date":            now.strftime("%Y-%m-%d"),
        "time":            now.strftime("%H:%M:%S"),
        "side":            result.get("side",            ""),
        "entry":           result.get("entry",           ""),
        "exit_price":      result.get("exit_price",      ""),
        "qty":             result.get("qty",             ""),
        "lot_qty":         (result.get("qty", 0) or 0) * LOT_SIZE,
        "pnl":             result.get("pnl",             ""),
        "pnl_pct":         result.get("pnl_pct",         ""),
        "reason":          result.get("reason",          ""),
        "sl_pct":          result.get("sl_pct",          ""),
        "trail_pct":       result.get("trail_pct",       ""),
        "option_atr":      result.get("option_atr",      ""),
        "peak_profit":     result.get("peak_profit",     ""),
        "peak_profit_pct": result.get("peak_profit_pct", ""),
        "held_secs":       result.get("held_secs",       ""),
        "equity_after":    result.get("equity_after",    ""),
    }
    with _csv_lock:
        is_new = not os.path.exists(TRADE_LOG) or os.path.getsize(TRADE_LOG) == 0
        with open(TRADE_LOG, "a", newline="") as f:
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


def _atr_to_trail_pct(atr: float | None) -> float:
    """Linear interpolation: option ATR → trail %."""
    if atr is None:
        return BUY_TRAIL_PCT
    if atr >= TRAIL_ATR_HIGH:
        return TRAIL_PCT_HIGH
    if atr <= TRAIL_ATR_LOW:
        return TRAIL_PCT_LOW
    t = (atr - TRAIL_ATR_LOW) / (TRAIL_ATR_HIGH - TRAIL_ATR_LOW)
    return round(TRAIL_PCT_LOW + t * (TRAIL_PCT_HIGH - TRAIL_PCT_LOW), 1)


# ── Strategy ──────────────────────────────────────────────────────────────────

class BuyExitStrategy:

    def __init__(self, capital: float):
        self.capital         = capital
        self._leg            = None
        self._open_time      = None
        self._opt_price_hist = deque(maxlen=OPTION_ATR_PERIOD + 1)
        self._brain          = ExitBrain()   # persists weights across trades

    def open_leg(
        self,
        side:            str,
        entry_price:     float,
        sl_pct_override: float | None = None,
        seed_prices:     list  | None = None,
        **_kwargs,
    ) -> dict:
        sl_pct = sl_pct_override if sl_pct_override is not None else SL_PHASE1_PCT
        sl     = round(entry_price * (1 - sl_pct / 100), 2)

        self._brain.reset()   # clear tick buffer + momentum for new trade
        self._opt_price_hist = deque(maxlen=OPTION_ATR_PERIOD + 1)
        if seed_prices:
            for p in seed_prices[-(OPTION_ATR_PERIOD + 1):]:
                self._opt_price_hist.append(p)
        self._opt_price_hist.append(entry_price)

        self._leg = {
            "side":            side,
            "entry":           entry_price,
            "qty":             BUY_QTY,
            "sl":              sl,
            "sl_pct":          sl_pct,
            "trail_pct":       BUY_TRAIL_PCT,
            "option_atr":      None,
            "peak_price":      entry_price,
            "peak_profit":     0.0,
            "peak_profit_pct": 0.0,
            "trail_price":     None,
            "phase2":          False,
            "breakeven_moved": False,
            "momentum_score":  0.5,
            "exit_score":      0.0,
            "open":            True,
        }
        self._open_time = datetime.now()
        return {"sl": sl, "qty": BUY_QTY, "sl_pct": sl_pct, "trail_pct": BUY_TRAIL_PCT}

    def on_price(self, price: float, elapsed: float = 0.0, regime: str = "unknown"):
        if not self._leg or not self._leg["open"]:
            return None

        leg      = self._leg
        qty_full = leg["qty"] * LOT_SIZE

        # ── Update price history + ATR ────────────────────────────────────────
        self._opt_price_hist.append(price)
        atr              = _compute_option_atr(list(self._opt_price_hist))
        leg["option_atr"] = atr

        # ── Track peak ────────────────────────────────────────────────────────
        if price > leg["peak_price"]:
            leg["peak_price"]      = price
            leg["peak_profit"]     = round((price - leg["entry"]) * qty_full, 2)
            leg["peak_profit_pct"] = round((price - leg["entry"]) / leg["entry"] * 100, 2)

        if not leg["phase2"] and price > leg["entry"]:
            leg["phase2"] = True

        # ── Phase SL tightening (15% → 8% after 30s) ─────────────────────────
        if elapsed >= SL_PHASE1_SECS and leg["sl_pct"] > SL_PHASE2_PCT:
            new_sl = round(leg["entry"] * (1 - SL_PHASE2_PCT / 100), 2)
            if new_sl > leg["sl"]:
                leg["sl"]     = new_sl
                leg["sl_pct"] = SL_PHASE2_PCT

        peak_pct = leg["peak_profit_pct"]

        # ── Breakeven ─────────────────────────────────────────────────────────
        if not leg["breakeven_moved"] and peak_pct >= BREAKEVEN_TRIGGER_PCT:
            leg["sl"]             = max(leg["sl"], leg["entry"])
            leg["breakeven_moved"] = True

        # ── Profit-lock SL (steps SL above entry at milestones) ───────────────
        if PROFIT_LOCK_TIERS:
            for min_pct, lock_pct in sorted(PROFIT_LOCK_TIERS, key=lambda x: x[0], reverse=True):
                if peak_pct >= min_pct:
                    locked = round(leg["entry"] * (1 + lock_pct / 100), 2)
                    leg["sl"] = max(leg["sl"], locked)
                    break

        # ── ATR-based trail stop (high-water mark) ────────────────────────────
        trail_pct        = _atr_to_trail_pct(atr)
        leg["trail_pct"] = trail_pct

        if leg["phase2"]:
            raw_trail  = leg["peak_price"] * (1 - trail_pct / 100)
            new_trail  = round(max(raw_trail, leg["entry"]), 2)
            prev_trail = leg["trail_price"] or leg["entry"]
            leg["trail_price"] = max(new_trail, prev_trail)

        # ── Build ExitBrain state + record this tick ──────────────────────────
        state = self._brain.build_state(
            price, leg["entry"], leg["peak_price"],
            elapsed, atr, regime, self._opt_price_hist,
        )
        leg["momentum_score"] = state[2]   # momentum is feature index 2
        leg["exit_score"]     = self._brain.exit_score(state)
        self._brain.record(state, price)

        current_pct = (price - leg["entry"]) / leg["entry"] * 100

        # ── Exit checks ───────────────────────────────────────────────────────
        reason = None
        if price <= leg["sl"]:
            reason = "sl"
        elif leg["phase2"] and leg["trail_price"] and price <= leg["trail_price"]:
            reason = "trail"
        elif elapsed >= MAX_HOLD_SECS and current_pct < 1.0:
            reason = "timeout"   # hard safety net only
        elif self._brain.should_exit(state, current_pct):
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
            "side":            leg["side"],
            "entry":           leg["entry"],
            "exit_price":      exit_price,
            "qty":             leg["qty"],
            "pnl":             pnl,
            "pnl_pct":         pnl_pct,
            "reason":          reason,
            "equity_after":    self.capital,
            "sl_pct":          leg["sl_pct"],
            "trail_pct":       leg["trail_pct"],
            "option_atr":      leg["option_atr"],
            "peak_profit":     leg["peak_profit"],
            "peak_profit_pct": leg["peak_profit_pct"],
            "held_secs":       held_secs,
        }

        try:
            write_trade_csv(result)
        except Exception as e:
            print(f"[CSV] write error: {e}")

        # Retrospective training: label every recorded tick and learn
        self._brain.learn()

        return result

    def leg_snapshot(self) -> dict:
        if not self._leg:
            return {}
        held = 0
        if self._open_time:
            held = int((datetime.now() - self._open_time).total_seconds())
        return {
            "side":            self._leg["side"],
            "entry":           self._leg["entry"],
            "sl":              self._leg["sl"],
            "sl_pct":          self._leg["sl_pct"],
            "trail_pct":       self._leg["trail_pct"],
            "option_atr":      self._leg["option_atr"],
            "qty":             self._leg["qty"],
            "peak_price":      self._leg["peak_price"],
            "peak_profit":     self._leg["peak_profit"],
            "peak_profit_pct": self._leg["peak_profit_pct"],
            "trail_price":     self._leg["trail_price"],
            "phase2":          self._leg["phase2"],
            "breakeven_moved": self._leg["breakeven_moved"],
            "momentum_score":  self._leg.get("momentum_score", 0.5),
            "exit_score":      self._leg.get("exit_score",     0.0),
            "brain":           self._brain.state,
            "open":            self._leg["open"],
            "held_secs":       held,
        }

    def reset(self):
        self._leg            = None
        self._open_time      = None
        self._opt_price_hist = deque(maxlen=OPTION_ATR_PERIOD + 1)
