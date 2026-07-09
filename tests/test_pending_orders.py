"""Pending conditional orders: multi-arm, OCO on fill, one-shot triggers."""
from datetime import datetime

import pytest

import buy_app
from buy_app import S, RC
from buy_exit_strategy import BuyExitStrategy
from core import clock


@pytest.fixture
def armed_session(monkeypatch):
    """Running robot in scalp-manual mode, mid trading hours, options priced."""
    snap = {k: S.get(k) for k in
            ("running", "trade_open", "trade_side", "active_side", "active_status",
             "manual_levels", "pending_orders", "nifty_price", "nifty_entry_price",
             "scalp_mode", "session_pnl", "trades_today", "wins", "losses",
             "capital", "day_start_capital", "exit_engine", "slots", "trade_pnl",
             "loss_streak", "cooldown_until", "nifty_prev_tick", "nifty_move",
             "index_token", "last_ws_tick_ts", "fast_entry", "nifty_ref",
             "jump_threshold", "regression_slope", "_po_seq")}
    clock.set_clock(lambda: datetime(2026, 7, 9, 11, 0, 0))   # prime hours IST
    with buy_app._state_lock:
        S["running"] = True
        S["trade_open"] = False
        S["scalp_mode"] = "manual"
        S["capital"] = S["day_start_capital"] = 100_000.0
        S["session_pnl"] = 0.0
        S["trades_today"] = 0
        S["wins"] = S["losses"] = 0
        S["loss_streak"] = 0
        S["cooldown_until"] = None
        S["manual_levels"] = None
        S["pending_orders"] = []
        S["nifty_price"] = 24000.0
        S["regression_slope"] = 0.0
        S["exit_engine"] = BuyExitStrategy(100_000.0)
        S["slots"]["CE"]["token"] = 111
        S["slots"]["CE"]["price"] = 100.0
        S["slots"]["PE"]["token"] = 222
        S["slots"]["PE"]["price"] = 100.0
    yield
    clock.clear_clock()
    with buy_app._state_lock:
        S.update(snap)


_seq = iter(range(1, 100))


def _arm(side, watch, level, cur):
    with buy_app._state_lock:
        S["pending_orders"].append(
            {"id": next(_seq), "side": side, "watch": watch, "level": level,
             "dir": "up" if level > cur else "down", "armed_at": cur})


def _tick(token, price):
    buy_app.process_ticks([{"instrument_token": token, "last_price": price}])


def test_nifty_up_trigger_opens_trade(armed_session):
    """BUY CE when NIFTY >= 24050 — fires only when crossed."""
    _arm("CE", "nifty", 24050.0, 24000.0)

    _tick(buy_app.NIFTY_TOKEN, 24030.0)          # below → still armed
    assert len(S["pending_orders"]) == 1
    assert S["trade_open"] is False

    _tick(buy_app.NIFTY_TOKEN, 24055.0)          # crossed → fire
    assert S["pending_orders"] == [], "order must be consumed on trigger"
    assert S["trade_open"] is True
    assert S["trade_side"] == "CE"
    assert S["manual_levels"] is not None
    assert S["exit_engine"]._leg["manual_mode"] is True


def test_nifty_down_trigger(armed_session):
    """BUY PE when NIFTY <= 23950 (dip trigger)."""
    _arm("PE", "nifty", 23950.0, 24000.0)
    _tick(buy_app.NIFTY_TOKEN, 23940.0)
    assert S["trade_open"] is True and S["trade_side"] == "PE"


def test_premium_trigger_only_reacts_to_own_side(armed_session):
    """BUY CE when CE premium <= 90 — PE ticks must not fire it."""
    _arm("CE", "premium", 90.0, 100.0)

    _tick(222, 85.0)                             # PE tick
    assert len(S["pending_orders"]) == 1 and S["trade_open"] is False

    _tick(111, 89.5)                             # CE tick
    assert S["trade_open"] is True and S["trade_side"] == "CE"


def test_oco_first_fill_cancels_the_rest(armed_session):
    """Breakout + breakdown bracket: the one that fills kills the other."""
    _arm("CE", "nifty", 24050.0, 24000.0)        # breakout above
    _arm("PE", "nifty", 23950.0, 24000.0)        # breakdown below
    _arm("CE", "premium", 150.0, 100.0)          # premium momentum
    assert len(S["pending_orders"]) == 3

    _tick(buy_app.NIFTY_TOKEN, 24060.0)          # breakout fires
    assert S["trade_open"] is True and S["trade_side"] == "CE"
    assert S["pending_orders"] == [], "OCO must cancel ALL remaining orders"


def test_blocked_trigger_cancels_only_itself(armed_session):
    """A gate-blocked trigger consumes itself; other orders stay armed."""
    with buy_app._state_lock:
        S["session_pnl"] = -(RC["max_daily_loss"] + 1)   # daily loss breached
    _arm("CE", "nifty", 24050.0, 24000.0)
    _arm("PE", "nifty", 23950.0, 24000.0)

    _tick(buy_app.NIFTY_TOKEN, 24060.0)          # CE trigger crosses, blocked
    assert S["trade_open"] is False
    remaining = S["pending_orders"]
    assert len(remaining) == 1 and remaining[0]["side"] == "PE", \
        "only the blocked order may be consumed"


def test_pending_cleared_when_trade_already_open(armed_session):
    """If a trade opened some other way, all pending orders disarm."""
    _arm("CE", "nifty", 24050.0, 24000.0)
    _arm("PE", "nifty", 23950.0, 24000.0)
    with buy_app._state_lock:
        S["trade_open"] = True
        S["trade_side"] = S["active_side"] = "CE"
    _tick(buy_app.NIFTY_TOKEN, 24060.0)
    assert S["pending_orders"] == []


def test_crossing_while_stopped_cancels_loudly(armed_session):
    """Idle branch: crossing with the robot stopped cancels (never silently
    missed — the old code never even checked triggers while stopped)."""
    _arm("CE", "nifty", 24050.0, 24000.0)
    with buy_app._state_lock:
        S["running"] = False
    _tick(buy_app.NIFTY_TOKEN, 24060.0)          # idle branch
    assert S["pending_orders"] == [], "crossed-while-stopped must cancel"
    assert S["trade_open"] is False


def test_poller_ticks_do_not_mark_ws_fresh(armed_session):
    """mark_ws=False (poller path) must not update last_ws_tick_ts."""
    with buy_app._state_lock:
        S["last_ws_tick_ts"] = 0
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24010.0}], mark_ws=False)
    assert S["last_ws_tick_ts"] == 0

    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24011.0}])          # WS path
    assert S["last_ws_tick_ts"] > 0


def test_poller_path_fires_triggers(armed_session):
    """REST gap-fill ticks must evaluate pending triggers (the missed-trigger
    bug: poller data previously bypassed all trigger/exit checks)."""
    _arm("CE", "nifty", 24050.0, 24000.0)
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24060.0}], mark_ws=False)
    assert S["trade_open"] is True and S["trade_side"] == "CE"
