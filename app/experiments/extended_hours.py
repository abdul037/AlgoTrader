"""Supervised extended-hours paper trading experiment."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from app.utils.ids import generate_id
from app.utils.time import utc_now


class ExtendedHoursExperimentService:
    """Limit-order-only extended-hours experiment isolated from normal execution."""

    TERMINAL_STATUSES = {"dry_run", "canceled", "closed", "failed", "blocked", "expired"}

    def __init__(
        self,
        *,
        settings: Any,
        alpaca_client: Any | None,
        etoro_demo_client: Any | None,
        repository: Any,
        safety_state: Any,
        automation: Any,
        run_logs: Any,
    ):
        self.settings = settings
        self.alpaca = alpaca_client
        self.etoro = etoro_demo_client
        self.repository = repository
        self.safety = safety_state
        self.automation = automation
        self.logs = run_logs

    def status(self) -> dict[str, Any]:
        expired_cancelled = self.cancel_expired_orders()
        return {
            "enabled": bool(getattr(self.settings, "extended_hours_experiment_enabled", False)),
            "submit_enabled": bool(
                getattr(self.settings, "extended_hours_experiment_submit_enabled", False)
            ),
            "etoro_probe_enabled": bool(
                getattr(self.settings, "extended_hours_etoro_probe_enabled", False)
            ),
            "mode": "supervised",
            "primary_broker": "alpaca",
            "regular_hours_isolated": True,
            "whitelist": self.whitelist(),
            "max_notional_usd": float(
                getattr(self.settings, "extended_hours_max_notional_usd", 100.0)
            ),
            "max_qty": float(getattr(self.settings, "extended_hours_max_qty", 1.0)),
            "max_open_orders": int(
                getattr(self.settings, "extended_hours_max_open_orders", 1)
            ),
            "open_order_count": self.repository.open_order_count(),
            "expired_orders_cancelled": len(expired_cancelled),
            "alpaca_configured": self.alpaca is not None,
            "etoro_configured": self.etoro is not None,
        }

    def whitelist(self) -> list[str]:
        return [
            str(symbol).upper().strip()
            for symbol in list(getattr(self.settings, "extended_hours_whitelist", []) or [])
            if str(symbol).strip()
        ]

    def list_orders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.cancel_expired_orders()
        return self.repository.list_orders(limit=limit)

    def list_etoro_probes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_etoro_probes(limit=limit)

    def probe_alpaca(
        self,
        *,
        symbol: str,
        side: str = "buy",
        limit_price: float | None = None,
        client_order_id: str | None = None,
        operator: str = "api",
    ) -> dict[str, Any]:
        normalized_symbol = symbol.upper().strip()
        normalized_side = side.lower().strip()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("Extended-hours experiment side must be buy or sell")
        if client_order_id:
            existing = self.repository.get_by_client_order_id(client_order_id)
            if existing is not None:
                return existing
        self.cancel_expired_orders()
        quote = self._validate_alpaca_entry_gates(normalized_symbol)
        computed = self._order_plan(
            symbol=normalized_symbol,
            side=normalized_side,
            quote=quote,
            limit_price=limit_price,
        )
        now = utc_now()
        client_id = client_order_id or f"ext-hours:{generate_id(normalized_symbol.lower())}"
        status = "dry_run"
        broker_order_id = None
        submitted_at = None
        response_payload: dict[str, Any] = {}
        if bool(getattr(self.settings, "extended_hours_experiment_submit_enabled", False)):
            broker_execution = self.alpaca.submit_order(
                symbol=normalized_symbol,
                side=normalized_side,
                qty=computed["qty"],
                order_type="limit",
                limit_price=computed["limit_price"],
                time_in_force="day",
                client_order_id=client_id,
                extended_hours=True,
            )
            broker_order_id = getattr(broker_execution, "broker_order_id", None)
            response_payload = (
                broker_execution.model_dump()
                if hasattr(broker_execution, "model_dump")
                else dict(getattr(broker_execution, "response_payload", {}) or {})
            )
            status = str(getattr(broker_execution, "status", None) or "submitted")
            submitted_at = now.isoformat()
        record = self.repository.create_order(
            {
                "id": generate_id("extord"),
                "broker": "alpaca",
                "symbol": normalized_symbol,
                "side": normalized_side,
                "qty": computed["qty"],
                "limit_price": computed["limit_price"],
                "notional_usd": computed["notional_usd"],
                "status": status,
                "client_order_id": client_id,
                "broker_order_id": broker_order_id,
                "quote": computed["quote"],
                "spread_bps": computed["spread_bps"],
                "quote_age_seconds": computed["quote_age_seconds"],
                "operator": operator,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "submitted_at": submitted_at,
                "expires_at": (
                    now
                    + timedelta(
                        seconds=int(
                            getattr(self.settings, "extended_hours_order_ttl_seconds", 300)
                        )
                    )
                ).isoformat()
                if broker_order_id
                else None,
            }
        )
        self.logs.log(
            "extended_hours_alpaca_probe",
            {
                "order_id": record["id"],
                "symbol": normalized_symbol,
                "status": record["status"],
                "broker_order_id": broker_order_id,
                "response": response_payload,
            },
        )
        return record

    def cancel_order(self, order_id: str, *, reason: str = "manual_cancel") -> dict[str, Any]:
        record = self.repository.get_order(order_id)
        if record is None:
            raise LookupError(order_id)
        broker_order_id = record.get("broker_order_id")
        if broker_order_id and self.alpaca is not None:
            self.alpaca.cancel_order(str(broker_order_id))
        return self.repository.update_order(
            order_id,
            status="canceled",
            canceled_at=utc_now().isoformat(),
            failure_reason=reason,
        )

    def cancel_expired_orders(self) -> list[dict[str, Any]]:
        expired = self.repository.list_expired_open_orders(now_iso=utc_now().isoformat())
        canceled: list[dict[str, Any]] = []
        for record in expired:
            try:
                canceled.append(self.cancel_order(record["id"], reason="ttl_expired"))
            except Exception as exc:
                self.repository.update_order(record["id"], failure_reason=f"ttl_cancel_failed:{exc}")
        return canceled

    def submit_exit(
        self,
        order_id: str,
        *,
        limit_price: float | None = None,
        client_order_id: str | None = None,
        operator: str = "api",
    ) -> dict[str, Any]:
        del operator
        if self.alpaca is None:
            raise RuntimeError("Alpaca client is not configured")
        record = self.repository.get_order(order_id)
        if record is None:
            raise LookupError(order_id)
        symbol = str(record["symbol"]).upper()
        position = self._alpaca_position(symbol)
        if position is None or abs(float(getattr(position, "quantity", 0.0))) <= 0:
            raise ValueError(f"No Alpaca position is available to exit for {symbol}")
        quote = self._quote(symbol)
        computed = self._order_plan(
            symbol=symbol,
            side="sell",
            quote=quote,
            limit_price=limit_price,
            max_qty=abs(float(getattr(position, "quantity", 0.0))),
        )
        client_id = client_order_id or f"ext-hours-exit:{generate_id(symbol.lower())}"
        broker_execution = self.alpaca.submit_order(
            symbol=symbol,
            side="sell",
            qty=computed["qty"],
            order_type="limit",
            limit_price=computed["limit_price"],
            time_in_force="day",
            client_order_id=client_id,
            extended_hours=True,
        )
        response_payload = (
            broker_execution.model_dump()
            if hasattr(broker_execution, "model_dump")
            else dict(getattr(broker_execution, "response_payload", {}) or {})
        )
        entry_price = float(record.get("fill_price") or record.get("limit_price") or 0.0)
        exit_price = _optional_float(
            response_payload.get("response_payload", {}).get("filled_avg_price")
        ) or _optional_float(response_payload.get("filled_avg_price"))
        realized_pnl = None
        if exit_price is not None and entry_price > 0:
            realized_pnl = (exit_price - entry_price) * computed["qty"]
        return self.repository.update_order(
            order_id,
            status="exit_submitted",
            exit_client_order_id=client_id,
            exit_broker_order_id=getattr(broker_execution, "broker_order_id", None),
            exit_limit_price=computed["limit_price"],
            exit_fill_price=exit_price,
            realized_pnl_usd=realized_pnl,
        )

    def run_etoro_capability_probe(self) -> dict[str, Any]:
        if not bool(getattr(self.settings, "extended_hours_etoro_probe_enabled", False)):
            raise PermissionError("EXTENDED_HOURS_ETORO_PROBE_ENABLED must be true")
        probe_id = generate_id("extetoro")
        evidence: dict[str, Any] = {
            "whitelist": self.whitelist(),
            "submitted_order": False,
            "outside_regular_hours_acceptance": "not_tested",
        }
        try:
            if self.etoro is None:
                return self.repository.record_etoro_probe(
                    probe_id=probe_id,
                    status="blocked",
                    classification="not_supported",
                    account_verified=False,
                    evidence={**evidence, "reason": "etoro_demo_client_not_configured"},
                )
            identity = (
                self.etoro.get_account_identity()
                if hasattr(self.etoro, "get_account_identity")
                else {}
            )
            evidence["identity"] = identity
            account_verified = bool(identity.get("verified"))
            expected = str(
                identity.get("expected_account_id")
                or getattr(self.settings, "etoro_demo_expected_account_id", "")
                or ""
            ).strip()
            if expected and not account_verified:
                return self.repository.record_etoro_probe(
                    probe_id=probe_id,
                    status="blocked",
                    classification="account_mismatch",
                    account_verified=False,
                    evidence=evidence,
                )
            capabilities = (
                self.etoro.get_capabilities() if hasattr(self.etoro, "get_capabilities") else {}
            )
            evidence["capabilities"] = capabilities
            instruments = (
                self.etoro.list_supported_instruments()
                if hasattr(self.etoro, "list_supported_instruments")
                else []
            )
            evidence["supported_instruments"] = instruments
            what_if_result = self._etoro_limit_what_if()
            evidence["limit_order_what_if"] = what_if_result
            supports_limit = bool(
                capabilities.get("supports_extended_hours_limit_orders")
                or capabilities.get("supports_limit_orders")
                or what_if_result.get("accepted")
            )
            supports_24_5 = bool(
                capabilities.get("supports_24_5")
                or capabilities.get("extended_hours_24_5")
                or capabilities.get("supports_extended_hours")
            )
            if not (supports_limit and supports_24_5):
                return self.repository.record_etoro_probe(
                    probe_id=probe_id,
                    status="blocked",
                    classification="non_equivalent_order_type",
                    account_verified=account_verified,
                    evidence=evidence,
                )
            supports_exit = bool(
                capabilities.get("supports_extended_hours_exits")
                or capabilities.get("supports_close_position")
            )
            if not supports_exit:
                return self.repository.record_etoro_probe(
                    probe_id=probe_id,
                    status="blocked",
                    classification="unverified_exit_behavior",
                    account_verified=account_verified,
                    evidence=evidence,
                )
            return self.repository.record_etoro_probe(
                probe_id=probe_id,
                status="ok",
                classification="supported",
                account_verified=account_verified,
                evidence=evidence,
            )
        except Exception as exc:
            return self.repository.record_etoro_probe(
                probe_id=probe_id,
                status="error",
                classification="api_error",
                account_verified=False,
                evidence={**evidence, "error": str(exc)},
            )

    def _validate_alpaca_entry_gates(self, symbol: str) -> Any:
        blockers: list[str] = []
        if not bool(getattr(self.settings, "extended_hours_experiment_enabled", False)):
            blockers.append("extended_hours_experiment_disabled")
        if self.alpaca is None:
            blockers.append("alpaca_client_not_configured")
        if str(getattr(self.settings, "execution_mode", "paper")) != "paper":
            blockers.append("execution_mode_not_paper")
        if bool(getattr(self.settings, "enable_real_trading", False)):
            blockers.append("real_trading_enabled")
        if symbol not in self.whitelist():
            blockers.append("symbol_not_whitelisted")
        automation_status = self.automation.status()
        if automation_status.kill_switch_enabled:
            blockers.append("kill_switch_enabled")
        if automation_status.circuit_breaker_reason:
            blockers.append("circuit_breaker_active")
        expected_account = str(getattr(self.settings, "alpaca_expected_account_number", "") or "")
        if not expected_account:
            blockers.append("alpaca_expected_account_not_configured")
        elif self.alpaca is not None and hasattr(self.alpaca, "get_account_identity"):
            identity = self.alpaca.get_account_identity()
            if str(identity.get("account_number") or "") != expected_account:
                blockers.append("alpaca_account_mismatch")
        latest_reconciliation = self.safety.latest_reconciliation()
        if latest_reconciliation is None:
            blockers.append("reconciliation_missing")
        elif latest_reconciliation.get("status") != "ok":
            blockers.append("reconciliation_not_clean")
        max_open = int(getattr(self.settings, "extended_hours_max_open_orders", 1) or 1)
        if self.repository.open_order_count() >= max_open:
            blockers.append("extended_hours_open_order_limit_reached")
        if self.alpaca is not None:
            positions = [
                item
                for item in self.alpaca.get_portfolio().positions
                if abs(float(getattr(item, "quantity", 0.0))) > 0
            ]
            if positions:
                blockers.append("broker_position_already_open")
        if blockers:
            raise ValueError(";".join(sorted(set(blockers))))
        return self._quote(symbol)

    def _quote(self, symbol: str) -> Any:
        quote = self.alpaca.get_quote(symbol, force_refresh=True) if self.alpaca is not None else None
        if quote is None:
            raise ValueError("quote_unavailable")
        return quote

    def _order_plan(
        self,
        *,
        symbol: str,
        side: str,
        quote: Any,
        limit_price: float | None,
        max_qty: float | None = None,
    ) -> dict[str, Any]:
        bid = _optional_float(getattr(quote, "bid", None))
        ask = _optional_float(getattr(quote, "ask", None))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("invalid_alpaca_quote")
        midpoint = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / midpoint) * 10_000.0
        max_spread = float(getattr(self.settings, "extended_hours_max_spread_bps", 75.0))
        if spread_bps > max_spread:
            raise ValueError("spread_too_wide")
        quote_age = _quote_age_seconds(quote)
        max_age = float(getattr(self.settings, "extended_hours_max_quote_age_seconds", 30))
        if quote_age > max_age:
            raise ValueError("quote_stale")
        reference = ask if side == "buy" else bid
        effective_limit = round(float(limit_price if limit_price is not None else reference), 2)
        max_allowed_drift = max_spread / 10_000.0
        if side == "buy" and effective_limit > ask * (1 + max_allowed_drift):
            raise ValueError("limit_price_exceeds_slippage_guard")
        if side == "sell" and effective_limit < bid * (1 - max_allowed_drift):
            raise ValueError("limit_price_exceeds_slippage_guard")
        max_notional = float(getattr(self.settings, "extended_hours_max_notional_usd", 100.0))
        configured_max_qty = float(getattr(self.settings, "extended_hours_max_qty", 1.0))
        effective_max_qty = min(configured_max_qty, max_qty) if max_qty is not None else configured_max_qty
        qty = min(effective_max_qty, max_notional / effective_limit)
        qty = math.floor(qty * 1_000_000.0) / 1_000_000.0
        if qty <= 0:
            raise ValueError("extended_hours_size_too_small")
        return {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "limit_price": effective_limit,
            "notional_usd": round(qty * effective_limit, 2),
            "spread_bps": spread_bps,
            "quote_age_seconds": quote_age,
            "quote": _quote_payload(quote),
        }

    def _alpaca_position(self, symbol: str) -> Any | None:
        portfolio = self.alpaca.get_portfolio()
        for position in portfolio.positions:
            if str(getattr(position, "symbol", "")).upper() == symbol:
                return position
        return None

    def _etoro_limit_what_if(self) -> dict[str, Any]:
        if self.etoro is None or not hasattr(self.etoro, "what_if"):
            return {"accepted": False, "reason": "what_if_unavailable"}
        symbol = self.whitelist()[0] if self.whitelist() else "SPY"
        payload = {
            "action": "open",
            "transaction": "buy",
            "symbol": symbol,
            "settlementType": "stock",
            "orderType": "lmt",
            "leverage": 1,
            "amount": 10.0,
            "orderCurrency": "usd",
            "stopLossRate": 1.0,
            "takeProfitRate": 2.0,
            "stopLossType": "fixed",
        }
        try:
            return {"accepted": True, "payload": payload, "response": self.etoro.what_if(payload)}
        except Exception as exc:
            return {"accepted": False, "payload": payload, "error": str(exc)}


def _quote_payload(quote: Any) -> dict[str, Any]:
    if hasattr(quote, "model_dump"):
        return quote.model_dump()
    return dict(getattr(quote, "__dict__", {}) or {})


def _quote_age_seconds(quote: Any) -> float:
    direct = _optional_float(getattr(quote, "data_age_seconds", None))
    if direct is not None and direct >= 0:
        return direct
    timestamp = getattr(quote, "timestamp", None)
    if not timestamp:
        return 0.0
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (utc_now() - parsed.astimezone(UTC)).total_seconds())


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
