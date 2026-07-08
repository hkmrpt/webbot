"""
performance_analytics.py  ──  Option Buy Robot v9
══════════════════════════════════════════════════

Reads trade_log.csv and computes comprehensive performance metrics:
  - Equity curve
  - Sharpe ratio, Sortino ratio
  - Max drawdown, profit factor
  - Win rate by hour, by day type, by exit reason
  - Average winner vs average loser
  - Streak analysis
  - Best/worst trading hours
"""

import csv
import os
import math
import sys
from datetime import datetime
from collections import defaultdict

# Anchored next to the module (or exe when frozen) — a bare relative path
# silently read from whatever cwd the process happened to have.
_BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
             if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
TRADE_LOG = os.path.join(_BASE_DIR, "trade_log.csv")


def load_trades() -> list:
    """Load all trades from CSV."""
    if not os.path.exists(TRADE_LOG):
        return []
    trades = []
    try:
        with open(TRADE_LOG, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    pnl = float(row.get("pnl", 0) or 0)
                    pnl_pct = float(row.get("pnl_pct", 0) or 0)
                    entry = float(row.get("entry", 0) or 0)
                    exit_p = float(row.get("exit_price", 0) or 0)
                    held = int(float(row.get("held_secs", 0) or 0))
                    equity = float(row.get("equity_after", 0) or 0)
                    trades.append({
                        "date": row.get("date", ""),
                        "time": row.get("time", ""),
                        "side": row.get("side", ""),
                        "entry": entry,
                        "exit": exit_p,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "reason": row.get("reason", ""),
                        "held_secs": held,
                        "equity": equity,
                        "symbol": row.get("option_symbol", ""),
                        "peak_pct": float(row.get("peak_profit_pct", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue
    except Exception:
        pass
    return trades


def compute_analytics(trades: list, initial_capital: float = 10000) -> dict:
    """Compute full performance analytics from trade list."""
    if not trades:
        return {"total_trades": 0, "error": "No trades"}

    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    pnl_pcts = [t["pnl_pct"] for t in trades]
    wins = [t for t in trades if t["pnl"] >= 0]
    losses = [t for t in trades if t["pnl"] < 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / n if n > 0 else 0

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    avg_win_pct = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss_pct = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    # Profit factor = gross profit / gross loss
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

    # Equity curve
    equity_curve = [initial_capital]
    for t in trades:
        equity_curve.append(equity_curve[-1] + t["pnl"])

    # Max drawdown
    peak = initial_capital
    max_dd = 0
    max_dd_pct = 0
    dd_start = 0
    dd_end = 0
    for i, eq in enumerate(equity_curve):
        if eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    # Sharpe ratio (annualized, assuming 250 trading days, ~5 trades/day)
    if len(pnl_pcts) >= 2:
        mean_r = sum(pnl_pcts) / len(pnl_pcts)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in pnl_pcts) / (len(pnl_pcts) - 1))
        sharpe = (mean_r / std_r) * math.sqrt(250 * 5) if std_r > 0 else 0
    else:
        sharpe = 0

    # Sortino ratio (only downside deviation)
    neg_returns = [r for r in pnl_pcts if r < 0]
    if neg_returns and len(pnl_pcts) >= 2:
        mean_r = sum(pnl_pcts) / len(pnl_pcts)
        downside_dev = math.sqrt(sum(r ** 2 for r in neg_returns) / len(neg_returns))
        sortino = (mean_r / downside_dev) * math.sqrt(250 * 5) if downside_dev > 0 else 0
    else:
        sortino = 0

    # Expectancy = (win_rate × avg_win) - (loss_rate × avg_loss)
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))

    # Streak analysis
    max_win_streak = 0
    max_loss_streak = 0
    curr_win = 0
    curr_loss = 0
    for t in trades:
        if t["pnl"] >= 0:
            curr_win += 1
            curr_loss = 0
            max_win_streak = max(max_win_streak, curr_win)
        else:
            curr_loss += 1
            curr_win = 0
            max_loss_streak = max(max_loss_streak, curr_loss)

    # By hour
    by_hour = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in trades:
        try:
            h = int(t["time"].split(":")[0])
        except (ValueError, IndexError):
            continue
        by_hour[h]["trades"] += 1
        by_hour[h]["pnl"] += t["pnl"]
        if t["pnl"] >= 0:
            by_hour[h]["wins"] += 1

    hourly = {}
    for h in sorted(by_hour):
        d = by_hour[h]
        hourly[f"{h:02d}:00"] = {
            "trades": d["trades"],
            "win_rate": round(d["wins"] / d["trades"], 2) if d["trades"] else 0,
            "pnl": round(d["pnl"], 2),
        }

    # By exit reason
    by_reason = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in trades:
        r = t.get("reason", "unknown")
        by_reason[r]["trades"] += 1
        by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] >= 0:
            by_reason[r]["wins"] += 1

    reasons = {}
    for r, d in by_reason.items():
        reasons[r] = {
            "trades": d["trades"],
            "win_rate": round(d["wins"] / d["trades"], 2) if d["trades"] else 0,
            "pnl": round(d["pnl"], 2),
        }

    # By date
    by_date = defaultdict(lambda: {"trades": 0, "pnl": 0})
    for t in trades:
        by_date[t["date"]]["trades"] += 1
        by_date[t["date"]]["pnl"] += t["pnl"]

    daily = {d: {"trades": v["trades"], "pnl": round(v["pnl"], 2)} for d, v in sorted(by_date.items())}

    # By side
    ce_trades = [t for t in trades if t["side"] == "CE"]
    pe_trades = [t for t in trades if t["side"] == "PE"]

    # Average hold time
    avg_held = sum(t["held_secs"] for t in trades) / n if n else 0
    avg_held_wins = sum(t["held_secs"] for t in wins) / len(wins) if wins else 0
    avg_held_losses = sum(t["held_secs"] for t in losses) / len(losses) if losses else 0

    # Average peak profit (how much we capture vs how much was available)
    peaks = [t["peak_pct"] for t in trades if t["peak_pct"] > 0]
    finals = [t["pnl_pct"] for t in trades if t["peak_pct"] > 0]
    if peaks and finals:
        capture_ratio = sum(finals) / sum(peaks) if sum(peaks) > 0 else 0
    else:
        capture_ratio = 0

    return {
        "total_trades": n,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 3),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_held_secs": round(avg_held),
        "avg_held_wins": round(avg_held_wins),
        "avg_held_losses": round(avg_held_losses),
        "capture_ratio": round(capture_ratio, 3),
        "equity_curve": [round(e, 2) for e in equity_curve],
        "by_hour": hourly,
        "by_reason": reasons,
        "by_date": daily,
        "ce_trades": len(ce_trades),
        "ce_win_rate": round(sum(1 for t in ce_trades if t["pnl"] >= 0) / len(ce_trades), 2) if ce_trades else 0,
        "pe_trades": len(pe_trades),
        "pe_win_rate": round(sum(1 for t in pe_trades if t["pnl"] >= 0) / len(pe_trades), 2) if pe_trades else 0,
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "current_equity": round(equity_curve[-1], 2),
    }
