"""AI position sizing (v9.6) — PositionSizer.

Day starts small (warmup ramp), losses size UP (bounded recovery), the ML
entry score scales size, and a learned multiplier adapts from outcomes —
all inside two HARD caps no factor can override: available capital and the
remaining daily-loss budget at the trade's own worst-case hard SL.
"""
import config
from position_sizer import PositionSizer


def _ctx(**over):
    """capital 100k / premium 100 / lot 65 / ATR 4 → base 7 lots, afford 15
    (headroom so boosts are visible). max_daily_loss=0 disables the budget
    cap unless a test enables it."""
    ctx = dict(capital=100_000.0, premium=100.0, lot_size=65, opt_atr=4.0,
               trades_today=5, loss_streak=0, session_pnl=0.0,
               entry_score=None, sl_pct=15.0, max_daily_loss=0.0)
    ctx.update(over)
    return ctx


def _sizer():
    return PositionSizer(mode="demo")


# ── Warmup: the day starts small ──────────────────────────────────────────────

def test_day_start_takes_lower_quantity():
    s = _sizer()
    first, _ = s.decide(_ctx(trades_today=0))
    later, _ = s.decide(_ctx(trades_today=5))
    assert first < later, "first trade of the day must be smaller"
    assert first >= 1


def test_warmup_ramps_monotonically_to_full_size():
    s = _sizer()
    sizes = [s.decide(_ctx(trades_today=t))[0]
             for t in range(config.SIZER_WARMUP_TRADES + 2)]
    for a, b in zip(sizes, sizes[1:]):
        assert b >= a, f"warmup ramp must not shrink: {sizes}"
    assert sizes[-1] == sizes[config.SIZER_WARMUP_TRADES], "full size reached"


# ── Recovery: losses size UP, capped ──────────────────────────────────────────

def test_loss_streak_increases_quantity():
    s = _sizer()
    normal, _   = s.decide(_ctx())
    losing, why = s.decide(_ctx(loss_streak=2))
    assert losing > normal, "a loss streak must size up to win the day back"
    assert any("recovery" in r for r in why)


def test_recovery_capped_at_max():
    s = _sizer()
    at_cap, _  = s.decide(_ctx(loss_streak=3))
    beyond, _  = s.decide(_ctx(loss_streak=8))
    assert beyond == at_cap, "recovery boost must cap at SIZER_RECOVERY_MAX"


# ── Hard caps: budget and capital always win ──────────────────────────────────

def test_loss_budget_caps_recovery():
    """Recovery can chase losses only INSIDE the daily loss budget."""
    s = _sizer()
    # ₹2000 budget, worst case ₹975/lot at 15% SL on a ₹6,500 lot → 2 lots max
    lots, why = s.decide(_ctx(loss_streak=3, max_daily_loss=2000.0))
    assert lots == 2
    assert any("budget" in r for r in why)


def test_nearly_spent_budget_floors_at_one_lot():
    s = _sizer()
    # ₹300 left of the budget < ₹975 worst case → floor at 1, never 0
    lots, why = s.decide(_ctx(loss_streak=4,
                              max_daily_loss=1000.0, session_pnl=-700.0))
    assert lots == 1
    assert any("budget" in r for r in why)


def test_available_capital_caps_quantity():
    s = _sizer()
    lots, _ = s.decide(_ctx(capital=10_000.0, loss_streak=3))  # 1 lot affordable
    assert lots == 1, "can never buy more lots than capital affords"


# ── ML confidence scales size ─────────────────────────────────────────────────

def test_entry_score_scales_quantity():
    s = _sizer()
    big_ctx = dict(capital=200_000.0, opt_atr=4.0)   # base 15, afford 30
    weak,   _ = s.decide(_ctx(entry_score=0.30, **big_ctx))
    neutral, _ = s.decide(_ctx(entry_score=None, **big_ctx))
    strong, _ = s.decide(_ctx(entry_score=0.80, **big_ctx))
    assert weak < neutral < strong, (
        f"score must scale size: {weak} / {neutral} / {strong}")


# ── Learning: mode-tagged, outcome-driven ─────────────────────────────────────

def test_boosted_loss_shrinks_learned_multiplier():
    s = _sizer()
    s.decide(_ctx(loss_streak=2))                    # boosted above base
    s.on_trade_closed({"pnl": -500.0, "mode": "demo"})
    assert s._mult < 1.0
    shrunk = s._mult
    s.decide(_ctx(loss_streak=2))
    s.on_trade_closed({"pnl": 800.0, "mode": "demo"})  # boosted win → grow back
    assert s._mult > shrunk


def test_unboosted_trade_does_not_move_multiplier():
    s = _sizer()
    s.decide(_ctx())                                 # plain base size
    s.on_trade_closed({"pnl": -500.0, "mode": "demo"})
    assert s._mult == 1.0


def test_multiplier_clamped():
    s = _sizer()
    for _ in range(50):
        s.decide(_ctx(loss_streak=2))
        s.on_trade_closed({"pnl": -100.0, "mode": "demo"})
    assert s._mult >= config.SIZER_MULT_MIN
    for _ in range(100):
        s.decide(_ctx(loss_streak=2))
        s.on_trade_closed({"pnl": 100.0, "mode": "demo"})
    assert s._mult <= config.SIZER_MULT_MAX


def test_mode_mismatch_never_trains():
    s = _sizer()                                     # demo sizer
    s.decide(_ctx(loss_streak=2))
    s.on_trade_closed({"pnl": -500.0, "mode": "real"})
    assert s._mult == 1.0 and s.n_trades == 0, (
        "real fills must never train demo sizing (and vice versa)")


def test_state_persists_across_restart():
    s = _sizer()
    s.decide(_ctx(loss_streak=2))
    s.on_trade_closed({"pnl": -500.0, "mode": "demo"})
    saved_mult, saved_n = s._mult, s.n_trades

    s2 = PositionSizer(mode="demo")                  # fresh instance → loads file
    assert s2._mult == saved_mult
    assert s2.n_trades == saved_n
