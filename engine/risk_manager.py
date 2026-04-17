"""
engine/risk_manager.py  ──  Risk Management
════════════════════════════════════════════
Handles:
  1. Position sizing — ATR-based fractional Kelly
     Risk X% of capital per trade; ATR determines SL width in points.
  2. Session limits  — max loss %, profit target %, max trades
  3. Dynamic SL and target calculation from ATR + regime
"""

from typing import Tuple, Optional


class RiskManager:

    def __init__(self,
                 capital:      float = 100_000.0,
                 risk_pct:     float = 1.0,     # % of capital risked per trade
                 lot_size:     int   = 75,
                 max_loss_pct: float = 3.0,      # stop if session P&L ≤ -this%
                 max_profit_pct: float = 8.0,    # stop if session P&L ≥ +this%
                 max_trades:   int   = 15):
        self.capital        = capital
        self.risk_pct       = risk_pct
        self.lot_size       = lot_size
        self.max_loss_pct   = max_loss_pct
        self.max_profit_pct = max_profit_pct
        self.max_trades     = max_trades

    # ── Position sizing ───────────────────────────────────────

    def size_trade(self,
                   option_price: float,
                   sl_pct:       float) -> Tuple[int, float]:
        """
        Returns (qty_lots, risk_amount_inr).

        Logic:
          risk_amount = capital × risk_pct / 100
          sl_pts      = option_price × sl_pct / 100
          max_units   = risk_amount / sl_pts
          qty_lots    = floor(max_units / lot_size), minimum 1
        """
        risk_inr = self.capital * (self.risk_pct / 100)
        sl_pts   = option_price * (sl_pct / 100)
        if sl_pts <= 0:
            return 1, risk_inr

        max_units = risk_inr / sl_pts
        lots      = max(1, int(max_units / self.lot_size))
        actual    = round(lots * self.lot_size * sl_pts, 2)
        return lots, actual

    # ── Dynamic SL from option ATR ────────────────────────────

    def atr_sl_pct(self,
                   option_price: float,
                   option_atr:   Optional[float],
                   multiplier:   float = 2.0,
                   fallback_pct: float = 12.0) -> float:
        """Convert option ATR to SL % (option_atr × mult / price × 100)."""
        if option_atr and option_price > 0:
            pct = (option_atr * multiplier / option_price) * 100
            return round(max(5.0, min(20.0, pct)), 1)
        return fallback_pct

    # ── Dynamic target R:R ────────────────────────────────────

    def target_pct(self,
                   sl_pct:     float,
                   confidence: float,
                   regime:     str) -> float:
        """Minimum R:R = 2; scales to 3 at high confidence / trending."""
        rr = 2.0
        if confidence >= 0.80:
            rr = 2.8
        if regime in ("trending_up", "trending_dn"):
            rr *= 1.2
        elif regime == "volatile":
            rr *= 1.1
        return round(sl_pct * rr, 1)

    # ── Session gate ──────────────────────────────────────────

    def can_trade(self,
                  session_pnl: float,
                  trades_today: int) -> Tuple[bool, str]:
        if trades_today >= self.max_trades:
            return False, f"Max trades {self.max_trades} reached"
        if session_pnl <= -(self.capital * self.max_loss_pct / 100):
            return False, f"Daily loss limit hit"
        if session_pnl >= (self.capital * self.max_profit_pct / 100):
            return False, f"Daily profit target hit"
        return True, ""

    def update_capital(self, new_capital: float):
        self.capital = new_capital

    @property
    def state(self) -> dict:
        return {
            "capital":        self.capital,
            "risk_pct":       self.risk_pct,
            "max_loss_pct":   self.max_loss_pct,
            "max_profit_pct": self.max_profit_pct,
            "max_trades":     self.max_trades,
        }
