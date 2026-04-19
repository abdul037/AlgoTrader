from __future__ import annotations

from app.universe import DEFAULT_TOP_100_US, resolve_universe
from tests.conftest import make_settings


def test_default_universe_returns_top_100(tmp_path) -> None:
    settings = make_settings(tmp_path, market_universe_limit=100)

    symbols = resolve_universe(settings)

    assert len(symbols) == 100
    assert symbols[0] == DEFAULT_TOP_100_US[0]
    assert "NVDA" in symbols


def test_custom_universe_is_normalized_and_deduplicated(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        market_universe_symbols=[" nvda ", "amd", "NVDA", " msft "],
        market_universe_limit=10,
    )

    symbols = resolve_universe(settings)

    assert symbols == ["NVDA", "AMD", "MSFT"]
