"""Stage-2 tests: hard entry gates, budget cap, clock, deque resize, recorder."""
import gzip
import json
import os
import time
from datetime import datetime

import pytest

import buy_app
from buy_app import S, RC


@pytest.fixture
def clean_session():
    """Snapshot and restore the S/RC keys the tests mutate."""
    s_keys  = ("session_pnl", "trades_today", "wins", "losses", "loss_streak",
               "capital", "day_start_capital", "running", "last_skip_reason",
               "nifty_ticks", "nifty_tick_times", "nifty_atr_ticks", "scalp_mode")
    rc_keys = ("max_daily_loss", "max_trades_day", "regression_window",
               "jump_atr_window")
    s_snap  = {k: S.get(k) for k in s_keys}
    rc_snap = {k: RC.get(k) for k in rc_keys}
    yield
    S.update(s_snap)
    RC.update(rc_snap)


# ── Daily-limit predicate is pure ─────────────────────────────────────────────

def test_daily_limit_breached_is_pure(clean_session):
    S["running"] = True
    S["day_start_capital"] = 100_000.0
    S["session_pnl"] = -(RC["max_daily_loss"] + 1)
    reason = buy_app._daily_limit_breached()
    assert reason and "loss limit" in reason.lower()
    assert S["running"] is True, "pure predicate must not stop the robot"


def test_daily_limit_ok_when_flat(clean_session):
    S["session_pnl"] = 0.0
    S["day_start_capital"] = 100_000.0
    assert buy_app._daily_limit_breached() is None


def test_zero_capital_does_not_trip_limits(clean_session):
    """Real mode with an unfunded account (capital ₹0) must not instantly
    breach drawdown/profit-target — those limits scale with capital."""
    S["running"] = True
    S["day_start_capital"] = 0.0
    S["session_pnl"] = 0.0
    assert buy_app._daily_limit_breached() is None


def test_daily_limit_breach_never_stops_robot(clean_session):
    """A breached limit pauses entries but must NOT flip running=False;
    the breach is announced once (deferred log), not on every tick."""
    S["running"] = True
    S["day_start_capital"] = 100_000.0
    S["session_pnl"] = -(RC["max_daily_loss"] + 1)
    S["_limit_breach_notified"] = False
    S["_deferred_logs"] = []

    assert buy_app._check_daily_limits() is False
    assert S["running"] is True, "limit breach must not stop the robot"
    assert len(S["_deferred_logs"]) == 1

    assert buy_app._check_daily_limits() is False   # second breach check
    assert len(S["_deferred_logs"]) == 1, "breach announced only once"

    S["session_pnl"] = 0.0                          # breach cleared
    assert buy_app._check_daily_limits() is True
    assert S["_limit_breach_notified"] is False, "flag resets when clear"
    S.pop("_deferred_logs", None)


# ── Hard entry gates (shared by auto + manual paths) ─────────────────────────

def _healthy_session():
    S["running"] = True
    S["session_pnl"] = 0.0
    S["capital"] = 100_000.0
    S["day_start_capital"] = 100_000.0
    S["wins"] = 0
    S["losses"] = 0
    S["loss_streak"] = 0


def test_entry_gates_block_on_daily_loss(clean_session):
    _healthy_session()
    S["trades_today"] = 0
    S["session_pnl"] = -(RC["max_daily_loss"] + 1)
    ok, reason = buy_app._entry_gates_ok()
    assert not ok and "loss limit" in reason.lower()


def test_entry_gates_block_on_max_trades(clean_session):
    _healthy_session()
    RC["max_trades_day"] = 5
    S["trades_today"] = 5
    ok, reason = buy_app._entry_gates_ok()
    assert not ok and "max trades" in reason.lower()


def test_entry_gates_pass_when_healthy(clean_session):
    _healthy_session()
    RC["max_trades_day"] = 15
    S["trades_today"] = 0
    ok, reason = buy_app._entry_gates_ok()
    assert ok, f"healthy session blocked: {reason}"


# ── Trade budget respects the hard operator cap ──────────────────────────────

def test_trade_budget_hard_cap():
    from market_intelligence import compute_trade_budget
    stats = {
        "trades_today": 3, "wins": 3, "losses": 0, "session_pnl": 5000.0,
        "loss_streak": 0, "capital": 105_000.0, "day_start_capital": 100_000.0,
        "max_trades_cap": 3,
    }
    budget = compute_trade_budget({}, {}, {}, stats, [])
    assert budget["max_trades"] <= 3
    assert budget["remaining"] == 0


# ── Clock ─────────────────────────────────────────────────────────────────────

def test_clock_returns_naive_datetime_and_is_injectable():
    from core import clock
    t = clock.now()
    assert isinstance(t, datetime) and t.tzinfo is None

    fixed = datetime(2026, 7, 8, 10, 30, 0)
    clock.set_clock(lambda: fixed)
    try:
        assert clock.now() == fixed
        assert clock.today() == fixed.date()
    finally:
        clock.clear_clock()
    assert clock.now() != fixed


# ── RC window params resize live deques ───────────────────────────────────────

def test_resize_tick_deques_preserves_contents(clean_session):
    from collections import deque
    S["nifty_ticks"]      = deque([1, 2, 3], maxlen=10)
    S["nifty_tick_times"] = deque([1, 2, 3], maxlen=10)
    S["nifty_atr_ticks"]  = deque([1, 2, 3], maxlen=10)
    RC["regression_window"] = 50
    RC["jump_atr_window"]   = 40
    buy_app._resize_tick_deques()
    assert S["nifty_ticks"].maxlen >= 50
    assert S["nifty_atr_ticks"].maxlen == 42
    assert list(S["nifty_ticks"]) == [1, 2, 3], "contents must be preserved"


# ── Tick recorder ─────────────────────────────────────────────────────────────

def test_recorder_writes_ticks_and_meta(tmp_path):
    from replay.recorder import TickRecorder
    rec = TickRecorder(str(tmp_path / "ticks"), enabled=True)
    rec.record(256265, 22415.5, 0)
    rec.record(12345, 118.4, 5000)
    rec.record_meta("atm_resolved", {"strike": 22400})

    # writer flushes every ~2s
    deadline = time.time() + 8
    files = []
    while time.time() < deadline:
        d = tmp_path / "ticks"
        files = list(d.glob("ticks_*.jsonl.gz")) if d.exists() else []
        if files:
            with gzip.open(files[0], "rt", encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]
            if len(lines) >= 3:
                break
        time.sleep(0.3)

    assert files, "recorder never created a tick file"
    assert len(lines) == 3
    assert lines[0]["tk"] == 256265 and lines[0]["p"] == 22415.5
    assert lines[2]["meta"] == "atm_resolved" and lines[2]["strike"] == 22400


def test_recorder_disabled_writes_nothing(tmp_path):
    from replay.recorder import TickRecorder
    rec = TickRecorder(str(tmp_path / "ticks"), enabled=False)
    rec.record(256265, 22415.5)
    time.sleep(0.1)
    assert not (tmp_path / "ticks").exists()
