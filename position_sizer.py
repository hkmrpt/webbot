"""
position_sizer.py — AI position sizing engine (v9.6)
═════════════════════════════════════════════════════

Decides lots per auto trade from the live session context instead of a
static formula. Multiplicative factors on an ATR-risk base, then hard caps
that no factor can override:

  base       — ATR risk sizing: capital × MAX_RISK_PER_TRADE / (ATR × lot),
               already capped by what the capital can afford
  warmup     — the day starts small (SIZER_WARMUP_FACTOR×) and ramps to
               full size over the first SIZER_WARMUP_TRADES trades
  recovery   — consecutive losses size UP (SIZER_RECOVERY_STEP per loss,
               capped at SIZER_RECOVERY_MAX×) to win the day back …
  confidence — the ML entry score scales SIZER_CONF_MIN×–SIZER_CONF_MAX×
  learned    — EWMA multiplier taught by closed trades: a boosted size that
               loses shrinks it, a boosted size that wins grows it

  HARD caps (applied last, in order):
    capital   — never more lots than the capital can buy
    budget    — lots × worst-case-SL-loss must fit inside what is left of
                MAX_DAILY_LOSS. This bounds the recovery martingale by
                design: the sizer can chase losses only inside the daily
                loss budget, never through it.

State is mode-tagged (position_sizer_state_demo.json / _real.json) — demo
fills never train real-money sizing. The replay harness overrides STATE_DIR.
"""

import json
import os
import sys as _sys

from config import (
    MAX_RISK_PER_TRADE, TRADING_MODE,
    SIZER_WARMUP_TRADES, SIZER_WARMUP_FACTOR,
    SIZER_RECOVERY_STEP, SIZER_RECOVERY_MAX,
    SIZER_CONF_MIN, SIZER_CONF_MAX,
    SIZER_LEARN_RATE, SIZER_MULT_MIN, SIZER_MULT_MAX,
    SIZER_STATE_FILE,
)

STATE_DIR = (os.path.dirname(os.path.abspath(_sys.executable))
             if getattr(_sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))


def _state_path(mode: str) -> str:
    base, ext = os.path.splitext(SIZER_STATE_FILE)
    return os.path.join(STATE_DIR, f"{base}_{mode}{ext}")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class PositionSizer:

    def __init__(self, mode: str | None = None):
        self._mode      = mode or TRADING_MODE
        self._mult      = 1.0     # learned size multiplier (EWMA)
        self.n_trades   = 0
        self.wins       = 0
        self._last: dict | None = None   # decision awaiting its outcome
        self._load()

    def set_mode(self, mode: str):
        """Switch demo/real state files (demo fills never train real sizing)."""
        if mode == self._mode:
            return
        self._save()
        self._mode = mode
        self._mult = 1.0
        self.n_trades = 0
        self.wins = 0
        self._last = None
        self._load()

    # ── Decision ──────────────────────────────────────────────────────────

    def decide(self, ctx: dict) -> tuple[int, list[str]]:
        """
        ctx: capital, premium, lot_size, opt_atr, trades_today, loss_streak,
             session_pnl, entry_score (0–1 or None), sl_pct, max_daily_loss.
        Returns (lots, reasons) — reasons explain every factor that moved
        the size away from base, for the entry log and the UI.
        """
        premium  = ctx.get("premium") or 0.0
        lot_size = int(ctx.get("lot_size") or 1)
        capital  = ctx.get("capital") or 0.0
        cost_per_lot = premium * lot_size
        if cost_per_lot <= 0 or capital <= 0:
            return 1, ["no price/capital info — 1 lot"]

        reasons: list[str] = []
        afford = max(1, int(capital / cost_per_lot))

        # Base: ATR risk sizing (1 ATR adverse move ≈ MAX_RISK_PER_TRADE)
        opt_atr = ctx.get("opt_atr") or premium * 0.05
        base = (max(1, int(capital * MAX_RISK_PER_TRADE / (opt_atr * lot_size)))
                if opt_atr > 0 else 1)
        base = min(base, afford)

        # Warmup — the day must earn full size
        trades_today = int(ctx.get("trades_today") or 0)
        if trades_today < SIZER_WARMUP_TRADES:
            wf = (SIZER_WARMUP_FACTOR
                  + (1.0 - SIZER_WARMUP_FACTOR) * trades_today / SIZER_WARMUP_TRADES)
            reasons.append(f"warmup {wf:.2f}× (trade {trades_today + 1} of day)")
        else:
            wf = 1.0

        # Recovery — size up after losses (hard-bounded by the budget cap)
        loss_streak = int(ctx.get("loss_streak") or 0)
        if loss_streak > 0:
            rf = min(1.0 + SIZER_RECOVERY_STEP * loss_streak, SIZER_RECOVERY_MAX)
            reasons.append(f"recovery {rf:.2f}× ({loss_streak} loss streak)")
        else:
            rf = 1.0

        # Confidence — ML entry score (0–1); 0.3→0.8 maps to CONF_MIN→CONF_MAX
        score = ctx.get("entry_score")
        if score is not None:
            t  = _clamp((float(score) - 0.3) / 0.5, 0.0, 1.0)
            cf = round(SIZER_CONF_MIN + t * (SIZER_CONF_MAX - SIZER_CONF_MIN), 2)
            if cf != 1.0:
                reasons.append(f"confidence {cf:.2f}× (score {float(score):.2f})")
        else:
            cf = 1.0

        if self._mult != 1.0:
            reasons.append(f"learned {self._mult:.2f}× ({self.n_trades} trades)")

        lots = max(1, int(round(base * wf * rf * cf * self._mult)))

        # ── HARD caps — no factor above can override these ───────────────
        if lots > afford:
            lots = afford
            reasons.append(f"capital cap {afford} lots (₹{capital:,.0f})")

        max_daily_loss = ctx.get("max_daily_loss") or 0.0
        sl_pct         = ctx.get("sl_pct") or 15.0
        worst_per_lot  = cost_per_lot * sl_pct / 100.0
        if max_daily_loss > 0 and worst_per_lot > 0:
            remaining   = max_daily_loss + min(0.0, ctx.get("session_pnl") or 0.0)
            budget_lots = int(remaining / worst_per_lot)
            if budget_lots >= 1 and lots > budget_lots:
                lots = budget_lots
                reasons.append(
                    f"loss-budget cap {budget_lots} lots (₹{remaining:.0f} left)")
            elif budget_lots < 1:
                lots = 1
                reasons.append("loss budget nearly spent — floor 1 lot")

        self._last = {"lots": lots, "base": base, "boosted": lots > base}
        return lots, reasons

    # ── Learning ──────────────────────────────────────────────────────────

    def on_trade_closed(self, result: dict):
        """Teach the learned multiplier from the sized trade's outcome."""
        if self._last is None:
            return
        if result.get("mode") and result["mode"] != self._mode:
            self._last = None
            return

        pnl = result.get("pnl_total", result.get("pnl", 0.0)) or 0.0
        won = pnl > 0
        self.n_trades += 1
        if won:
            self.wins += 1

        # Only decisions that sized ABOVE base carry sizing risk worth
        # learning from: boosted loss → shrink, boosted win → grow slowly.
        if self._last.get("boosted"):
            if won:
                self._mult *= (1.0 + SIZER_LEARN_RATE * 0.5)
            else:
                self._mult *= (1.0 - SIZER_LEARN_RATE)
            self._mult = round(_clamp(self._mult, SIZER_MULT_MIN, SIZER_MULT_MAX), 4)

        self._last = None
        self._save()

    # ── State / persistence ───────────────────────────────────────────────

    @property
    def state(self) -> dict:
        return {
            "mult":     self._mult,
            "n_trades": self.n_trades,
            "win_rate": round(self.wins / self.n_trades, 3) if self.n_trades else 0.0,
            "mode":     self._mode,
        }

    def _save(self):
        try:
            with open(_state_path(self._mode), "w") as f:
                json.dump({"mult": self._mult, "n_trades": self.n_trades,
                           "wins": self.wins}, f, indent=2)
        except Exception as e:
            print(f"[PositionSizer] save error: {e}")

    def _load(self):
        path = _state_path(self._mode)
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._mult    = float(data.get("mult", 1.0))
            self.n_trades = int(data.get("n_trades", 0))
            self.wins     = int(data.get("wins", 0))
            print(f"[PositionSizer] loaded — mult={self._mult}  "
                  f"trades={self.n_trades}")
        except Exception as e:
            print(f"[PositionSizer] load error (starting fresh): {e}")
