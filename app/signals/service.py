"""Live signal evaluation and market scanning."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from app.live_signal_schema import (
    LiveSignalSnapshot,
    MarketQuote,
    SignalScanResponse,
    SignalState,
    TelegramAlertResponse,
)
from app.models.signal import Signal
from app.runtime_settings import AppSettings
from app.telegram_notify import TelegramNotifier
from app.utils.time import utc_now

if TYPE_CHECKING:
    from app.broker.etoro_market_data import EtoroMarketDataClient
    from app.storage.repositories import BacktestRepository, RunLogRepository, SignalRepository, SignalStateRepository


class LiveSignalService:
    """Evaluate live market-data signals and scan ranked candidates."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        market_data_client: "EtoroMarketDataClient" | Any,
        signal_repository: "SignalRepository" | Any,
        signal_state_repository: "SignalStateRepository" | Any,
        run_log_repository: "RunLogRepository" | Any,
        backtest_repository: "BacktestRepository" | Any | None = None,
        telegram_notifier: TelegramNotifier | Any | None = None,
        ledger_service: Any | None = None,
    ):
        self.settings = settings
        self.market_data = market_data_client
        self.signals = signal_repository
        self.signal_states = signal_state_repository
        self.logs = run_log_repository
        self.backtests = backtest_repository
        self.notifier = telegram_notifier
        self.ledger_service = ledger_service
        from app.broker.instrument_resolver import InstrumentResolver

        self.resolver = InstrumentResolver(settings)

    def get_latest_signal(
        self,
        symbol: str,
        *,
        commit: bool = False,
        notify: bool = False,
    ) -> LiveSignalSnapshot:
        """Evaluate the latest daily signal for one symbol."""

        normalized = symbol.upper().strip()
        snapshot = self._evaluate_symbol(normalized)
        if commit or notify:
            self._commit_snapshot(snapshot, notify=notify)
        return snapshot

    def scan_market(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int | None = None,
        supported_only: bool = False,
        commit: bool = False,
        notify: bool = False,
    ) -> SignalScanResponse:
        """Scan a universe and rank the best current setups."""

        universe = [symbol.upper().strip() for symbol in (symbols or self.settings.signal_scan_universe)]
        errors: list[str] = []
        candidates: list[LiveSignalSnapshot] = []
        alerts_sent = 0

        for symbol in universe:
            try:
                snapshot = self._evaluate_symbol(symbol)
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue

            if supported_only and not snapshot.supported:
                continue
            candidates.append(snapshot)

            if commit or notify:
                if self._commit_snapshot(snapshot, notify=notify):
                    alerts_sent += 1

        ranked = sorted(candidates, key=self._ranking_key, reverse=True)
        max_items = max(1, min(limit or self.settings.signal_scan_limit, 100))
        response = SignalScanResponse(
            generated_at=utc_now().isoformat(),
            timeframe=self.settings.live_signal_interval,
            evaluated_count=len(candidates),
            limit=max_items,
            alerts_sent=alerts_sent,
            candidates=ranked[:max_items],
            errors=errors,
        )
        self.logs.log(
            "live_signal_scan",
            {
                "evaluated_count": response.evaluated_count,
                "limit": response.limit,
                "alerts_sent": response.alerts_sent,
                "supported_only": supported_only,
            },
        )
        return response

    def send_test_alert(self, message: str) -> TelegramAlertResponse:
        """Send a manual Telegram test message."""

        if not self.notifier or not hasattr(self.notifier, "send_text"):
            return TelegramAlertResponse(
                sent=False,
                detail="Telegram notifier is not configured on the app.",
            )

        sent = bool(self.notifier.send_text(message))
        if sent:
            self.logs.log(
                "telegram_test_sent",
                {
                    "message": message,
                },
            )
        return TelegramAlertResponse(
            sent=sent,
            detail="Telegram test message sent." if sent else "Telegram test message was not sent.",
            chat_id=self.settings.telegram_chat_id or None,
        )

    def send_signal_alert(self, symbol: str) -> TelegramAlertResponse:
        """Force-send the current live signal snapshot to Telegram."""

        return self.send_signal_alert_with_label(symbol, previous_state="manual")

    def send_signal_alert_with_label(
        self,
        symbol: str,
        *,
        previous_state: str,
    ) -> TelegramAlertResponse:
        """Force-send the current live signal snapshot to Telegram with a label."""

        snapshot = self.get_latest_signal(symbol, commit=True, notify=False)
        if self.settings.require_verified_market_data_for_alerts and not bool(snapshot.metadata.get("data_source_verified", False)):
            reason = str(snapshot.metadata.get("data_source_verification_reason") or "market_data_unverified")
            self.logs.log(
                "telegram_signal_suppressed_market_data_gate",
                {
                    "symbol": snapshot.symbol,
                    "strategy_name": snapshot.strategy_name,
                    "state": snapshot.state.value,
                    "label": previous_state,
                    "reason": reason,
                },
            )
            return TelegramAlertResponse(
                sent=False,
                detail=f"Telegram signal suppressed by market-data gate: {reason}",
                symbol=snapshot.symbol,
                chat_id=self.settings.telegram_chat_id or None,
            )
        validation = self._backtest_validation(snapshot)
        if self.settings.require_backtest_validation_for_alerts and not validation["passes"]:
            self.logs.log(
                "telegram_signal_suppressed_backtest_gate",
                {
                    "symbol": snapshot.symbol,
                    "strategy_name": snapshot.strategy_name,
                    "state": snapshot.state.value,
                    "label": previous_state,
                    "reason": validation["reason"],
                },
            )
            return TelegramAlertResponse(
                sent=False,
                detail=f"Telegram signal suppressed by backtest gate: {validation['reason']}",
                symbol=snapshot.symbol,
                chat_id=self.settings.telegram_chat_id or None,
            )
        if not self.notifier or not hasattr(self.notifier, "send_signal_change"):
            return TelegramAlertResponse(
                sent=False,
                detail="Telegram notifier is not configured on the app.",
                symbol=snapshot.symbol,
            )

        snapshot = self._snapshot_with_ledger_outcome(
            snapshot,
            alert_source=f"telegram_{previous_state}",
        )
        sent = bool(self.notifier.send_signal_change(snapshot, previous_state=previous_state))
        if sent:
            self.logs.log(
                "telegram_signal_sent",
                {
                    "symbol": snapshot.symbol,
                    "strategy_name": snapshot.strategy_name,
                    "state": snapshot.state.value,
                    "label": previous_state,
                },
            )
        return TelegramAlertResponse(
            sent=sent,
            detail="Telegram signal alert sent." if sent else "Telegram signal alert was not sent.",
            symbol=snapshot.symbol,
            chat_id=self.settings.telegram_chat_id or None,
        )

    def _evaluate_symbol(self, symbol: str) -> LiveSignalSnapshot:
        candles = self.market_data.get_daily_candles(
            symbol,
            candles_count=self.settings.live_signal_candles_count,
            interval=self.settings.live_signal_interval,
        )
        quote = self.market_data.get_rates([symbol]).get(symbol.upper())
        if quote is None:
            raise RuntimeError(f"No quote returned for {symbol.upper()}")

        if symbol.upper() == "GOLD":
            snapshot = self._evaluate_gold(symbol.upper(), candles, quote)
        else:
            snapshot = self._evaluate_equity(symbol.upper(), candles, quote)
        return self._attach_backtest_context(snapshot)

    def _evaluate_equity(
        self,
        symbol: str,
        candles: pd.DataFrame,
        quote: MarketQuote,
    ) -> LiveSignalSnapshot:
        from app.strategies.pullback_trend import PullbackTrendStrategy

        strategy = PullbackTrendStrategy(
            trend_window=self.settings.live_signal_trend_window,
            pullback_window=self.settings.live_signal_pullback_window,
        )
        signal = strategy.generate_signal(candles.copy(), symbol)
        frame = candles.copy()
        frame["trend_ma"] = frame["close"].rolling(self.settings.live_signal_trend_window).mean()
        frame["pullback_ma"] = frame["close"].rolling(self.settings.live_signal_pullback_window).mean()
        frame["ema_short"] = frame["close"].ewm(span=8, adjust=False).mean()
        frame["ema_long"] = frame["close"].ewm(span=21, adjust=False).mean()
        frame["momentum_20"] = frame["close"].pct_change(20)
        frame["recent_low_10"] = frame["low"].rolling(10).min()

        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        trend_up = (
            last["close"] > last["trend_ma"]
            and last["ema_short"] > last["ema_long"]
            and last["trend_ma"] > frame["trend_ma"].iloc[-5]
        )
        pullback_active = prev["close"] <= prev["pullback_ma"] * 1.01
        resuming_higher = last["close"] > last["pullback_ma"] and last["close"] > prev["close"]
        trade_supported, support_note = self._trade_support(symbol)

        current_price = quote.last_execution or quote.ask or quote.bid or float(last["close"])
        entry_watch = float(last["pullback_ma"]) if pd.notna(last["pullback_ma"]) else current_price
        stop_loss = float(min(last["recent_low_10"], last["trend_ma"] * 0.98))
        entry_price = quote.ask or current_price
        if signal is not None:
            stop_loss = float(signal.stop_loss or stop_loss)
            entry_price = float(signal.price or entry_price)
        risk_per_share = max(entry_price - stop_loss, entry_price * 0.02, 0.01)
        take_profit = float((entry_price if signal is not None else entry_watch) + (risk_per_share * 2.0))
        state = SignalState(signal.action.value) if signal is not None else SignalState.NONE
        if signal is not None:
            rationale = signal.rationale
            confidence = signal.confidence
        elif trend_up and not pullback_active:
            rationale = "Trend is positive but price is extended above the pullback average; wait for a cleaner retracement."
            confidence = 0.45
        elif trend_up and pullback_active and not resuming_higher:
            rationale = "Trend is positive and a pullback is active, but the rebound candle has not confirmed yet."
            confidence = 0.5
        elif not trend_up:
            rationale = "Trend filter is not aligned for a long entry on the latest closed daily bar."
            confidence = 0.3
        else:
            rationale = "No fresh signal on the latest closed daily bar."
            confidence = 0.35

        score = self._score_equity_setup(
            state=state,
            last_close=float(last["close"]),
            trend_ma=float(last["trend_ma"]),
            pullback_ma=float(last["pullback_ma"]),
            ema_short=float(last["ema_short"]),
            ema_long=float(last["ema_long"]),
            momentum_20=float(last["momentum_20"] or 0.0),
        )
        indicator_payload = {
            "trend_ma": round(float(last["trend_ma"]), 4),
            "pullback_ma": round(float(last["pullback_ma"]), 4),
            "ema_short": round(float(last["ema_short"]), 4),
            "ema_long": round(float(last["ema_long"]), 4),
            "momentum_20_pct": round(float(last["momentum_20"]) * 100.0, 4) if pd.notna(last["momentum_20"]) else 0.0,
            "trend_up": bool(trend_up),
            "pullback_active": bool(pullback_active),
            "resuming_higher": bool(resuming_higher),
        }
        return LiveSignalSnapshot(
            symbol=symbol,
            strategy_name=f"pullback_trend_{self.settings.live_signal_trend_window}_{self.settings.live_signal_pullback_window}",
            timeframe=self.settings.live_signal_interval,
            state=state,
            generated_at=utc_now().isoformat(),
            candle_timestamp=last["timestamp"].isoformat(),
            rate_timestamp=quote.timestamp,
            current_bid=quote.bid,
            current_ask=quote.ask,
            current_price=current_price,
            entry_price=entry_price if state == SignalState.BUY else entry_watch,
            exit_price=float(last["trend_ma"]) if state != SignalState.SELL else (quote.bid or current_price),
            stop_loss=stop_loss if state != SignalState.SELL else None,
            take_profit=take_profit if state != SignalState.SELL else None,
            confidence=confidence,
            score=score,
            tradable=trade_supported,
            supported=trade_supported,
            asset_class="equity",
            rationale=rationale,
            indicators=indicator_payload,
            metadata={
                "data_source": "eToro",
                "data_source_verified": True,
                "support_note": support_note,
                **indicator_payload,
            },
        )

    def _evaluate_gold(
        self,
        symbol: str,
        candles: pd.DataFrame,
        quote: MarketQuote,
    ) -> LiveSignalSnapshot:
        from app.strategies.gold_momentum import GoldMomentumStrategy

        strategy = GoldMomentumStrategy()
        signal = strategy.generate_signal(candles.copy(), symbol)
        frame = candles.copy()
        frame["trend_ma"] = frame["close"].rolling(20).mean()
        frame["breakout_high"] = frame["high"].rolling(15).max().shift(1)
        frame["mom_5"] = frame["close"].pct_change(5)
        last = frame.iloc[-1]
        current_price = quote.last_execution or quote.ask or quote.bid or float(last["close"])
        state = SignalState(signal.action.value) if signal is not None else SignalState.NONE
        rationale = signal.rationale if signal is not None else "No fresh gold momentum signal on the latest closed daily bar."
        confidence = signal.confidence if signal is not None else 0.35
        entry_price = float(signal.price or current_price) if signal is not None else float(last["breakout_high"])
        stop_loss = float(signal.stop_loss or last["trend_ma"] * 0.985) if signal is not None else float(last["trend_ma"] * 0.985)
        risk_per_share = max(entry_price - stop_loss, entry_price * 0.015, 0.01)
        take_profit = float(signal.take_profit or (entry_price + risk_per_share * 2.0)) if signal is not None else float(entry_price + risk_per_share * 2.0)
        trade_supported, support_note = self._trade_support(symbol)
        score = 100.0 if state == SignalState.BUY else 20.0
        indicator_payload = {
            "trend_ma": round(float(last["trend_ma"]), 4),
            "breakout_high": round(float(last["breakout_high"]), 4) if pd.notna(last["breakout_high"]) else None,
            "momentum_5_pct": round(float(last["mom_5"]) * 100.0, 4) if pd.notna(last["mom_5"]) else 0.0,
        }
        return LiveSignalSnapshot(
            symbol=symbol,
            strategy_name="gold_momentum_live",
            timeframe=self.settings.live_signal_interval,
            state=state,
            generated_at=utc_now().isoformat(),
            candle_timestamp=last["timestamp"].isoformat(),
            rate_timestamp=quote.timestamp,
            current_bid=quote.bid,
            current_ask=quote.ask,
            current_price=current_price,
            entry_price=entry_price,
            exit_price=float(last["trend_ma"]),
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            score=score,
            tradable=trade_supported,
            supported=trade_supported,
            asset_class="commodity",
            rationale=rationale,
            indicators=indicator_payload,
            metadata={
                "data_source": "eToro",
                "data_source_verified": True,
                "support_note": support_note,
                **indicator_payload,
            },
        )

    def _commit_snapshot(self, snapshot: LiveSignalSnapshot, *, notify: bool) -> bool:
        previous = self.signal_states.get(
            snapshot.symbol,
            snapshot.strategy_name,
            snapshot.timeframe,
        )
        changed = previous is None or previous.state != snapshot.state
        self.signal_states.upsert(snapshot)

        if changed and snapshot.state != SignalState.NONE:
            self.signals.create(self._signal_from_snapshot(snapshot))

        if changed and notify:
            if snapshot.state != SignalState.NONE or self.settings.notify_on_none_signal_change:
                if self.settings.require_verified_market_data_for_alerts and not bool(snapshot.metadata.get("data_source_verified", False)):
                    self.logs.log(
                        "signal_notification_suppressed_market_data_gate",
                        {
                            "symbol": snapshot.symbol,
                            "strategy_name": snapshot.strategy_name,
                            "state": snapshot.state.value,
                            "reason": snapshot.metadata.get("data_source_verification_reason", "market_data_unverified"),
                        },
                    )
                    return False
                validation = self._backtest_validation(snapshot)
                if self.settings.require_backtest_validation_for_alerts and not validation["passes"]:
                    self.logs.log(
                        "signal_notification_suppressed_backtest_gate",
                        {
                            "symbol": snapshot.symbol,
                            "strategy_name": snapshot.strategy_name,
                            "state": snapshot.state.value,
                            "reason": validation["reason"],
                        },
                    )
                    return False
                if self.notifier and hasattr(self.notifier, "send_signal_change"):
                    snapshot_to_send = self._snapshot_with_ledger_outcome(
                        snapshot,
                        alert_source="signal_notification",
                    )
                    sent = bool(
                        self.notifier.send_signal_change(
                            snapshot_to_send,
                            previous_state=previous.state.value if previous else None,
                        )
                    )
                    if sent:
                        self.logs.log(
                            "signal_notification_sent",
                            {
                                "symbol": snapshot.symbol,
                                "strategy_name": snapshot.strategy_name,
                                "state": snapshot.state.value,
                            },
                        )
                    return sent
        return False

    def _snapshot_with_ledger_outcome(self, snapshot: LiveSignalSnapshot, *, alert_source: str) -> LiveSignalSnapshot:
        if self.ledger_service is None:
            return snapshot
        if not bool(getattr(self.settings, "ledger_enabled", False)):
            return snapshot
        if not bool(getattr(self.settings, "ledger_record_alerts_enabled", False)):
            return snapshot
        try:
            generated_at = snapshot.generated_at or snapshot.signal_generated_at or utc_now().isoformat()
            target = snapshot.take_profit or (snapshot.targets[0] if snapshot.targets else None)
            alert_id = (
                f"{alert_source}:{snapshot.symbol}:{snapshot.strategy_name}:"
                f"{snapshot.timeframe}:{generated_at}"
            )
            payload = snapshot.model_dump()
            payload.update(
                {
                    "alert_source": alert_source,
                    "direction": str(snapshot.direction_label or snapshot.state.value),
                    "timestamp_utc": generated_at,
                    "score": snapshot.score,
                    "target": target,
                    "stop": snapshot.stop_loss,
                    "confluence_vector": {
                        "score_breakdown": dict(snapshot.score_breakdown or {}),
                        "indicators": dict(snapshot.indicators or {}),
                        "pass_reasons": list(snapshot.pass_reasons or []),
                        "reject_reasons": list(snapshot.reject_reasons or []),
                        "metadata": dict(snapshot.metadata or {}),
                    },
                }
            )
            outcome_id = self.ledger_service.record_alert(
                alert_source=alert_source,
                alert_id=alert_id,
                symbol=snapshot.symbol,
                strategy_name=snapshot.strategy_name,
                timeframe=snapshot.timeframe,
                alert_created_at=generated_at,
                alert_entry_price=snapshot.entry_price or snapshot.current_price,
                alert_stop=snapshot.stop_loss,
                alert_target=target,
                alert_score=snapshot.score,
                alert_payload=payload,
            )
            metadata = dict(snapshot.metadata or {})
            metadata.update({"ledger_outcome_id": outcome_id, "ledger_alert_id": alert_id})
            self.logs.log(
                "ledger_alert_recorded",
                {
                    "outcome_id": outcome_id,
                    "alert_source": alert_source,
                    "symbol": snapshot.symbol,
                    "strategy_name": snapshot.strategy_name,
                    "timeframe": snapshot.timeframe,
                    "alert_id": alert_id,
                },
            )
            return snapshot.model_copy(update={"metadata": metadata})
        except Exception as exc:  # noqa: BLE001
            self.logs.log(
                "ledger_alert_record_error",
                {
                    "alert_source": alert_source,
                    "symbol": snapshot.symbol,
                    "error": str(exc),
                },
            )
            return snapshot

    def _signal_from_snapshot(self, snapshot: LiveSignalSnapshot) -> Signal:
        return Signal(
            symbol=snapshot.symbol,
            strategy_name=snapshot.strategy_name,
            action=snapshot.state.value,
            rationale=snapshot.rationale,
            confidence=snapshot.confidence,
            price=snapshot.entry_price or snapshot.current_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            metadata=snapshot.metadata,
        )

    def _attach_backtest_context(self, snapshot: LiveSignalSnapshot) -> LiveSignalSnapshot:
        metadata = dict(snapshot.metadata)
        metadata.setdefault("data_source", "eToro")
        metadata.setdefault("data_source_verified", True)

        validation = self._backtest_validation(snapshot)
        metadata.update(
            {
                "backtest_validated": validation["passes"],
                "backtest_validation_reason": validation["reason"],
            }
        )
        summary = validation.get("summary")
        if summary:
            metrics = summary.get("metrics", {})
            metadata.update(
                {
                    "backtest_strategy_name": summary.get("strategy_name"),
                    "backtest_completed_at": summary.get("completed_at"),
                    "backtest_number_of_trades": metrics.get("number_of_trades"),
                    "backtest_profit_factor": metrics.get("profit_factor"),
                    "backtest_annualized_return_pct": metrics.get("annualized_return_pct"),
                    "backtest_max_drawdown_pct": metrics.get("max_drawdown_pct"),
                    "backtest_win_rate": metrics.get("win_rate"),
                }
            )
        return snapshot.model_copy(update={"metadata": metadata, "indicators": dict(snapshot.indicators or metadata)})

    def _backtest_validation(self, snapshot: LiveSignalSnapshot) -> dict[str, Any]:
        if self.backtests is None:
            return {"passes": False, "reason": "no_backtest_repository", "summary": None}

        summary = None
        for strategy_name in self._backtest_strategy_candidates(snapshot.strategy_name):
            summary = self.backtests.get_latest_summary(snapshot.symbol, strategy_name)
            if summary is not None:
                break
        if summary is None:
            return {"passes": False, "reason": "no_backtest_summary", "summary": None}

        metrics = summary.get("metrics", {})
        trade_count = int(metrics.get("number_of_trades", 0) or 0)
        profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
        annualized_return = float(metrics.get("annualized_return_pct", 0.0) or 0.0)
        max_drawdown = float(metrics.get("max_drawdown_pct", 9999.0) or 9999.0)

        failures: list[str] = []
        if trade_count < self.settings.min_backtest_trades_for_alerts:
            failures.append("too_few_trades")
        if profit_factor < self.settings.min_backtest_profit_factor:
            failures.append("profit_factor_below_threshold")
        if annualized_return < self.settings.min_backtest_annualized_return_pct:
            failures.append("annualized_return_below_threshold")
        if max_drawdown > self.settings.max_backtest_drawdown_pct:
            failures.append("drawdown_above_threshold")

        return {
            "passes": not failures,
            "reason": ",".join(failures) if failures else "passed",
            "summary": summary,
        }

    @staticmethod
    def _backtest_strategy_candidates(strategy_name: str) -> list[str]:
        candidates = [strategy_name]
        if strategy_name.startswith("pullback_trend_"):
            candidates.append("pullback_trend")
        if strategy_name.startswith("gold_momentum"):
            candidates.append("gold_momentum")
        if strategy_name.startswith("ma_crossover_"):
            candidates.append("ma_crossover")
        return candidates

    def _trade_support(self, symbol: str) -> tuple[bool, str | None]:
        try:
            self.resolver.resolve(symbol)
            return True, None
        except ValueError as exc:
            return False, str(exc)

    @staticmethod
    def _ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, float]:
        state_rank = {
            SignalState.BUY: 3,
            SignalState.NONE: 2,
            SignalState.SELL: 1,
        }[snapshot.state]
        return state_rank, snapshot.score

    @staticmethod
    def _score_equity_setup(
        *,
        state: SignalState,
        last_close: float,
        trend_ma: float,
        pullback_ma: float,
        ema_short: float,
        ema_long: float,
        momentum_20: float,
    ) -> float:
        state_bonus = {
            SignalState.BUY: 100.0,
            SignalState.NONE: 45.0,
            SignalState.SELL: 0.0,
        }[state]
        trend_strength = max((last_close / max(trend_ma, 0.01) - 1.0) * 100.0, -20.0)
        proximity = max(0.0, 15.0 - abs(last_close / max(pullback_ma, 0.01) - 1.0) * 1000.0)
        ema_gap = max((ema_short / max(ema_long, 0.01) - 1.0) * 200.0, -10.0)
        momentum_score = max(min(momentum_20 * 100.0, 20.0), -20.0)
        return round(state_bonus + trend_strength * 2.5 + proximity + ema_gap + momentum_score, 2)
