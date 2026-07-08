"""
replay/report.py — summary statistics for a replay run's trade log.
"""

import csv
import os
from collections import defaultdict


def _f(row, key, default=0.0):
    try:
        v = row.get(key, "")
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


def build_report(trade_csv: str, *, n_ticks: int = 0, n_meta: int = 0,
                 fills: list | None = None) -> dict:
    rows = []
    if os.path.exists(trade_csv):
        with open(trade_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    full     = [r for r in rows if r.get("row_type", "full") == "full"]
    partials = [r for r in rows if r.get("row_type") == "partial"]

    pnls   = [_f(r, "pnl_total") if r.get("pnl_total") else _f(r, "pnl") for r in full]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_win  = sum(wins)
    gross_loss = -sum(losses)

    # Max drawdown from the equity_after series
    max_dd, peak = 0.0, None
    for r in full:
        eq = _f(r, "equity_after")
        if eq:
            peak = eq if peak is None else max(peak, eq)
            max_dd = max(max_dd, peak - eq)

    by_reason: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for r, p in zip(full, pnls):
        b = by_reason[r.get("reason", "?")]
        b["n"]   += 1
        b["pnl"]  = round(b["pnl"] + p, 2)
        b["wins"] += 1 if p > 0 else 0

    by_regime: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for r, p in zip(full, pnls):
        b = by_regime[r.get("regime") or "?"]
        b["n"]   += 1
        b["pnl"]  = round(b["pnl"] + p, 2)
        b["wins"] += 1 if p > 0 else 0

    holds = [_f(r, "held_secs") for r in full if r.get("held_secs")]

    return {
        "ticks_replayed":  n_ticks,
        "meta_events":     n_meta,
        "trades":          len(full),
        "partial_bookings": len(partials),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins) / len(full), 3) if full else 0.0,
        "total_pnl":       round(sum(pnls), 2),
        "avg_winner":      round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loser":       round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor":   round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown":    round(max_dd, 2),
        "avg_hold_secs":   round(sum(holds) / len(holds), 1) if holds else 0.0,
        "by_exit_reason":  dict(by_reason),
        "by_regime":       dict(by_regime),
        "paper_fills":     len(fills or []),
    }
