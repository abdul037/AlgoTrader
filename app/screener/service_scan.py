"""Universe scan execution for the market screener service."""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.models.screener import ScreenerRunResponse
from app.screener.accuracy import build_accuracy_profile
from app.screener.filters import FilterOutcome, build_market_context
from app.screener.profiles import (
    effective_auto_execution_min_score,
    paper_exploration_profile_enabled,
)
from app.screener.scoring import build_backtest_snapshot, freshness_for_decision, rank_live_signal
from app.universe import resolve_universe
from app.utils.time import utc_now


class ScanTimeoutError(RuntimeError):
    """Raised when a bounded scan subtask exceeds its configured timeout."""


def _bounded_call(label: str, timeout_seconds: float, func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one blocking scan dependency behind a small timeout."""

    timeout = float(timeout_seconds or 0.0)
    if timeout <= 0:
        return func(*args, **kwargs)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener-timeout")
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise ScanTimeoutError(f"{label}_timeout_after_{timeout:g}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _normalize_spec_keys(strategy_spec_keys: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
    return {str(item).strip().lower() for item in strategy_spec_keys or [] if str(item).strip()}


def _spec_key(spec: Any) -> str:
    return f"{str(getattr(spec, 'name', '')).strip().lower()}:{str(getattr(spec, 'timeframe', '')).strip().lower()}"


def _strategy_specs_for_timeframe(service: Any, timeframe: str, requested_spec_keys: set[str]) -> list[Any]:
    try:
        return list(service._strategy_specs_for_timeframe(timeframe, strategy_spec_keys=requested_spec_keys or None))
    except TypeError:
        specs = list(service._strategy_specs_for_timeframe(timeframe))
        if requested_spec_keys:
            specs = [spec for spec in specs if _spec_key(spec) in requested_spec_keys]
        return specs


def _near_miss_allowed_reasons(settings: Any) -> set[str]:
    return {
        str(item).strip().lower()
        for item in (getattr(settings, "paper_near_miss_allowed_reasons", []) or [])
        if str(item).strip()
    }


def _paper_near_miss_blockers(
    service: Any,
    *,
    signal: Any,
    context: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    ranking: dict[str, Any],
    reasons: list[str],
) -> list[str]:
    settings = service.settings
    blockers: list[str] = []
    if not bool(getattr(settings, "paper_near_miss_promotion_enabled", False)):
        blockers.append("paper_near_miss_disabled")
    if not paper_exploration_profile_enabled(settings):
        blockers.append("paper_exploration_profile_inactive")
    action_value = getattr(getattr(signal, "action", None), "value", getattr(signal, "action", ""))
    if str(action_value).lower() != "buy":
        blockers.append("paper_near_miss_long_only")
    if str(getattr(signal, "metadata", {}).get("signal_role") or "entry_long").lower() == "entry_short":
        blockers.append("paper_near_miss_short_blocked")
    entry = float(getattr(signal, "price", None) or getattr(context, "current_price", 0.0) or 0.0)
    stop = getattr(signal, "stop_loss", None)
    target = getattr(signal, "take_profit", None)
    if stop is None or target is None:
        blockers.append("paper_near_miss_bracket_missing")
    else:
        try:
            if not (float(stop) < entry < float(target)):
                blockers.append("paper_near_miss_invalid_bracket")
        except (TypeError, ValueError):
            blockers.append("paper_near_miss_invalid_bracket")
    if not bool(market_data_status.get("verified", False)):
        blockers.append(str(market_data_status.get("verification_reason") or "market_data_unverified"))
    spread_bps = getattr(context, "spread_bps", None)
    if spread_bps is None:
        blockers.append("paper_near_miss_spread_unavailable")
    elif float(spread_bps) > float(getattr(settings, "screener_max_spread_bps", 50.0)):
        blockers.append("paper_near_miss_spread_too_wide")
    risk_reward = getattr(signal, "metadata", {}).get("risk_reward_ratio")
    if risk_reward is None:
        risk_reward = service._compute_risk_reward(signal)
    if risk_reward is None or float(risk_reward) < float(service.effective_settings.screener_min_reward_to_risk):
        blockers.append("paper_near_miss_reward_to_risk_too_low")
    near_miss_rvol_floor = float(getattr(settings, "paper_exploration_near_miss_min_relative_volume", 0.75))
    if float(getattr(context, "relative_volume", 0.0) or 0.0) < near_miss_rvol_floor:
        blockers.append("paper_near_miss_relative_volume_too_low")
    allowed = _near_miss_allowed_reasons(settings)
    normalized_reasons = {str(reason).strip().lower() for reason in reasons if str(reason).strip()}
    unsupported_reasons = sorted(normalized_reasons - allowed)
    if unsupported_reasons:
        blockers.append("paper_near_miss_unsupported_reasons:" + ",".join(unsupported_reasons))
    if filter_outcome.watchlist_only:
        blockers.append("paper_near_miss_watchlist_filter")
    score = float(ranking.get("final_score") or 0.0)
    minimum = effective_auto_execution_min_score(settings) - float(getattr(settings, "paper_near_miss_max_score_gap", 5.0) or 0.0)
    if score < minimum:
        blockers.append("paper_near_miss_score_gap_too_large")
    return blockers


def _maybe_promote_paper_near_miss(
    service: Any,
    *,
    signal: Any,
    quote: Any,
    timeframe: str,
    context: Any,
    intelligence: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    backtest_snapshot: dict[str, Any],
    ranking: dict[str, Any],
    freshness: str,
    reasons: list[str],
) -> Any | None:
    blockers = _paper_near_miss_blockers(
        service,
        signal=signal,
        context=context,
        market_data_status=market_data_status,
        filter_outcome=filter_outcome,
        ranking=ranking,
        reasons=reasons,
    )
    if blockers:
        return None
    snapshot = service._snapshot_from_signal(
        signal,
        quote=quote,
        timeframe=timeframe,
        context=context,
        intelligence=intelligence,
        market_data_status=market_data_status,
        filter_outcome=filter_outcome,
        backtest_snapshot=backtest_snapshot,
        ranking=ranking,
        freshness=freshness,
    )
    original_metadata = dict(snapshot.metadata or {})
    metadata = {
        **original_metadata,
        "alert_eligible": True,
        "execution_ready": True,
        "execution_blockers": [],
        "paper_near_miss_original_execution_blockers": list(original_metadata.get("execution_blockers") or []),
        "paper_near_miss_original_actionability": ranking.get("actionability"),
        "paper_near_miss_reasons": list(dict.fromkeys(reasons)),
        "paper_near_miss_score_gap": round(
            effective_auto_execution_min_score(service.settings) - float(ranking.get("final_score") or 0.0),
            4,
        ),
        "paper_near_miss_min_relative_volume": float(
            getattr(service.settings, "paper_exploration_near_miss_min_relative_volume", 0.75)
        ),
        "production_qualified": False,
        "signal_classification": "paper_near_miss",
        "source": "paper_near_miss",
    }
    return snapshot.model_copy(
        update={
            "execution_ready": True,
            "tradable": True,
            "direction_label": "buy",
            "metadata": metadata,
            "reject_reasons": list(dict.fromkeys(reasons)),
        }
    )


def scan_universe(
    service: Any,
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
    universe = [symbol.upper() for symbol in (symbols or resolve_universe(service.settings))]
    scan_timeframes = [timeframe.lower() for timeframe in (timeframes or service.settings.screener_default_timeframes)]
    requested_spec_keys = _normalize_spec_keys(strategy_spec_keys)
    candidates: list[Any] = []
    errors: list[str] = []
    rejection_summary: dict[str, int] = {}
    closest_rejections: list[dict[str, Any]] = []
    suppressed = 0
    evaluated_strategy_runs = 0
    evaluated_symbols = 0
    timed_out_runs = 0
    specs_by_timeframe: dict[str, int] = {}
    evaluated_spec_keys: set[str] = set()
    abort_scan = False
    quote_cache: dict[str, Any] = {}
    started_at = time.monotonic()
    deadline_seconds = float(getattr(service.settings, "screener_batch_deadline_seconds", 180.0) or 0.0)
    market_data_timeout = float(getattr(service.settings, "screener_market_data_timeout_seconds", 20.0) or 0.0)
    intelligence_timeout = float(getattr(service.settings, "screener_intelligence_timeout_seconds", 20.0) or 0.0)
    service.logs.log(
        "market_universe_scan_started",
        {
            "scan_task": scan_task,
            "universe_name": service.settings.market_universe_name,
            "symbols": universe,
            "timeframes": scan_timeframes,
            "strategy_spec_keys": sorted(requested_spec_keys),
            "validated_only": validated_only,
        },
    )

    for symbol in universe:
        if deadline_seconds > 0 and (time.monotonic() - started_at) >= deadline_seconds:
            errors.append("scan_deadline_exceeded")
            break
        if service._scan_cancelled(cancel_event):
            errors.append("scan_cancelled")
            break
        if abort_scan:
            break
        evaluated_symbols += 1
        for timeframe in scan_timeframes:
            if deadline_seconds > 0 and (time.monotonic() - started_at) >= deadline_seconds:
                errors.append("scan_deadline_exceeded")
                abort_scan = True
                break
            if service._scan_cancelled(cancel_event):
                errors.append("scan_cancelled")
                abort_scan = True
                break
            try:
                history = _bounded_call(
                    f"{symbol}_{timeframe}_history",
                    market_data_timeout,
                    service.market_data.get_history,
                    symbol,
                    timeframe=timeframe,
                    bars=service._bars_for_timeframe(timeframe),
                    force_refresh=force_refresh,
                )
                quote = quote_cache.get(symbol)
                if quote is None:
                    quote = _bounded_call(
                        f"{symbol}_{timeframe}_quote",
                        market_data_timeout,
                        service.market_data.get_quote,
                        symbol,
                        timeframe=timeframe,
                        force_refresh=force_refresh,
                    )
                    quote_cache[symbol] = quote
                market_data_status = service._market_data_status(history=history, quote=quote)
            except EToroRateLimitError as exc:
                errors.append(f"{symbol} {timeframe}: {exc}")
                service._add_scan_diagnostic(
                    rejection_summary,
                    closest_rejections,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_name="market_data",
                    status="error",
                    rejection_reasons=["market_data_rate_limited"],
                )
                abort_scan = True
                break
            except ScanTimeoutError as exc:
                timed_out_runs += 1
                errors.append(f"{symbol} {timeframe}: {exc}")
                service._add_scan_diagnostic(
                    rejection_summary,
                    closest_rejections,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_name="market_data",
                    status="error",
                    rejection_reasons=["market_data_timeout"],
                )
                continue
            except Exception as exc:
                errors.append(f"{symbol} {timeframe}: {exc}")
                service._add_scan_diagnostic(
                    rejection_summary,
                    closest_rejections,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_name="market_data",
                    status="error",
                    rejection_reasons=["market_data_error"],
                )
                continue

            specs = _strategy_specs_for_timeframe(service, timeframe, requested_spec_keys)
            specs_by_timeframe.setdefault(timeframe, len(specs))
            for spec in specs:
                if service._scan_cancelled(cancel_event):
                    errors.append("scan_cancelled")
                    abort_scan = True
                    break
                evaluated_strategy_runs += 1
                evaluated_spec_keys.add(_spec_key(spec))
                strategy = service._build_strategy(spec)
                try:
                    signal = strategy.generate_signal(history.copy(), symbol)
                except Exception as exc:
                    if isinstance(exc, ScanTimeoutError):
                        timed_out_runs += 1
                    errors.append(f"{symbol} {timeframe} {spec.name}: {exc}")
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=symbol,
                        timeframe=timeframe,
                        strategy_name=spec.name,
                        status="error",
                        rejection_reasons=["strategy_error"],
                    )
                    continue
                if signal is None:
                    strategy_diagnostics = getattr(strategy, "last_diagnostics", None)
                    if isinstance(strategy_diagnostics, dict):
                        measurements = {
                            **dict(strategy_diagnostics.get("measurements") or {}),
                            **market_data_status,
                        }
                        rejection_reasons = list(strategy_diagnostics.get("rejection_reasons") or ["no_strategy_signal"])
                        service._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=symbol,
                            timeframe=timeframe,
                            strategy_name=spec.name,
                            status=str(strategy_diagnostics.get("status") or "no_signal"),
                            rejection_reasons=rejection_reasons,
                            final_score=strategy_diagnostics.get("score"),
                            measurements=measurements,
                        )
                        if service.scan_decisions is not None:
                            service.scan_decisions.create(
                                scan_task=scan_task,
                                symbol=symbol,
                                strategy_name=spec.name,
                                timeframe=timeframe,
                                status=str(strategy_diagnostics.get("status") or "no_signal"),
                                final_score=strategy_diagnostics.get("score"),
                                alert_eligible=False,
                                freshness=None,
                                reason_codes=list(strategy_diagnostics.get("reason_codes") or rejection_reasons),
                                rejection_reasons=rejection_reasons,
                                payload={
                                    "measurements": service._diagnostic_measurements(measurements),
                                    "strategy_diagnostics": strategy_diagnostics,
                                    "market_data_status": market_data_status,
                                },
                            )
                    else:
                        service._increment_rejection(rejection_summary, "no_strategy_signal")
                        if service.scan_decisions is not None:
                            service.scan_decisions.create(
                                scan_task=scan_task,
                                symbol=symbol,
                                strategy_name=spec.name,
                                timeframe=timeframe,
                                status="no_signal",
                                final_score=None,
                                alert_eligible=False,
                                freshness=None,
                                reason_codes=["no_strategy_signal"],
                                rejection_reasons=["no_strategy_signal"],
                                payload={"market_data_status": market_data_status},
                            )
                    continue
                signal.metadata.setdefault("timeframe", timeframe)
                signal.metadata.setdefault("strategy_style", spec.style)
                backtest = service._backtest_validation(signal.symbol, signal.strategy_name, timeframe)
                backtest_snapshot = build_backtest_snapshot(
                    backtest["summary"],
                    validated=backtest["passes"],
                    validation_reason=backtest["reason"],
                )
                context = build_market_context(history, quote=quote, signal=signal)
                signal.metadata.setdefault(
                    "indicator_confluence_score",
                    float(context.measurements.get("indicator_confluence_score") or 0.0),
                )
                signal.metadata.setdefault(
                    "execution_quality",
                    float(context.measurements.get("execution_quality") or 0.5),
                )
                accuracy_profile = build_accuracy_profile(
                    history,
                    signal=signal,
                    context=context,
                    settings=service.effective_settings,
                )
                signal.metadata.update(
                    {
                        "accuracy_score": accuracy_profile.overall_score,
                        "entry_location_score": accuracy_profile.entry_location_score,
                        "support_resistance_score": accuracy_profile.support_resistance_score,
                        "confirmation_score": accuracy_profile.confirmation_score,
                        "false_positive_risk_score": accuracy_profile.false_positive_risk_score,
                        "accuracy_pass_reasons": list(accuracy_profile.pass_reasons),
                        "accuracy_rejection_reasons": list(accuracy_profile.rejection_reasons),
                        **accuracy_profile.measurements,
                    }
                )
                try:
                    intelligence = _bounded_call(
                        f"{symbol}_{timeframe}_{signal.strategy_name}_intelligence",
                        intelligence_timeout,
                        service.intelligence.analyze,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        history=history,
                        quote=quote,
                        signal=signal,
                        force_refresh=force_refresh,
                    )
                except ScanTimeoutError as exc:
                    timed_out_runs += 1
                    errors.append(f"{symbol} {timeframe} {signal.strategy_name}: {exc}")
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        strategy_name=signal.strategy_name,
                        status="error",
                        rejection_reasons=["intelligence_timeout"],
                    )
                    continue
                if service.settings.require_verified_market_data_for_alerts and not market_data_status["verified"]:
                    suppressed += 1
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        strategy_name=signal.strategy_name,
                        status="suppressed",
                        rejection_reasons=[market_data_status["verification_reason"]],
                        measurements=market_data_status,
                    )
                    service._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="suppressed",
                        final_score=None,
                        alert_eligible=False,
                        freshness=None,
                        filter_outcome=FilterOutcome(
                            passed=False,
                            pass_reasons=[],
                            rejection_reasons=[market_data_status["verification_reason"]],
                            reason_codes=[market_data_status["verification_reason"]],
                            measurements=market_data_status,
                        ),
                        payload={
                            "market_data_status": market_data_status,
                            "backtest_snapshot": backtest_snapshot,
                        },
                    )
                    continue
                filter_outcome = service.filters.evaluate(
                    signal=signal,
                    context=context,
                    backtest_snapshot=backtest_snapshot,
                    intelligence=intelligence,
                )
                if market_data_status["verified"]:
                    filter_outcome.pass_reasons.append("market_data_verified")
                    filter_outcome.reason_codes.append("market_data_verified")
                else:
                    filter_outcome.pass_reasons.append("market_data_unverified")
                    filter_outcome.reason_codes.append("market_data_unverified")
                filter_outcome.measurements.update(market_data_status)
                if not filter_outcome.passed:
                    previous_decision = (
                        service.scan_decisions.get_latest(
                            symbol=signal.symbol,
                            strategy_name=signal.strategy_name,
                            timeframe=timeframe,
                            since_minutes=service.settings.screener_duplicate_alert_window_minutes,
                            statuses=["candidate", "watchlist", "alerted"],
                        )
                        if service.scan_decisions is not None and scan_task != "manual_scan"
                        else None
                    )
                    provisional_freshness = "fresh" if previous_decision is None else "repeated_upgraded"
                    ranking = rank_live_signal(
                        settings=service.effective_settings,
                        signal=signal,
                        context=context,
                        backtest_snapshot=backtest_snapshot,
                        intelligence=intelligence,
                        watchlist_only=False,
                        freshness=provisional_freshness,
                    )
                    freshness, suppress_repeat = freshness_for_decision(
                        previous_decision,
                        final_score=float(ranking["final_score"]),
                        minimum_improvement=float(service.settings.screener_min_score_improvement_for_repeat),
                    )
                    near_miss_reasons = list(filter_outcome.rejection_reasons)
                    if float(ranking["final_score"]) < effective_auto_execution_min_score(service.settings):
                        near_miss_reasons.append("final_score_below_auto_threshold")
                    near_miss = None if suppress_repeat else _maybe_promote_paper_near_miss(
                        service,
                        signal=signal,
                        quote=quote,
                        timeframe=timeframe,
                        context=context,
                        intelligence=intelligence,
                        market_data_status=market_data_status,
                        filter_outcome=filter_outcome,
                        backtest_snapshot=backtest_snapshot,
                        ranking=ranking,
                        freshness=freshness,
                        reasons=near_miss_reasons,
                    )
                    if near_miss is not None:
                        service.signal_states.upsert(near_miss)
                        candidates.append(near_miss)
                        service._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=near_miss.symbol,
                            timeframe=timeframe,
                            strategy_name=near_miss.strategy_name,
                            status="paper_near_miss",
                            rejection_reasons=near_miss_reasons,
                            final_score=near_miss.score,
                            measurements=filter_outcome.measurements,
                        )
                        service._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="candidate",
                            final_score=near_miss.score,
                            alert_eligible=True,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=True,
                                pass_reasons=[*filter_outcome.pass_reasons, "paper_near_miss_promoted"],
                                rejection_reasons=near_miss_reasons,
                                reason_codes=[*filter_outcome.reason_codes, "paper_near_miss_promoted"],
                                measurements=filter_outcome.measurements,
                                watchlist_only=False,
                            ),
                            payload=near_miss.model_dump(),
                        )
                        continue
                    suppressed += 1
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        strategy_name=signal.strategy_name,
                        status="rejected",
                        rejection_reasons=filter_outcome.rejection_reasons,
                        final_score=None,
                        measurements=filter_outcome.measurements,
                    )
                    service._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="rejected",
                        final_score=None,
                        alert_eligible=False,
                        freshness=None,
                        filter_outcome=filter_outcome,
                        payload={
                            "backtest_snapshot": backtest_snapshot,
                            "measurements": filter_outcome.measurements,
                            "rationale": signal.rationale,
                        },
                    )
                    continue

                previous_decision = (
                    service.scan_decisions.get_latest(
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        timeframe=timeframe,
                        since_minutes=service.settings.screener_duplicate_alert_window_minutes,
                        statuses=["candidate", "watchlist", "alerted"],
                    )
                    if service.scan_decisions is not None and scan_task != "manual_scan"
                    else None
                )
                provisional_freshness = "fresh" if previous_decision is None else "repeated_upgraded"
                ranking = rank_live_signal(
                    settings=service.effective_settings,
                    signal=signal,
                    context=context,
                    backtest_snapshot=backtest_snapshot,
                    intelligence=intelligence,
                    watchlist_only=filter_outcome.watchlist_only,
                    freshness=provisional_freshness,
                )
                freshness, suppress_repeat = freshness_for_decision(
                    previous_decision,
                    final_score=float(ranking["final_score"]),
                    minimum_improvement=float(service.settings.screener_min_score_improvement_for_repeat),
                )
                if suppress_repeat:
                    suppressed += 1
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        strategy_name=signal.strategy_name,
                        status="suppressed",
                        rejection_reasons=["recent_alert_without_material_score_improvement"],
                        final_score=float(ranking["final_score"]),
                        measurements=filter_outcome.measurements,
                    )
                    service._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="suppressed",
                        final_score=float(ranking["final_score"]),
                        alert_eligible=False,
                        freshness=freshness,
                        filter_outcome=FilterOutcome(
                            passed=False,
                            pass_reasons=filter_outcome.pass_reasons,
                            rejection_reasons=["recent_alert_without_material_score_improvement"],
                            reason_codes=[*filter_outcome.pass_reasons, "recent_alert_without_material_score_improvement"],
                            measurements=filter_outcome.measurements,
                            watchlist_only=filter_outcome.watchlist_only,
                        ),
                        payload={
                            "backtest_snapshot": backtest_snapshot,
                            "measurements": filter_outcome.measurements,
                            "score_breakdown": ranking["score_breakdown"],
                        },
                    )
                    continue
                ranking = rank_live_signal(
                    settings=service.effective_settings,
                    signal=signal,
                    context=context,
                    backtest_snapshot=backtest_snapshot,
                    intelligence=intelligence,
                    watchlist_only=filter_outcome.watchlist_only,
                    freshness=freshness,
                )
                if ranking["actionability"] == "reject":
                    near_miss = _maybe_promote_paper_near_miss(
                        service,
                        signal=signal,
                        quote=quote,
                        timeframe=timeframe,
                        context=context,
                        intelligence=intelligence,
                        market_data_status=market_data_status,
                        filter_outcome=filter_outcome,
                        backtest_snapshot=backtest_snapshot,
                        ranking=ranking,
                        freshness=freshness,
                        reasons=["final_score_below_auto_threshold"],
                    )
                    if near_miss is not None:
                        service.signal_states.upsert(near_miss)
                        candidates.append(near_miss)
                        service._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="candidate",
                            final_score=near_miss.score,
                            alert_eligible=True,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=True,
                                pass_reasons=[*filter_outcome.pass_reasons, "paper_near_miss_promoted"],
                                rejection_reasons=["final_score_below_auto_threshold"],
                                reason_codes=[*filter_outcome.reason_codes, "paper_near_miss_promoted"],
                                measurements={**filter_outcome.measurements, **intelligence.measurements},
                                watchlist_only=False,
                            ),
                            payload=near_miss.model_dump(),
                        )
                        continue
                    suppressed += 1
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        strategy_name=signal.strategy_name,
                        status="rejected",
                        rejection_reasons=["final_score_below_keep_threshold"],
                        final_score=float(ranking["final_score"]),
                        measurements={**filter_outcome.measurements, **intelligence.measurements},
                    )
                    service._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="rejected",
                        final_score=float(ranking["final_score"]),
                        alert_eligible=False,
                        freshness=freshness,
                        filter_outcome=FilterOutcome(
                            passed=False,
                            pass_reasons=filter_outcome.pass_reasons,
                            rejection_reasons=["final_score_below_keep_threshold"],
                            reason_codes=[*filter_outcome.pass_reasons, "final_score_below_keep_threshold"],
                            measurements={**filter_outcome.measurements, **intelligence.measurements},
                            watchlist_only=filter_outcome.watchlist_only,
                        ),
                        payload={
                            "backtest_snapshot": backtest_snapshot,
                            "measurements": filter_outcome.measurements,
                            "score_breakdown": ranking["score_breakdown"],
                            "market_intelligence": intelligence.measurements,
                        },
                    )
                    continue

                snapshot = service._snapshot_from_signal(
                    signal,
                    quote=quote,
                    timeframe=timeframe,
                    context=context,
                    intelligence=intelligence,
                    market_data_status=market_data_status,
                    filter_outcome=filter_outcome,
                    backtest_snapshot=backtest_snapshot,
                    ranking=ranking,
                    freshness=freshness,
                )
                if not bool(snapshot.metadata.get("alert_eligible", False)):
                    promoted = _maybe_promote_paper_near_miss(
                        service,
                        signal=signal,
                        quote=quote,
                        timeframe=timeframe,
                        context=context,
                        intelligence=intelligence,
                        market_data_status=market_data_status,
                        filter_outcome=filter_outcome,
                        backtest_snapshot=backtest_snapshot,
                        ranking=ranking,
                        freshness=freshness,
                        reasons=["final_score_below_auto_threshold"],
                    )
                    if promoted is not None:
                        snapshot = promoted
                if validated_only and not bool(snapshot.metadata.get("backtest_validated")):
                    suppressed += 1
                    service._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=snapshot.symbol,
                        timeframe=timeframe,
                        strategy_name=snapshot.strategy_name,
                        status="suppressed",
                        rejection_reasons=["validated_only_filter"],
                        final_score=snapshot.score,
                        measurements=filter_outcome.measurements,
                    )
                    service._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="suppressed",
                        final_score=snapshot.score,
                        alert_eligible=False,
                        freshness=freshness,
                        filter_outcome=FilterOutcome(
                            passed=False,
                            pass_reasons=snapshot.pass_reasons,
                            rejection_reasons=["validated_only_filter"],
                            reason_codes=[*snapshot.pass_reasons, "validated_only_filter"],
                            measurements=filter_outcome.measurements,
                            watchlist_only=filter_outcome.watchlist_only,
                        ),
                        payload=snapshot.model_dump(),
                    )
                    continue

                service.signal_states.upsert(snapshot)
                candidates.append(snapshot)
                service._record_scan_decision(
                    scan_task=scan_task,
                    signal=signal,
                    timeframe=timeframe,
                    status="candidate" if bool(snapshot.metadata.get("alert_eligible")) else "watchlist",
                    final_score=snapshot.score,
                    alert_eligible=bool(snapshot.metadata.get("alert_eligible")),
                    freshness=freshness,
                    filter_outcome=filter_outcome,
                    payload=snapshot.model_dump(),
                )
            if abort_scan:
                break

    ranked = sorted(candidates, key=service._ranking_key, reverse=True)
    top_k = min(limit or service.settings.screener_top_k, len(ranked)) if ranked else 0
    top_candidates = [
        item.model_copy(update={"rank": index + 1})
        for index, item in enumerate(ranked[:top_k])
    ]
    expected_strategy_runs = 0
    for timeframe in scan_timeframes:
        expected_strategy_runs += len(_strategy_specs_for_timeframe(service, timeframe, requested_spec_keys)) * len(universe)
    skipped_strategy_runs = max(expected_strategy_runs - evaluated_strategy_runs, 0)
    deadline_exceeded = any("scan_deadline_exceeded" in error for error in errors)
    requested_spec_count = len(requested_spec_keys) if requested_spec_keys else sum(
        len(_strategy_specs_for_timeframe(service, timeframe, set())) for timeframe in scan_timeframes
    )
    evaluated_spec_count = len(evaluated_spec_keys)
    response = ScreenerRunResponse(
        generated_at=utc_now().isoformat(),
        universe_name=service.settings.market_universe_name,
        timeframes=scan_timeframes,
        evaluated_symbols=evaluated_symbols,
        evaluated_strategy_runs=evaluated_strategy_runs,
        candidates=top_candidates,
        suppressed=suppressed,
        alerts_sent=0,
        errors=errors,
        rejection_summary=dict(sorted(rejection_summary.items(), key=lambda item: (-item[1], item[0]))),
        closest_rejections=service._rank_closest_rejections(closest_rejections),
        coverage={
            "mode": str(getattr(service.settings, "screener_spec_coverage_mode", "default")),
            "timeframes": scan_timeframes,
            "specs_by_timeframe": specs_by_timeframe,
            "requested_spec_keys": sorted(requested_spec_keys),
            "specs_requested": requested_spec_count,
            "specs_evaluated": evaluated_spec_count,
            "specs_skipped": max(requested_spec_count - evaluated_spec_count, 0),
            "symbols_requested": len(universe),
            "symbols_evaluated": evaluated_symbols,
            "expected_strategy_runs": expected_strategy_runs,
            "evaluated_strategy_runs": evaluated_strategy_runs,
            "skipped_strategy_runs": skipped_strategy_runs,
            "timed_out_runs": timed_out_runs,
            "deadline_exceeded": deadline_exceeded,
            "candidates_found": len(candidates),
            "proposals_created": 0,
        },
    )
    if notify and service.notifier is not None and hasattr(service.notifier, "send_text"):
        sent = bool(service.notifier.send_text(service.notifier.format_screener_summary(response)))
        response.alerts_sent = 1 if sent else 0

    service.logs.log(
        "market_universe_scan_completed",
        {
            "scan_task": scan_task,
            "universe_name": service.settings.market_universe_name,
            "symbols_scanned": universe[:evaluated_symbols],
            "timeframes": scan_timeframes,
            "evaluated_strategy_runs": evaluated_strategy_runs,
            "symbols_passed": [item.symbol for item in top_candidates],
            "candidates": len(candidates),
            "suppressed": suppressed,
            "alerts_sent": response.alerts_sent,
            "errors": errors,
            "rejection_summary": response.rejection_summary,
            "closest_rejections": response.closest_rejections,
            "coverage": response.coverage,
        },
    )
    return response
