"""Scan hot-path fixes (2026-10-10): the strategy-lab lookup cache and atomic cache writes."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.data.engine import MarketDataEngine
from app.models.strategy_lab import (
    StrategyBacktestRequest,
    StrategyGenerationRequest,
    StrategyPromotionRequest,
)
from app.strategy_lab.dsl import GeneratedRuleStrategy
from tests.conftest import make_settings
from tests.test_strategy_lab import _dsl
from tests.test_strategy_lab import _service as _lab_service


def _by_name(name: str) -> SimpleNamespace:  # a spec with no generated_strategy_id
    return SimpleNamespace(name=name, default_kwargs={})


def test_built_in_specs_skip_the_by_name_read_within_the_ttl(tmp_path, monkeypatch) -> None:
    # Every scan strategy run asked the database for a generated strategy of the same
    # name (~0.58 s from Railway to Sydney) in a table that holds none.
    service, repository, _ = _lab_service(tmp_path)
    calls: Counter[str] = Counter()
    real_list = repository.list_generated
    monkeypatch.setattr(
        repository, "list_generated", lambda **kw: calls.update(["list"]) or real_list(**kw)
    )
    monkeypatch.setattr(repository, "get_generated_by_name", lambda name: calls.update(["by_name"]))
    assert all(
        service.build_strategy_for_spec(_by_name("trend_following")) is None for _ in range(50)
    )
    assert service.active_specs(timeframe="1d") == []
    assert calls == Counter({"list": 1})


def test_promoting_through_the_service_is_seen_at_once(tmp_path) -> None:
    service, _, _ = _lab_service(tmp_path)
    generated = service.generate(StrategyGenerationRequest(dsl=_dsl(), source="test"))
    assert (
        service.build_strategy_for_spec(_by_name(generated.name)) is None
    )  # not promoted; now cached
    service.backtest(generated.id, StrategyBacktestRequest(symbols=["AAPL"], limit=1))
    assert service.build_strategy_for_spec(_by_name(generated.name)) is None  # passed, not promoted
    service.promote_paper(generated.id, StrategyPromotionRequest(decided_by="test"))
    assert isinstance(
        service.build_strategy_for_spec(_by_name(generated.name)), GeneratedRuleStrategy
    )
    assert [spec.name for spec in service.active_specs(timeframe="1d")] == [generated.name]


def test_a_change_made_elsewhere_shows_after_the_ttl(tmp_path) -> None:
    service, repository, _ = _lab_service(tmp_path)
    generated = service.generate(StrategyGenerationRequest(dsl=_dsl(), source="test"))
    assert service.build_strategy_for_spec(_by_name(generated.name)) is None
    repository.update_generated_status(
        generated.id, status="paper_generated", latest_backtest_id=None
    )
    assert service.build_strategy_for_spec(_by_name(generated.name)) is None  # cached up to 60 s
    stamp, items = service._paper_generated_cache
    service._paper_generated_cache = (stamp - 61.0, items)
    assert isinstance(
        service.build_strategy_for_spec(_by_name(generated.name)), GeneratedRuleStrategy
    )


def test_a_full_cached_list_still_reads_by_name(tmp_path, monkeypatch) -> None:
    service, repository, _ = _lab_service(tmp_path)
    other = service.generate(StrategyGenerationRequest(dsl=_dsl("other_rule"), source="test"))
    repository.update_generated_status(other.id, status="paper_generated", latest_backtest_id=None)
    monkeypatch.setattr(service, "_PAPER_GENERATED_LIMIT", 1)  # the list may be truncated
    looked: list[str] = []
    monkeypatch.setattr(repository, "get_generated_by_name", lambda name: looked.append(name))
    assert service.build_strategy_for_spec(_by_name("unlisted_rule")) is None
    assert looked == ["unlisted_rule"]


def test_a_write_during_the_refresh_is_not_cached_away(tmp_path, monkeypatch) -> None:
    # Review 2026-10-10: a refresh whose SELECT started before a promotion committed stored
    # its stale result after the promotion's invalidation, hiding it for up to 60 s.
    service, repository, _ = _lab_service(tmp_path)
    generated = service.generate(StrategyGenerationRequest(dsl=_dsl(), source="test"))
    real_list = repository.list_generated

    def racing_list(**kwargs):
        stale = real_list(**kwargs)  # the SELECT ran before the promotion committed
        repository.update_generated_status(
            generated.id, status="paper_generated", latest_backtest_id=None
        )
        service._invalidate_generated_cache()  # what promote_paper does after its write
        return stale

    monkeypatch.setattr(repository, "list_generated", racing_list)
    assert service.active_specs(timeframe="1d") == []  # the stale read is used once...
    monkeypatch.setattr(repository, "list_generated", real_list)
    assert [s.name for s in service.active_specs(timeframe="1d")] == [generated.name]  # ...not kept


def test_a_listed_strategy_retired_elsewhere_stops_being_listed(tmp_path) -> None:
    service, repository, _ = _lab_service(tmp_path)
    generated = service.generate(StrategyGenerationRequest(dsl=_dsl(), source="test"))
    repository.update_generated_status(
        generated.id, status="paper_generated", latest_backtest_id=None
    )
    [spec] = service.active_specs(timeframe="1d")  # now cached
    repository.update_generated_status(generated.id, status="retired", latest_backtest_id=None)
    assert service.build_strategy_for_spec(spec) is None  # the by-id read is always fresh
    assert service.active_specs(timeframe="1d") == []  # and the miss clears the cache at once


def test_a_strategy_that_cannot_be_built_does_not_abort_the_scan(tmp_path) -> None:
    # Review 2026-10-10 repro: a listed generated strategy retired mid-scan made
    # _build_strategy raise outside the per-strategy try, failing the whole scan_universe.
    import numpy as np

    from app.live_signal_schema import MarketQuote
    from app.main import create_app
    from app.models.strategy_lab import GeneratedStrategyRecord
    from tests.conftest import MockBroker

    class _Data:
        def get_history(
            self, symbol, *, timeframe="1d", bars=250, force_refresh=False, provider=None
        ):
            close = 100 * np.exp(np.cumsum(np.random.default_rng(3).normal(0.001, 0.01, 300)))
            return pd.DataFrame(
                {
                    "timestamp": pd.date_range(
                        end="2026-10-09 04:00", periods=300, freq="B", tz="UTC"
                    ),
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": np.full(300, 5_000_000.0),
                }
            )

        def get_quote(self, symbol, *, timeframe="1d", force_refresh=False, provider=None):
            return MarketQuote(symbol=symbol, bid=100.0, ask=100.0, last_execution=100.0)

    settings = make_settings(
        tmp_path,
        strategy_lab_enabled=True,
        strategy_lab_generation_enabled=True,
        strategy_lab_paper_trading_enabled=True,
        market_data_cache_dir=str(tmp_path / "cache"),
    )
    app = create_app(settings, broker=MockBroker(), enable_background_jobs=False)
    repository, screener = app.state.strategy_lab_repository, app.state.market_screener_service
    screener.market_data = _Data()
    record = repository.create_generated(
        GeneratedStrategyRecord(
            name="generated_x", dsl=_dsl("generated_x"), status="paper_generated"
        )
    )
    real_specs = screener._strategy_specs_for_timeframe
    listed = real_specs("1d")
    assert "generated_x" in {s.name for s in listed}
    repository.update_generated_status(record.id, status="retired", latest_backtest_id=None)
    screener._strategy_specs_for_timeframe = lambda timeframe, strategy_spec_keys=None: listed
    response = screener.scan_universe(symbols=["NVDA", "AMD"], timeframes=["1d"], limit=5)
    assert response.evaluated_strategy_runs == 2 * len(listed)  # every other strategy still ran
    assert any("generated_x" in error for error in response.errors)
    screener._strategy_specs_for_timeframe = real_specs
    assert "generated_x" not in {s.name for s in real_specs("1d")}  # and it is no longer listed


def _bars(count: int) -> pd.DataFrame:
    stamps = pd.date_range(datetime(2026, 10, 1, 4, tzinfo=UTC), periods=count, freq="1D")
    return pd.DataFrame(
        {"timestamp": stamps, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
    )


def _engine(tmp_path) -> MarketDataEngine:
    return MarketDataEngine(make_settings(tmp_path, market_data_cache_dir=str(tmp_path / "cache")))


def test_cache_writes_replace_the_file_whole(tmp_path) -> None:
    engine = _engine(tmp_path)
    path = tmp_path / "cache" / "NVDA_1d.csv"
    engine._write_cached_frame(path, _bars(3))
    first_inode = path.stat().st_ino
    engine._write_cached_frame(path, _bars(5))
    assert len(pd.read_csv(path)) == 5
    assert path.stat().st_ino != first_inode  # renamed into place, never rewritten in place
    assert sorted(p.name for p in path.parent.iterdir()) == ["NVDA_1d.csv", "NVDA_1d.meta.json"]


def test_a_failed_cache_write_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch) -> None:
    # A reader (e.g. a timed-out scan thread still running) must never see a truncated frame.
    engine = _engine(tmp_path)
    path = tmp_path / "cache" / "NVDA_1d.csv"
    engine._write_cached_frame(path, _bars(3))
    before = path.read_text()

    def half_write(self, target, *args, **kwargs):
        Path(target).write_text("timestamp,open\n2026-")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", half_write)
    engine._write_cached_frame(path, _bars(5))  # logs a warning, never raises
    assert path.read_text() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["NVDA_1d.csv", "NVDA_1d.meta.json"]
