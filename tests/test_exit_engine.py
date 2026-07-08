"""Stage-1 money-safety tests for BuyExitStrategy.

Covers: exit precedence (hard SL before AI), trail ratchet (never widens),
partial-booking P&L accounting (credited exactly once), monotonic tier table,
and the trade-log v2 CSV rows.
"""
import csv
import os

import config
import buy_exit_strategy as bes
from buy_exit_strategy import BuyExitStrategy


def _engine(capital=100_000.0):
    eng = BuyExitStrategy(capital)
    return eng


def _feed(eng, prices):
    """Feed prices; return the first non-None engine event (exit/partial)."""
    for p in prices:
        r = eng.on_price(p)
        if r:
            return r
    return None


# ── 1. Hard SL outranks the AI analyzer ──────────────────────────────────────

def test_sl_fires_before_ai_analyzer_on_crash():
    eng = _engine()
    # Fully trained analyzer — prove ORDER, not warmup, protects the SL
    eng._exit_analyzer.n_trades = 100
    eng.open_leg("CE", 100.0)                     # phase-1 SL 10% → 90.0

    # Mild drift, then one violent tick that BOTH breaches the SL (89 ≤ 90)
    # and fires cascade_risk (accelerating drops, 11% total) on the SAME tick.
    # The hard SL must win the tick.
    result = _feed(eng, [100.0, 99.8, 99.5, 99.0, 89.0])
    assert result is not None
    assert result["reason"] == "sl", f"expected sl, got {result['reason']}"
    assert result["exit_price"] == 89.0


def test_cascade_protector_still_live_during_warmup():
    eng = _engine()
    assert eng._exit_analyzer.n_trades == 0       # fresh — warmup active
    eng.open_leg("CE", 100.0)
    # Accelerating drop that stays ABOVE the 10% SL (floor 92.5 > 90)
    result = _feed(eng, [100.0, 99.7, 99.2, 98.4, 97.2, 95.4, 92.8])
    assert result is not None
    assert result["reason"] in ("ai_analyzer",), (
        f"cascade crash protector should fire during warmup, got {result['reason']}")


# ── 2. Trail ratchet: never widens, even with brain influence ────────────────

def test_trail_pct_never_widens_with_ratchet():
    eng = _engine()
    eng.open_leg("CE", 100.0)

    trail_seq = []
    # Rise (peak grows, tiers activate) then chop — momentum swings wildly,
    # which previously let the brain multiply the trail wider than the ratchet.
    path = [100.5, 101.5, 103.0, 104.0, 105.0, 106.0, 105.5, 105.8,
            105.2, 105.6, 105.0, 105.4, 104.8, 105.2, 104.9]
    for p in path:
        r = eng.on_price(p)
        if eng._leg and eng._leg["open"]:
            trail_seq.append(eng._leg["trail_pct"])
        if r:
            break

    assert len(trail_seq) >= 5
    for a, b in zip(trail_seq, trail_seq[1:]):
        assert b <= a + 1e-9, f"trail widened: {a} → {b} in {trail_seq}"


def test_trail_price_high_water_mark_never_drops():
    eng = _engine()
    eng.open_leg("CE", 100.0)
    tps = []
    for p in [101.0, 103.0, 105.0, 104.0, 104.5, 103.5]:
        r = eng.on_price(p)
        if eng._leg and eng._leg["open"] and eng._leg["trail_price"]:
            tps.append(eng._leg["trail_price"])
        if r:
            break
    for a, b in zip(tps, tps[1:]):
        assert b >= a - 1e-9, f"trail price went backwards: {a} → {b}"


# ── 3. Partial booking P&L credited exactly once ─────────────────────────────

def test_partial_booking_credits_capital_exactly_once():
    cap0 = 100_000.0
    eng = _engine(cap0)
    eng.open_leg("CE", 100.0, qty_override=4, lot_size=65)

    # Ride up past the first partial target (peak ≥ 8%)
    event = _feed(eng, [101.0, 102.5, 104.0, 105.5, 107.0, 108.5])
    assert event is not None and event.get("event_type") == "partial"

    sell_qty = event["partial_qty"]
    px       = event["price"]
    expected_partial = round((px - 100.0) * sell_qty * 65, 2)
    assert event["partial_pnl"] == expected_partial
    assert eng.capital == round(cap0 + expected_partial, 2), \
        "partial P&L must be credited to capital at booking time"

    # Close the remainder and verify nothing is lost or double-counted
    result = eng.force_close(106.0, "manual")
    remaining_pnl = round((106.0 - 100.0) * event["remaining_qty"] * 65, 2)
    assert result["pnl"] == remaining_pnl
    assert result["partial_realized_pnl"] == expected_partial
    assert result["pnl_total"] == round(remaining_pnl + expected_partial, 2)
    assert eng.capital == round(cap0 + expected_partial + remaining_pnl, 2)


def test_partial_writes_csv_row_and_close_writes_full_row():
    eng = _engine()
    eng.open_leg("CE", 100.0, qty_override=4, lot_size=65,
                 meta={"mode": "demo", "scalp_mode": "off", "nifty_entry": 22000})
    event = _feed(eng, [101.0, 102.5, 104.0, 105.5, 107.0, 108.5])
    assert event and event["event_type"] == "partial"
    eng.force_close(106.0, "manual")

    path = bes._log_path()
    assert os.path.exists(path)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    types = [r["row_type"] for r in rows]
    assert types == ["partial", "full"]
    assert rows[0]["mode"] == "demo"
    assert rows[1]["nifty_entry"] == "22000"
    # equity math is joinable from the CSV alone
    assert float(rows[1]["pnl_total"]) == round(
        float(rows[1]["pnl"]) + float(rows[1]["partial_realized_pnl"]), 2)


# ── 4. Config sanity: tier tables monotonic ──────────────────────────────────

def test_profit_tier_trail_monotonic():
    tiers = config.PROFIT_TIER_TRAIL
    pcts   = [t[0] for t in tiers]
    trails = [t[1] for t in tiers]
    assert pcts == sorted(pcts)
    assert trails == sorted(trails), (
        "trail% must be non-decreasing with profit tier — a dip means a "
        "higher-profit tier trails TIGHTER than a lower one")


def test_profit_lock_tiers_monotonic():
    tiers = config.PROFIT_LOCK_TIERS
    pcts  = [t[0] for t in tiers]
    locks = [t[1] for t in tiers]
    assert pcts == sorted(pcts)
    assert locks == sorted(locks)


# ── 5. AI analyzer warmup ─────────────────────────────────────────────────────

def test_warmup_blocks_non_cascade_critical_signals():
    from exit_analyzer import ExitAnalyzer
    an = ExitAnalyzer()
    assert an.n_trades == 0

    # Price series engineered so vol_spike fires critical (0.9):
    # 6 quiet ticks then 6 violent ticks while losing.
    prices = [100.0, 100.05, 100.1, 100.05, 100.1, 100.05,
              99.0, 97.5, 98.9, 96.8, 98.2, 96.0]
    fire, _ = an.check(prices=prices, entry=101.0, side="CE",
                       current_pct=-4.9, peak_pct=0.5, held_secs=15,
                       profit_history=[-1, -2, -3, -4, -4.5, -4.9],
                       opt_price=96.0)
    assert fire is False, "vol_spike must have no authority during warmup"

    an.n_trades = config.AI_EXIT_MIN_TRADES
    fire2, reason2 = an.check(prices=prices, entry=101.0, side="CE",
                              current_pct=-4.9, peak_pct=0.5, held_secs=15,
                              profit_history=[-1, -2, -3, -4, -4.5, -4.9],
                              opt_price=96.0)
    assert fire2 is True, f"after warmup the same storm should exit ({reason2})"
