from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.live_signal_schema import MarketQuote
from app.screener.service import MarketScreenerService, _strategy_specs
from app.screener.scoring import build_backtest_snapshot
from tests.conftest import make_settings


class FakeMarketDataEngine:
    def __init__(
        self,
        frames: dict[tuple[str, str], pd.DataFrame],
        quotes: dict[str, MarketQuote],
        *,
        verified: bool = True,
        provider: str = "test_feed",
    ):
        self.frames = frames
        self.quotes = quotes
        self.verified = verified
        self.provider = provider

    def get_history(self, symbol: str, *, timeframe: str = "1d", bars: int = 250, provider=None, force_refresh: bool = False):
        frame = self.frames[(symbol.upper(), timeframe)].copy()
        if self.verified:
            frame.attrs.update(
                {
                    "provider": self.provider,
                    "requested_provider": self.provider,
                    "used_fallback": False,
                    "from_cache": False,
                    "data_age_seconds": 30.0,
                }
            )
        return frame

    def get_quote(self, symbol: str, *, timeframe: str = "1d", provider=None, force_refresh: bool = False):
        quote = self.quotes[symbol.upper()]
        if not self.verified:
            return quote
        return quote.model_copy(
            update={
                "source": self.provider,
                "is_primary": True,
                "used_fallback": False,
                "from_cache": False,
                "quote_derived_from_history": False,
                "data_age_seconds": 30.0,
            }
        )


class FakeSignalStateRepository:
    def __init__(self) -> None:
        self.items = []

    def upsert(self, snapshot):
        self.items.append(snapshot)
        return snapshot


class FakeRunLogRepository:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def log(self, event_type: str, payload: dict) -> None:
        self.items.append((event_type, payload))


class FakeBacktestRepository:
    def __init__(self, summary: dict | None = None) -> None:
        self.summary = summary

    def get_latest_summary(self, symbol: str, strategy_name: str | None = None):
        return self.summary


class FakeTelegramNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(self, message: str, *, chat_id: str | None = None) -> bool:
        self.messages.append(message)
        return True

    @staticmethod
    def format_screener_summary(response) -> str:
        return f"summary:{len(response.candidates)}"


class FakeScanDecisionRepository:
    def __init__(self) -> None:
        self.items: list[SimpleNamespace] = []

    def create(self, **kwargs):
        record = SimpleNamespace(**kwargs)
        self.items.append(record)
        return record

    def get_latest(
        self,
        *,
        symbol: str,
        strategy_name: str,
        timeframe: str,
        since_minutes: int | None = None,
        statuses: list[str] | None = None,
    ):
        for item in reversed(self.items):
            if item.symbol != symbol.upper():
                continue
            if item.strategy_name != strategy_name or item.timeframe != timeframe:
                continue
            if statuses and item.status not in statuses:
                continue
            return item
        return None


def _frame(closes: list[float], *, start: str = "2026-01-01T00:00:00Z", step: str = "1D") -> pd.DataFrame:
    timestamps = pd.date_range(start=start, periods=len(closes), freq=step, tz="UTC")
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "timestamp": timestamps[index],
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + (index * 1000),
            }
        )
    return pd.DataFrame(rows)


def test_primary_strategy_mode_limits_specs_to_confluence(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        screener_active_strategy_names=["rsi_vwap_ema_confluence"],
    )

    for timeframe in ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]:
        specs = _strategy_specs(settings, timeframe=timeframe)
        assert specs
        assert {spec.name for spec in specs} == {"rsi_vwap_ema_confluence"}


def test_screener_scan_returns_ranked_candidates(tmp_path) -> None:
    daily = _frame([100 + (index * 0.8) for index in range(120)] + [198, 202, 206, 211, 218])
    daily.loc[daily.index[-1], "volume"] = daily["volume"].tail(20).mean() * 2.0
    hourly = _frame([50 + (index * 0.4) for index in range(80)], step="1H")
    quote = MarketQuote(symbol="NVDA", bid=218.0, ask=218.2, last_execution=218.1, timestamp="2026-04-11T10:00:00Z")
    backtest_summary = {
        "strategy_name": "momentum_breakout",
        "completed_at": "2026-04-10T12:00:00Z",
        "out_of_sample": True,
        "metrics": {
            "number_of_trades": 24,
            "profit_factor": 1.8,
            "annualized_return_pct": 16.5,
            "max_drawdown_pct": 18.0,
            "win_rate": 54.0,
            "out_of_sample": True,
        },
    }
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            screener_min_confidence=0.4,
            screener_max_extension_atr_multiple=10.0,
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): daily, ("NVDA", "1h"): hourly}, {"NVDA": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=backtest_summary),
        telegram_notifier=FakeTelegramNotifier(),
    )

    response = service.scan_universe(limit=5)

    assert response.evaluated_symbols == 1
    assert response.candidates
    assert response.candidates[0].symbol == "NVDA"
    assert response.candidates[0].metadata["backtest_validated"] is True
    assert response.candidates[0].risk_reward_ratio is not None


def test_screener_notify_sends_summary(tmp_path) -> None:
    daily = _frame([100 + (index * 0.5) for index in range(120)] + [165, 169, 173, 178, 182])
    quote = MarketQuote(symbol="AMD", bid=182.0, ask=182.2, last_execution=182.1, timestamp="2026-04-11T10:00:00Z")
    notifier = FakeTelegramNotifier()
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["AMD"],
            screener_default_timeframes=["1d"],
            require_backtest_validation_for_alerts=False,
        ),
        market_data_engine=FakeMarketDataEngine({("AMD", "1d"): daily}, {"AMD": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=None),
        telegram_notifier=notifier,
    )

    response = service.scan_universe(limit=3, notify=True)

    assert response.alerts_sent == 1
    assert len(notifier.messages) == 1
    assert notifier.messages[0].startswith("summary:")


def test_screener_candidate_includes_backtest_snapshot_and_reasons(tmp_path) -> None:
    daily = _frame([100 + (index * 0.8) for index in range(120)] + [198, 202, 206, 211, 218])
    daily.loc[daily.index[-1], "volume"] = daily["volume"].tail(20).mean() * 2.0
    quote = MarketQuote(symbol="NVDA", bid=218.0, ask=218.1, last_execution=218.05, timestamp="2026-04-11T10:00:00Z")
    backtest_summary = {
        "strategy_name": "momentum_breakout",
        "completed_at": "2026-04-10T12:00:00Z",
        "out_of_sample": True,
        "metrics": {
            "number_of_trades": 28,
            "profit_factor": 1.9,
            "annualized_return_pct": 18.5,
            "max_drawdown_pct": 16.0,
            "win_rate": 58.0,
            "out_of_sample": True,
        },
        "trades": [{"pnl_usd": 120.0, "pnl_pct": 1.4}, {"pnl_usd": -45.0, "pnl_pct": -0.5}],
    }
    decisions = FakeScanDecisionRepository()
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            screener_min_confidence=0.4,
            screener_max_extension_atr_multiple=10.0,
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): daily}, {"NVDA": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=backtest_summary),
        scan_decision_repository=decisions,
        telegram_notifier=FakeTelegramNotifier(),
    )

    response = service.scan_universe(limit=3)

    assert response.candidates
    candidate = response.candidates[0]
    assert candidate.score_breakdown
    assert candidate.pass_reasons
    assert candidate.backtest_snapshot["validated"] is True
    assert candidate.metadata["alert_eligible"] is True
    assert any(item.status == "candidate" for item in decisions.items)


def test_diagnostic_measurements_preserve_volume_thresholds() -> None:
    compacted = MarketScreenerService._diagnostic_measurements(
        {
            "relative_volume": 0.87,
            "minimum_relative_volume": 1.08,
            "minimum_relative_volume_relaxed": 1.03,
            "volume_check_mode": "session_aware_relaxed",
        }
    )

    assert compacted["relative_volume"] == 0.87
    assert compacted["minimum_relative_volume"] == 1.08
    assert compacted["minimum_relative_volume_relaxed"] == 1.03
    assert compacted["volume_check_mode"] == "session_aware_relaxed"


def test_build_backtest_snapshot_zeroes_in_sample_evidence() -> None:
    built = build_backtest_snapshot(
        {
            "strategy_name": "momentum_breakout",
            "completed_at": "2026-04-10T12:00:00Z",
            "file_path": "auto:1d:NVDA",
            "out_of_sample": False,
            "metrics": {
                "number_of_trades": 50,
                "profit_factor": 1.8,
                "annualized_return_pct": 18.5,
                "win_rate": 58.0,
                "out_of_sample": False,
            },
            "trades": [{"pnl_usd": 120.0, "pnl_pct": 1.4}],
        },
        validated=True,
        validation_reason="passed",
    )

    assert built["validated"] is False
    assert built["validation_reason"] == "in_sample_only"
    assert built["profit_factor"] == 0.0
    assert built["profile_label"] == "in_sample_only"


def test_scheduled_scan_suppresses_flat_repeat_without_score_improvement(tmp_path) -> None:
    daily = _frame([100 + (index * 0.8) for index in range(120)] + [198, 202, 206, 211, 218])
    daily.loc[daily.index[-1], "volume"] = daily["volume"].tail(20).mean() * 2.0
    quote = MarketQuote(symbol="NVDA", bid=218.0, ask=218.1, last_execution=218.05, timestamp="2026-04-11T10:00:00Z")
    backtest_summary = {
        "strategy_name": "momentum_breakout",
        "completed_at": "2026-04-10T12:00:00Z",
        "out_of_sample": True,
        "metrics": {
            "number_of_trades": 28,
            "profit_factor": 1.9,
            "annualized_return_pct": 18.5,
            "max_drawdown_pct": 16.0,
            "win_rate": 58.0,
            "out_of_sample": True,
        },
        "trades": [{"pnl_usd": 120.0, "pnl_pct": 1.4}, {"pnl_usd": -45.0, "pnl_pct": -0.5}],
    }
    decisions = FakeScanDecisionRepository()
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            screener_min_confidence=0.4,
            screener_min_score_improvement_for_repeat=5.0,
            screener_max_extension_atr_multiple=10.0,
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): daily}, {"NVDA": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=backtest_summary),
        scan_decision_repository=decisions,
        telegram_notifier=FakeTelegramNotifier(),
    )

    first = service.scan_universe(limit=3, scan_task="premarket_scan")
    second = service.scan_universe(limit=3, scan_task="premarket_scan")

    assert first.candidates
    assert second.candidates == []
    assert second.suppressed >= 1
    assert any(item.status == "suppressed" for item in decisions.items)


def test_screener_strict_market_data_gate_blocks_unverified_candidates(tmp_path) -> None:
    daily = _frame([100 + (index * 0.8) for index in range(120)] + [198, 202, 206, 211, 218])
    daily.loc[daily.index[-1], "volume"] = daily["volume"].tail(20).mean() * 2.0
    quote = MarketQuote(symbol="NVDA", bid=218.0, ask=218.1, last_execution=218.05, timestamp="2026-04-11T10:00:00Z")
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            screener_min_confidence=0.4,
            require_verified_market_data_for_alerts=True,
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): daily}, {"NVDA": quote}, verified=False),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=None),
        scan_decision_repository=FakeScanDecisionRepository(),
        telegram_notifier=FakeTelegramNotifier(),
    )

    response = service.scan_universe(limit=3, scan_task="market_open_scan")

    assert response.candidates == []
    assert response.suppressed >= 1
    assert response.rejection_summary["missing_quote_provider"] >= 1
    assert response.rejection_summary["missing_history_provider"] >= 1
    assert response.closest_rejections
    assert response.closest_rejections[0]["symbol"] == "NVDA"


def test_screener_uses_strategy_near_miss_diagnostics_when_no_signal_fires(tmp_path) -> None:
    flat = _frame([100.0 for _ in range(90)])
    quote = MarketQuote(symbol="NVDA", bid=100.0, ask=100.1, last_execution=100.05, timestamp="2026-04-11T10:00:00Z")
    service = MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            screener_active_strategy_names=["rsi_vwap_ema_confluence"],
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): flat}, {"NVDA": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=None),
        scan_decision_repository=FakeScanDecisionRepository(),
        telegram_notifier=FakeTelegramNotifier(),
    )

    response = service.scan_universe(limit=3)

    assert response.candidates == []
    assert response.rejection_summary.get("no_strategy_signal", 0) == 0
    assert response.rejection_summary["relative_volume_too_low"] >= 1
    assert response.rejection_summary["adx_too_low"] >= 1
    assert response.closest_rejections
    assert response.closest_rejections[0]["status"] == "no_signal"
    assert response.closest_rejections[0]["strategy_name"] == "rsi_vwap_ema_confluence"
