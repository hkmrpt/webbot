"""
self_tuner.py  ──  Option Buy Robot v8.3
════════════════════════════════════════

Self-learning entry parameter tuner.
SL%, trail%, and timeout are now fully owned by ExitBrain (retrospective LR).
This tuner handles only the three entry gate parameters:

  confirm_ticks — spike confirmation ticks required     default 3    range 1–5
  jump_floor    — minimum spike pts (NIFTY points)      default 4    range 4–7
  slope_min     — minimum NIFTY regression slope        default 0.3  range 0.1–1.5

Learning rule (EWMA per trade):
  On a winning trade → nudge parameter toward the value that was active
  On a losing trade  → nudge parameter away from the value that was active
  After MIN_TRADES trades → apply learned values to RC for next trade

Bootstraps from trade_log.csv if tuner_state.json does not exist.
Persists to tuner_state.json after every trade.
"""

import csv
import json
import os

TUNER_STATE_FILE = "tuner_state.json"

MIN_TRADES       = 15    # neutral below this — not enough data
CONFIDENCE_SCALE = 40.0  # trades above MIN_TRADES to reach full confidence
NUDGE_STRENGTH   = 0.40  # how hard to push toward winning values (0–1)
ALPHA            = 0.12  # EWMA learning rate per trade
WIN_THRESHOLD    = 1.5   # pnl_pct >= this → win

TUNABLE = {
    "confirm_ticks": {"default": 3.0, "min": 1.0, "max": 5.0},
    "jump_floor":    {"default": 4.0, "min": 4.0, "max": 7.0},
    "slope_min":     {"default": 0.3, "min": 0.1, "max": 1.5},
}


class SelfTuner:
    """
    Online EWMA entry parameter optimizer.

    Usage in buy_app.py:
      At trade entry:
        S["entry_params"] = _self_tuner.snapshot_entry(S, RC)

      At trade close:
        _self_tuner.on_trade_closed(result, S["entry_params"])
        _self_tuner.apply_to_rc(RC)

      Dashboard:
        _self_tuner.state
    """

    def __init__(self):
        self._n        = 0
        self._win_n    = 0
        self._loss_n   = 0
        self._win_avg  = {k: v["default"] for k, v in TUNABLE.items()}
        self._loss_avg = {k: v["default"] for k, v in TUNABLE.items()}
        self._learned  = {k: v["default"] for k, v in TUNABLE.items()}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def snapshot_entry(self, S: dict, RC: dict) -> dict:
        """Call at trade entry. Returns dict to store in S['entry_params']."""
        return {
            "confirm_ticks": float(S.get("confirm_needed",       self._learned["confirm_ticks"])),
            "jump_floor":    float(S.get("effective_jump_mult",   self._learned["jump_floor"])),
            "slope_min":     float(RC.get("regression_slope_min", self._learned["slope_min"])),
        }

    def on_trade_closed(self, result: dict, active: dict):
        """Update EWMA baselines from this trade's outcome."""
        if not active:
            return
        self._n += 1
        win    = (result.get("pnl_pct") or 0.0) >= WIN_THRESHOLD
        target = self._win_avg if win else self._loss_avg

        if win:
            self._win_n  += 1
        else:
            self._loss_n += 1

        for k in TUNABLE:
            if k in active:
                v = float(active[k])
                target[k] = target[k] * (1 - ALPHA) + v * ALPHA

        self._recompute()
        self._save()

    def apply_to_rc(self, RC: dict):
        """Write learned values into RC for the next trade."""
        RC["confirm_ticks_mid"]    = int(round(self._learned["confirm_ticks"]))
        RC["regression_slope_min"] = self._learned["slope_min"]
        # jump_floor used directly via self.get() in _update_jump_threshold()

    def get(self, key: str) -> float:
        return self._learned.get(key, TUNABLE[key]["default"])

    @property
    def ready(self) -> bool:
        return self._n >= MIN_TRADES

    @property
    def state(self) -> dict:
        return {
            "n_trades": self._n,
            "win_n":    self._win_n,
            "loss_n":   self._loss_n,
            "ready":    self.ready,
            "learned":  {k: round(v, 2) for k, v in self._learned.items()},
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _recompute(self):
        if self._n < MIN_TRADES:
            for k, cfg in TUNABLE.items():
                self._learned[k] = cfg["default"]
            return
        confidence = min(1.0, (self._n - MIN_TRADES) / CONFIDENCE_SCALE)
        for k, cfg in TUNABLE.items():
            direction = (self._win_avg[k] - cfg["default"]) - 0.5 * (self._loss_avg[k] - cfg["default"])
            raw       = cfg["default"] + confidence * direction * NUDGE_STRENGTH
            self._learned[k] = round(max(cfg["min"], min(cfg["max"], raw)), 2)

    def _save(self):
        try:
            with open(TUNER_STATE_FILE, "w") as f:
                json.dump({
                    "n":        self._n,
                    "win_n":    self._win_n,
                    "loss_n":   self._loss_n,
                    "win_avg":  self._win_avg,
                    "loss_avg": self._loss_avg,
                    "learned":  self._learned,
                }, f, indent=2)
        except Exception as e:
            print(f"[SelfTuner] save error: {e}")

    def _load(self):
        if not os.path.exists(TUNER_STATE_FILE):
            self._bootstrap_csv()
            return
        try:
            with open(TUNER_STATE_FILE) as f:
                d = json.load(f)
            self._n        = d.get("n",        0)
            self._win_n    = d.get("win_n",     0)
            self._loss_n   = d.get("loss_n",    0)
            self._win_avg  = d.get("win_avg",   self._win_avg)
            self._loss_avg = d.get("loss_avg",  self._loss_avg)
            self._learned  = d.get("learned",   self._learned)
            # Ensure all keys present (migration from older versions)
            for k, cfg in TUNABLE.items():
                self._win_avg.setdefault(k,  cfg["default"])
                self._loss_avg.setdefault(k, cfg["default"])
                self._learned.setdefault(k,  cfg["default"])
            print(f"[SelfTuner] loaded — trades={self._n}  learned={self._learned}")
        except Exception as e:
            print(f"[SelfTuner] load error (starting fresh): {e}")

    def _bootstrap_csv(self):
        from config import TRADE_LOG
        if not os.path.exists(TRADE_LOG):
            return
        count = 0
        try:
            with open(TRADE_LOG, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        result = {"pnl_pct": float(row.get("pnl_pct") or 0)}
                        active = {}  # no confirm_ticks/slope_min in CSV — skip
                        self.on_trade_closed(result, active)
                        count += 1
                    except (ValueError, TypeError):
                        continue
            if count:
                print(f"[SelfTuner] bootstrapped {count} trades from trade_log.csv")
        except Exception as e:
            print(f"[SelfTuner] CSV bootstrap error: {e}")
