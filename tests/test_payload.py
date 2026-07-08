"""Stage-4: the state payload must keep its key contract (dashboard JS
reads these keys) after the cheap/slow split and TTL caching."""
import buy_app


REQUIRED_KEYS = {
    # cheap
    "running", "trade_open", "trading_mode", "nifty_price", "nifty_move",
    "jump_threshold_pts", "ce_price", "pe_price", "session_pnl", "capital",
    "live_pnl", "entry", "sl", "trail_price", "trail_pct", "scalp_mode",
    "cooldown_active", "cooldown_remaining", "trades_today", "wins", "losses",
    "ai_entry_score", "ai_regime", "entry_verdict", "nifty_atm",
    # slow (TTL-cached sub-dict, same keys as before the split)
    "vix", "option_chain", "multi_tf", "ai_brain", "entry_analyzer",
    "market_intel", "trade_budget", "rsi", "range_position", "vol_detector",
}


def test_state_payload_key_contract():
    with buy_app._state_lock:
        payload = buy_app._build_state_payload()
    missing = REQUIRED_KEYS - set(payload.keys())
    assert not missing, f"payload lost keys the dashboard depends on: {missing}"


def test_slow_payload_is_cached():
    with buy_app._state_lock:
        a = buy_app._payload_slow()
        b = buy_app._payload_slow()
    assert a is b, "second call within TTL must return the cached dict"
