"""
exit_analyzer.py  ──  Option Buy Robot v9
═════════════════════════════════════════

Advanced AI/ML exit analysis engine.

Analyzes 9 exit signals every tick and returns a composite exit decision:

  1. Volatility Spike Detection    — sudden ATR expansion = take profit or protect
  2. Price Action Reversal         — bearish/bullish engulfing, pin bars, double tops
  3. Momentum Collapse             — velocity + acceleration + consistency all dropping
  4. Profit Decay Curve            — profit peak → current forms a decay pattern
  5. Volume Activity Collapse      — price movement drying up = exit stale trade
  6. Gamma Scalping Detection      — rapid oscillation around entry = gamma trap
  7. Time Decay Pressure           — theta eats premium, urgent exit as time passes
  8. Trend Exhaustion              — RSI divergence, slope flattening
  9. Cascade Risk                  — rapid multi-tick drop = panic selling

(The trailing stop itself lives in BuyExitStrategy and always outranks
these signals, together with the hard SL and timeout.)

Self-learning: learns from each trade what signals predicted profitable exits
vs premature exits, and adjusts signal weights accordingly.
"""

import json
import math
import os
import sys
from collections import deque
from datetime import datetime

from config import AI_EXIT_MIN_TRADES

# State dir anchored next to the module (or exe when frozen) so state never
# silently lands in a different working directory. Tests may override.
STATE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
             if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))

EXIT_ANALYZER_STATE = "exit_analyzer_state.json"


def _state_path(mode: str = "") -> str:
    if mode:
        base, ext = os.path.splitext(EXIT_ANALYZER_STATE)
        return os.path.join(STATE_DIR, f"{base}_{mode}{ext}")
    return os.path.join(STATE_DIR, EXIT_ANALYZER_STATE)


class SignalResult:
    """Result of one exit signal check."""
    __slots__ = ("name", "fire", "urgency", "reason")

    def __init__(self, name: str, fire: bool = False, urgency: float = 0.0, reason: str = ""):
        self.name = name
        self.fire = fire
        self.urgency = urgency  # 0-1, higher = more urgent exit
        self.reason = reason


def _slope(data: list) -> float:
    """Least-squares slope, normalized."""
    n = len(data)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(data) / n
    num = sum((i - x_mean) * (data[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den else 0.0


def _rsi(data: list, period: int = 14) -> float:
    if len(data) < period + 1:
        return 50.0
    changes = [data[i] - data[i - 1] for i in range(len(data) - period, len(data))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_g = sum(gains) / period if gains else 0
    avg_l = sum(losses) / period if losses else 0.001
    return 100 - 100 / (1 + avg_g / avg_l)


# ── Signal Functions ─────────────────────────────────────────────────────────

def _sig_volatility_spike(prices: list, entry: float, current_pct: float) -> SignalResult:
    """
    Detect sudden ATR expansion — the market is going crazy.
    If we're in profit, this is a take-profit signal.
    If we're losing, this accelerates the stop.
    """
    if len(prices) < 12:
        return SignalResult("vol_spike")

    # Recent ATR vs historical ATR
    recent = prices[-6:]
    recent_atr = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))) / (len(recent) - 1)
    older = prices[-12:-6] if len(prices) >= 12 else prices[:6]
    older_atr = sum(abs(older[i] - older[i - 1]) for i in range(1, len(older))) / max(len(older) - 1, 1)

    if older_atr <= 0:
        return SignalResult("vol_spike")

    expansion = recent_atr / older_atr

    # Massive expansion (3x+) while in profit → take profit before reversal
    if expansion >= 3.0 and current_pct >= 2.0:
        return SignalResult("vol_spike", True, 0.85,
                            f"ATR expanded {expansion:.1f}x — take profit before reversal")
    # Large expansion while losing → get out fast
    if expansion >= 2.5 and current_pct < 0:
        return SignalResult("vol_spike", True, 0.9,
                            f"ATR expanded {expansion:.1f}x while losing — panic exit")
    return SignalResult("vol_spike")


def _sig_reversal_pattern(prices: list, side: str) -> SignalResult:
    """
    Detect bearish/bullish reversal candlestick patterns.
    Uses last 5 price ticks to form pseudo-candles.
    """
    if len(prices) < 8:
        return SignalResult("reversal_pattern")

    p = prices
    # Check for bearish engulfing (for CE longs) / bullish engulfing (for PE longs)
    # Last 4 ticks: was moving in our direction, then reversed sharply
    recent = p[-6:]
    move1 = recent[2] - recent[0]  # first half
    move2 = recent[5] - recent[3]  # second half

    if side == "CE":
        # Bearish: was going up, now going down with bigger magnitude
        if move1 > 0 and move2 < 0 and abs(move2) > abs(move1) * 1.3:
            return SignalResult("reversal_pattern", True, 0.7,
                                f"Bearish engulfing — reversal {move2:.2f} > {move1:.2f}")
    else:
        # Bullish (bad for PE longs): was going down, now going up
        if move1 < 0 and move2 > 0 and abs(move2) > abs(move1) * 1.3:
            return SignalResult("reversal_pattern", True, 0.7,
                                f"Bullish engulfing — reversal {move2:.2f} > {move1:.2f}")

    # Pin bar / rejection: long wick against our direction
    if len(prices) >= 5:
        body = abs(prices[-1] - prices[-2])
        wick_up = max(prices[-3:]) - max(prices[-1], prices[-2])
        wick_dn = min(prices[-1], prices[-2]) - min(prices[-3:])

        if side == "CE" and wick_up > body * 2.5 and wick_up > 0.3:
            return SignalResult("reversal_pattern", True, 0.6,
                                f"Upper rejection wick — selling pressure")
        if side == "PE" and wick_dn > body * 2.5 and wick_dn > 0.3:
            return SignalResult("reversal_pattern", True, 0.6,
                                f"Lower rejection wick — buying pressure")

    return SignalResult("reversal_pattern")


def _sig_momentum_collapse(prices: list, side: str) -> SignalResult:
    """
    Multi-factor momentum analysis:
    velocity + acceleration + directional consistency all declining.
    """
    if len(prices) < 12:
        return SignalResult("momentum_collapse")

    recent = prices[-8:]
    older = prices[-16:-8] if len(prices) >= 16 else prices[:8]

    # Velocity
    vel_recent = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))) / (len(recent) - 1)
    vel_older = sum(abs(older[i] - older[i - 1]) for i in range(1, len(older))) / max(len(older) - 1, 1)
    vel_ratio = vel_recent / vel_older if vel_older > 0 else 1.0

    # Directional consistency
    changes = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    if side == "CE":
        favorable = sum(1 for c in changes if c > 0) / len(changes)
    else:
        favorable = sum(1 for c in changes if c < 0) / len(changes)

    # Acceleration (slope of velocity is negative = decelerating)
    if len(prices) >= 12:
        vels = []
        for i in range(len(prices) - 6, len(prices)):
            if i >= 1:
                vels.append(abs(prices[i] - prices[i - 1]))
        accel = _slope(vels) if len(vels) >= 3 else 0

        if vel_ratio < 0.4 and favorable < 0.35 and accel < -0.1:
            return SignalResult("momentum_collapse", True, 0.75,
                                f"Momentum collapsed — vel={vel_ratio:.1f}x dir={favorable:.0%} accel={accel:.2f}")

    return SignalResult("momentum_collapse")


def _sig_profit_decay(profit_history: list, peak_pct: float) -> SignalResult:
    """
    Detect consistent profit decay from peak — profit is slipping away.
    """
    if len(profit_history) < 6 or peak_pct < 1.5:
        return SignalResult("profit_decay")

    recent = profit_history[-6:]
    # Non-increasing with a net decline — strict < on every tick would be
    # silently disabled by a single tied print (prices are rounded to 2dp).
    all_declining = (
        all(recent[i] <= recent[i - 1] for i in range(1, len(recent)))
        and recent[-1] < recent[0]
    )

    if all_declining:
        decay_amount = recent[0] - recent[-1]
        decay_pct_of_peak = (decay_amount / peak_pct * 100) if peak_pct > 0 else 0
        if decay_pct_of_peak >= 30:  # lost 30%+ of peak profit
            return SignalResult("profit_decay", True, 0.8,
                                f"Lost {decay_pct_of_peak:.0f}% of peak profit in 6 ticks")

    # Slower decay: lost 50%+ of peak profit over any timeframe
    if len(profit_history) >= 3:
        current = profit_history[-1]
        if peak_pct >= 3.0 and current < peak_pct * 0.5:
            return SignalResult("profit_decay", True, 0.7,
                                f"Profit halved: peak={peak_pct:.1f}% now={current:.1f}%")

    return SignalResult("profit_decay")


def _sig_gamma_trap(prices: list, entry: float) -> SignalResult:
    """
    Detect oscillation around entry — the option is stuck in gamma territory.
    Price keeps crossing back and forth = bleeding from spread + theta.
    """
    if len(prices) < 12:
        return SignalResult("gamma_trap")

    recent = prices[-12:]
    crossings = 0
    for i in range(1, len(recent)):
        if (recent[i - 1] < entry and recent[i] >= entry) or \
           (recent[i - 1] >= entry and recent[i] < entry):
            crossings += 1

    # Net movement vs total movement (efficiency)
    net = abs(recent[-1] - recent[0])
    total = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
    efficiency = net / total if total > 0 else 0

    if crossings >= 4 and efficiency < 0.15:
        return SignalResult("gamma_trap", True, 0.65,
                            f"Gamma trap — {crossings} entry crossings, efficiency={efficiency:.0%}")

    return SignalResult("gamma_trap")


def _sig_time_pressure(held_secs: float, current_pct: float, opt_price: float) -> SignalResult:
    """
    Theta decay pressure increases with time.
    Cheap options decay faster — urgency increases.
    """
    if held_secs < 30:
        return SignalResult("time_pressure")

    # Urgency increases with time held
    time_factor = min(held_secs / 120, 1.0)  # max at 2 minutes
    price_factor = 1.0 if opt_price < 30 else (0.7 if opt_price < 100 else 0.4)
    urgency = time_factor * price_factor

    # If barely profitable after a long time, theta is eating us
    if held_secs >= 60 and current_pct < 1.0 and current_pct >= 0:
        return SignalResult("time_pressure", True, urgency,
                            f"Held {held_secs:.0f}s with only {current_pct:.1f}% — theta eating premium")

    # If losing after 90s, strongly suggest exit
    if held_secs >= 90 and current_pct < 0:
        return SignalResult("time_pressure", True, min(urgency + 0.2, 1.0),
                            f"Losing {current_pct:.1f}% after {held_secs:.0f}s — time pressure")

    return SignalResult("time_pressure")


def _sig_trend_exhaustion(prices: list, side: str) -> SignalResult:
    """
    Detect trend exhaustion: RSI divergence + slope flattening.
    """
    if len(prices) < 20:
        return SignalResult("trend_exhaustion")

    rsi_val = _rsi(prices, 14)
    slope_val = _slope(prices[-10:])
    price = prices[-1] or 1
    slope_norm = slope_val / price * 1000  # normalized

    if side == "CE":
        # Overbought RSI + flattening/negative slope = exhaustion
        if rsi_val > 75 and slope_norm < 0.1:
            return SignalResult("trend_exhaustion", True, 0.6,
                                f"CE exhaustion: RSI={rsi_val:.0f} slope={slope_norm:.2f}")
    else:
        # Oversold RSI + flattening/positive slope = exhaustion
        if rsi_val < 25 and slope_norm > -0.1:
            return SignalResult("trend_exhaustion", True, 0.6,
                                f"PE exhaustion: RSI={rsi_val:.0f} slope={slope_norm:.2f}")

    return SignalResult("trend_exhaustion")


def _sig_cascade_risk(prices: list, entry: float) -> SignalResult:
    """
    Detect rapid multi-tick cascading drop — panic selling.
    3+ consecutive ticks all moving against us with increasing speed.
    """
    if len(prices) < 5:
        return SignalResult("cascade_risk")

    recent = prices[-5:]
    drops = [recent[i] - recent[i - 1] for i in range(1, len(recent))]

    # All negative (for longs) and accelerating
    if all(d < 0 for d in drops):
        abs_drops = [abs(d) for d in drops]
        if abs_drops[-1] > abs_drops[0] * 1.5:  # accelerating
            total_drop_pct = abs(recent[-1] - recent[0]) / entry * 100 if entry > 0 else 0
            if total_drop_pct >= 2.0:
                return SignalResult("cascade_risk", True, 0.95,
                                    f"Cascade drop: {total_drop_pct:.1f}% in 4 ticks, accelerating")

    return SignalResult("cascade_risk")


# ── Exit Analyzer Engine ─────────────────────────────────────────────────────

class ExitAnalyzer:
    """
    Composite exit analysis engine.

    Every tick: runs 10 signals, weights them, decides exit.

    Usage:
      should_exit, reason = analyzer.check(prices, entry, side, ...)
      # After trade close:
      analyzer.on_trade_closed(result)
    """

    SIGNAL_NAMES = [
        "vol_spike", "reversal_pattern", "momentum_collapse", "profit_decay",
        "volume_dryup", "gamma_trap", "time_pressure", "trend_exhaustion",
        "cascade_risk",
    ]

    # Default urgency threshold: signal must be this urgent to trigger exit
    URGENCY_THRESHOLD = 0.55

    def __init__(self, mode: str | None = None):
        from config import TRADING_MODE
        self._mode = mode or TRADING_MODE
        # Learned weights per signal (how reliable each signal is)
        self.weights = {s: 1.0 for s in self.SIGNAL_NAMES}
        self.n_trades = 0
        self.n_correct_exits = 0  # exits that saved from further loss
        self._last_signals: dict | None = None
        # Signals snapshotted at the tick the analyzer DECIDED to exit —
        # learning must credit these, not whatever fired on the final tick
        # of an SL/trail/timeout exit the analyzer had nothing to do with.
        self._fired_at_decision: dict | None = None
        self._load()

    def set_mode(self, mode: str):
        """Switch demo/real state files (demo fills never train real weights)."""
        if mode == self._mode:
            return
        self._save()
        self._mode = mode
        self.weights = {s: 1.0 for s in self.SIGNAL_NAMES}
        self.n_trades = 0
        self.n_correct_exits = 0
        self._load()

    def check(
        self,
        prices: list,
        entry: float,
        side: str,
        current_pct: float,
        peak_pct: float,
        held_secs: float,
        profit_history: list,
        opt_price: float,
    ) -> tuple[bool, str]:
        """
        Run all exit signals. Returns (should_exit, reason).
        """
        signals = {}

        # 1. Volatility spike
        signals["vol_spike"] = _sig_volatility_spike(prices, entry, current_pct)

        # 2. Reversal pattern
        signals["reversal_pattern"] = _sig_reversal_pattern(prices, side)

        # 3. Momentum collapse
        signals["momentum_collapse"] = _sig_momentum_collapse(prices, side)

        # 4. Profit decay
        signals["profit_decay"] = _sig_profit_decay(profit_history, peak_pct)

        # 5. Volume dry-up (reuse from price movement)
        if len(prices) >= 10:
            recent = prices[-6:]
            avg_move = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))) / (len(recent) - 1) if len(recent) > 1 else 0
            avg_all = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))) / max(len(prices) - 1, 1)
            if avg_all > 0 and (avg_move / avg_all) < 0.3 and current_pct >= 1.0:
                signals["volume_dryup"] = SignalResult("volume_dryup", True, 0.55,
                    f"Volume dried up — activity at {avg_move/avg_all:.0%} of average")
            else:
                signals["volume_dryup"] = SignalResult("volume_dryup")
        else:
            signals["volume_dryup"] = SignalResult("volume_dryup")

        # 6. Gamma trap
        signals["gamma_trap"] = _sig_gamma_trap(prices, entry)

        # 7. Time pressure
        signals["time_pressure"] = _sig_time_pressure(held_secs, current_pct, opt_price)

        # 8. Trend exhaustion
        signals["trend_exhaustion"] = _sig_trend_exhaustion(prices, side)

        # 9. Cascade risk
        signals["cascade_risk"] = _sig_cascade_risk(prices, entry)

        self._last_signals = {k: {"fire": v.fire, "urgency": v.urgency} for k, v in signals.items()}

        # ── Warmup gate ──────────────────────────────────────────────
        # Until the analyzer has learned from AI_EXIT_MIN_TRADES closed
        # trades, it has no exit authority — hard SL / trail / timeout
        # protect the trade. Exception: cascade_risk stays live because it
        # is a mechanical crash protector (4 accelerating drops), not a
        # learned signal.
        warmup = self.n_trades < AI_EXIT_MIN_TRADES
        if warmup:
            casc = signals["cascade_risk"]
            if casc.fire and casc.urgency >= 0.85:
                return self._decide(True, f"[EXIT-AI] {casc.reason}")
            return False, ""

        # ── Composite decision ───────────────────────────────────────
        # Check for any critical signal (urgency >= 0.85)
        for name, sig in signals.items():
            if sig.fire and sig.urgency >= 0.85:
                return self._decide(True, f"[EXIT-AI] {sig.reason}")

        # Weighted urgency sum — if multiple signals fire, their combined weight matters
        weighted_urgency = 0
        fire_count = 0
        top_reason = ""
        top_urgency = 0

        for name, sig in signals.items():
            if sig.fire:
                fire_count += 1
                w = self.weights.get(name, 1.0)
                weighted_urgency += sig.urgency * w
                if sig.urgency > top_urgency:
                    top_urgency = sig.urgency
                    top_reason = sig.reason

        # Multiple signals firing = stronger exit signal
        if fire_count >= 3 and weighted_urgency >= 1.5:
            return self._decide(True, f"[EXIT-AI] {fire_count} signals fired — {top_reason}")

        # Strong single signal: judged on ITS OWN learned weight. The old rule
        # compared against max(all weights), so one well-trusted signal
        # suppressed every unrelated signal in the book.
        for name, sig in signals.items():
            if sig.fire and sig.urgency * self.weights.get(name, 1.0) >= self.URGENCY_THRESHOLD:
                return self._decide(True, f"[EXIT-AI] {sig.reason}")

        return False, ""

    def _decide(self, fire: bool, reason: str) -> tuple[bool, str]:
        """Snapshot the firing signals at DECISION time for attribution."""
        if fire:
            self._fired_at_decision = dict(self._last_signals or {})
        return fire, reason

    def on_trade_closed(self, result: dict):
        """
        Learn from the trade — but ONLY when the analyzer actually caused the
        exit. The old rule credited/blamed every signal that happened to fire
        on the final tick even when the exit was an SL/trail/timeout the
        analyzer had nothing to do with (no attribution).
        """
        fired = self._fired_at_decision
        self._last_signals = None
        self._fired_at_decision = None

        # Mode isolation: never learn from a different trading mode's fills
        if result.get("mode") and result["mode"] != self._mode:
            return

        # n_trades counts every closed trade — it gates the warmup, which
        # should reflect total experience, not just AI-caused exits.
        self.n_trades += 1

        if result.get("reason") != "ai_analyzer" or not fired:
            self._save()
            return

        pnl_pct = result.get("pnl_pct", 0) or 0
        peak_pct = result.get("peak_profit_pct", 0) or 0

        # Good exit = didn't give back too much of peak profit
        gave_back = peak_pct - pnl_pct
        good_exit = pnl_pct >= 0 and (peak_pct < 2 or gave_back < peak_pct * 0.5)

        lr = 0.06
        for name, sig_data in fired.items():
            if sig_data["fire"] and name in self.weights:
                if good_exit:
                    # Signal fired and exit was good → boost weight
                    self.weights[name] = min(3.0, self.weights[name] + lr)
                else:
                    # Signal fired but exit was premature → penalize
                    self.weights[name] = max(0.2, self.weights[name] - lr * 0.5)

        if good_exit:
            self.n_correct_exits += 1
        self._save()

    @property
    def state(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "exit_accuracy": round(self.n_correct_exits / self.n_trades, 3) if self.n_trades else 0,
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
        }

    def _save(self):
        try:
            data = {
                "weights": self.weights,
                "n_trades": self.n_trades,
                "n_correct_exits": self.n_correct_exits,
            }
            with open(_state_path(self._mode), "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load(self):
        if not os.path.exists(_state_path(self._mode)):
            return
        try:
            with open(_state_path(self._mode)) as f:
                data = json.load(f)
            self.weights = data.get("weights", self.weights)
            self.n_trades = data.get("n_trades", 0)
            self.n_correct_exits = data.get("n_correct_exits", 0)
            # Migration: drop retired signals (e.g. the old "adaptive_trail"
            # placeholder), ensure current signals all have a weight.
            self.weights = {k: v for k, v in self.weights.items()
                            if k in self.SIGNAL_NAMES}
            for s in self.SIGNAL_NAMES:
                if s not in self.weights:
                    self.weights[s] = 1.0
            print(f"[ExitAnalyzer] loaded — trades={self.n_trades} "
                  f"accuracy={self.n_correct_exits}/{self.n_trades}")
        except Exception:
            pass
