"""
excel_logger.py  ──  Option Buy Robot
════════════════════════════════════════
Maintains a daily Excel trade journal: trade_log.xlsx

Sheet layout:
  Row 1 : headers (frozen)
  Row N : one trade per row, green = win, red = loss
  Last  : daily summary appended when day ends / target hit

Usage:
    from excel_logger import write_trade_excel
    write_trade_excel(result_dict)
"""

import os
import threading
from datetime import datetime

_xl_lock = threading.Lock()
EXCEL_LOG = "trade_log.xlsx"

COLUMNS = [
    "Date",
    "Time",
    "Trade #",
    "Symbol",
    "Option Symbol",
    "Side",
    "Entry ₹",
    "Exit ₹",
    "P&L ₹",
    "P&L %",
    "Exit Reason",
    "Held (s)",
    "Capital After ₹",
    "Day Start Capital ₹",
    "Day Target ₹",
    "Day P&L ₹",
    "Target Hit?",
]

# Column widths (characters)
_COL_WIDTHS = [12, 10, 9, 10, 22, 6, 10, 10, 10, 8, 18, 8, 16, 18, 14, 12, 11]


def _get_workbook():
    """Load existing workbook or create a fresh one."""
    try:
        from openpyxl import load_workbook
        if os.path.exists(EXCEL_LOG):
            return load_workbook(EXCEL_LOG)
    except Exception:
        pass

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side as XlSide
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Trades"

    # Header row
    hdr_fill  = PatternFill("solid", fgColor="1F3864")   # dark blue
    hdr_font  = Font(bold=True, color="FFFFFF", size=11)
    hdr_align = Alignment(horizontal="center", vertical="center")
    thin      = XlSide(style="thin", color="CCCCCC")
    border    = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, col_name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill  = hdr_fill
        cell.font  = hdr_font
        cell.alignment = hdr_align
        cell.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = _COL_WIDTHS[col_idx - 1]

    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"
    return wb


def write_trade_excel(result: dict):
    """
    Append one trade row to trade_log.xlsx.
    Thread-safe. Creates the file with headers if it does not exist.

    result dict keys used:
        date / time (optional, defaults to now)
        symbol, option_symbol, side
        entry, exit_price, pnl, pnl_pct, reason, held_secs
        equity_after, day_start_capital, day_target, session_pnl
    """
    try:
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side as XlSide
        from openpyxl.styles.numbers import FORMAT_NUMBER_COMMA_SEPARATED1
    except ImportError:
        # openpyxl not installed — silently skip Excel logging
        return

    with _xl_lock:
        try:
            wb = _get_workbook()
            ws = wb.active

            now        = datetime.now()
            pnl        = result.get("pnl", 0) or 0
            pnl_pct    = result.get("pnl_pct", 0) or 0
            equity     = result.get("equity_after", 0) or 0
            day_start  = result.get("day_start_capital", 0) or 0
            day_target = result.get("day_target", 0) or 0
            session_pnl= result.get("session_pnl", 0) or 0
            target_hit = "YES" if (day_target > 0 and session_pnl >= day_target) else "no"

            # Find next trade number for today
            today_str = now.strftime("%Y-%m-%d")
            trade_num = 1
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[0] == today_str:
                    trade_num += 1

            # Row data in column order
            row_data = [
                today_str,
                now.strftime("%H:%M:%S"),
                trade_num,
                result.get("symbol",        "NIFTY"),
                result.get("option_symbol", ""),
                result.get("side",          ""),
                result.get("entry",         ""),
                result.get("exit_price",    ""),
                round(pnl,     2),
                round(pnl_pct, 2),
                result.get("reason", ""),
                result.get("held_secs", ""),
                round(equity,      2),
                round(day_start,   2),
                round(day_target,  2),
                round(session_pnl, 2),
                target_hit,
            ]

            # Styling
            win_fill  = PatternFill("solid", fgColor="D9F0D3")   # light green
            loss_fill = PatternFill("solid", fgColor="FADADD")   # light red
            hit_fill  = PatternFill("solid", fgColor="FFF2CC")   # gold — target achieved
            row_fill  = hit_fill if target_hit == "YES" else (win_fill if pnl >= 0 else loss_fill)

            thin   = XlSide(style="thin", color="CCCCCC")
            border = Border(left=thin, right=thin, top=thin, bottom=thin)
            center = Alignment(horizontal="center")

            next_row = ws.max_row + 1
            for col_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=next_row, column=col_idx, value=value)
                cell.fill   = row_fill
                cell.border = border
                if col_idx in (1, 2, 3, 6, 11, 17):
                    cell.alignment = center
                # Currency format for money columns
                if col_idx in (7, 8, 9, 13, 14, 15, 16):
                    cell.number_format = '#,##0.00'
                # Percentage column
                if col_idx == 10:
                    cell.number_format = '0.00'

            wb.save(EXCEL_LOG)

        except Exception as exc:
            # Never crash the main bot over Excel errors
            import logging
            logging.getLogger(__name__).warning(f"Excel log failed: {exc}")


def write_daily_summary(session_pnl: float, day_start_capital: float,
                        trades: int, wins: int, losses: int, reason: str = ""):
    """
    Append a summary row at the end of the day (or when target/loss limit hit).
    Called from buy_app.py when the bot stops for the day.
    """
    try:
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side as XlSide
    except ImportError:
        return

    with _xl_lock:
        try:
            wb = _get_workbook()
            ws = wb.active

            day_target = round(day_start_capital * 0.025, 2)
            achieved   = session_pnl >= day_target
            pct_done   = round(session_pnl / day_start_capital * 100, 2) if day_start_capital else 0

            summary_data = [
                datetime.now().strftime("%Y-%m-%d"),
                datetime.now().strftime("%H:%M:%S"),
                "─ SUMMARY ─",
                "", "", "",
                "", "",
                round(session_pnl, 2),
                round(pct_done, 2),
                reason or ("TARGET HIT" if achieved else "Day ended"),
                "",
                round(day_start_capital + session_pnl, 2),
                round(day_start_capital, 2),
                round(day_target, 2),
                round(session_pnl, 2),
                "YES" if achieved else "no",
            ]

            fill   = PatternFill("solid", fgColor="BDD7EE")   # light blue summary
            font   = Font(bold=True, size=11)
            thin   = XlSide(style="thin", color="888888")
            border = Border(left=thin, right=thin, top=thin, bottom=thin)
            center = Alignment(horizontal="center")

            next_row = ws.max_row + 1
            for col_idx, value in enumerate(summary_data, start=1):
                cell = ws.cell(row=next_row, column=col_idx, value=value)
                cell.fill   = fill
                cell.font   = font
                cell.border = border
                if col_idx in (1, 2, 3, 17):
                    cell.alignment = center
                if col_idx in (9, 13, 14, 15, 16):
                    cell.number_format = '#,##0.00'
                if col_idx == 10:
                    cell.number_format = '0.00'

            # Blank separator row
            ws.append([""] * len(COLUMNS))

            wb.save(EXCEL_LOG)

        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"Excel summary failed: {exc}")


def read_total_pnl() -> float:
    """
    Read trade_log.xlsx and return sum of all P&L values.
    Skips summary rows (Trade # = '─ SUMMARY ─').
    Returns 0.0 if file missing or unreadable.
    """
    if not os.path.exists(EXCEL_LOG):
        return 0.0
    try:
        from openpyxl import load_workbook
        wb = load_workbook(EXCEL_LOG, read_only=True, data_only=True)
        ws = wb.active
        total = 0.0
        for row in ws.iter_rows(min_row=2, values_only=True):
            # Skip summary rows and blank rows
            trade_num = row[2] if len(row) > 2 else None
            if trade_num is None or trade_num == "" or trade_num == "─ SUMMARY ─":
                continue
            pnl = row[8] if len(row) > 8 else None  # "P&L ₹" is column 9 (index 8)
            if pnl is not None:
                try:
                    total += float(pnl)
                except (ValueError, TypeError):
                    pass
        wb.close()
        return round(total, 2)
    except Exception:
        return 0.0
