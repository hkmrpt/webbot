"""
engine/signal_hub.py  ──  Signal Aggregation
══════════════════════════════════════════════
Collects signals from all strategies each tick and:
  1. Filters by active sides and minimum confidence
  2. Detects CE/PE conflicts — cancels if too close in confidence
  3. Applies global inter-trade cooldown
  4. Returns the single best Signal or None

Also tracks signal history for the dashboard.
"""

from datetime import datetime
from typing import Optional, List
from .strategies import Signal


class SignalHub:

    def __init__(self, min_confidence: float = 0.65, global_cooldown: float = 10.0):
        self.min_confidence   = min_confidence
        self._global_cooldown = global_cooldown
        self._last_ts: Optional[datetime] = None
        self.history: list = []       # last 200 signals for dashboard

    # ── Main entry ────────────────────────────────────────────

    def process(self,
                signals: List[Optional[Signal]],
                active_sides: set) -> Optional[Signal]:
        """
        Given signals from all strategies, return best Signal or None.
        signals may contain None entries (strategies that passed).
        """
        # Strip None
        valid = [s for s in signals if s is not None]
        if not valid:
            return None

        # Only sides that are armed (CE/PE tokens loaded)
        valid = [s for s in valid if s.side in active_sides]
        if not valid:
            return None

        # Confidence floor
        valid = [s for s in valid if s.confidence >= self.min_confidence]
        if not valid:
            return None

        # Global inter-trade cooldown
        if self._last_ts:
            elapsed = (datetime.now() - self._last_ts).total_seconds()
            if elapsed < self._global_cooldown:
                return None

        # Conflict detection: opposing sides present
        sides_present = {s.side for s in valid}
        if len(sides_present) > 1:
            # Keep only the dominant side if confidence gap >= 0.12
            ce_best = max((s for s in valid if s.side == "CE"),
                          key=lambda s: s.confidence, default=None)
            pe_best = max((s for s in valid if s.side == "PE"),
                          key=lambda s: s.confidence, default=None)
            if ce_best and pe_best:
                gap = abs(ce_best.confidence - pe_best.confidence)
                if gap < 0.12:
                    return None   # too ambiguous
                valid = [ce_best] if ce_best.confidence > pe_best.confidence else [pe_best]

        # Pick highest confidence
        best = max(valid, key=lambda s: s.confidence)
        self._last_ts = datetime.now()
        self._record(best)
        return best

    # ── Helpers ───────────────────────────────────────────────

    def _record(self, sig: Signal):
        self.history.append({
            "ts":         datetime.now().strftime("%H:%M:%S"),
            "strategy":   sig.strategy,
            "side":       sig.side,
            "confidence": round(sig.confidence, 3),
            "reason":     sig.reason,
            "urgency":    sig.urgency,
        })
        if len(self.history) > 200:
            self.history = self.history[-200:]

    def last_signals_snapshot(self, n: int = 10) -> list:
        return self.history[-n:]
