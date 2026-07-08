"""Stage-4 equivalence tests: incremental indicators == batch formulas."""
import random

import numpy as np
import pytest

from engine.indicators import RollingATR, RollingSlope, WilderRSI


def _random_walk(n, seed, start=22000.0, step=3.0):
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p += rng.uniform(-step, step)
        out.append(round(p, 2))
    return out


# ── RollingSlope vs np.polyfit ────────────────────────────────────────────────

@pytest.mark.parametrize("seed", [1, 7, 42])
def test_rolling_slope_matches_polyfit(seed):
    window = 32
    rs = RollingSlope(window)
    prices = _random_walk(200, seed)
    for i, p in enumerate(prices):
        rs.push(p)
        tail = prices[max(0, i + 1 - window): i + 1]
        if len(tail) >= 3:
            expected = float(np.polyfit(range(len(tail)), tail, 1)[0])
            got = rs.slope()
            assert got == pytest.approx(expected, abs=1e-6), f"tick {i}"
        else:
            assert rs.slope() is None


# ── RollingATR vs batch mean-|Δ| ─────────────────────────────────────────────

@pytest.mark.parametrize("seed", [2, 9])
def test_rolling_atr_matches_batch(seed):
    window = 31          # buy_app deque holds window+1 prices → window diffs
    ra = RollingATR(window)
    prices = _random_walk(150, seed)
    for i, p in enumerate(prices):
        ra.push(p)
        tail = prices[max(0, i + 1 - (window + 1)): i + 1]
        if len(tail) >= 2:
            diffs = [abs(tail[j] - tail[j - 1]) for j in range(1, len(tail))]
            expected = sum(diffs) / len(diffs)
            assert ra.atr() == pytest.approx(expected, abs=1e-6), f"tick {i}"
        else:
            assert ra.atr() is None


# ── WilderRSI sanity ─────────────────────────────────────────────────────────

def test_wilder_rsi_bounds_and_direction():
    r = WilderRSI(14)
    for p in range(100, 140):        # monotonic rise
        r.push(float(p))
    assert r.rsi() > 90

    r2 = WilderRSI(14)
    for p in range(140, 100, -1):    # monotonic fall
        r2.push(float(p))
    assert r2.rsi() < 10

    r3 = WilderRSI(14)
    r3.push(100.0)
    r3.push(101.0)
    assert r3.rsi() == 50.0          # warmup → neutral


def test_wilder_rsi_matches_simple_avg_during_accumulation():
    """First `period` diffs must match the bot's original simple-average RSI."""
    prices = _random_walk(15, seed=5)          # exactly 14 diffs
    r = WilderRSI(14)
    for p in prices:
        r.push(p)
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_g = sum(gains) / 14 if gains else 0.0
    avg_l = sum(losses) / 14 if losses else 0.0
    expected = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    assert r.rsi() == pytest.approx(expected, abs=1e-9)


def test_reset_clears_state():
    rs, ra, wr = RollingSlope(10), RollingATR(10), WilderRSI(5)
    for p in [1.0, 2.0, 3.0, 4.0]:
        rs.push(p); ra.push(p); wr.push(p)
    rs.reset(); ra.reset(); wr.reset()
    assert rs.slope() is None and ra.atr() is None and wr.rsi() == 50.0
