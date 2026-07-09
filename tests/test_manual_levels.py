"""Manual-trade NIFTY-level exits: premium exits disabled, spot levels rule."""
import pytest

import buy_app
import config
from buy_app import S, RC
from buy_exit_strategy import BuyExitStrategy


@pytest.fixture
def manual_trade(monkeypatch):
    """Open a manual CE trade at NIFTY 24000, option ₹100, levels SL/TP set."""
    snap = {k: S.get(k) for k in
            ("running", "trade_open", "trade_side", "active_side", "active_status",
             "manual_levels", "nifty_price", "nifty_entry_price", "scalp_mode",
             "session_pnl", "trades_today", "wins", "losses", "capital",
             "day_start_capital", "exit_engine", "slots", "trade_pnl",
             "loss_streak", "cooldown_until", "nifty_prev_tick", "nifty_move",
             "index_token", "last_ws_tick_ts")}
    with buy_app._state_lock:
        S["running"] = True
        S["capital"] = S["day_start_capital"] = 100_000.0
        S["session_pnl"] = 0.0
        S["scalp_mode"] = "manual"
        S["nifty_price"] = 24000.0
        eng = BuyExitStrategy(100_000.0)
        eng._exit_analyzer.n_trades = 100          # fully trained — prove mode, not warmup
        S["exit_engine"] = eng
        eng.open_leg("CE", 100.0, qty_override=1, lot_size=65,
                     manual_exit_mode=True,
                     meta={"mode": "demo"})
        S["trade_open"] = True
        S["trade_side"] = S["active_side"] = "CE"
        S["active_status"] = "open"
        S["nifty_entry_price"] = 24000.0
        S["slots"]["CE"]["token"] = 111
        S["slots"]["CE"]["price"] = 100.0
        S["manual_levels"] = {"sl": 23985.0, "tp": 24030.0, "side": "CE", "ref": 24000.0}
    yield eng
    with buy_app._state_lock:
        S.update(snap)


def test_manual_mode_ignores_premium_crash(manual_trade):
    """Premium drops 40% — a normal trade would SL out; manual mode holds."""
    eng = manual_trade
    r = eng.on_price(60.0)                      # -40% premium
    assert r is None, "manual trade must ignore premium-based SL/trail/timeout"
    assert eng._leg["open"] is True


def test_manual_mode_catastrophic_floor(manual_trade):
    """Premium down > MANUAL_MAX_PREMIUM_LOSS_PCT still force-exits (safety)."""
    eng = manual_trade
    r = eng.on_price(100.0 * (1 - config.MANUAL_MAX_PREMIUM_LOSS_PCT / 100) - 1)
    assert r is not None and r["reason"] == "sl"


def test_nifty_sl_level_closes_ce_trade(manual_trade):
    """NIFTY dropping through the SL line closes the CE trade."""
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 23980.0}])   # below SL 23985
    assert S["trade_open"] is False
    assert S["manual_levels"] is None, "levels must clear on close"


def test_nifty_tp_level_closes_ce_trade(manual_trade):
    """NIFTY rising through the TP line closes the CE trade."""
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24035.0}])   # above TP 24030
    assert S["trade_open"] is False


def test_nifty_inside_levels_holds(manual_trade):
    """NIFTY between the lines — trade stays open."""
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24010.0}])
    assert S["trade_open"] is True


def test_pe_levels_are_mirrored(manual_trade):
    """For PE: SL sits ABOVE the market, TP below."""
    eng = manual_trade
    with buy_app._state_lock:
        S["trade_side"] = S["active_side"] = "PE"
        S["slots"]["PE"]["token"] = 222
        S["slots"]["PE"]["price"] = 100.0
        eng._leg["side"] = "PE"
        S["manual_levels"] = {"sl": 24015.0, "tp": 23970.0, "side": "PE", "ref": 24000.0}
    buy_app.process_ticks([{"instrument_token": buy_app.NIFTY_TOKEN,
                            "last_price": 24020.0}])   # above PE SL → adverse
    assert S["trade_open"] is False
