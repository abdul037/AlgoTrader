from __future__ import annotations

from types import SimpleNamespace

from scripts.verify_alpaca import config_blockers


def _settings(**overrides):
    base = dict(
        alpaca_enabled=True,
        alpaca_api_key="PKxxxxxxxxxxxxxxxxxxx",
        alpaca_secret_key="secretxxxxxxxxxxxxxx",
        alpaca_expected_account_number="PA3B287XBZYU",
        execution_mode="paper",
        enable_real_trading=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_ready_config_has_no_blockers() -> None:
    assert config_blockers(_settings()) == []


def test_disabled_alpaca_blocks() -> None:
    assert "ALPACA_ENABLED is false" in config_blockers(_settings(alpaca_enabled=False))


def test_missing_keys_block() -> None:
    blockers = config_blockers(_settings(alpaca_api_key="", alpaca_secret_key=""))
    assert "ALPACA_API_KEY is not set" in blockers
    assert "ALPACA_SECRET_KEY is not set" in blockers


def test_missing_account_pin_blocks() -> None:
    assert "ALPACA_EXPECTED_ACCOUNT_NUMBER is not set" in config_blockers(
        _settings(alpaca_expected_account_number="")
    )


def test_real_trading_blocks_paper_preflight() -> None:
    blockers = config_blockers(_settings(enable_real_trading=True))
    assert any("ENABLE_REAL_TRADING is true" in b for b in blockers)
