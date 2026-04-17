"""
engine/trade_manager.py  ──  Trade Lifecycle Manager
════════════════════════════════════════════════════
Manages one open option leg with 8 exit mechanisms (priority order):

  1. Hard SL         — Phase 1 (wide) → Phase 2 (tight after 30 s)
  2. Profit-lock SL  — SL moves above entry at profit milestones
  3. Breakeven       — SL moves to entry once peak profit ≥ 3%
  4. Target          — Fixed TP if set
  5. Trail stop      — ATR-adaptive high-water mark trail
  6. Momentum decay  — Price falling from peak for N ticks + low velocity
  7. Timeout         — Hard safety: held > 120 s with < 1% profit
  8. Force close     — Manual / force-exit / NIFTY reversal
"""

import csv
import os
import threading
from collections import deque
from datetime import datetime
from typing import Optional

from config import LOT_SIZE, TRADE_LOG

_csv_lock = threading.Lock()

_CSV_FIELDS = [
    "date", "time", "strategy", "side",
    "entry", "exit_price", "qty", "lot_qty",
    "pnl", "pnl_pct",
    "reason", "sl_pct", "trail_pct",
    "peak_profit_pct", "held_secs", "equity_after",
]


def _write_csv(row: dict):
    """Thread-safe CSV append."""
    with _csv_lock:
        is_new = not os.path.exists(TRADE_LOG) or os.path.getsize(TRADE_LOG) == 0
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            if is_new:
                w.writeheader()
            w.writerow(row)


# Profit-lock milestones: (peak_pct_trigger, sl_moves_to_above_entry_pct)
_DEFAULT_PROFIT_LOCKS = [
    (10.0,  3.0),
    (20.0,  8.0),
    (30.0, 15.0),
]

# ATR ↔ trail % interpolation
_TRAIL_ATR_LOW   = 2.0
_TRAIL_ATR_HIGH  = 5.0
_TRAIL_PCT_LOW   = 12.0
_TRAIL_PCT_HIGH  = 30.0


def _atr_to_trail_pct(atr: Optional[float]) -> float:
    if atr is None:
        return 20.0
    if atr >= _TRAIL_ATR_HIGH:
        return _TRAIL_PCT_HIGH
    if atr <= _TRAIL_ATR_LOW:
        return _TRAIL_PCT_LOW
    t = (atr - _TRAIL_ATR_LOW) / (_TRAIL_ATR_HIGH - _TRAIL_ATR_LOW)
    return round(_TRAIL_PCT_LOW + t * (_TRAIL_PCT_HIGH - _TRAIL_PCT_LOW), 1)


class TradeManager:
    """Manages one open option trade. Create a new instance per trade."""

    def __init__(self,
                 side:          str,
                 entry_price:   float,
                 qty_lots:      int,
                 strategy_name: str,
                 sl_pct:        float = 12.0,
                 target_pct:    float = 0.0,    # 0 = no fixed target
                 capital:       float = 100_000.0):

        self.side          = side
        self.entry         = entry_price
        self.qty           = qty_lots
        self.strategy_name = strategy_name
        self.capital       = capital

        # SL
        self.sl_pct        = sl_pct
        self.sl            = round(entry_price * (1 - sl_pct / 100), 2)

        # Target
        self.target        = round(entry_price * (1 + target_pct / 100), 2) if target_pct > 0 else None

        # Trail (starts at SL level)
        self.trail_pct     = 20.0
        self.trail_price   = self.sl

        # Phase-2 SL
        self._phase2_sl_pct  = 8.0
        self._phase2_secs    = 30
        self._phase2         = False

        # Peak tracking
        self.peak_price    = entry_price
        self.current_price = entry_price

        # State flags
        self._breakeven    = False
        self._profit_locks = _DEFAULT_PROFIT_LOCKS

        # Momentum decay tracking
        self._decay_count    = 0
        self._decay_window   = 6
        self._price_buf: deque = deque(maxlen=8)
        self._velocity       = 0.0

        self.open_ts  = datetime.now()
        self.closed   = False

    # ── Per-tick update ───────────────────────────────────────

    def on_price(self,
                 price:    float,
                 opt_atr:  Optional[float] = None,
                 elapsed:  Optional[float] = None) -> Optional[dict]:
        """
        Call every tick the trade is open.
        Returns exit dict on any exit trigger, else None.
        """
        if self.closed:
            return None

        self.current_price = price
        elapsed = elapsed if elapsed is not None else self._held_secs()

        # Velocity
        self._price_buf.append(price)
        if len(self._price_buf) >= 2:
            pts = list(self._price_buf)
            self._velocity = sum(abs(pts[i] - pts[i-1]) for i in range(1, len(pts))) / (len(pts) - 1)

        # Peak update
        if price > self.peak_price:
            self.peak_price = price
            self._decay_count = 0
        else:
            self._decay_count += 1

        # --- Exit checks (priority order) ---

        # 1. Phase-2 SL tightening
        if not self._phase2 and elapsed >= self._phase2_secs:
            self._phase2 = True
            new_sl = round(self.entry * (1 - self._phase2_sl_pct / 100), 2)
            if new_sl > self.sl:
                self.sl     = new_sl
                self.sl_pct = self._phase2_sl_pct

        # 2. Profit-lock SL
        peak_pct = self._peak_pct()
        for trigger, lock in sorted(_DEFAULT_PROFIT_LOCKS, key=lambda x: x[0], reverse=True):
            if peak_pct >= trigger:
                locked = round(self.entry * (1 + lock / 100), 2)
                if locked > self.sl:
                    self.sl = locked
                break

        # 3. Breakeven
        if not self._breakeven and peak_pct >= 3.0:
            be = max(self.sl, self.entry)
            self.sl        = be
            self._breakeven = True

        # 4. Hard SL hit
        if price <= self.sl:
            return self._close(price, "sl", opt_atr)

        # 5. Target hit
        if self.target and price >= self.target:
            return self._close(price, "target", opt_atr)

        # 6. Trail stop
        profit_pct = self._profit_pct()
        if profit_pct >= 5.0:
            self.trail_pct   = _atr_to_trail_pct(opt_atr)
            new_trail        = round(self.peak_price * (1 - self.trail_pct / 100), 2)
            new_trail        = max(new_trail, self.entry)  # never trail below entry after BE
            self.trail_price = max(self.trail_price, new_trail)
            if price <= self.trail_price:
                return self._close(price, "trail", opt_atr)

        # 7. Momentum decay
        if peak_pct >= 6.0 and self._decay_count >= self._decay_window and self._velocity < 0.4:
            return self._close(price, "momentum_decay", opt_atr)

        # 8. Hard timeout
        if elapsed >= 120 and profit_pct < 1.0:
            return self._close(price, "timeout", opt_atr)

        return None

    def force_close(self, price: float, reason: str = "manual",
                    opt_atr: Optional[float] = None) -> dict:
        if not self.closed:
            self.current_price = price
            return self._close(price, reason, opt_atr)
        return self._make_result(price, reason, opt_atr)

    # ── Internal ──────────────────────────────────────────────

    def _profit_pct(self) -> float:
        return (self.current_price - self.entry) / self.entry * 100

    def _peak_pct(self) -> float:
        return (self.peak_price - self.entry) / self.entry * 100

    def _held_secs(self) -> float:
        return (datetime.now() - self.open_ts).total_seconds()

    def _close(self, price: float, reason: str,
               opt_atr: Optional[float]) -> dict:
        self.closed = True
        result = self._make_result(price, reason, opt_atr)
        pnl    = result["pnl"]
        self.capital = round(self.capital + pnl, 2)
        result["equity_after"] = self.capital
        self._write_csv_safe(result)
        return result

    def _make_result(self, price: float, reason: str,
                     opt_atr: Optional[float]) -> dict:
        pnl     = round((price - self.entry) * self.qty * LOT_SIZE, 2)
        pnl_pct = round((price - self.entry) / self.entry * 100, 2)
        return {
            "strategy":        self.strategy_name,
            "side":            self.side,
            "entry":           self.entry,
            "exit_price":      price,
            "qty":             self.qty,
            "pnl":             pnl,
            "pnl_pct":         pnl_pct,
            "reason":          reason,
            "sl_pct":          self.sl_pct,
            "trail_pct":       self.trail_pct,
            "peak_price":      self.peak_price,
            "peak_profit_pct": round(self._peak_pct(), 2),
            "held_secs":       int(self._held_secs()),
            "equity_after":    self.capital,
        }

    def _write_csv_safe(self, result: dict):
        now = datetime.now()
        row = {
            "date":            now.strftime("%Y-%m-%d"),
            "time":            now.strftime("%H:%M:%S"),
            "strategy":        result.get("strategy", ""),
            "side":            result.get("side", ""),
            "entry":           result.get("entry", ""),
            "exit_price":      result.get("exit_price", ""),
            "qty":             result.get("qty", ""),
            "lot_qty":         (result.get("qty") or 0) * LOT_SIZE,
            "pnl":             result.get("pnl", ""),
            "pnl_pct":         result.get("pnl_pct", ""),
            "reason":          result.get("reason", ""),
            "sl_pct":          result.get("sl_pct", ""),
            "trail_pct":       result.get("trail_pct", ""),
            "peak_profit_pct": result.get("peak_profit_pct", ""),
            "held_secs":       result.get("held_secs", ""),
            "equity_after":    result.get("equity_after", ""),
        }
        try:
            _write_csv(row)
        except Exception as e:
            print(f"[CSV] {e}")

    # ── Snapshot ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "strategy":        self.strategy_name,
            "side":            self.side,
            "entry":           self.entry,
            "sl":              round(self.sl, 2),
            "sl_pct":          self.sl_pct,
            "target":          round(self.target, 2) if self.target else None,
            "trail_price":     round(self.trail_price, 2),
            "trail_pct":       self.trail_pct,
            "peak_price":      round(self.peak_price, 2),
            "peak_profit_pct": round(self._peak_pct(), 2),
            "profit_pct":      round(self._profit_pct(), 2),
            "held_secs":       int(self._held_secs()),
            "phase2":          self._phase2,
            "breakeven":       self._breakeven,
            "velocity":        round(self._velocity, 3),
        }
