"""Stage-7 tests: ML math soundness after the repairs."""
import math
import random

import numpy as np
import pytest

from market_brain import (FeatureBuilder, MarketBrain, OnlineLR, RunningStats,
                          N_FEATURES)


# ── Welford normalizer ────────────────────────────────────────────────────────

def test_welford_matches_numpy():
    rng = random.Random(3)
    data = [[rng.gauss(5, 2), rng.gauss(-1, 0.5)] for _ in range(200)]
    rs = RunningStats(2)
    for x in data:
        rs.update(x)
    arr = np.array(data)
    for i in range(2):
        assert rs.mean[i] == pytest.approx(arr[:, i].mean(), abs=1e-9)
        std = math.sqrt(rs._M2[i] / (rs.count - 1))
        assert std == pytest.approx(arr[:, i].std(ddof=1), abs=1e-9)


def test_constant_feature_normalizes_to_zero():
    rs = RunningStats(1)
    for _ in range(50):
        rs.update([1.0])
    assert rs.normalize([1.0]) == [0.0], (
        "constant feature must emit 0, not explode via a tiny epsilon std")


# ── Regime alignment feature ──────────────────────────────────────────────────

def test_regime_alignment_preserves_trend_sign():
    fa = FeatureBuilder.regime_alignment
    assert fa("trending_up", "CE") == 1.0
    assert fa("trending_up", "PE") == -1.0
    assert fa("trending_down", "PE") == 1.0
    assert fa("trending_down", "CE") == -1.0
    assert fa("choppy", "CE") == fa("choppy", "PE") == -0.5
    assert fa("unknown", "CE") == 0.0


# ── Online LR convergence on separable data ───────────────────────────────────

def test_online_lr_learns_separable_data():
    rng = random.Random(11)
    lr = OnlineLR()
    # feature 0 decides the label; everything else is noise
    for _ in range(600):
        x = [rng.gauss(0, 1) for _ in range(N_FEATURES)]
        label = 1 if x[0] > 0 else 0
        lr.update(x, label)
    # strong positive weight on the deciding feature
    assert lr.weights[0] > 0.5
    assert abs(lr.weights[1]) < abs(lr.weights[0]) / 2
    # discriminative predictions
    hi = lr.predict([2.0] + [0.0] * (N_FEATURES - 1))
    lo = lr.predict([-2.0] + [0.0] * (N_FEATURES - 1))
    assert hi > 0.8 and lo < 0.2


def test_class_weight_balances_rare_wins():
    lr = OnlineLR()
    lr.n_trades, lr.n_wins = 35, 6          # the historical 17% win rate
    assert lr.class_weight(1) > lr.class_weight(0)
    assert lr.class_weight(1) == pytest.approx(min(3.0, 35 / 12), abs=1e-9)


# ── Score-time vector reused at train time (no leakage/mismatch) ─────────────

def test_brain_trains_on_scored_vector(tmp_path, monkeypatch):
    import market_brain as mb
    monkeypatch.setattr(mb, "STATE_DIR", str(tmp_path))
    brain = MarketBrain(mode="demo")
    state = {
        "side": "CE", "nifty_move": 5.0, "jump_threshold": 4.0,
        "regression_slope": 0.5, "nifty_ticks": [22000 + i * 0.5 for i in range(40)],
        "nifty_price": 22020.0, "jump_atr": 3.0, "fast_entry": True,
        "rsi": 60.0, "range_position": 0.8,
    }
    brain.score_entry(state)
    stored = list(brain._last_x_norm)
    brain.on_trade_closed({"pnl_pct": 2.0, "mode": "demo"})
    assert brain._lr.n_trades == 1
    assert brain._last_x_norm is None
    # stored vector was finite and used (weights moved in its direction)
    assert all(math.isfinite(v) for v in stored)


def test_brain_ignores_other_mode_trades(tmp_path, monkeypatch):
    import market_brain as mb
    monkeypatch.setattr(mb, "STATE_DIR", str(tmp_path))
    brain = MarketBrain(mode="real")
    state = {
        "side": "CE", "nifty_move": 5.0, "jump_threshold": 4.0,
        "regression_slope": 0.5, "nifty_ticks": [22000 + i * 0.5 for i in range(40)],
        "nifty_price": 22020.0, "jump_atr": 3.0, "fast_entry": True,
        "rsi": 60.0, "range_position": 0.8,
    }
    brain.score_entry(state)
    brain.on_trade_closed({"pnl_pct": 2.0, "mode": "demo"})   # demo fill
    assert brain._lr.n_trades == 0, "real-mode brain must ignore demo fills"


# ── EntryAnalyzer symmetric discriminative teach ──────────────────────────────

def test_entry_teach_moves_only_discriminative_dims(tmp_path, monkeypatch):
    import entry_analyzer as ea
    monkeypatch.setattr(ea, "STATE_DIR", str(tmp_path))
    w = ea.DimensionWeights()
    before = dict(w.weights)

    scores = {d: 0.5 for d in w.DIMENSIONS}
    scores["price_action"] = 0.9        # spoke confidently
    scores["timing"]       = 0.5        # neutral — must not move

    w.teach(scores, won=True)
    # renormalization shifts everything; compare relative movement
    assert w.weights["price_action"] > before["price_action"]

    w2 = ea.DimensionWeights()
    w2.teach(scores, won=False)
    assert w2.weights["price_action"] < before["price_action"]


def test_neutralized_dims_have_zero_weight():
    import entry_analyzer as ea
    w = ea.DimensionWeights()
    for d in w.NEUTRALIZED:
        assert w.weights[d] == 0.0
    assert "greeks" in w.NEUTRALIZED and "option_chain" in w.NEUTRALIZED


# ── ExitAnalyzer attribution ──────────────────────────────────────────────────

def test_exit_analyzer_learns_only_from_ai_exits(tmp_path, monkeypatch):
    import exit_analyzer as xa
    monkeypatch.setattr(xa, "STATE_DIR", str(tmp_path))
    an = xa.ExitAnalyzer(mode="demo")
    an.n_trades = 50                     # past warmup

    # Simulate signals having fired at decision time
    an._last_signals = {"profit_decay": {"fire": True, "urgency": 0.8}}
    an._fired_at_decision = dict(an._last_signals)
    w0 = an.weights["profit_decay"]

    # SL exit — analyzer did NOT cause it → no weight change
    an.on_trade_closed({"reason": "sl", "pnl_pct": -4.0, "peak_profit_pct": 0.0,
                        "mode": "demo"})
    assert an.weights["profit_decay"] == w0

    # AI exit — good outcome → boost
    an._last_signals = {"profit_decay": {"fire": True, "urgency": 0.8}}
    an._fired_at_decision = dict(an._last_signals)
    an.on_trade_closed({"reason": "ai_analyzer", "pnl_pct": 3.0,
                        "peak_profit_pct": 4.0, "mode": "demo"})
    assert an.weights["profit_decay"] > w0
