"""Shared fixtures — isolate every test from real state/log files."""
import os
import sys

import pytest

# Make the project root importable when pytest is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import buy_exit_strategy as bes            # noqa: E402
import exit_analyzer as ea_mod             # noqa: E402
import excel_logger as xl_mod              # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_files(tmp_path, monkeypatch):
    """Redirect trade log + Excel journal + analyzer state + cwd into a temp
    dir so tests never touch the real log/state files."""
    monkeypatch.setattr(bes, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(ea_mod, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(xl_mod, "EXCEL_LOG", str(tmp_path / "trade_log.xlsx"))
    monkeypatch.chdir(tmp_path)
    yield
