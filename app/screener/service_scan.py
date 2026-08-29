"""Universe scan execution for the market screener service."""

from __future__ import annotations

import time
from typing import Any

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.models.screener import ScreenerRunResponse
from app.screener.accuracy import build_accuracy_profile
from app.screener.filters import FilterOutcome, build_market_context
from app.screener.profiles import effective_auto_execution_min_score
from app.screener.scoring import build_backtest_snapshot, freshness_for_decision, rank_live_signal
from app.screener.scan_support import (
    ScanTimeoutError,
    _bounded_call,
    _normalize_spec_keys,
    _spec_key,
    _strategy_specs_for_timeframe,
)
from app.screener.scan_promotion import (
    _diagnostic_intelligence,
    _diagnostic_weak_valid_signal,
    _maybe_promote_paper_near_miss,
    _maybe_promote_supervised_weak_valid,
    _weak_valid_daily_count,
)
from app.universe import resolve_universe
from app.utils.time import utc_now


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
    weak_valid_scan_promotions = 0
    weak_valid_daily_promotions = _weak_valid_daily_count(service)
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
                        diagnostic_signal = _diagnostic_weak_valid_signal(
                            symbol=symbol,
                            strategy_name=spec.name,
                            timeframe=timeframe,
                            diagnostics=strategy_diagnostics,
                            quote=quote,
                            context=None,
                            rejection_reasons=rejection_reasons,
                        )
                        if diagnostic_signal is not None:
                            diagnostic_signal.metadata.setdefault("timeframe", timeframe)
                            diagnostic_signal.metadata.setdefault("strategy_style", getattr(spec, "style", "diagnostic"))
                            backtest = service._backtest_validation(
                                diagnostic_signal.symbol,
                                diagnostic_signal.strategy_name,
                                timeframe,
                            )
                            backtest_snapshot = build_backtest_snapshot(
                                backtest["summary"],
                                validated=backtest["passes"],
                                validation_reason=backtest["reason"],
                            )
                            diagnostic_context = build_market_context(history, quote=quote, signal=diagnostic_signal)
                            diagnostic_intelligence = _diagnostic_intelligence(diagnostic_context, strategy_diagnostics)
                            final_score = float(strategy_diagnostics.get("score") or 0.0)
                            weak_valid_reasons = list(rejection_reasons)
                            if final_score < effective_auto_execution_min_score(service.settings):
                                weak_valid_reasons.append("final_score_below_auto_threshold")
                            filter_outcome = FilterOutcome(
                                passed=False,
                                pass_reasons=["market_data_verified"] if market_data_status["verified"] else [],
                                rejection_reasons=list(rejection_reasons),
                                reason_codes=[
                                    *list(strategy_diagnostics.get("reason_codes") or rejection_reasons),
                                    *([] if market_data_status["verified"] else ["market_data_unverified"]),
                                ],
                                measurements=measurements,
                                watchlist_only=True,
                            )
                            ranking = {
                                "final_score": final_score,
                                "score_breakdown": {},
                                "confidence_label": "weak",
                                "direction_label": "watchlist",
                                "actionability": "watchlist",
                            }
                            diagnostic_weak_valid = _maybe_promote_supervised_weak_valid(
                                service,
                                signal=diagnostic_signal,
                                quote=quote,
                                timeframe=timeframe,
                                context=diagnostic_context,
                                intelligence=diagnostic_intelligence,
                                market_data_status=market_data_status,
                                filter_outcome=filter_outcome,
                                backtest_snapshot=backtest_snapshot,
                                ranking=ranking,
                                freshness="fresh",
                                reasons=weak_valid_reasons,
                                weak_valid_scan_count=weak_valid_scan_promotions,
                                weak_valid_daily_count=weak_valid_daily_promotions,
                            )
                            if diagnostic_weak_valid is not None:
                                weak_valid_scan_promotions += 1
                                service.signal_states.upsert(diagnostic_weak_valid)
                                candidates.append(diagnostic_weak_valid)
                                service._record_scan_decision(
                                    scan_task=scan_task,
                                    signal=diagnostic_signal,
                                    timeframe=timeframe,
                                    status="candidate",
                                    final_score=diagnostic_weak_valid.score,
                                    alert_eligible=True,
                                    freshness="fresh",
                                    filter_outcome=FilterOutcome(
                                        passed=True,
                                        pass_reasons=["diagnostic_supervised_weak_valid_promoted"],
                                        rejection_reasons=weak_valid_reasons,
                                        reason_codes=[
                                            *list(strategy_diagnostics.get("reason_codes") or rejection_reasons),
                                            "diagnostic_supervised_weak_valid_promoted",
                                        ],
                                        measurements=measurements,
                                        watchlist_only=False,
                                    ),
                                    payload=diagnostic_weak_valid.model_dump(),
                                )
                                continue
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
                    weak_valid = None if suppress_repeat else _maybe_promote_supervised_weak_valid(
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
                        weak_valid_scan_count=weak_valid_scan_promotions,
                        weak_valid_daily_count=weak_valid_daily_promotions,
                    )
                    if weak_valid is not None:
                        weak_valid_scan_promotions += 1
                        service.signal_states.upsert(weak_valid)
                        candidates.append(weak_valid)
                        service._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=weak_valid.symbol,
                            timeframe=timeframe,
                            strategy_name=weak_valid.strategy_name,
                            status="supervised_weak_valid",
                            rejection_reasons=near_miss_reasons,
                            final_score=weak_valid.score,
                            measurements=filter_outcome.measurements,
                        )
                        service._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="candidate",
                            final_score=weak_valid.score,
                            alert_eligible=True,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=True,
                                pass_reasons=[*filter_outcome.pass_reasons, "supervised_weak_valid_promoted"],
                                rejection_reasons=near_miss_reasons,
                                reason_codes=[*filter_outcome.reason_codes, "supervised_weak_valid_promoted"],
                                measurements=filter_outcome.measurements,
                                watchlist_only=False,
                            ),
                            payload=weak_valid.model_dump(),
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
                            "entry_price": getattr(signal, "price", None),
                            "current_price": getattr(context, "current_price", None),
                            "stop_loss": getattr(signal, "stop_loss", None),
                            "take_profit": getattr(signal, "take_profit", None),
                            "risk_reward_ratio": service._compute_risk_reward(signal),
                            "direction_label": "buy",
                            "metadata": dict(getattr(signal, "metadata", {}) or {}),
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
                    weak_valid_reasons = ["final_score_below_auto_threshold"]
                    weak_valid = _maybe_promote_supervised_weak_valid(
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
                        reasons=weak_valid_reasons,
                        weak_valid_scan_count=weak_valid_scan_promotions,
                        weak_valid_daily_count=weak_valid_daily_promotions,
                    )
                    if weak_valid is not None:
                        weak_valid_scan_promotions += 1
                        service.signal_states.upsert(weak_valid)
                        candidates.append(weak_valid)
                        service._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="candidate",
                            final_score=weak_valid.score,
                            alert_eligible=True,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=True,
                                pass_reasons=[*filter_outcome.pass_reasons, "supervised_weak_valid_promoted"],
                                rejection_reasons=weak_valid_reasons,
                                reason_codes=[*filter_outcome.reason_codes, "supervised_weak_valid_promoted"],
                                measurements={**filter_outcome.measurements, **intelligence.measurements},
                                watchlist_only=False,
                            ),
                            payload=weak_valid.model_dump(),
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
                            "entry_price": getattr(signal, "price", None),
                            "current_price": getattr(context, "current_price", None),
                            "stop_loss": getattr(signal, "stop_loss", None),
                            "take_profit": getattr(signal, "take_profit", None),
                            "risk_reward_ratio": service._compute_risk_reward(signal),
                            "direction_label": "buy",
                            "metadata": dict(getattr(signal, "metadata", {}) or {}),
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
                    else:
                        weak_valid = _maybe_promote_supervised_weak_valid(
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
                            weak_valid_scan_count=weak_valid_scan_promotions,
                            weak_valid_daily_count=weak_valid_daily_promotions,
                        )
                        if weak_valid is not None:
                            weak_valid_scan_promotions += 1
                            snapshot = weak_valid
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
    # Cross-sectional momentum overlay: concentrate in the universe's leaders by
    # dropping all but the top-momentum slice before truncation. No-op unless
    # cross_sectional_momentum_enabled.
    if bool(getattr(service.settings, "cross_sectional_momentum_enabled", False)) and ranked:
        from app.screener.cross_sectional_momentum import filter_top_momentum

        ranked = filter_top_momentum(
            ranked, top_pct=float(getattr(service.settings, "cross_sectional_momentum_top_pct", 30.0) or 30.0)
        )
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
