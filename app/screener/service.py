"""Universe screener service."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from app.backtesting.batch import BatchBacktestService
from app.backtesting.strategy_selection import (
    active_strategy_names as _active_strategy_names,
)
from app.backtesting.strategy_selection import (
    strategy_kwargs_for as _strategy_kwargs,
)
from app.backtesting.strategy_selection import (
    strategy_specs_for as _strategy_specs,
)
from app.intelligence import MarketIntelligenceService
from app.live_signal_schema import LiveSignalSnapshot
from app.models.screener import MarketUniverseResponse, ScreenerRunResponse
from app.runtime_settings import AppSettings
from app.screener.filters import ScreenerFilterPipeline
from app.screener.profiles import effective_screener_settings
from app.screener.service_backtests import (
    backtest_validation,
    bars_for_timeframe,
    compute_risk_reward,
    get_latest_backtest_summary,
)
from app.screener.service_diagnostics import (
    add_scan_diagnostic,
    best_rejected_setup,
    diagnostic_measurements,
    execution_blockers,
    guidance_for_rejection,
    increment_rejection,
    market_data_status,
    no_trade_improvement_guidance,
    normalize_rejection_reasons,
    rank_closest_rejections,
    ranking_key,
    recent_scan_decisions,
    record_scan_decision,
    scan_cancelled,
)
from app.screener.service_scan import scan_universe
from app.screener.service_snapshots import (
    build_data_unavailable_snapshot,
    build_no_trade_snapshot,
    snapshot_from_signal,
)
from app.strategies import get_strategy
from app.strategies.weak_signals import configure_weak_signal_emission
from app.telegram_notify import TelegramNotifier
from app.universe import resolve_universe

__all__ = [
    "BatchBacktestService",
    "MarketScreenerService",
    "_active_strategy_names",
    "_strategy_kwargs",
    "_strategy_specs",
]


class MarketScreenerService:
    """Evaluate a configurable market universe across multiple strategies and timeframes."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        market_data_engine: Any,
        signal_state_repository: Any,
        run_log_repository: Any,
        backtest_repository: Any | None = None,
        scan_decision_repository: Any | None = None,
        telegram_notifier: TelegramNotifier | Any | None = None,
        learning_service: Any | None = None,
        strategy_lab_service: Any | None = None,
    ):
        self.settings = settings
        self.effective_settings = effective_screener_settings(settings)
        self.market_data = market_data_engine
        self.signal_states = signal_state_repository
        self.logs = run_log_repository
        self.backtests = backtest_repository
        self.scan_decisions = scan_decision_repository
        self.notifier = telegram_notifier
        self.learning = learning_service
        self.strategy_lab = strategy_lab_service
        self.filters = ScreenerFilterPipeline(self.effective_settings)
        self.intelligence = MarketIntelligenceService(self.effective_settings, market_data_engine)
        self._regime_cache: tuple[float, Any] | None = None

    def get_universe(self, *, limit: int | None = None) -> MarketUniverseResponse:
        symbols = resolve_universe(self.settings, limit=limit)
        return MarketUniverseResponse(
            universe_name=self.settings.market_universe_name,
            symbols=symbols,
            count=len(symbols),
        )

    def scan_universe(
        self,
        *,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        limit: int | None = None,
        validated_only: bool = False,
        notify: bool = False,
        force_refresh: bool = False,
        scan_task: str = "manual_scan",
        strategy_spec_keys: list[str] | None = None,
        cancel_event: Any | None = None,
    ) -> ScreenerRunResponse:
        return scan_universe(
            self,
            symbols=symbols,
            timeframes=timeframes,
            limit=limit,
            validated_only=validated_only,
            notify=notify,
            force_refresh=force_refresh,
            scan_task=scan_task,
            strategy_spec_keys=strategy_spec_keys,
            cancel_event=cancel_event,
        )

    def analyze_symbol(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
    ) -> LiveSignalSnapshot:
        """Return a premium single-symbol analysis using the screener stack."""

        normalized = symbol.upper().strip()
        response = self.scan_universe(
            symbols=[normalized],
            timeframes=list(self.settings.single_symbol_analysis_timeframes),
            limit=5,
            validated_only=False,
            notify=False,
            force_refresh=force_refresh,
            scan_task="single_symbol_analysis",
        )
        if response.candidates:
            best = response.candidates[0].model_copy(deep=True)
            best.metadata["analysis_mode"] = "single_symbol"
            best.metadata["analysis_candidates_evaluated"] = response.evaluated_strategy_runs
            best.metadata["analysis_errors"] = list(response.errors)
            return best
        if response.errors:
            return self._build_data_unavailable_snapshot(normalized, response=response)
        return self._build_no_trade_snapshot(normalized, response=response, force_refresh=force_refresh)

    def _snapshot_from_signal(self, signal: Any, **kwargs: Any) -> LiveSignalSnapshot:
        snapshot = snapshot_from_signal(self, signal, **kwargs)
        return self.learning.apply_to_snapshot(snapshot) if self.learning is not None else snapshot

    def _backtest_validation(self, symbol: str, strategy_name: str, timeframe: str | None = None) -> dict[str, Any]:
        return backtest_validation(self, symbol, strategy_name, timeframe)

    def _strategy_specs_for_timeframe(self, timeframe: str, strategy_spec_keys: list[str] | set[str] | None = None) -> list[Any]:
        specs = list(_strategy_specs(self.settings, timeframe=timeframe, regime=self._current_regime()))
        if self.strategy_lab is not None:
            specs.extend(self.strategy_lab.active_specs(timeframe=timeframe))
        if strategy_spec_keys:
            requested = {str(item).strip().lower() for item in strategy_spec_keys if str(item).strip()}
            specs = [spec for spec in specs if self._strategy_spec_key(spec) in requested]
        return specs

    def _current_regime(self) -> Any | None:
        """The current market regime for the router, or None when disabled/unavailable.

        Computed at most once per ``regime_router_cache_seconds`` window and cached
        on the instance so a full scan (many symbols x timeframes) triggers a single
        index-ETF fetch, not one per candidate. Any failure yields None, and the
        router treats None as 'run every family' — so a regime hiccup never starves
        the scan. Entirely inert unless ``regime_router_enabled``."""

        if not bool(getattr(self.settings, "regime_router_enabled", False)):
            return None
        ttl = float(getattr(self.settings, "regime_router_cache_seconds", 300.0) or 0.0)
        now = time.monotonic()
        if self._regime_cache is not None and (now - self._regime_cache[0]) < ttl:
            return self._regime_cache[1]
        try:
            signal = self.intelligence.market_regime_signal()
        except Exception:
            signal = None
        self._regime_cache = (now, signal)
        return signal

    def strategy_spec_keys_for_timeframes(self, timeframes: list[str]) -> list[str]:
        """Return stable spec keys for workflow-level scheduled batching."""

        keys: list[str] = []
        for timeframe in timeframes:
            for spec in self._strategy_specs_for_timeframe(str(timeframe).strip().lower()):
                key = self._strategy_spec_key(spec)
                if key not in keys:
                    keys.append(key)
        return keys

    @staticmethod
    def _strategy_spec_key(spec: Any) -> str:
        return f"{str(getattr(spec, 'name', '')).strip().lower()}:{str(getattr(spec, 'timeframe', '')).strip().lower()}"

    def _build_strategy(self, spec: Any) -> Any:
        if self.strategy_lab is not None:
            strategy = self.strategy_lab.build_strategy_for_spec(spec)
            if strategy is not None:
                self._align_strategy_liquidity_floor(strategy)
                return configure_weak_signal_emission(strategy, self.settings)
        strategy = get_strategy(spec.name, **_strategy_kwargs(self.settings, spec))
        self._inject_providers(strategy, spec)
        self._align_strategy_liquidity_floor(strategy)
        return configure_weak_signal_emission(strategy, self.settings)

    def _align_strategy_liquidity_floor(self, strategy: Any) -> None:
        """Align a strategy's internal dollar-volume floor to the screener's.

        Live scanning runs on the Alpaca IEX feed, which prints only a small
        slice of consolidated volume. Strategy classes (gap/breakout/
        continuation) carry SIP-scale ``minimum_dollar_volume`` defaults
        ($2-5M) that reject every liquid name on IEX, poisoning the near-miss
        reason list with false ``average_dollar_volume_below_threshold``. Pin
        their floor to the (feed-calibrated) screener liquidity floor so the
        live path and the screener agree on what "liquid enough" means.

        This is intentionally the *live* path only -- the batch backtester
        builds strategies via ``get_strategy`` directly and keeps the SIP-scale
        defaults, since it evaluates against consolidated historical data.
        """
        if not hasattr(strategy, "minimum_dollar_volume"):
            return
        with suppress(Exception):
            strategy.minimum_dollar_volume = float(
                self.settings.screener_min_average_dollar_volume
            )

    def _inject_providers(self, strategy: Any, spec: Any) -> None:
        """Give multi-leg strategies (pairs/stat-arb) the extra data they need.

        The single-symbol scan can't hand a strategy its second leg, so a pairs
        strategy pulls it through a provider bound to this screener's market-data
        engine and the spec's timeframe -- the same wiring the batch backtester
        uses, so a pairs edge validated in backtest behaves identically live."""

        if hasattr(strategy, "set_hedge_provider"):
            timeframe = getattr(spec, "timeframe", "1d")
            strategy.set_hedge_provider(
                lambda sym, bars, _tf=timeframe: self.market_data.get_history(sym, timeframe=_tf, bars=bars)
            )

    def _get_latest_backtest_summary(
        self,
        symbol: str,
        strategy_name: str,
        timeframe: str | None,
    ) -> dict[str, Any] | None:
        return get_latest_backtest_summary(self, symbol, strategy_name, timeframe)

    @staticmethod
    def _compute_risk_reward(signal: Any) -> float | None:
        return compute_risk_reward(signal)

    @staticmethod
    def _bars_for_timeframe(timeframe: str) -> int:
        return bars_for_timeframe(timeframe)

    @staticmethod
    def _ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, int, float, float]:
        return ranking_key(snapshot)

    @staticmethod
    def _add_scan_diagnostic(rejection_summary: dict[str, int], closest_rejections: list[dict[str, Any]], **kwargs: Any) -> None:
        add_scan_diagnostic(rejection_summary, closest_rejections, **kwargs)

    @staticmethod
    def _increment_rejection(rejection_summary: dict[str, int], reason: str) -> None:
        increment_rejection(rejection_summary, reason)

    @staticmethod
    def _normalize_rejection_reasons(rejection_reasons: list[str]) -> list[str]:
        return normalize_rejection_reasons(rejection_reasons)

    @staticmethod
    def _rank_closest_rejections(closest_rejections: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
        return rank_closest_rejections(closest_rejections, limit=limit)

    @staticmethod
    def _diagnostic_measurements(measurements: dict[str, Any]) -> dict[str, Any]:
        return diagnostic_measurements(measurements)

    def _record_scan_decision(self, **kwargs: Any) -> None:
        record_scan_decision(self, **kwargs)

    def _market_data_status(self, *, history: Any, quote: Any) -> dict[str, Any]:
        return market_data_status(self, history=history, quote=quote)

    def _execution_blockers(
        self,
        *,
        market_data_status: dict[str, Any],
        final_score: float,
        risk_reward_ratio: float | None,
        actionability: str,
    ) -> list[str]:
        return execution_blockers(
            self,
            settings=self.effective_settings,
            market_data_status=market_data_status,
            final_score=final_score,
            risk_reward_ratio=risk_reward_ratio,
            actionability=actionability,
        )

    def _build_no_trade_snapshot(
        self,
        symbol: str,
        *,
        response: ScreenerRunResponse,
        force_refresh: bool,
    ) -> LiveSignalSnapshot:
        return build_no_trade_snapshot(self, symbol, response=response, force_refresh=force_refresh)

    def _recent_scan_decisions(self, *, symbol: str, limit: int) -> list[Any]:
        return recent_scan_decisions(self, symbol=symbol, limit=limit)

    @staticmethod
    def _best_rejected_setup(decisions: list[Any]) -> dict[str, Any] | None:
        return best_rejected_setup(decisions)

    def _no_trade_improvement_guidance(
        self,
        *,
        reasons: list[str],
        measurements: dict[str, Any],
        market_data_status: dict[str, Any],
    ) -> str:
        return no_trade_improvement_guidance(
            self,
            reasons=reasons,
            measurements=measurements,
            market_data_status=market_data_status,
        )

    def _guidance_for_rejection(self, reason: str, measurements: dict[str, Any]) -> str | None:
        return guidance_for_rejection(self, reason, measurements)

    @staticmethod
    def _scan_cancelled(cancel_event: Any | None) -> bool:
        return scan_cancelled(cancel_event)

    def _build_data_unavailable_snapshot(
        self,
        symbol: str,
        *,
        response: ScreenerRunResponse,
    ) -> LiveSignalSnapshot:
        return build_data_unavailable_snapshot(self, symbol, response=response)
