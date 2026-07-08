"""
replay/recorder.py — buffered tick recorder.

Appends every decoded WebSocket tick (plus meta events like ATM resolution
and session start) to a daily gzip JSONL file:

    ticks/ticks_YYYYMMDD.jsonl.gz

Design constraints:
  * The hot tick path must never block: ``record()`` is a single
    ``queue.put_nowait`` — no disk I/O, no locks shared with trading state.
  * A daemon writer thread drains the queue and flushes every ~2 s.
  * Ticks are recorded with wall-clock timestamps so the replay harness can
    reproduce elapsed-time behaviour (SL phases, timeouts, session gates).

Line formats (JSON, one per line):
  {"t": 1751971800.123, "tk": 256265, "p": 22415.6, "v": 0}
  {"t": ..., "meta": "atm_resolved", ...arbitrary payload}
"""

import gzip
import json
import os
import queue
import sys
import threading
import time

_BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
             if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FLUSH_INTERVAL_SECS = 2.0
QUEUE_MAX           = 50_000     # ~ minutes of extreme burst before dropping


class TickRecorder:
    """Lock-free-ish buffered recorder. Safe to call from the tick thread."""

    def __init__(self, tick_dir: str = "ticks", enabled: bool = True):
        self.enabled  = enabled
        self._dir     = tick_dir if os.path.isabs(tick_dir) else os.path.join(_BASE_DIR, tick_dir)
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._dropped = 0
        self._thread  = None
        self._started = False
        self._lock    = threading.Lock()   # writer-startup lock only

    # ── hot path ──────────────────────────────────────────────────────────
    def record(self, token: int, price: float, volume: float = 0, ts: float | None = None):
        if not self.enabled:
            return
        self._ensure_writer()
        try:
            self._q.put_nowait({"t": ts or time.time(), "tk": token, "p": price, "v": volume})
        except queue.Full:
            self._dropped += 1

    def record_meta(self, event: str, payload: dict | None = None):
        if not self.enabled:
            return
        self._ensure_writer()
        line = {"t": time.time(), "meta": event}
        if payload:
            line.update(payload)
        try:
            self._q.put_nowait(line)
        except queue.Full:
            self._dropped += 1

    # ── writer thread ─────────────────────────────────────────────────────
    def _ensure_writer(self):
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            os.makedirs(self._dir, exist_ok=True)
            self._thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="tick-recorder")
            self._thread.start()
            self._started = True

    def _path_for_today(self) -> str:
        day = time.strftime("%Y%m%d")
        return os.path.join(self._dir, f"ticks_{day}.jsonl.gz")

    def _writer_loop(self):
        buf: list[dict] = []
        while True:
            try:
                # Block for the first item, then drain whatever is queued
                buf.append(self._q.get(timeout=FLUSH_INTERVAL_SECS))
                while True:
                    try:
                        buf.append(self._q.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if buf:
                try:
                    with gzip.open(self._path_for_today(), "at", encoding="utf-8") as f:
                        for line in buf:
                            f.write(json.dumps(line, separators=(",", ":")) + "\n")
                except OSError as e:
                    print(f"[Recorder] write error: {e}")
                buf.clear()
            time.sleep(FLUSH_INTERVAL_SECS)

    @property
    def stats(self) -> dict:
        return {"queued": self._q.qsize(), "dropped": self._dropped,
                "enabled": self.enabled, "dir": self._dir}
