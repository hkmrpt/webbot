"""
core/emitter.py — pluggable event sink.

Live app installs a SocketIO-backed emitter at startup; the replay harness
installs a no-op (or stats-collecting) emitter so the exact strategy code can
run headless without a web server.
"""

_impl = None            # callable(event: str, payload) -> None


def set_emitter(fn):
    global _impl
    _impl = fn


def clear_emitter():
    global _impl
    _impl = None


def emit(event: str, payload):
    if _impl is not None:
        try:
            _impl(event, payload)
        except Exception as e:      # an emitter failure must never kill trading
            print(f"[emitter] {event} failed: {e}")


class NullEmitter:
    """Collects nothing, emits nothing — replay default."""
    def __call__(self, event, payload):
        pass


class CollectingEmitter:
    """Collects events in memory — useful for replay stats/tests."""
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def __call__(self, event, payload):
        self.events.append((event, payload))
