"""Universe scan execution for the market screener service."""

from __future__ import annotations

from typing import Any

from app.backtesting.strategy_selection import strategy_kwargs_for as _strategy_kwargs
from app.backtesting.strategy_selection import strategy_specs_for as _strategy_specs
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.models.screener import ScreenerRunResponse
from app.screener.accuracy import build_accuracy_profile
from app.screener.filters import FilterOutcome, build_market_context
from app.screener.scoring import build_backtest_snapshot, freshness_for_decision, rank_live_signal
from app.strategies import get_strategy
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
    cancel_event: Any | None = None,
) -> ScreenerRunResponse:
    universe = [symbol.upper() for symbol in (symbols or resolve_universe(service.settings))]
    scan_timeframes = [timeframe.lower() for timeframe in (timeframes or service.settings.screener_default_timeframes)]
    candidates: list[Any] = []
    errors: list[str] = []
    rejection_summary: dict[str, int] = {}
    closest_rejections: list[dict[str, Any]] = []
    suppressed = 0
    evaluated_strategy_runs = 0
    evaluated_symbols = 0
    abort_scan = False
    service.logs.log(
        "market_universe_scan_started",
        {
            "scan_task": scan_task,
            "universe_name": service.settings.market_universe_name,
            "symbols": universe,
            "timeframes": scan_timeframes,
            "validated_only": validated_only,
        },
    )

    for symbol in universe:
        if service._scan_cancelled(cancel_event):
            errors.append("scan_cancelled")
            break
        if abort_scan:
            break
        evaluated_symbols += 1
        for timeframe in scan_timeframes:
            if service._scan_cancelled(cancel_event):
                errors.append("scan_cancelled")
                abort_scan = True
                break
            try:
                history = service.market_data.get_history(
                    symbol,
                    timeframe=timeframe,
                    bars=service._bars_for_timeframe(timeframe),
                    force_refresh=force_refresh,
                )
                quote = service.market_data.get_quote(
                    symbol,
                    timeframe=timeframe,
                    force_refresh=force_refresh,
                )
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

            for spec in _strategy_specs(service.settings, timeframe=timeframe):
                if service._scan_cancelled(cancel_event):
                    errors.append("scan_cancelled")
                    abort_scan = True
                    break
                evaluated_strategy_runs += 1
                strategy = get_strategy(spec.name, **_strategy_kwargs(service.settings, spec))
                try:
                    signal = strategy.generate_signal(history.copy(), symbol)
                except Exception as exc:
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
                        service._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=symbol,
                            timeframe=timeframe,
                            strategy_name=spec.name,
                            status=str(strategy_diagnostics.get("status") or "no_signal"),
                            rejection_reasons=list(strategy_diagnostics.get("rejection_reasons") or ["no_strategy_signal"]),
                            final_score=strategy_diagnostics.get("score"),
                            measurements=dict(strategy_diagnostics.get("measurements") or {}),
                        )
                    else:
                        service._increment_rejection(rejection_summary, "no_strategy_signal")
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
                    settings=service.settings,
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
                intelligence = service.intelligence.analyze(
                    symbol=signal.symbol,
                    timeframe=timeframe,
                    history=history,
                    quote=quote,
                    signal=signal,
                    force_refresh=force_refresh,
                )
                market_data_status = service._market_data_status(history=history, quote=quote)
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
                    settings=service.settings,
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
                    settings=service.settings,
                    signal=signal,
                    context=context,
                    backtest_snapshot=backtest_snapshot,
                    intelligence=intelligence,
                    watchlist_only=filter_outcome.watchlist_only,
                    freshness=freshness,
                )
                if ranking["actionability"] == "reject":
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
        },
    )
    return response
