"""Stage-6 smoke test: the replay harness runs the real strategy code over a
synthetic recorded day, applies ATM meta events, keeps all output isolated,
and produces a report."""
import gzip
import json
import os
from datetime import datetime

import pytest

from core.clock import IST

NIFTY_TOKEN = 256265
CE_TOKEN, PE_TOKEN = 111111, 222222


def _epoch(h, m, s=0):
    return datetime(2026, 7, 8, h, m, s, tzinfo=IST).timestamp()


def _write_fixture(path):
    """A synthetic session: ATM meta, then NIFTY + option ticks with a
    quiet phase followed by a sharp CE-friendly spike."""
    lines = [
        {"t": _epoch(9, 30), "meta": "session_start", "index": "NIFTY", "mode": "demo"},
        {"t": _epoch(9, 30, 1), "meta": "atm_resolved", "strike": 22400,
         "expiry": "2026-07-14", "ce_token": CE_TOKEN, "pe_token": PE_TOKEN,
         "ce_symbol": "NIFTY26714224 00CE".replace(" ", ""), "pe_symbol": "NIFTY2671422400PE"},
    ]
    nifty, ce, pe = 22400.0, 120.0, 118.0
    t = _epoch(10, 0)
    # quiet phase — builds indicator history
    for i in range(60):
        t += 1.0
        nifty += 0.4 if i % 2 == 0 else -0.3
        ce    += 0.1 if i % 2 == 0 else -0.1
        lines.append({"t": t, "tk": NIFTY_TOKEN, "p": round(nifty, 2), "v": 0})
        lines.append({"t": t + 0.01, "tk": CE_TOKEN, "p": round(ce, 2), "v": 1000 + i})
        lines.append({"t": t + 0.02, "tk": PE_TOKEN, "p": round(pe, 2), "v": 1000 + i})
    # spike phase — fast rise
    for i in range(30):
        t += 1.0
        nifty += 4.0
        ce    += 2.5
        pe    -= 1.5
        lines.append({"t": t, "tk": NIFTY_TOKEN, "p": round(nifty, 2), "v": 0})
        lines.append({"t": t + 0.01, "tk": CE_TOKEN, "p": round(ce, 2), "v": 5000 + i * 100})
        lines.append({"t": t + 0.02, "tk": PE_TOKEN, "p": round(max(pe, 5), 2), "v": 900})
    # fade phase
    for i in range(40):
        t += 1.0
        nifty -= 1.2
        ce    -= 0.9
        lines.append({"t": t, "tk": NIFTY_TOKEN, "p": round(nifty, 2), "v": 0})
        lines.append({"t": t + 0.01, "tk": CE_TOKEN, "p": round(max(ce, 5), 2), "v": 2000})

    with gzip.open(path, "wt", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return len([l for l in lines if "tk" in l]), len([l for l in lines if "meta" in l])


@pytest.fixture
def _module_attr_guard():
    """The runner rebinds module-level dirs — restore them afterwards."""
    import buy_exit_strategy as bes
    import exit_analyzer, market_brain, entry_analyzer, market_intelligence, excel_logger
    saved = [(bes, "LOG_DIR", bes.LOG_DIR),
             (exit_analyzer, "STATE_DIR", exit_analyzer.STATE_DIR),
             (market_brain, "STATE_DIR", market_brain.STATE_DIR),
             (entry_analyzer, "STATE_DIR", entry_analyzer.STATE_DIR),
             (market_intelligence, "STATE_DIR", market_intelligence.STATE_DIR),
             (excel_logger, "EXCEL_LOG", excel_logger.EXCEL_LOG)]
    yield
    for mod, attr, val in saved:
        setattr(mod, attr, val)


def test_replay_runs_end_to_end_isolated(tmp_path, _module_attr_guard):
    fixture = tmp_path / "ticks_20260708.jsonl.gz"
    n_ticks, n_meta = _write_fixture(str(fixture))

    out_dir = tmp_path / "replay_out"
    from replay.replay import ReplayRunner
    runner = ReplayRunner([str(fixture)], out_dir=str(out_dir), capital=100_000.0)
    report = runner.run()

    # Every tick consumed, meta applied
    assert report["ticks_replayed"] == n_ticks
    assert report["meta_events"] == n_meta

    # ATM meta actually landed in the slots
    import buy_app
    assert buy_app.S["slots"]["CE"]["token"] == CE_TOKEN
    assert buy_app.S["slots"]["PE"]["token"] == PE_TOKEN
    assert buy_app.S["nifty_atm"]["strike"] == 22400

    # Report file written to the isolated out dir
    assert os.path.exists(out_dir / "report.json")

    # If the strategy traded, the trade log must be in the ISOLATED dir
    if report["trades"] > 0:
        assert os.path.exists(out_dir / "trade_log.csv")

    # Replay flag cleared, robot not left running
    assert buy_app.S["_replay"] is False
    assert buy_app.S["running"] is False


def test_replay_exit_engine_produces_isolated_trade_log(tmp_path, _module_attr_guard):
    """Force a trade mid-replay to exercise the exit path + CSV isolation
    deterministically (entry gates may legitimately reject the synthetic
    spike — that's strategy behaviour, not harness behaviour)."""
    fixture = tmp_path / "ticks_20260708.jsonl.gz"
    _write_fixture(str(fixture))

    out_dir = tmp_path / "replay_out2"
    from replay.replay import ReplayRunner
    runner = ReplayRunner([str(fixture)], out_dir=str(out_dir), capital=100_000.0)
    runner._isolate()

    import buy_app
    from core import clock
    lines = json.loads("[]")  # placeholder for clarity

    sim = {"now": _epoch(10, 0)}
    clock.set_clock(lambda: datetime.fromtimestamp(sim["now"], IST).replace(tzinfo=None))
    try:
        with buy_app._state_lock:
            buy_app._reset_session_state(NIFTY_TOKEN, "NIFTY")
            buy_app.S["_replay"] = True
            buy_app.S["trading_mode"] = "demo"
            buy_app.S["slots"]["CE"]["token"] = CE_TOKEN
            buy_app.S["slots"]["CE"]["symbol"] = "TESTCE"
            buy_app.S["slots"]["CE"]["price"] = 100.0
            buy_app.S["nifty_price"] = 22400.0
            buy_app.S["nifty_move"] = 0.0
            buy_app.S["regression_slope"] = 0.0
            r = buy_app._enter_trade_state("CE", 22400.0,
                                           override_params={"sl_pct_p1": 10.0})
            assert r is not None

        # Crash the option price → hard SL must close the trade
        sim["now"] += 5
        buy_app.process_ticks([{"instrument_token": CE_TOKEN, "last_price": 85.0}])

        assert buy_app.S["trade_open"] is False
        csv_path = out_dir / "trade_log.csv"
        assert csv_path.exists(), "trade CSV must land in the replay out dir"
        content = csv_path.read_text()
        assert ",sl," in content or content.count("sl") > 0
    finally:
        clock.clear_clock()
        with buy_app._state_lock:
            buy_app.S["_replay"] = False
            buy_app.S["running"] = False
