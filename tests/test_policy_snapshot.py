"""The effective-execution-policy snapshot must expose the auto-exec decision
inputs (so a non-firing bot is diagnosable from run_logs) while never leaking a
credential."""

from __future__ import annotations

from app.automation.policy_snapshot import effective_execution_policy
from tests.conftest import make_settings

# Substrings that must never appear in a snapshot key — a snapshot is meant to be
# written to a shared log store, so it carries policy only, never secrets.
_SECRET_MARKERS = ("key", "secret", "token", "password", "credential", "dsn")


def test_snapshot_reflects_the_auto_exec_enablement_chain(tmp_path) -> None:
    snapshot = effective_execution_policy(
        make_settings(
            tmp_path,
            execution_mode="paper",
            enable_real_trading=False,
            paper_auto_operation_mode="unattended",
            paper_auto_approve_proposals=True,
            auto_execution_worker_enabled=True,
            paper_auto_approval_tier="tier2_strict_valid",
        )
    )

    assert snapshot["execution_mode"] == "paper"
    assert snapshot["enable_real_trading"] is False
    assert snapshot["paper_auto_operation_mode"] == "unattended"
    assert snapshot["paper_auto_approve_proposals"] is True
    assert snapshot["auto_execution_worker_enabled"] is True
    assert snapshot["paper_auto_approval_tier"] == "tier2_strict_valid"
    # The two 240s-timeout fixes are visible for at-a-glance health.
    assert "backtest_scheduler_deadline_seconds" in snapshot
    assert "screener_batch_deadline_seconds" in snapshot


def test_snapshot_contains_no_secret_bearing_keys(tmp_path) -> None:
    snapshot = effective_execution_policy(make_settings(tmp_path))

    offenders = [
        key
        for key in snapshot
        # "account_configured" is an allowed boolean; the raw number never appears.
        if any(marker in key.lower() for marker in _SECRET_MARKERS)
    ]
    assert not offenders, f"snapshot keys look secret-bearing: {offenders}"


def test_snapshot_never_leaks_credential_values(tmp_path) -> None:
    # Even with credentials configured, none of their values may appear anywhere
    # in the snapshot (it records a boolean for account configuration, not the id).
    settings = make_settings(
        tmp_path,
        alpaca_api_key="AKSECRETKEY123",
        alpaca_secret_key="SECRETVALUE456",
        alpaca_expected_account_number="PAPER-XYZ",
    )
    snapshot = effective_execution_policy(settings)

    serialized = repr(snapshot)
    assert "AKSECRETKEY123" not in serialized
    assert "SECRETVALUE456" not in serialized
    assert "PAPER-XYZ" not in serialized
    # ...but the fact that an expected account IS configured is still surfaced.
    assert snapshot["alpaca_expected_account_configured"] is True


def test_snapshot_exposes_position_sizing_caps(tmp_path) -> None:
    # The first blocked live attempt (AMD, 2026-09-08) failed at the broker step
    # because one share exceeded the per-trade amount; both sizing terms must be
    # visible from run_logs so that failure mode is diagnosable without app access.
    snapshot = effective_execution_policy(
        make_settings(tmp_path, default_trade_amount_usd=1000.0, max_trade_amount_usd=1000.0)
    )

    assert snapshot["default_trade_amount_usd"] == 1000.0
    assert snapshot["max_trade_amount_usd"] == 1000.0
    assert "max_open_positions" in snapshot


def test_snapshot_flags_universe_symbols_missing_from_the_allowlist(tmp_path) -> None:
    # 2026-09-08: the scanner used a 25-name universe while ALLOWED_INSTRUMENTS was
    # still a 6-name list, so most promoted candidates were rejected at the proposal
    # step. The snapshot must make that disagreement visible at boot.
    snapshot = effective_execution_policy(
        make_settings(
            tmp_path,
            market_universe_symbols=["AAPL", "META", "INTC"],
            allowed_instruments=["AAPL", "GOOGL"],
        )
    )

    assert snapshot["allowed_instruments_count"] == 2
    assert snapshot["market_universe_symbols_count"] == 3
    assert snapshot["universe_not_in_allowlist"] == ["INTC", "META"]


def test_snapshot_reports_empty_gap_when_lists_agree(tmp_path) -> None:
    snapshot = effective_execution_policy(
        make_settings(tmp_path, market_universe_symbols=["AAPL", "META"], allowed_instruments=["META", "AAPL"])
    )

    assert snapshot["universe_not_in_allowlist"] == []


def test_snapshot_shows_whether_the_shadow_premarket_scan_is_on(tmp_path) -> None:
    assert effective_execution_policy(make_settings(tmp_path))["premarket_deep_scan_enabled"] is False
    on = make_settings(tmp_path, premarket_deep_scan_enabled=True)
    assert effective_execution_policy(on)["premarket_deep_scan_enabled"] is True


def test_snapshot_shows_whether_short_setups_are_recorded_in_shadow(tmp_path) -> None:
    # Shorts step 2: default on (never traded); logged so the switch is checkable at boot.
    assert effective_execution_policy(make_settings(tmp_path))["premarket_short_shadow_enabled"] is True
    off = make_settings(tmp_path, premarket_short_shadow_enabled=False)
    assert effective_execution_policy(off)["premarket_short_shadow_enabled"] is False


def test_snapshot_shows_the_scheduled_backtest_timeframes_and_minute_bar_delay(tmp_path) -> None:
    # A Railway variable replaces the default list, so whether 10m is really scheduled is
    # only checkable from the boot log (2026-10-20).
    settings = make_settings(
        tmp_path,
        backtest_scheduler_timeframes="1d,15m,5m,10m",
        walk_forward_intraday_entry_delay_bars=1,
    )
    snapshot = effective_execution_policy(settings)
    assert snapshot["backtest_scheduler_timeframes"] == ["1d", "15m", "5m", "10m"]
    assert snapshot["walk_forward_intraday_entry_delay_bars"] == 1


def test_snapshot_shows_the_effective_etoro_copy_sizing(tmp_path) -> None:
    # Operator 2026-10-26: equal-risk copies (25% / $2,500, 0.5% risk, 100% exposure, 3 a day).
    expected = {
        "etoro_live_trade_pct_of_equity": 25.0,
        "etoro_live_hard_max_trade_pct": 25.0,
        "etoro_live_hard_max_trade_usd": 2_500.0,
        "etoro_live_hard_max_risk_pct": 0.5,
        "etoro_live_hard_max_exposure_pct": 100.0,
        "etoro_live_hard_max_trades_per_day": 3,
    }
    snapshot = effective_execution_policy(make_settings(tmp_path))
    assert {k: snapshot[k] for k in expected} == expected
    # A Railway value left at the old 10 shows as 10; one above the hard cap shows the cap.
    for value, effective in ((10.0, 10.0), (90.0, 25.0), (-5.0, 0.0)):
        settings = make_settings(tmp_path, etoro_live_trade_pct_of_equity=value)
        assert effective_execution_policy(settings)["etoro_live_trade_pct_of_equity"] == effective
    # Caps only, from the settings and code constants: no account value, key or secret.
    from app.broker.etoro_live_mirror import sizing_policy

    assert set(sizing_policy(make_settings(tmp_path))) == set(expected)
    assert not any(m in k for k in expected for m in _SECRET_MARKERS)
