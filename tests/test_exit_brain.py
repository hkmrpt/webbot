"""Stage-1 tests for ExitBrain / AdaptiveTrailEngine / MomentumScorer."""
from collections import deque

import config
from exit_brain import AdaptiveTrailEngine, ExitBrain, MomentumScorer


# ── Brain trail influence clamped ─────────────────────────────────────────────

def test_adaptive_trail_clamped_upper():
    eng = AdaptiveTrailEngine()
    eng._multiplier = 2.0                      # learned max
    # momentum 1.0 → factor 1.5 × 2.0 = 3.0 → must clamp to 1.3
    assert eng.get_trail_pct(10.0, 1.0) == round(10.0 * config.BRAIN_TRAIL_MAX_FACTOR, 1)


def test_adaptive_trail_clamped_lower():
    eng = AdaptiveTrailEngine()
    eng._multiplier = 0.5                      # learned min
    # momentum 0.0 → factor 0.5 × 0.5 = 0.25 → must clamp to 0.7
    assert eng.get_trail_pct(10.0, 0.0) == round(10.0 * config.BRAIN_TRAIL_MIN_FACTOR, 1)


def test_adaptive_trail_absolute_floor_and_ceiling():
    eng = AdaptiveTrailEngine()
    assert eng.get_trail_pct(1.0, 0.0) >= eng.MIN_TRAIL
    assert eng.get_trail_pct(50.0, 1.0) <= eng.MAX_TRAIL


# ── Momentum state must reset between trades ──────────────────────────────────

def test_momentum_scorer_reset_clears_velocity_history():
    sc = MomentumScorer()
    sc.score(deque([100, 105, 111, 118, 126, 135]))   # populates _vel_hist
    assert len(sc._vel_hist) > 0
    sc.reset()
    assert len(sc._vel_hist) == 0


def test_exit_brain_on_trade_closed_resets_trade_state():
    brain = ExitBrain()
    brain.score(deque([100, 103, 107, 112, 118]))
    brain.update_profit(5.0)
    assert len(brain._scorer._vel_hist) > 0
    assert len(brain._profit_hist) > 0
    brain.on_trade_closed({"pnl_pct": 2.0, "peak_profit_pct": 3.0, "reason": "trail"})
    assert len(brain._scorer._vel_hist) == 0
    assert len(brain._profit_hist) == 0
    assert brain._n_trades == 1


# ── Decay exit: warmup + tied-price sequences ─────────────────────────────────

def _trained_brain():
    b = ExitBrain()
    b._n_trades = config.AI_EXIT_MIN_TRADES
    return b


def test_decay_exit_blocked_during_warmup():
    b = ExitBrain()                            # 0 trades
    for p in [5.0, 4.5, 4.0, 3.5, 3.0, 2.5]:
        b.update_profit(p)
    assert b.check_ai_exit(2.5, momentum_score=0.1) is False


def test_decay_fires_on_tied_then_down_sequence():
    b = _trained_brain()
    # A tied print (5.0, 5.0) previously disabled the strict < check entirely
    for p in [5.0, 5.0, 4.5, 4.0, 4.0, 3.5]:
        b.update_profit(p)
    assert b.check_ai_exit(3.5, momentum_score=0.1) is True


def test_decay_does_not_fire_on_flat_sequence():
    b = _trained_brain()
    for p in [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]:
        b.update_profit(p)
    assert b.check_ai_exit(5.0, momentum_score=0.1) is False


def test_decay_does_not_fire_when_momentum_high():
    b = _trained_brain()
    for p in [5.0, 4.5, 4.0, 3.5, 3.0, 2.5]:
        b.update_profit(p)
    assert b.check_ai_exit(2.5, momentum_score=0.8) is False
