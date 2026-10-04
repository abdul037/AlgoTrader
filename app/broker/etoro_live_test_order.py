"""One-off eToro LIVE crypto test order (operator request 2026-10-04).

US stocks are closed at the weekend, so the operator asked to prove the eToro
live chain on crypto: one small real position in the AlgoBot account, copied in
proportion into the operator's $500, closed by the bot's usual bracket.
Operator choices: ETH, $1,000 in AlgoBot (about $50 of the copy; first asked
$200 / $10, then "increase the copy value from 10 to 50$"), exit "based on
the strategy and profit made" -- stop ``STOP_ATR_MULT`` x daily ATR(14) below
the entry, target ``REWARD_TO_RISK`` x that distance above, both held by eToro,
plus a ``MAX_HOLD_DAYS`` time stop.

It only acts on an explicit request row (``REQUEST_KEY`` in runtime_state,
e.g. ``{"symbol": "ETH", "amount_usd": 200}``) and only while the live
mirror's locks hold (enabled, acknowledgement, not halted, real client). One
test position at a time; the caps are code constants. A failed test is logged
and never halts the live mirror or touches the Alpaca paper bot.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

REQUEST_KEY = "etoro_live:test_order_request"
TEST_STATE_KEY = "etoro_live:test_order"
HARD_MAX_TEST_ORDER_USD = 1_000.0  # same per-position backstop as the live mirror
MAX_TEST_PCT_OF_EQUITY = 10.0  # same share of the AlgoBot balance as a mirror trade
ALLOWED_TEST_SYMBOLS = frozenset({"ETH", "BTC"})
ATR_PERIOD = 14
STOP_ATR_MULT = 1.5  # same multiple as the live signal ATR stop
REWARD_TO_RISK = 2.0
MIN_STOP_PCT = 1.0  # eToro's crypto fee is about 1% a side; a tighter stop is noise
MAX_STOP_PCT = 10.0
MAX_HOLD_DAYS = 7
NOT_FOUND_GRACE_MINUTES = 30


def run_from_maintenance(service: Any, completed: list[str]) -> None:
    """Maintenance hook: run the test order if the app wired one."""

    execution = getattr(getattr(service, "auto_trading", None), "execution", None)
    tester = getattr(execution, "etoro_live_test_order", None)
    if tester is None:
        return
    try:
        if tester.run() is not None:
            completed.append("etoro_live_test_order")
    except Exception as exc:  # noqa: BLE001 - maintenance must keep running
        with suppress(Exception):
            service.run_logs.log("etoro_live_test_order_error", {"error": str(exc)})


def bracket_from_atr(rate: float, atr: float) -> tuple[float, float, float]:
    """(stop, target, stop distance) for a long entry at ``rate``."""

    distance = STOP_ATR_MULT * atr
    distance = min(max(distance, rate * MIN_STOP_PCT / 100.0), rate * MAX_STOP_PCT / 100.0)
    return round(rate - distance, 2), round(rate + REWARD_TO_RISK * distance, 2), distance


class EtoroLiveTestOrder:
    """Place, watch and close one operator-requested crypto test position."""

    def __init__(self, *, mirror: Any, bars: Any | None = None):
        self.mirror = mirror
        self.bars = bars

    @property
    def client(self) -> Any:
        return self.mirror.client

    def run(self) -> dict[str, Any] | None:
        test = self._load()
        if test.get("status") == "open":
            return self._monitor(test)
        raw = self.mirror.state.get(REQUEST_KEY)
        if not raw:
            return None
        self.mirror.state.set(REQUEST_KEY, "")  # one-shot, even if the order fails
        try:
            request = json.loads(raw)
        except (TypeError, ValueError):
            return self._fail({}, "request_not_json")
        return self._open(request if isinstance(request, dict) else {})

    # -- open --------------------------------------------------------------------
    def _blockers(self, symbol: str) -> list[str]:
        from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
        from app.broker.etoro_live_mirror import HALTED_KEY, client_problem

        settings = self.mirror.settings
        reasons: list[str] = []
        if not bool(getattr(settings, "etoro_live_mirror_enabled", False)):
            reasons.append("etoro_live_mirror_disabled")
        problem = client_problem(self.client)
        if problem:
            reasons.append(problem)
        if (
            str(getattr(settings, "etoro_live_acknowledgement", "") or "")
            != LIVE_OPERATOR_ACKNOWLEDGEMENT
        ):
            reasons.append("etoro_live_acknowledgement_missing")
        if self.mirror.state.get(HALTED_KEY):
            reasons.append("etoro_live_mirror_halted")
        if symbol not in ALLOWED_TEST_SYMBOLS:
            reasons.append(f"test_symbol_not_allowed:{symbol}")
        if self.bars is None:
            reasons.append("no_bar_source_for_atr")
        return reasons

    def _open(self, request: dict[str, Any]) -> dict[str, Any]:
        symbol = str(request.get("symbol") or "").upper().strip()
        amount = min(float(request.get("amount_usd") or 0.0), HARD_MAX_TEST_ORDER_USD)
        with suppress(Exception):
            equity = json.loads(self.mirror.state.get("etoro_live:state") or "{}").get(
                "last_equity"
            )
            if equity:
                amount = min(amount, round(float(equity) * MAX_TEST_PCT_OF_EQUITY / 100.0, 2))
        test: dict[str, Any] = {
            "symbol": symbol,
            "amount_usd": amount,
            "requested_at": utc_now().isoformat(),
        }
        reasons = self._blockers(symbol)
        if amount < 10.0:
            reasons.append("test_amount_below_10_usd")
        if reasons:
            return self._fail(test, ",".join(reasons))
        try:
            instrument = self.client._search_instrument(symbol)
            if not (instrument["is_tradable"] and instrument["is_buy_enabled"]):
                return self._fail(test, f"{symbol}_not_tradable_on_etoro_now")
            rate = float(instrument["current_rate"])
            now = utc_now()
            candles = self.bars(
                f"{symbol}/USD", timeframe="1d", start=now - timedelta(days=45), end=now
            )
            from app.signals.evaluation import atr_from_candles

            atr = atr_from_candles(candles, period=ATR_PERIOD)
            if rate <= 0 or atr <= 0:
                return self._fail(test, f"no_price_or_atr:rate={rate}:atr={atr}")
            stop, target, distance = bracket_from_atr(rate, atr)
            payload = {
                "InstrumentID": instrument["instrument_id"],
                "Amount": amount,
                "Leverage": 1,
                "IsBuy": True,
                "IsTslEnabled": False,
                "IsNoStopLoss": False,
                "IsNoTakeProfit": False,
                "StopLossRate": stop,
                "TakeProfitRate": target,
            }
            self.client._ensure_order_mode_allowed()
            response = self.client._request(
                "POST",
                self.client._trading_execution_path("market-open-orders/by-amount"),
                json_body=payload,
            )
        except Exception as exc:  # noqa: BLE001 - a failed test never halts the mirror
            return self._fail(test, f"open_failed:{exc}")
        order = response.get("orderForOpen", {}) if isinstance(response, dict) else {}
        test.update(
            status="open",
            order_id=str(order.get("orderID") or ""),
            order_status_id=order.get("statusID"),
            instrument_id=int(instrument["instrument_id"]),
            entry_rate_ref=rate,
            atr_1d=round(atr, 2),
            stop_loss=stop,
            take_profit=target,
            risk_pct=round(distance / rate * 100.0, 2),
            opened_at=utc_now().isoformat(),
            max_hold_until=(utc_now() + timedelta(days=MAX_HOLD_DAYS)).isoformat(),
            equity_at_open=self._equity(),
            position_id=None,
        )
        self._save(test)
        self.mirror.logs.log("etoro_live_test_order_submitted", test)
        self._notify(
            f"eToro LIVE test: BUY ${amount:.0f} {symbol} x1 near {rate:,.2f} "
            f"(stop {stop:,.2f}, target {target:,.2f}, closes by {test['max_hold_until'][:10]} at the latest)"
        )
        return test

    # -- watch -------------------------------------------------------------------
    def _monitor(self, test: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw = self.client.fetch_raw_portfolio()
        except Exception as exc:  # noqa: BLE001
            self.mirror.logs.log("etoro_live_test_order_watch_error", {"error": str(exc)})
            return None
        positions = (raw.get("clientPortfolio", {}) or {}).get("positions", []) or []
        mine = [
            p for p in positions if int(p.get("instrumentID") or 0) == test.get("instrument_id")
        ]
        position = next((p for p in mine if p.get("positionID") == test.get("position_id")), None)
        if position is None and test.get("position_id") is None and mine:
            position = mine[0]
            test.update(
                position_id=position.get("positionID"),
                open_rate=position.get("openRate"),
                units=position.get("units"),
                stop_loss_at_broker=position.get("stopLossRate"),
                take_profit_at_broker=position.get("takeProfitRate"),
                filled_seen_at=utc_now().isoformat(),
            )
            self.mirror.logs.log("etoro_live_test_order_filled", test)
            self._notify(
                f"eToro LIVE test filled: {test['symbol']} @ {position.get('openRate')} "
                f"(stop {position.get('stopLossRate')}, target {position.get('takeProfitRate')})"
            )
        if position is not None:
            unprotected = not position.get("stopLossRate") or bool(position.get("isNoStopLoss"))
            expired = utc_now().isoformat() >= str(test.get("max_hold_until") or "9999")
            if unprotected or expired:
                return self._close(
                    test, position, "no_stop_at_broker" if unprotected else "time_stop"
                )
            self._save(test)
            return test
        if test.get("position_id") is not None:
            return self._finish(test, "closed_at_broker_by_stop_or_target")
        opened = str(test.get("opened_at") or "")
        grace_over = utc_now() - timedelta(minutes=NOT_FOUND_GRACE_MINUTES)
        if opened and opened < grace_over.isoformat():
            return self._finish(test, "no_position_seen_order_likely_rejected")
        return test

    def _close(self, test: dict[str, Any], position: dict[str, Any], reason: str) -> dict[str, Any]:
        try:
            self.client.close_position_by_id(
                int(position["positionID"]), int(test["instrument_id"])
            )
        except Exception as exc:  # noqa: BLE001
            self.mirror.logs.log(
                "etoro_live_test_order_close_failed", {"reason": reason, "error": str(exc)}
            )
            self._notify(
                f"eToro LIVE test: close FAILED ({reason}): {exc}. Close {test['symbol']} by hand."
            )
            return test
        return self._finish(test, reason)

    def _finish(self, test: dict[str, Any], reason: str) -> dict[str, Any]:
        equity = self._equity()
        test.update(
            status="closed",
            close_reason=reason,
            closed_seen_at=utc_now().isoformat(),
            equity_at_close=equity,
        )
        if equity is not None and test.get("equity_at_open") is not None:
            test["approx_pnl_usd"] = round(float(equity) - float(test["equity_at_open"]), 2)
        self._save(test)
        self.mirror.logs.log("etoro_live_test_order_closed", test)
        self._notify(
            f"eToro LIVE test closed ({reason}). Approx P&L in AlgoBot: {test.get('approx_pnl_usd', 'n/a')} USD."
        )
        return test

    # -- helpers -----------------------------------------------------------------
    def _fail(self, test: dict[str, Any], reason: str) -> dict[str, Any]:
        test.update(status="failed", reason=reason, failed_at=utc_now().isoformat())
        self._save(test)
        self.mirror.logs.log("etoro_live_test_order_failed", test)
        self._notify(f"eToro LIVE test not placed: {reason}")
        return test

    def _equity(self) -> float | None:
        with suppress(Exception):
            return float(self.client.get_portfolio().account.equity)
        return None

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.mirror.state.get(TEST_STATE_KEY) or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _save(self, test: dict[str, Any]) -> None:
        self.mirror.state.set(TEST_STATE_KEY, json.dumps(test))

    def _notify(self, text: str) -> None:
        self.mirror._notify(text)
