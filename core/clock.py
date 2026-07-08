"""
core/clock.py — single injectable time source for the whole bot.

Every session gate, cooldown, SL phase and timeout used naive
``datetime.now()`` — correct only when the host clock is IST. This module
pins the market timezone (Asia/Kolkata) explicitly and returns **naive IST**
datetimes so all existing naive-datetime arithmetic keeps working unchanged
on any host.

It is also the replay seam: the backtest harness calls ``set_clock()`` with a
simulated clock so timeouts, SL phases and session gates behave identically
when ticks are replayed from disk.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_clock = None   # optional injected callable returning a naive datetime


def set_clock(fn):
    """Inject a clock (replay/testing). ``fn()`` must return a naive datetime."""
    global _clock
    _clock = fn


def clear_clock():
    global _clock
    _clock = None


def now() -> datetime:
    """Current time as a NAIVE datetime in IST (or the injected clock)."""
    if _clock is not None:
        return _clock()
    return datetime.now(IST).replace(tzinfo=None)


def today():
    return now().date()
