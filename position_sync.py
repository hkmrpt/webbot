"""
position_sync.py  ──  Option Buy Robot v9
═════════════════════════════════════════

Syncs with Zerodha to detect and adopt externally-opened positions.

When the bot starts in REAL mode, or periodically while running:
  1. Fetch current positions from Zerodha (GET /oms/portfolio/positions)
  2. Fetch today's orders (GET /oms/orders) for context
  3. Detect any open NFO positions that the bot doesn't know about
  4. Adopt them into the bot's exit engine for intelligent management
  5. Track position changes (partial fills, manual exits on Kite web)

This means: if you buy an option on Zerodha's website/app, the bot
will detect it, start managing it with trailing stops, and exit
intelligently — even though the bot didn't place the original order.
"""

from datetime import datetime


def detect_open_positions(positions_data: dict, known_symbol: str = "") -> list:
    """
    Detect open NFO positions from Zerodha positions data.

    Args:
        positions_data: dict with 'net' and 'day' lists from /oms/portfolio/positions
        known_symbol: option symbol the bot is already tracking (skip this one)

    Returns:
        List of dicts for positions that need to be adopted:
        [{symbol, exchange, qty, avg_price, last_price, pnl, product, side, ...}]
    """
    if not positions_data:
        return []

    net_positions = positions_data.get("net", [])
    open_positions = []

    for pos in net_positions:
        qty = pos.get("quantity", 0)
        if qty == 0:
            continue  # closed position

        symbol = pos.get("tradingsymbol", "")
        exchange = pos.get("exchange", "")

        # Only NFO positions
        if exchange != "NFO":
            continue

        # Skip if this is the position the bot is already managing
        if known_symbol and symbol == known_symbol:
            continue

        side = "CE" if "CE" in symbol.upper() else "PE" if "PE" in symbol.upper() else "unknown"

        open_positions.append({
            "symbol":       symbol,
            "exchange":     exchange,
            "instrument_token": pos.get("instrument_token"),
            "qty":          abs(qty),
            "qty_signed":   qty,
            "avg_price":    pos.get("average_price", 0),
            "last_price":   pos.get("last_price", 0),
            "pnl":          pos.get("pnl", 0),
            "m2m":          pos.get("m2m", 0),
            "product":      pos.get("product", "MIS"),
            "side":         side,
            "buy_qty":      pos.get("buy_quantity", 0),
            "sell_qty":     pos.get("sell_quantity", 0),
            "buy_price":    pos.get("buy_price", 0),
            "sell_price":   pos.get("sell_price", 0),
            "is_long":      qty > 0,
        })

    return open_positions


def match_position_to_atm(position: dict, atm_info: dict) -> bool:
    """
    Check if a position matches the current ATM options.
    Returns True if the position's symbol matches either the CE or PE ATM symbol.
    """
    if not atm_info or not position:
        return False

    sym = position.get("symbol", "").upper()
    ce_sym = (atm_info.get("ce_symbol", "") or "").upper()
    pe_sym = (atm_info.get("pe_symbol", "") or "").upper()

    return sym == ce_sym or sym == pe_sym


def build_adoption_params(position: dict) -> dict:
    """
    Build parameters to adopt an external position into the bot's exit engine.

    Returns dict compatible with BuyExitStrategy.open_leg() and _enter_trade_state().
    """
    avg_price = position.get("avg_price", 0) or position.get("buy_price", 0)
    last_price = position.get("last_price", avg_price)
    qty_lots = position.get("qty", 1)
    side = position.get("side", "CE")
    pnl_pct = ((last_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

    # Adaptive SL based on current P&L
    if pnl_pct >= 5:
        # Already in good profit — tight SL to protect
        sl_pct = 4.0
    elif pnl_pct >= 2:
        # Small profit — moderate SL
        sl_pct = 6.0
    elif pnl_pct >= 0:
        # Near breakeven — standard SL
        sl_pct = 8.0
    else:
        # Underwater — give some room but not too much
        sl_pct = max(6.0, min(12.0, abs(pnl_pct) + 3.0))

    # Trail based on current state
    if pnl_pct >= 8:
        trail_pct = 4.0
    elif pnl_pct >= 3:
        trail_pct = 5.0
    else:
        trail_pct = 8.0

    return {
        "side":           side,
        "symbol":         position.get("symbol", ""),
        "entry_price":    avg_price,
        "current_price":  last_price,
        "qty_lots":       qty_lots,
        "pnl_pct":        round(pnl_pct, 2),
        "sl_pct_p1":      sl_pct,
        "sl_pct_p2":      max(4.0, sl_pct - 2.0),
        "trail_pct":      trail_pct,
        "timeout_secs":   90,  # give external trades more time
        "adopted":        True,
        "adopt_time":     datetime.now().isoformat(),
    }


def detect_position_changes(
    prev_positions: list,
    curr_positions: list,
    bot_symbol: str = "",
) -> dict:
    """
    Compare two position snapshots to detect changes.

    Returns:
      {
        "new_positions": [...],       # positions that appeared
        "closed_positions": [...],    # positions that disappeared
        "qty_changed": [...],         # positions where qty changed (partial fill/exit)
        "externally_closed": bool,    # True if bot's tracked position was closed externally
      }
    """
    prev_map = {p["symbol"]: p for p in prev_positions}
    curr_map = {p["symbol"]: p for p in curr_positions}

    new_positions = []
    closed_positions = []
    qty_changed = []
    externally_closed = False

    # New positions (in curr but not prev)
    for sym, pos in curr_map.items():
        if sym not in prev_map:
            new_positions.append(pos)

    # Closed positions (in prev but not curr, or qty went to 0)
    for sym, prev in prev_map.items():
        curr = curr_map.get(sym)
        if not curr:
            closed_positions.append(prev)
            if sym == bot_symbol:
                externally_closed = True
        elif curr.get("qty", 0) != prev.get("qty", 0):
            qty_changed.append({
                "symbol":   sym,
                "prev_qty": prev.get("qty", 0),
                "curr_qty": curr.get("qty", 0),
                "diff":     curr.get("qty", 0) - prev.get("qty", 0),
            })
            if sym == bot_symbol and curr.get("qty", 0) == 0:
                externally_closed = True

    return {
        "new_positions":     new_positions,
        "closed_positions":  closed_positions,
        "qty_changed":       qty_changed,
        "externally_closed": externally_closed,
    }
