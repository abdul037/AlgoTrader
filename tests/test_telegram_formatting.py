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

    assert "Why not now:" in message
    assert "Nearest setup: rsi_vwap_ema_confluence | 15m | rejected | near-score 53.40" in message
    assert "Strategy checks: 25 evaluated" in message
    assert "Blockers: confirmation_too_weak, false_positive_risk_too_high" in message
    assert "Indicators:" in message
    assert "Accuracy 0.43" in message
    assert "Confirm 0.38" in message
    assert "FP-risk 0.71" in message
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
            }
        ],
    )

    message = TelegramNotifier.format_screener_summary(response)

    assert "Diagnostics:" in message
    assert "Top blockers: final score below keep threshold (2), confirmation too weak (1)" in message
    assert "- NVDA 1h rsi_vwap_ema_confluence | score 53.4" in message
