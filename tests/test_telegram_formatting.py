from __future__ import annotations

from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.models.screener import ScreenerRunResponse
from app.telegram_notify import TelegramNotifier


def test_verified_no_trade_message_includes_diagnostic_context() -> None:
    snapshot = LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="market_intelligence",
        state=SignalState.NONE,
        timeframe="1d",
        current_price=200.0,
        direction_label="no_trade",
        confidence_label="reject",
        rationale="No clear edge for NVDA. Top blockers: confirmation_too_weak.",
        score=42.0,
        metadata={
            "verdict": "no_trade",
            "timing_label": "no_trade",
            "market_context_summary": "risk-on trend",
            "data_source": "etoro",
            "data_source_quote": "etoro",
            "data_source_history": "etoro",
            "data_source_verified": True,
            "quote_live_verified": True,
            "bars_fresh": True,
            "freshness_status": "fresh",
            "data_gate_blocked": False,
            "indicator_confluence_score": 0.51,
            "accuracy_score": 0.43,
            "confirmation_score": 0.38,
            "false_positive_risk_score": 0.71,
            "near_miss_setup": {
                "strategy_name": "rsi_vwap_ema_confluence",
                "timeframe": "15m",
                "status": "rejected",
                "score": 53.4,
                "rejection_reasons": ["confirmation_too_weak", "false_positive_risk_too_high"],
            },
            "analysis_strategy_runs_evaluated": 25,
            "trade_plan": {
                "verdict": "no_trade",
                "timing_label": "no_trade",
                "confirmation_trigger": "Wait for stronger confirmation.",
            },
        },
    )

    message = TelegramNotifier.format_signal_message(snapshot)

    assert "NVDA: WAIT / NO TRADE" in message
    assert "Now: 200.00 | 1d | score 42.0/100" in message
    assert "Closest setup: rsi_vwap_ema_confluence 15m | score 53.40 | rejected" in message
    assert "Why wait: confirmation is too weak; false-positive risk too high" in message
    assert "Watch for: Wait for stronger confirmation." in message
    assert "Key data:" in message
    assert "Quality 0.51" in message
    assert "Confirm 0.38" in message
    assert "FP-risk 0.71" in message
    assert "Data: etoro quote verified; etoro candles fresh" in message
    assert "Safety: no order created. Manual approval required." in message
    assert "Gate: blocked" not in message


def test_screener_summary_includes_rejection_diagnostics() -> None:
    response = ScreenerRunResponse(
        generated_at="2026-04-21T10:00:00+00:00",
        universe_name="top100_us",
        timeframes=["15m", "1h", "1d"],
        evaluated_symbols=8,
        evaluated_strategy_runs=24,
        candidates=[],
        suppressed=3,
        rejection_summary={
            "final_score_below_keep_threshold": 2,
            "confirmation_too_weak": 1,
        },
        closest_rejections=[
            {
                "symbol": "NVDA",
                "timeframe": "1h",
                "strategy_name": "rsi_vwap_ema_confluence",
                "status": "rejected",
                "score": 53.4,
                "rejection_reasons": ["final_score_below_keep_threshold", "confirmation_too_weak"],
                "measurements": {
                    "current_price": 210.4,
                    "watchlist_trigger": "breakout_above",
                    "indicative_entry": 211.0,
                    "indicative_stop": 207.2,
                    "indicative_target": 220.5,
                    "indicative_rr": 2.5,
                    "indicative_target_move_pct": 4.5,
                    "breakout_gap_atr": 0.18,
                    "relative_volume": 0.98,
                    "minimum_relative_volume_relaxed": 1.03,
                    "minimum_relative_volume": 1.08,
                    "volume_check_mode": "session_aware_relaxed",
                },
            },
            {
                "symbol": "AAPL",
                "timeframe": "15m",
                "strategy_name": "rsi_vwap_ema_confluence",
                "status": "rejected",
                "score": 49.1,
                "rejection_reasons": ["breakout_level_not_cleared"],
                "measurements": {
                    "current_price": 180.0,
                    "watchlist_trigger": "breakout_above",
                    "indicative_entry": 181.5,
                    "indicative_stop": 177.2,
                    "indicative_target": 192.25,
                },
            }
        ],
    )

    message = TelegramNotifier.format_screener_summary(response)
    detailed = TelegramNotifier.format_screener_summary(response, include_other_watches=True)

    assert "Market scan: WAIT" in message
    assert "Action: do not open a trade now." in message
    assert "Best watch:" in message
    assert "1. NVDA 1h LONG | score 53.4" in message
    assert "Trigger: above 211.00 | now 210.40 | gap 0.18 ATR" in message
    assert "Plan: stop 207.20 | target 220.50 | RR 2.50R" in message
    assert "Volume: LOW (RVOL 0.98, need 1.03-1.08)" in message
    assert "Potential move: 4.50%" in message
    assert "Why wait: setup quality below threshold; confirmation is too weak" in message
    assert "Scanned: 8 symbol(s) | Checks: 24 | Timeframes: 15m, 1h, 1d" in message
    assert "Safety: no order created. Manual approval required." in message
    assert "Other watches:" not in message
    assert "Other watches:" in detailed
    assert "- AAPL 15m LONG: above 181.50 | now 180.00" in detailed


def test_screener_candidate_reads_like_trade_signal() -> None:
    snapshot = LiveSignalSnapshot(
        symbol="NVDA",
        strategy_name="rsi_vwap_ema_confluence",
        state=SignalState.BUY,
        timeframe="15m",
        current_price=198.42,
        entry_price=198.50,
        stop_loss=196.25,
        take_profit=204.13,
        targets=[204.13],
        risk_reward_ratio=2.5,
        signal_role="entry_long",
        confidence_label="high",
        score=88.2,
        pass_reasons=["breakout confirmed", "volume confirmed"],
        metadata={
            "signal_classification": "execution_ready",
            "trade_plan": {"entry_zone_low": 198.5, "entry_zone_high": 199.0},
        },
    )
    response = ScreenerRunResponse(
        generated_at="2026-04-21T10:00:00+00:00",
        universe_name="top100_us",
        timeframes=["15m"],
        evaluated_symbols=1,
        evaluated_strategy_runs=1,
        candidates=[snapshot],
        suppressed=0,
    )

    message = TelegramNotifier.format_screener_summary(response)

    assert "Market scan: REVIEW" in message
    assert "Action: review manually before any broker action." in message
    assert "1. NVDA 15m LONG | score 88.2" in message
    assert "Entry: 198.50 - 199.00 | now 198.42" in message
    assert "Stop: 196.25 | target 204.13 | RR 2.50R" in message
    assert "Why: breakout confirmed, volume confirmed" in message
    assert "Safety: no order placed. Manual approval required." in message
