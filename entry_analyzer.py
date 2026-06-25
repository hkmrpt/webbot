"""
entry_analyzer.py  ──  Option Buy Robot v9
═══════════════════════════════════════════

AI/ML-based trade entry analysis engine.

Analyzes 8 dimensions before allowing entry:
  1. Capital & Position Sizing    — how much to deploy, ATR-based risk sizing
  2. Volatility Regime            — current vol state, option IV proxy
  3. Price Action                 — momentum, trend, S/R, candle patterns
  4. Options Greeks Estimation    — delta, gamma, theta, vega exposure
  5. Statistical Edge             — historical win rate for similar setups
  6. Risk/Reward                  — expected value, Kelly fraction
  7. Market Microstructure        — spread proxy, liquidity, speed
  8. Timing & Session Quality     — avoid opening noise, end-of-day decay

Returns an EntryVerdict with:
  - composite score (0-100)
  - recommended lots, SL%, trail%, timeout
  - per-dimension scores and reasoning
  - ALLOW / BLOCK decision with human-readable explanation

Self-learning: after each trade close, updates internal weights
based on which dimensions predicted wins vs losses.
"""

import json
import math
import os
from collections import deque
from datetime import datetime

# ── Persistence ──────────────────────────────────────────────────────────────
ANALYZER_STATE_FILE = "entry_analyzer_state.json"


# ── Greeks estimation ────────────────────────────────────────────────────────

def estimate_greeks(nifty_price: float, strike: float, opt_price: float,
                    side: str, atr_pct: float, held_expiry_days: float = 1.0) -> dict:
    """
    Estimate option Greeks from available data (no IV surface needed).

    Delta: moneyness-based approximation
    Gamma: peak at ATM, decays with distance
    Theta: time decay approximation (options lose ~1/√days per day)
    Vega:  sensitivity proxy from ATR
    """
    if nifty_price <= 0 or opt_price <= 0:
        return {"delta": 0.5, "gamma": 0.02, "theta": -0.5, "vega": 0.1, "moneyness": 1.0}

    moneyness = nifty_price / strike if strike > 0 else 1.0

    # Delta approximation (simplified Black-Scholes proxy)
    if side == "CE":
        if moneyness >= 1.05:    delta = 0.75 + min((moneyness - 1.05) * 5, 0.2)
        elif moneyness >= 0.95:  delta = 0.35 + (moneyness - 0.95) * 4.0
        else:                    delta = max(0.05, 0.35 - (0.95 - moneyness) * 3.0)
    else:  # PE
        if moneyness <= 0.95:    delta = -(0.75 + min((0.95 - moneyness) * 5, 0.2))
        elif moneyness <= 1.05:  delta = -(0.35 + (1.05 - moneyness) * 4.0)
        else:                    delta = -max(0.05, 0.35 - (moneyness - 1.05) * 3.0)

    # Gamma — highest at ATM, Gaussian decay
    atm_dist = abs(moneyness - 1.0)
    gamma = 0.04 * math.exp(-50 * atm_dist ** 2)

    # Theta — time decay (options lose value faster as expiry approaches)
    # Rough: theta ≈ -option_price / (2 × sqrt(days_to_expiry) × 252)
    days = max(held_expiry_days, 0.1)
    theta = -opt_price / (2 * math.sqrt(days) * 16)  # 16 ≈ sqrt(252)

    # Vega — sensitivity to volatility changes
    vega = opt_price * 0.01 * (atr_pct / 0.5) if atr_pct > 0 else 0.1

    return {
        "delta":     round(delta, 4),
        "gamma":     round(gamma, 5),
        "theta":     round(theta, 3),
        "vega":      round(vega, 3),
        "moneyness": round(moneyness, 4),
    }


# ── Price Action Analyzer ────────────────────────────────────────────────────

def analyze_price_action(nifty_ticks: list, side: str) -> dict:
    """
    Multi-factor price action analysis.

    Returns scores (0-1) for:
      momentum, trend_strength, support_resistance, candle_quality, rsi_signal
    """
    n = len(nifty_ticks)
    if n < 20:
        return {"momentum": 0.5, "trend_strength": 0.5, "sr_quality": 0.5,
                "candle_quality": 0.5, "rsi_signal": 0.5, "composite": 0.5}

    prices = nifty_ticks

    # ── Momentum (directional consistency over last 10 ticks) ────────────
    recent = prices[-10:]
    changes = [recent[i] - recent[i-1] for i in range(1, len(recent))]
    if side == "CE":
        pos = sum(1 for c in changes if c > 0)
    else:
        pos = sum(1 for c in changes if c < 0)
    momentum = pos / len(changes) if changes else 0.5

    # ── Trend strength (linear regression R² over last 20 ticks) ─────────
    window = prices[-20:]
    n_w = len(window)
    x_mean = (n_w - 1) / 2.0
    y_mean = sum(window) / n_w
    ss_xy = sum((i - x_mean) * (window[i] - y_mean) for i in range(n_w))
    ss_xx = sum((i - x_mean) ** 2 for i in range(n_w))
    ss_yy = sum((window[i] - y_mean) ** 2 for i in range(n_w))
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0
    slope = ss_xy / ss_xx if ss_xx > 0 else 0
    # Trend must be in the right direction
    if (side == "CE" and slope < 0) or (side == "PE" and slope > 0):
        trend_strength = r_squared * 0.3  # penalize wrong direction
    else:
        trend_strength = r_squared

    # ── Support/Resistance quality ───────────────────────────────────────
    # Score higher if price is breaking out from a level, not bouncing off
    hi = max(prices[-30:]) if n >= 30 else max(prices)
    lo = min(prices[-30:]) if n >= 30 else min(prices)
    rng = hi - lo if hi > lo else 1
    current = prices[-1]
    position = (current - lo) / rng  # 0 = at support, 1 = at resistance

    if side == "CE":
        sr_quality = position  # breaking resistance = good for CE
    else:
        sr_quality = 1 - position  # breaking support = good for PE

    # ── Candle quality (last few ticks form a strong pattern) ────────────
    last5 = prices[-5:]
    body = abs(last5[-1] - last5[0])
    wicks = max(last5) - min(last5)
    candle_quality = (body / wicks) if wicks > 0 else 0.5  # strong body = quality
    candle_quality = min(candle_quality, 1.0)

    # ── RSI signal ───────────────────────────────────────────────────────
    period = min(14, n - 1)
    changes_all = [prices[i] - prices[i-1] for i in range(n - period, n)]
    gains = [c for c in changes_all if c > 0]
    losses = [-c for c in changes_all if c < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)

    # RSI signal: for CE, oversold (low RSI) bouncing up = good
    # For PE, overbought (high RSI) dropping = good
    if side == "CE":
        rsi_signal = 1.0 if rsi < 35 else (0.7 if rsi < 50 else (0.4 if rsi < 70 else 0.1))
    else:
        rsi_signal = 1.0 if rsi > 65 else (0.7 if rsi > 50 else (0.4 if rsi > 30 else 0.1))

    composite = (
        0.25 * momentum +
        0.25 * trend_strength +
        0.20 * sr_quality +
        0.15 * candle_quality +
        0.15 * rsi_signal
    )

    return {
        "momentum":      round(momentum, 3),
        "trend_strength": round(trend_strength, 3),
        "sr_quality":    round(sr_quality, 3),
        "candle_quality": round(candle_quality, 3),
        "rsi_signal":    round(rsi_signal, 3),
        "rsi_value":     round(rsi, 1),
        "composite":     round(composite, 3),
    }


# ── Volatility Regime ────────────────────────────────────────────────────────

def analyze_volatility(nifty_ticks: list, opt_price_history: list) -> dict:
    """
    Classify current volatility regime and score suitability for option buying.
    """
    if len(nifty_ticks) < 10:
        return {"regime": "unknown", "nifty_atr": 0, "opt_atr": 0,
                "vol_score": 0.5, "atr_pct": 0}

    # NIFTY ATR (tick-to-tick)
    trs = [abs(nifty_ticks[i] - nifty_ticks[i-1]) for i in range(1, len(nifty_ticks))]
    nifty_atr = sum(trs[-20:]) / min(20, len(trs)) if trs else 0
    price = nifty_ticks[-1] or 1
    atr_pct = (nifty_atr / price) * 100

    # Option ATR
    opt_atr = 0
    if len(opt_price_history) >= 2:
        opt_trs = [abs(opt_price_history[i] - opt_price_history[i-1])
                   for i in range(1, len(opt_price_history))]
        opt_atr = sum(opt_trs[-10:]) / min(10, len(opt_trs)) if opt_trs else 0

    # Regime classification
    if atr_pct >= 0.08:
        regime = "high_vol"
        vol_score = 0.6   # high vol = wider moves but more risk
    elif atr_pct >= 0.03:
        regime = "normal"
        vol_score = 0.85  # sweet spot for option buying
    elif atr_pct >= 0.015:
        regime = "low_vol"
        vol_score = 0.7   # breakout potential
    else:
        regime = "dead"
        vol_score = 0.2   # not enough movement

    return {
        "regime":    regime,
        "nifty_atr": round(nifty_atr, 3),
        "opt_atr":   round(opt_atr, 3),
        "atr_pct":   round(atr_pct, 4),
        "vol_score": round(vol_score, 3),
    }


# ── Timing Analysis ─────────────────────────────────────────────────────────

def analyze_timing() -> dict:
    """
    Score current time for option buying suitability.
    """
    now = datetime.now()
    h, m = now.hour, now.minute
    mins = h * 60 + m
    market_open = 9 * 60 + 15   # 9:15
    market_close = 15 * 60 + 30  # 15:30

    if mins < market_open or mins > market_close:
        return {"session": "closed", "score": 0.0, "minutes_left": 0, "phase": "closed"}

    elapsed = mins - market_open
    remaining = market_close - mins
    total = market_close - market_open

    # Opening 15 min — chaotic, wider spreads
    if elapsed < 15:
        score = 0.35
        phase = "opening"
    # First hour — trending setups form
    elif elapsed < 60:
        score = 0.75
        phase = "early"
    # Mid-session — best for option buying (trends established)
    elif elapsed < 240:
        score = 0.90
        phase = "prime"
    # Last 90 min — theta decay accelerates, fewer entries
    elif remaining > 30:
        score = 0.60
        phase = "late"
    # Last 30 min — avoid new entries (theta crush)
    else:
        score = 0.25
        phase = "closing"

    return {
        "session":      "open",
        "score":        round(score, 3),
        "minutes_left": remaining,
        "phase":        phase,
        "elapsed_mins": elapsed,
    }


# ── Risk/Reward Calculator ───────────────────────────────────────────────────

def analyze_risk_reward(opt_price: float, sl_pct: float, target_pct: float,
                        win_rate: float, capital: float, lot_size: int) -> dict:
    """
    Calculate expected value, Kelly fraction, and optimal position size.
    """
    if opt_price <= 0 or capital <= 0:
        return {"ev": 0, "kelly": 0, "rr_ratio": 0, "score": 0.5,
                "risk_per_lot": 0, "max_lots_kelly": 1}

    risk_per_lot = opt_price * sl_pct / 100 * lot_size
    reward_per_lot = opt_price * target_pct / 100 * lot_size
    rr_ratio = reward_per_lot / risk_per_lot if risk_per_lot > 0 else 1

    # Expected value per trade
    ev = (win_rate * reward_per_lot) - ((1 - win_rate) * risk_per_lot)

    # Kelly Criterion: f* = (bp - q) / b
    # b = reward/risk ratio, p = win probability, q = 1 - p
    b = rr_ratio
    p = win_rate
    q = 1 - p
    kelly = ((b * p) - q) / b if b > 0 else 0
    kelly = max(0, min(kelly, 0.25))  # cap at 25% of capital

    # Kelly-optimal lots
    kelly_capital = capital * kelly
    max_lots_kelly = max(1, int(kelly_capital / (opt_price * lot_size))) if kelly > 0 else 1

    # Score based on EV and R:R
    if ev > 0 and rr_ratio >= 2:
        score = 0.9
    elif ev > 0 and rr_ratio >= 1.5:
        score = 0.75
    elif ev > 0:
        score = 0.6
    elif rr_ratio >= 1:
        score = 0.4
    else:
        score = 0.15

    return {
        "ev":              round(ev, 2),
        "kelly":           round(kelly, 4),
        "rr_ratio":        round(rr_ratio, 2),
        "score":           round(score, 3),
        "risk_per_lot":    round(risk_per_lot, 2),
        "max_lots_kelly":  max_lots_kelly,
    }


# ── Market Microstructure ────────────────────────────────────────────────────

def analyze_microstructure(opt_price_history: list, opt_price: float) -> dict:
    """
    Analyze spread proxy and liquidity from option price movement.
    """
    if len(opt_price_history) < 5 or opt_price <= 0:
        return {"spread_pct": 0, "liquidity": 0.5, "score": 0.5}

    # Spread proxy: minimum absolute tick movement (approximates bid-ask spread)
    changes = [abs(opt_price_history[i] - opt_price_history[i-1])
               for i in range(1, len(opt_price_history))]
    non_zero = [c for c in changes if c > 0]
    min_tick = min(non_zero) if non_zero else 0.05
    spread_pct = (min_tick / opt_price) * 100

    # Liquidity proxy: frequency of price changes (more changes = more liquid)
    active_ticks = len(non_zero)
    total_ticks = len(changes)
    liquidity = active_ticks / total_ticks if total_ticks > 0 else 0.5

    # Score: tight spread + high liquidity = good
    if spread_pct < 1.0 and liquidity > 0.7:
        score = 0.9
    elif spread_pct < 2.0 and liquidity > 0.5:
        score = 0.7
    elif spread_pct < 4.0:
        score = 0.5
    else:
        score = 0.2  # wide spread = avoid

    return {
        "spread_pct": round(spread_pct, 3),
        "liquidity":  round(liquidity, 3),
        "score":      round(score, 3),
    }


# ── Self-Learning Weight Engine ──────────────────────────────────────────────

class DimensionWeights:
    """
    Learns which analysis dimensions best predict winning trades.
    Updates weights after each trade using exponential moving average.
    """

    DIMENSIONS = [
        "price_action", "volatility", "greeks", "risk_reward",
        "timing", "microstructure", "ai_brain", "capital",
        "vix", "option_chain", "multi_tf"
    ]
    LEARNING_RATE = 0.08

    def __init__(self):
        n = len(self.DIMENSIONS)
        self.weights = {d: 1.0 / n for d in self.DIMENSIONS}
        self.n_trades = 0
        self.n_wins = 0
        self._last_scores: dict | None = None

    def score(self, dim_scores: dict) -> float:
        """Weighted sum of dimension scores. Returns 0-100."""
        total = 0
        w_sum = 0
        for d in self.DIMENSIONS:
            w = self.weights.get(d, 0.125)
            s = dim_scores.get(d, 0.5)
            total += w * s
            w_sum += w
        return round((total / w_sum) * 100, 1) if w_sum > 0 else 50.0

    def teach(self, dim_scores: dict, won: bool):
        """
        Update weights: boost dimensions that scored high on wins,
        penalize dimensions that scored high on losses.
        """
        self.n_trades += 1
        if won:
            self.n_wins += 1

        lr = self.LEARNING_RATE
        for d in self.DIMENSIONS:
            s = dim_scores.get(d, 0.5)
            if won:
                # High score + win → good predictor → boost weight
                self.weights[d] += lr * s * 0.1
            else:
                # High score + loss → bad predictor → reduce weight
                self.weights[d] -= lr * s * 0.05

            self.weights[d] = max(0.02, min(0.5, self.weights[d]))

        # Normalize to sum = 1
        total = sum(self.weights.values())
        if total > 0:
            for d in self.DIMENSIONS:
                self.weights[d] /= total

    @property
    def win_rate(self) -> float:
        return round(self.n_wins / self.n_trades, 3) if self.n_trades else 0.0

    def to_dict(self) -> dict:
        return {
            "weights": self.weights,
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
        }

    def from_dict(self, d: dict):
        self.weights = d.get("weights", self.weights)
        self.n_trades = d.get("n_trades", 0)
        self.n_wins = d.get("n_wins", 0)
        # Ensure all dimensions have weights
        for dim in self.DIMENSIONS:
            if dim not in self.weights:
                self.weights[dim] = 0.125


# ── Entry Verdict ────────────────────────────────────────────────────────────

class EntryVerdict:
    """Result of the full entry analysis."""

    def __init__(self):
        self.allow = False
        self.score = 0.0           # 0-100
        self.grade = "F"           # A/B/C/D/F
        self.reason = ""
        self.dimensions = {}       # per-dimension scores and details
        self.recommended_lots = 1
        self.recommended_sl = 10.0
        self.recommended_trail = 8.0
        self.recommended_timeout = 50
        self.greeks = {}
        self.risk_reward = {}

    def to_dict(self) -> dict:
        return {
            "allow":       self.allow,
            "score":       self.score,
            "grade":       self.grade,
            "reason":      self.reason,
            "dimensions":  self.dimensions,
            "lots":        self.recommended_lots,
            "sl_pct":      self.recommended_sl,
            "trail_pct":   self.recommended_trail,
            "timeout":     self.recommended_timeout,
            "greeks":      self.greeks,
            "risk_reward": self.risk_reward,
        }


# ── Main Entry Analyzer ─────────────────────────────────────────────────────

class EntryAnalyzer:
    """
    Comprehensive AI/ML entry analysis engine.

    Usage:
      verdict = analyzer.analyze(state_dict)
      if verdict.allow:
          enter_trade(lots=verdict.recommended_lots, ...)

      # After trade closes:
      analyzer.on_trade_closed(result_dict)
    """

    MIN_SCORE_ENTRY = 45    # below this → block
    MIN_SCORE_TRAINED = 52  # after 20 trades, require higher score

    def __init__(self):
        self._weights = DimensionWeights()
        self._last_dim_scores: dict | None = None
        self._load()

    def analyze(self, state: dict) -> EntryVerdict:
        """
        Full 8-dimension analysis.

        state keys:
          side, nifty_price, nifty_ticks, opt_price, opt_price_history,
          strike, jump_threshold, jump_atr, regression_slope,
          capital, lot_size, ai_score, regime, loss_streak,
          fast_entry, nifty_move
        """
        v = EntryVerdict()

        side         = state.get("side", "CE")
        nifty_price  = state.get("nifty_price", 0)
        nifty_ticks  = state.get("nifty_ticks", [])
        opt_price    = state.get("opt_price", 0)
        opt_history  = state.get("opt_price_history", [])
        strike       = state.get("strike", nifty_price)
        capital      = state.get("capital", 10000)
        lot_size     = state.get("lot_size", 75)
        ai_score     = state.get("ai_score", 0.5)
        loss_streak  = state.get("loss_streak", 0)

        # ── 1. Price Action Analysis ────────────────────────────────────
        pa = analyze_price_action(nifty_ticks, side)
        v.dimensions["price_action"] = pa

        # ── 2. Volatility Analysis ──────────────────────────────────────
        vol = analyze_volatility(nifty_ticks, opt_history)
        v.dimensions["volatility"] = vol

        # ── 3. Greeks Estimation (uses actual DTE from market intelligence) ──
        intel = state.get("market_intel", {})
        dte = intel.get("day_info", {}).get("dte", 3)
        theta_pressure = intel.get("day_info", {}).get("theta_pressure", 0.3)
        greeks = estimate_greeks(
            nifty_price, strike, opt_price, side,
            vol.get("atr_pct", 0.03), held_expiry_days=max(dte, 0.1)
        )
        v.greeks = greeks
        v.greeks["dte"] = dte
        v.greeks["theta_pressure"] = theta_pressure
        v.dimensions["greeks"] = greeks

        # Greeks score: prefer high |delta| (ITM/ATM), high gamma, low theta drag
        abs_delta = abs(greeks["delta"])
        greeks_score = (
            0.4 * min(abs_delta / 0.5, 1.0) +       # delta quality
            0.3 * min(greeks["gamma"] / 0.03, 1.0) +  # gamma quality
            0.3 * (1.0 - min(abs(greeks["theta"]) / 2.0, 1.0))  # theta drag
        )
        v.dimensions["greeks"]["score"] = round(greeks_score, 3)

        # ── 4. Risk/Reward Analysis ─────────────────────────────────────
        win_rate = self._weights.win_rate if self._weights.n_trades >= 10 else 0.45
        sl_pct = 10.0  # initial SL
        target_pct = sl_pct * 2.0  # aim for 2:1 R:R minimum

        rr = analyze_risk_reward(opt_price, sl_pct, target_pct, win_rate, capital, lot_size)
        v.risk_reward = rr
        v.dimensions["risk_reward"] = rr

        # ── 5. Timing Analysis ──────────────────────────────────────────
        timing = analyze_timing()
        v.dimensions["timing"] = timing

        # ── 6. Microstructure ───────────────────────────────────────────
        micro = analyze_microstructure(opt_history, opt_price)
        v.dimensions["microstructure"] = micro

        # ── 7. AI Brain Score (from MarketBrain) ────────────────────────
        ai_dim_score = ai_score if isinstance(ai_score, (int, float)) else 0.5
        v.dimensions["ai_brain"] = {"score": round(ai_dim_score, 3)}

        # ── 8. Capital Analysis ─────────────────────────────────────────
        cost_per_lot = opt_price * lot_size
        affordable = max(1, int(capital / cost_per_lot)) if cost_per_lot > 0 else 1
        capital_util = (cost_per_lot / capital) if capital > 0 else 1
        # Score: deploying 10-40% of capital = sweet spot
        if 0.1 <= capital_util <= 0.4:
            cap_score = 0.9
        elif capital_util < 0.1:
            cap_score = 0.7  # too conservative but ok
        elif capital_util <= 0.6:
            cap_score = 0.6  # slightly aggressive
        else:
            cap_score = 0.3  # too much capital at risk

        # Loss streak penalty
        if loss_streak >= 3:
            cap_score *= 0.5
        elif loss_streak >= 2:
            cap_score *= 0.7

        v.dimensions["capital"] = {
            "score":          round(cap_score, 3),
            "affordable_lots": affordable,
            "capital_util":   round(capital_util, 3),
            "loss_streak":    loss_streak,
        }

        # ── 9. VIX Analysis ─────────────────────────────────────────────
        vix_data = state.get("vix", {})
        vix_score = vix_data.get("score", 0.5)
        v.dimensions["vix"] = vix_data

        # ── 10. Option Chain (OI/PCR) ──────────────────────────────────
        oi_data = state.get("option_chain", {})
        oi_score = oi_data.get("score", 0.5)
        v.dimensions["option_chain"] = oi_data

        # ── 11. Multi-Timeframe Alignment ──────────────────────────────
        mtf_data = state.get("multi_tf", {})
        mtf_score = mtf_data.get("score", 0.5)
        v.dimensions["multi_tf"] = mtf_data

        # ── Composite Score ─────────────────────────────────────────────
        dim_scores = {
            "price_action":   pa.get("composite", 0.5),
            "volatility":     vol.get("vol_score", 0.5),
            "greeks":         greeks_score,
            "risk_reward":    rr.get("score", 0.5),
            "timing":         timing.get("score", 0.5),
            "microstructure": micro.get("score", 0.5),
            "ai_brain":       ai_dim_score,
            "capital":        cap_score,
            "vix":            vix_score,
            "option_chain":   oi_score,
            "multi_tf":       mtf_score,
        }

        composite = self._weights.score(dim_scores)
        v.score = composite
        self._last_dim_scores = dim_scores

        # Grade
        if composite >= 75:
            v.grade = "A"
        elif composite >= 62:
            v.grade = "B"
        elif composite >= 50:
            v.grade = "C"
        elif composite >= 40:
            v.grade = "D"
        else:
            v.grade = "F"

        # ── Decision ───────────────────────────────────────────────────
        threshold = self.MIN_SCORE_TRAINED if self._weights.n_trades >= 20 else self.MIN_SCORE_ENTRY

        if composite >= threshold:
            v.allow = True
            v.reason = (
                f"ENTRY APPROVED [{v.grade}] score={composite:.0f}/100  "
                f"PA={pa['composite']:.0f}% Vol={vol['regime']} "
                f"Δ={greeks['delta']:+.2f} R:R={rr['rr_ratio']:.1f} "
                f"time={timing['phase']}"
            )
        else:
            v.allow = False
            # Find weakest dimension
            weakest = min(dim_scores, key=dim_scores.get)
            v.reason = (
                f"ENTRY BLOCKED [{v.grade}] score={composite:.0f}/100 < {threshold}  "
                f"weakest={weakest}({dim_scores[weakest]:.0f}%)  "
                f"PA={pa['composite']:.0f}% Vol={vol['regime']}"
            )

        # ── Adaptive parameters based on analysis ───────────────────────
        # SL: tighter in high vol, wider in low vol
        if vol["regime"] == "high_vol":
            v.recommended_sl = 8.0
            v.recommended_trail = 6.0
            v.recommended_timeout = 40
        elif vol["regime"] == "low_vol":
            v.recommended_sl = 12.0
            v.recommended_trail = 10.0
            v.recommended_timeout = 60
        else:
            v.recommended_sl = 10.0
            v.recommended_trail = 8.0
            v.recommended_timeout = 50

        # Lots: use Kelly if available, otherwise ATR-based
        if rr.get("max_lots_kelly", 1) > 0:
            v.recommended_lots = min(rr["max_lots_kelly"], affordable)
        else:
            v.recommended_lots = max(1, affordable // 2)

        # Cap lots if on a loss streak
        if loss_streak >= 2:
            v.recommended_lots = max(1, v.recommended_lots // 2)

        return v

    def on_trade_closed(self, result: dict):
        """
        Learn from trade outcome. Updates dimension weights.
        """
        if self._last_dim_scores is None:
            return

        pnl_pct = result.get("pnl_pct", 0) or 0
        won = pnl_pct >= 0.5  # 0.5% = win (covers transaction costs)

        self._weights.teach(self._last_dim_scores, won)
        self._last_dim_scores = None
        self._save()

    @property
    def state(self) -> dict:
        return {
            "n_trades":  self._weights.n_trades,
            "win_rate":  self._weights.win_rate,
            "weights":   {k: round(v, 4) for k, v in self._weights.weights.items()},
            "dimensions": DimensionWeights.DIMENSIONS,
        }

    # ── Persistence ──────────────────────────────────────────────────────

    def _save(self):
        try:
            with open(ANALYZER_STATE_FILE, "w") as f:
                json.dump(self._weights.to_dict(), f, indent=2)
        except Exception as e:
            print(f"[EntryAnalyzer] save error: {e}")

    def _load(self):
        if not os.path.exists(ANALYZER_STATE_FILE):
            return
        try:
            with open(ANALYZER_STATE_FILE) as f:
                data = json.load(f)
            self._weights.from_dict(data)
            print(
                f"[EntryAnalyzer] loaded — "
                f"trades={self._weights.n_trades}  "
                f"win_rate={self._weights.win_rate:.1%}"
            )
        except Exception as e:
            print(f"[EntryAnalyzer] load error (starting fresh): {e}")
