"""
core/broker.py — order-execution seam.

RealBroker wraps Zerodha_api (live MARKET/LIMIT MIS orders on NFO).
PaperBroker fills instantly at the given/last price and records every fill —
it formalizes what "demo mode" always did implicitly and gives the replay
harness a fill ledger to compute stats from.

Selection is by trading mode at call time: get_broker("real") / ("demo").
"""

import itertools
import threading


class RealBroker:
    """Thin wrapper over Zerodha_api — imported lazily so paper-only runs
    (tests, replay) never touch credentials."""

    def place_buy(self, symbol: str, qty: int):
        from Zerodha_api import place_buy
        return place_buy(symbol, qty)

    def place_sell(self, symbol: str, qty: int):
        from Zerodha_api import place_sell
        return place_sell(symbol, qty)

    def place_limit_sell(self, symbol: str, qty: int, price: float):
        from Zerodha_api import place_limit_sell
        return place_limit_sell(symbol, qty, price)

    def cancel_order(self, order_id: str):
        from Zerodha_api import cancel_order
        return cancel_order(order_id)


class PaperBroker:
    """
    Instant-fill paper broker.

    Knobs (used by the replay harness):
      slippage_bps — adverse fill slippage in basis points of price
      fee_per_order — flat fee charged per order (recorded, not applied to fills)
    """

    def __init__(self, slippage_bps: float = 0.0, fee_per_order: float = 0.0):
        self.slippage_bps  = slippage_bps
        self.fee_per_order = fee_per_order
        self.fills: list[dict] = []
        self._seq  = itertools.count(1)
        self._lock = threading.Lock()

    def _fill(self, action: str, symbol: str, qty: int, price: float | None):
        with self._lock:
            oid = f"PAPER{next(self._seq)}"
        px = price
        if px is not None and self.slippage_bps:
            adj = px * self.slippage_bps / 10_000.0
            px = round(px + adj if action == "buy" else px - adj, 2)
        self.fills.append({"order_id": oid, "action": action, "symbol": symbol,
                           "qty": qty, "price": px, "fee": self.fee_per_order})
        return oid, None

    # Signature-compatible with Zerodha_api wrappers: return (order_id, error)
    def place_buy(self, symbol, qty, price=None):
        return self._fill("buy", symbol, qty, price)

    def place_sell(self, symbol, qty, price=None):
        return self._fill("sell", symbol, qty, price)

    def place_limit_sell(self, symbol, qty, price):
        # Standing target orders are not simulated (the exit engine manages
        # exits in paper mode) — record the intent, return no order id.
        return None, "paper: standing limit orders not simulated"

    def cancel_order(self, order_id):
        return True, None


_paper = PaperBroker()


def get_broker(mode: str):
    """Broker for the given trading mode. The paper broker is a singleton so
    its fill ledger accumulates across a session."""
    return RealBroker() if mode == "real" else _paper


def reset_paper_broker(slippage_bps: float = 0.0, fee_per_order: float = 0.0) -> PaperBroker:
    """Fresh paper broker (replay runs call this so ledgers don't mix)."""
    global _paper
    _paper = PaperBroker(slippage_bps, fee_per_order)
    return _paper
