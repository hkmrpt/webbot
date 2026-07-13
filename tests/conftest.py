"""Shared fixtures — isolate every test from real state/log files."""
import os
import sys

import pytest

# Make the project root importable when pytest is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import buy_exit_strategy as bes            # noqa: E402
import exit_analyzer as ea_mod             # noqa: E402
import excel_logger as xl_mod              # noqa: E402
import position_sizer as ps_mod            # noqa: E402
import market_brain as mb_mod              # noqa: E402
import entry_analyzer as ent_mod           # noqa: E402
import market_intelligence as mi_mod       # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_files(tmp_path, monkeypatch):
    """Redirect trade log + Excel journal + ALL learning-engine state + cwd
    into a temp dir so tests never touch (or train) the real log/state
    files. A missed engine here silently contaminates live ML state and
    makes later tests order-dependent."""
    monkeypatch.setattr(bes, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(ea_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ps_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mb_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ent_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mi_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(xl_mod, "EXCEL_LOG", str(tmp_path / "trade_log.xlsx"))
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture(autouse=True)
def _no_live_atm_resolve(monkeypatch):
    """Tests must never reach Zerodha. process_ticks spawns daemon threads
    that resolve/roll the ATM strike over live HTTP; one finishing LATE
    stamps real option tokens over a later test's fixture slots — an
    intermittent, order-dependent failure. Resolve always 'fails' here."""
    import buy_app as app_mod
    monkeypatch.setattr(app_mod, "_resolve_nifty_atm", lambda p: None)
