"""Adaptive timeout + OCO timeout bracket (v9.6).

The fixed timeout market-exited flat trades on the exact second. Now:
  - the effective timeout stretches/shrinks with tick momentum
    (_adaptive_timeout_factor), and
  - on expiry the engine arms a virtual OCO bracket (target = recent swing
    high, floor = recent swing low clamped to the hard SL) instead of an
    instant market exit. First level touched wins; a grace deadline falls
    back to the plain timeout. Hard SL / trail always outrank the bracket.
"""
from datetime import datetime, timedelta

import pytest

import config
from buy_exit_strategy import (
    BuyExitStrategy,
    _adaptive_timeout_factor,
    _build_pseudo_candles,
    _compute_oco_levels,
)
from core import clock


# ── Clock fixture ─────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += timedelta(seconds=secs)


@pytest.fixture
def clk():
    c = FakeClock(datetime(2026, 7, 13, 10, 0, 0))
    clock.set_clock(c)
    yield c
    clock.clear_clock()


# Range-shaped tick history: swing high 102 early, support dip to 99 near the
# end, closing back at the 100 entry. Drives the OCO bracket to (102 / 99).
SEED_RANGE = [100.0, 100.8, 101.6, 102.0, 101.4,
              100.9, 100.4, 100.1, 99.6, 99.2,
              99.0, 99.3, 99.6, 99.8, 100.0]


def _open_scalp_leg(eng, seed=None):
    """Open a leg with a 20s base timeout (scalp-style override)."""
    return eng.open_leg("CE", 100.0, timeout_secs_override=20.0,
                        seed_prices=seed)


def _feed_flat(eng, clk, n, secs_per_tick=0.5, price=100.0):
    """Feed n identical ticks (dead tape → momentum ≈ 0.34 → stalled)."""
    for _ in range(n):
        clk.advance(secs_per_tick)
        r = eng.on_price(price)
        if r:
            return r
    return None


# ── 1. Factor function: momentum from previous ticks decides the time ────────

def test_factor_dead_tape_shrinks_to_min():
    assert _adaptive_timeout_factor(0.0) == config.TIMEOUT_MIN_FACTOR
    assert _adaptive_timeout_factor(config.TIMEOUT_STALL_MOMENTUM) == \
        config.TIMEOUT_MIN_FACTOR


def test_factor_neutral_momentum_keeps_base():
    assert _adaptive_timeout_factor(0.45) == 1.0
    assert _adaptive_timeout_factor(0.59) == 1.0


def test_factor_extends_monotonically_to_max():
    assert _adaptive_timeout_factor(1.0) == config.TIMEOUT_MAX_FACTOR
    prev = 1.0
    for s in (0.60, 0.70, 0.80, 0.90, 1.00):
        f = _adaptive_timeout_factor(s)
        assert f >= prev - 1e-9, f"factor not monotonic at {s}"
        prev = f


def test_factor_config_sanity():
    assert config.TIMEOUT_MIN_FACTOR <= 1.0 <= config.TIMEOUT_MAX_FACTOR
    assert config.TIMEOUT_STALL_MOMENTUM < config.TIMEOUT_EXTEND_MOMENTUM


def test_factor_none_momentum_is_neutral():
    assert _adaptive_timeout_factor(None) == 1.0


# ── 2. Stalled tape exits BEFORE the base timeout ─────────────────────────────

def test_dead_tape_times_out_early(clk):
    """Flat prices → momentum ~0.34 → eff timeout 20 × 0.5 = 10s. A dead-flat
    tape also has no swing structure (floor == price), so the bracket is
    degenerate and the engine falls back to the plain timeout exit."""
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng, seed=[100.0] * 40)

    r = _feed_flat(eng, clk, 18)          # reaches t = 9s — still open
    assert r is None
    assert eng._leg["timeout_eff_secs"] == pytest.approx(10.0)

    clk.advance(2.0)                       # t = 11s > 10s eff, < 20s base
    r = eng.on_price(100.0)
    assert r is not None and r["reason"] == "timeout"
    assert r["held_secs"] < 20, "must exit before the base timeout"


# ── 3. Developing move EXTENDS the timeout ────────────────────────────────────

def test_high_momentum_extends_effective_timeout(clk):
    """Strong oscillation (±4.8 pts/tick) → momentum ≈ 0.65 → factor > 1."""
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng)

    price, alt = 99.8, 95.0
    for i in range(10):                    # elapsed stays ~5s — no timeout
        clk.advance(0.5)
        r = eng.on_price(price if i % 2 == 0 else alt)
        assert r is None, f"unexpected exit: {r and r['reason']}"

    assert eng._leg["timeout_eff_secs"] > 20.0, (
        "still-moving tape must stretch the timeout beyond its base")


# ── 4. Timeout arms an OCO bracket from previous ticks/candles ────────────────

def test_timeout_arms_oco_bracket_with_swing_levels(clk):
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng, seed=SEED_RANGE)

    assert _feed_flat(eng, clk, 18) is None    # t = 9s — still open
    clk.advance(1.5)
    event = eng.on_price(100.0)            # t = 10.5s > 10s eff → bracket arms

    assert event is not None
    assert event["event_type"] == "oco_armed"
    assert eng._leg["open"] is True, "arming must NOT close the trade"
    assert eng._leg["oco_armed"] is True
    assert eng._leg["oco_target"] == pytest.approx(102.0)   # swing high
    assert eng._leg["oco_floor"] == pytest.approx(99.0)     # swing low
    assert event["grace_secs"] == config.OCO_GRACE_SECS


def test_oco_target_touch_closes_in_profit(clk):
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng, seed=SEED_RANGE)
    assert _feed_flat(eng, clk, 18) is None
    clk.advance(1.5)
    assert eng.on_price(100.0)["event_type"] == "oco_armed"

    clk.advance(1.0)
    r = eng.on_price(102.5)                # pops through the swing high
    assert r is not None and r["reason"] == "oco_target"
    assert r["pnl_pct"] > 0


def test_oco_floor_touch_closes_the_trade(clk):
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng, seed=SEED_RANGE)
    assert _feed_flat(eng, clk, 18) is None
    clk.advance(1.5)
    assert eng.on_price(100.0)["event_type"] == "oco_armed"

    clk.advance(1.0)
    r = eng.on_price(98.9)                 # breaks the swing-low support
    assert r is not None and r["reason"] == "oco_floor"


def test_oco_grace_deadline_falls_back_to_timeout(clk):
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng, seed=SEED_RANGE)
    assert _feed_flat(eng, clk, 18) is None
    clk.advance(1.5)
    assert eng.on_price(100.0)["event_type"] == "oco_armed"

    clk.advance(config.OCO_GRACE_SECS + 1)
    r = eng.on_price(100.0)                # still inside the bracket
    assert r is not None and r["reason"] == "timeout"


# ── 5. Money-safety: bracket never weakens the hard rules ─────────────────────

def test_oco_floor_never_below_hard_sl(clk):
    """A deep old swing low (80) must be clamped to the hard SL."""
    deep_seed = [100.0, 100.8, 101.6, 102.0, 101.4,
                 100.9, 100.4, 95.0, 88.0, 80.0,
                 85.0, 92.0, 97.0, 99.5, 100.0]
    eng = BuyExitStrategy(100_000.0)
    res = _open_scalp_leg(eng, seed=deep_seed)

    assert _feed_flat(eng, clk, 18) is None
    clk.advance(1.5)
    event = eng.on_price(100.0)
    assert event is not None and event["event_type"] == "oco_armed"
    assert eng._leg["oco_floor"] >= res["sl"], (
        "OCO floor must never sit below the hard stop-loss")


def test_hard_sl_outranks_armed_bracket(clk):
    eng = BuyExitStrategy(100_000.0)
    res = _open_scalp_leg(eng, seed=SEED_RANGE)
    assert _feed_flat(eng, clk, 18) is None
    clk.advance(1.5)
    assert eng.on_price(100.0)["event_type"] == "oco_armed"

    clk.advance(1.0)
    r = eng.on_price(res["sl"] - 1.0)      # breaches SL and floor together
    assert r is not None and r["reason"] == "sl", (
        "hard SL must win the tick even while the OCO bracket is armed")


def test_no_structure_means_plain_timeout_not_bracket(clk):
    """Too little tick history → no levels → old-style timeout exit."""
    eng = BuyExitStrategy(100_000.0)
    _open_scalp_leg(eng)                   # no seed
    r = _feed_flat(eng, clk, 6)            # only ~7 ticks of history
    assert r is None
    clk.advance(9.0)                       # past the shrunk 10s timeout
    r = eng.on_price(100.0)
    assert r is not None and r["reason"] == "timeout"
    assert eng._leg["oco_armed"] is False


# ── 6. Level computation unit tests ───────────────────────────────────────────

def test_pseudo_candles_ohlc():
    candles = _build_pseudo_candles([1, 3, 2, 4, 5, 6, 8, 7, 9, 10], 5)
    assert len(candles) == 2
    assert candles[0] == {"open": 1, "high": 5, "low": 1, "close": 5}
    assert candles[1] == {"open": 6, "high": 10, "low": 6, "close": 10}


def test_oco_levels_degenerate_when_price_below_support():
    """Price sitting at/below its own recent lows → no bracket (weak tape)."""
    prices = [101.0, 101.5, 102.0, 101.5, 101.0] * 6   # support at 101
    assert _compute_oco_levels(prices, 100.0, 0.5, 90.0) is None


def test_oco_levels_target_at_least_one_atr_away():
    """Flat-high structure: target must clear current + OCO_MIN_TARGET_ATR×ATR."""
    prices = [99.0, 99.5, 100.0, 99.5, 99.0] * 6       # swing high only 100
    levels = _compute_oco_levels(prices, 99.9, 2.0, 90.0)
    assert levels is not None
    target, floor = levels
    assert target >= 99.9 + 2.0 * config.OCO_MIN_TARGET_ATR
    assert 90.0 <= floor < 99.9
