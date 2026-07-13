"""AI take-profit decision (v9.6).

The fixed target% is now a REFERENCE level, not a mechanical exit. When the
premium reaches it, the tick-derived momentum score decides every tick:
  momentum ≥ AI_TP_HOLD_MOMENTUM → ride the trail (SL first ratchets to lock
    AI_TP_LOCK_FRACTION of the target profit — the ride can never give the
    reached target back below the lock)
  momentum <  AI_TP_HOLD_MOMENTUM → book the profit now (reason "ai_tp")
"""
import config
import buy_exit_strategy as bes
from buy_exit_strategy import BuyExitStrategy


def _engine():
    return BuyExitStrategy(100_000.0)


# ── Booking on fading momentum ────────────────────────────────────────────────

def test_fading_momentum_books_profit_at_target():
    """Climb to the target, then stall — the AI must book, not sit there."""
    eng = _engine()
    eng.open_leg("CE", 100.0, tp_pct_override=2.0)

    r = None
    for p in [100.5, 101.0, 101.5, 102.0, 102.1] + [102.1] * 8:
        r = eng.on_price(p)
        if r:
            break
    assert r is not None, "stalled trade above target must be booked"
    assert r["reason"] == "ai_tp"
    assert r["pnl_pct"] >= 2.0, "booked at/above the reference target"


def test_below_target_no_ai_tp():
    """The AI TP has no authority below the reference level."""
    eng = _engine()
    eng.open_leg("CE", 100.0, tp_pct_override=2.0)
    for p in [100.5, 101.0, 101.5] + [101.5] * 8:      # peak +1.5% < 2%
        r = eng.on_price(p)
        assert r is None or r["reason"] != "ai_tp", (
            "ai_tp must never fire below the reference target")


# ── Riding on strong momentum ─────────────────────────────────────────────────

def test_strong_momentum_rides_beyond_fixed_target():
    """A violent move through the target must NOT be capped at +2%."""
    eng = _engine()
    eng.open_leg("CE", 100.0, tp_pct_override=2.0)

    for p in [103.0, 106.0, 109.0, 112.0]:
        r = eng.on_price(p)
        assert r is None, (
            f"AI must ride strong momentum past the target, got "
            f"{r and r['reason']} at {p}")
    assert eng._leg["tp_riding"] is True
    assert eng._leg["peak_profit_pct"] >= 12.0

    # Wind the trade down — whatever closes it, the ride must have kept
    # far more than the fixed 2% target would have.
    r = None
    for p in [110.0, 108.0, 106.0, 104.0, 102.0, 101.0]:
        r = eng.on_price(p)
        if r:
            break
    assert r is not None
    assert r["pnl_pct"] > 1.0, "lock guarantees most of the target survives"
    assert r["peak_profit_pct"] >= 12.0, "the ride went far beyond +2%"


def test_ride_lock_guarantees_most_of_target():
    """Riding ratchets the SL to lock AI_TP_LOCK_FRACTION of the target."""
    eng = _engine()
    eng.open_leg("CE", 100.0, tp_pct_override=2.0)

    r = eng.on_price(103.0)          # target reached, momentum neutral → ride
    assert r is None
    assert eng._leg["tp_riding"] is True
    expected_lock = round(100.0 * (1 + 2.0 * config.AI_TP_LOCK_FRACTION / 100), 2)
    assert eng._leg["sl"] >= expected_lock

    r = eng.on_price(101.2)          # collapse below the lock
    assert r is not None and r["reason"] == "sl"
    assert r["pnl_pct"] > 1.0, "the reached target can never turn into a loss"


# ── Config / plumbing ─────────────────────────────────────────────────────────

def test_tp_reference_override_respected():
    """A 5% override must not book at +3%."""
    eng = _engine()
    eng.open_leg("CE", 100.0, tp_pct_override=5.0)
    for p in [101.0, 102.0, 103.0] + [103.0] * 8:      # stalls at +3% < 5%
        r = eng.on_price(p)
        assert r is None or r["reason"] != "ai_tp"


def test_disabled_ai_tp_never_books(monkeypatch):
    monkeypatch.setattr(bes, "AI_TP_ENABLED", False)
    eng = _engine()
    res = eng.open_leg("CE", 100.0, tp_pct_override=2.0)
    for p in [100.5, 101.0, 101.5, 102.0, 102.1] + [102.1] * 8:
        r = eng.on_price(p)
        assert r is None, f"disabled AI TP must not exit ({r and r['reason']})"
    assert eng._leg["tp_riding"] is False
    # Breakeven may lift the SL to entry, but the AI TP lock (above entry)
    # must not have been applied.
    assert eng._leg["sl"] <= 100.0, "no lock ratchet above entry when disabled"
    assert res["sl"] < 100.0


def test_partial_booking_still_outranks_ai_tp():
    """Slot 3 (partial) fires before slot 3.5 (AI TP) on the same tick."""
    eng = _engine()
    eng.open_leg("CE", 100.0, qty_override=4, lot_size=65, tp_pct_override=2.0)
    # Strong climb: momentum stays high → AI rides through the target,
    # letting the partial ladder do its job at +8%.
    event = None
    for p in [101.5, 103.0, 104.5, 106.0, 107.5, 109.0]:
        event = eng.on_price(p)
        if event:
            break
    assert event is not None and event.get("event_type") == "partial", (
        f"expected partial booking while riding, got {event}")
    assert eng._leg["open"] is True
