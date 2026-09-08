"""Auto-proposals must be sized from the stop distance when risk-based sizing is on."""

from __future__ import annotations

from app.risk.proposal_sizing import risk_based_proposal_notional
from tests.conftest import make_settings


def test_flat_default_when_disabled(tmp_path) -> None:
    settings = make_settings(tmp_path, default_trade_amount_usd=1000.0, auto_propose_risk_based_sizing=False)

    amount, details = risk_based_proposal_notional(
        settings, entry_price=100.0, stop_price=98.0, equity_usd=100_000.0
    )

    assert amount == 1000.0
    assert details["sizing_mode"] == "flat_default"


def test_sizes_to_risk_pct_of_equity(tmp_path) -> None:
    # 1% of $100k = $1,000 risk budget; a 2% stop -> $50,000 notional, under a $60k cap.
    settings = make_settings(
        tmp_path,
        auto_propose_risk_based_sizing=True,
        max_risk_per_trade_pct=1.0,
        default_trade_amount_usd=60_000.0,
        max_trade_amount_usd=60_000.0,
    )

    amount, details = risk_based_proposal_notional(
        settings, entry_price=100.0, stop_price=98.0, equity_usd=100_000.0
    )

    assert amount == 50_000.0
    assert details["sizing_mode"] == "risk_based"
    assert details["capped"] is False
    assert details["effective_risk_usd"] == 1000.0


def test_notional_cap_bounds_tight_stops(tmp_path) -> None:
    # A 0.2% stop would imply $500k notional; the $12,500 cap wins and the
    # effective risk drops to $25 — the cap protects against oversized positions.
    settings = make_settings(
        tmp_path,
        auto_propose_risk_based_sizing=True,
        max_risk_per_trade_pct=1.0,
        default_trade_amount_usd=12_500.0,
        max_trade_amount_usd=12_500.0,
    )

    amount, details = risk_based_proposal_notional(
        settings, entry_price=100.0, stop_price=99.8, equity_usd=100_000.0
    )

    assert amount == 12_500.0
    assert details["capped"] is True
    assert details["effective_risk_usd"] == 25.0


def test_falls_back_to_flat_default_without_a_stop(tmp_path) -> None:
    settings = make_settings(tmp_path, auto_propose_risk_based_sizing=True, default_trade_amount_usd=1000.0)

    amount, details = risk_based_proposal_notional(settings, entry_price=100.0, stop_price=None, equity_usd=100_000.0)

    assert amount == 1000.0
    assert details["fallback_reason"] == "missing_entry_stop_or_equity"
