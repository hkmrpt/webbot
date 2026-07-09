"""Pending conditional orders: arm → watch → trigger on level crossing."""
from datetime import datetime

import pytest

import buy_app
from buy_app import S, RC
from buy_exit_strategy import BuyExitStrategy
from core import clock


@pytest.fixture
def armed_session(monkeypatch):
    """Running robot in scalp-manual mode, mid trading hours, CE priced."""
    snap = {k: S.get(k) for k in
            ("running", "trade_open", "trade_side", "active_side", "active_status",
             "manual_levels", "pending_order", "nifty_price", "nifty_entry_price",
             "scalp_mode", "session_pnl", "trades_today", "wins", "losses",
             "capital", "day_start_capital", "exit_engine", "slots", "trade_pnl",
             "loss_streak", "cooldown_until", "nifty_prev_tick", "nifty_move",
             "index_token", "last_ws_tick_ts", "fast_entry", "nifty_ref",
             "jump_threshold", "regression_slope")}
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
        S["pending_order"] = None
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


def _arm(side, watch, level, cur):
    with buy_app._state_lock:
        S["pending_order"] = {"side": side, "watch": watch, "level": level,
                              "dir": "up" if level > cur else "down",
                              "armed_at": cur}


def test_nifty_up_trigger_opens_trade(armed_session):
    """BUY CE when NIFTY >= 24050 — fires only when crossed."""
    _arm("CE", "nifty", 24050.0, 24000.0)

    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24030.0}])          # below → still armed
    assert S["pending_order"] is not None
    assert S["trade_open"] is False

    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24055.0}])          # crossed → fire
    assert S["pending_order"] is None, "pending must be consumed on trigger"
    assert S["trade_open"] is True
    assert S["trade_side"] == "CE"
    assert S["manual_levels"] is not None, "manual NIFTY exits must arm on entry"
    assert S["exit_engine"]._leg["manual_mode"] is True


def test_nifty_down_trigger(armed_session):
    """BUY PE when NIFTY <= 23950 (dip trigger)."""
    _arm("PE", "nifty", 23950.0, 24000.0)
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 23940.0}])
    assert S["trade_open"] is True and S["trade_side"] == "PE"


def test_premium_trigger_only_reacts_to_own_side(armed_session):
    """BUY CE when CE premium <= 90 — PE ticks must not fire it."""
    _arm("CE", "premium", 90.0, 100.0)

    buy_app.process_ticks([{"instrument_token": 222, "last_price": 85.0}])  # PE tick
    assert S["pending_order"] is not None and S["trade_open"] is False

    buy_app.process_ticks([{"instrument_token": 111, "last_price": 89.5}]) # CE tick
    assert S["trade_open"] is True and S["trade_side"] == "CE"


def test_blocked_trigger_cancels_pending(armed_session):
    """Trigger crossing while gates fail cancels the pending order."""
    with buy_app._state_lock:
        S["session_pnl"] = -(RC["max_daily_loss"] + 1)   # daily loss breached
    _arm("CE", "nifty", 24050.0, 24000.0)
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24060.0}])
    assert S["pending_order"] is None, "blocked trigger must cancel, not retry"
    assert S["trade_open"] is False


def test_pending_cleared_when_trade_already_open(armed_session):
    """If a trade opened some other way, the pending order disarms."""
    _arm("CE", "nifty", 24050.0, 24000.0)
    with buy_app._state_lock:
        S["trade_open"] = True
        S["trade_side"] = S["active_side"] = "CE"
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24060.0}])
    assert S["pending_order"] is None
