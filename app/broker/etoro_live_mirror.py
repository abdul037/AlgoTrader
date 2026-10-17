"""Capped eToro LIVE test mirror (operator decision 2026-10-04: "go with option 1").

Every Alpaca *paper* entry the bot makes -- since Phase 2 only the daily
strategies with pooled out-of-sample evidence -- is mirrored as a small REAL
eToro position, so the operator can see how the strategies behave in a real
market and prove the eToro live plumbing (orders, stop/target, P&L, leverage).
Operator choices: automatic within caps, 1x first then exactly one 2x test.
Sizing (operator decision 2026-10-04): each position is 10% of the eToro
account equity last read by ``reconcile`` -- not a fixed dollar amount --
because the AlgoBot account is funded by copying and eToro copies its trades
in proportion. 10% of AlgoBot's $10,000 = $1,000, so about $50 of the
operator's $500 copy; if AlgoBot is ever funded directly, 10% of $500 = $50.
Operator 2026-10-16 (AMD's 15.65% stop lost $155 on a flat $994 copy while SPY's 1.4% stop
risked $14): each copy is also cut so a stop-out costs at most
``HARD_MAX_RISK_PCT_OF_EQUITY`` of equity, a stop more than ``HARD_MAX_STOP_DISTANCE_PCT``
below the entry is not copied, and the strategy needs a pooled OOS expectancy of at least
``MIRROR_MIN_EXPECTANCY_R`` (paper keeps its own "> 0" bar, so it keeps collecting evidence).

Real money. The mirror only acts when ALL of these hold:

* ``ETORO_LIVE_MIRROR_ENABLED=true`` (operator-set);
* ``ETORO_LIVE_API_KEY`` / ``ETORO_LIVE_USER_KEY`` set (operator-set Railway
  secrets; never committed, never pasted in chat);
* ``ETORO_LIVE_ACKNOWLEDGEMENT`` equals ``LIVE_OPERATOR_ACKNOWLEDGEMENT``;
* the primary trade is an Alpaca *paper* long equity entry, with stop and
  target, from a strategy that passes the Phase 2 evidence gate by at least
  ``MIRROR_MIN_EXPECTANCY_R``, with its stop at most ``HARD_MAX_STOP_DISTANCE_PCT``
  below the entry;
* the mirror is not halted.

Hard caps are code constants, not settings: at most
``HARD_MAX_TRADE_PCT_OF_EQUITY`` of equity and never more than
``HARD_MAX_TRADE_USD`` per position, at most ``HARD_MAX_RISK_PCT_OF_EQUITY`` of equity
lost at the stop, ``HARD_MAX_TRADES_PER_DAY`` new positions
per UTC day, ``HARD_MAX_OPEN_POSITIONS`` open, a ``HARD_DAILY_LOSS_STOP_PCT``
equity drop per day, no trade until equity has been read, and leverage 1 (the one
2x test the operator asked for ran on 2026-10-07; ``LEVERAGE_TEST_ENABLED`` is off so a
reset state cannot re-arm it). Any eToro error halts the mirror -- it never touches the
Alpaca paper bot or its circuit breaker -- until the operator clears
``etoro_live:halted``. The global ``ENABLE_REAL_TRADING`` stays false: the
mirror's own client gets a private settings copy with real mode enabled.
"""

from __future__ import annotations

import functools
import json
import math
import threading
from contextlib import suppress
from typing import Any

from app.utils.time import utc_now

HARD_MAX_TRADE_PCT_OF_EQUITY = 10.0
HARD_MAX_TRADE_USD = 1_000.0  # absolute backstop against a bad equity reading
MIN_TRADE_USD = 10.0  # eToro's minimum position size
HARD_MAX_TRADES_PER_DAY = 2
# Operator 2026-10-06: 3 -> 6 "if there is any fund left"; 2026-10-07: 6 -> 9 so the cash can
# be invested when strategies approve trades (9 x 10% + the $100 reserve fits the account).
HARD_MAX_OPEN_POSITIONS = 9
CASH_RESERVE_USD = 100.0  # free cash kept back for fees; an entry must fit in the rest
HARD_DAILY_LOSS_STOP_PCT = 5.0
# Operator 2026-10-16: size each copy from its stop (a stop-out costs <= 0.5% of equity,
# about $50 on $10,000), skip stops wider than 8%, and copy only strategies whose pooled
# out-of-sample backtest earns at least +0.03 R per trade.
HARD_MAX_RISK_PCT_OF_EQUITY = 0.5
HARD_MAX_STOP_DISTANCE_PCT = 8.0
MIRROR_MIN_EXPECTANCY_R = 0.03
LEVERAGE_TEST_AFTER_1X_TRADES = 2
# Operator 2026-10-06 "try the 2x leverage": eToro's Amount is margin, so the 2x test sends
# half the usual amount -- same $ exposure as a 1x trade ($500 x 2 = $1,000 notional).
# The one test ran on 2026-10-07 (AMD); off since 2026-10-16 so a reset state can't re-arm it.
LEVERAGE_TEST_ENABLED = False

HALTED_KEY = "etoro_live:halted"
STATE_KEY = "etoro_live:state"


def build_live_client(settings: Any) -> Any | None:
    """eToro client bound to the LIVE keys, with real mode scoped to this client only."""

    api_key = str(getattr(settings, "etoro_live_api_key", "") or "")
    user_key = str(getattr(settings, "etoro_live_user_key", "") or "")
    if not api_key or not user_key:
        return None
    from app.broker.etoro_client import EToroClient

    scoped = settings.model_copy(
        update={
            "etoro_api_key": api_key,
            "etoro_user_key": user_key,
            "etoro_account_mode": "real",
            "enable_real_trading": True,  # private copy only; the global flag is unchanged
        }
    )
    return EToroClient(scoped)


def account_value_at_cost(raw: dict[str, Any], fallback: float = 0.0) -> float:
    """AlgoBot balance = cash ``credit`` + the dollars invested in open positions.

    eToro's portfolio ``credit`` is cash only: a $1,000 buy drops it by $1,000 plus the
    fee (seen 2026-10-04: $10,000 -> $8,990 after the ETH test). Using it as equity would
    shrink every later trade and read money in open trades as a daily loss. Positions
    carry ``amount`` (invested at cost) but no live value, so open P&L counts on close.
    """

    portfolio = (raw or {}).get("clientPortfolio", {}) or {}
    if portfolio.get("credit") is None:
        return float(fallback or 0.0)
    invested = sum(float(p.get("amount") or 0.0) for p in portfolio.get("positions", []) or [])
    return round(float(portfolio["credit"]) + invested, 2)


def client_problem(client: Any | None) -> str | None:
    """Why a live client must not be used, or None. A simulated client returns a fake
    $10,000 account (seen 2026-10-04) and would make mirrored "trades" look real."""

    if client is None:
        return "etoro_live_keys_missing"
    client_settings = getattr(client, "settings", None)
    if bool(getattr(client_settings, "broker_simulation_enabled", False)):
        host = (
            str(getattr(client_settings, "etoro_base_url", "") or "").split("//")[-1].split("/")[0]
        )
        return f"etoro_live_client_in_simulation_mode:base_url_host={host}"
    return None


def stop_distance_pct(entry: Any, stop: Any) -> float | None:
    """How far a long's stop sits below its entry, in % of the entry (None: not a valid stop)."""

    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry) and math.isfinite(stop)) or not 0 < stop < entry:
        return None
    return (entry - stop) / entry * 100.0


def _serialized(method: Any) -> Any:
    """Run a mirror method under the mirror's lock."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


class EtoroLiveMirrorService:
    """Mirror qualifying Alpaca paper entries into small, capped eToro live positions."""

    def __init__(
        self,
        *,
        settings: Any,
        client: Any | None,
        runtime_state: Any,
        run_logs: Any,
        notifier: Any | None = None,
    ):
        self.settings = settings
        self.client = client
        self.state = runtime_state
        self.logs = run_logs
        self.notifier = notifier
        # One lock for every live-eToro state change: entries (execution thread), reconcile
        # (scheduler) and the guard thread's test order / backup stops.
        self.lock = threading.RLock()

    # -- gates -----------------------------------------------------------------
    def blockers(self, proposal: Any, primary_broker: str) -> list[str]:
        from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
        from app.performance.strategy_evidence import EDGE_BELOW_BAR, evidence_blocker

        settings = self.settings
        if not bool(getattr(settings, "etoro_live_mirror_enabled", False)):
            return ["etoro_live_mirror_disabled"]
        reasons: list[str] = []
        problem = client_problem(self.client)
        if problem:
            reasons.append(problem)
        if (
            str(getattr(settings, "etoro_live_acknowledgement", "") or "")
            != LIVE_OPERATOR_ACKNOWLEDGEMENT
        ):
            reasons.append("etoro_live_acknowledgement_missing")
        if self.state.get(HALTED_KEY):
            reasons.append("etoro_live_mirror_halted")
        if (
            primary_broker != "alpaca"
            or str(getattr(settings, "execution_mode", "paper")) != "paper"
        ):
            reasons.append("mirror_requires_alpaca_paper_primary")
        order = proposal.order
        if str(getattr(order.side, "value", order.side)).lower() != "buy":
            reasons.append("mirror_long_only")
        from app.broker import crypto as crypto_symbols

        bare = str(order.symbol or "").strip().upper()  # eToro names crypto "BTC", "ETH"
        if crypto_symbols.is_crypto_symbol(bare) or bare in crypto_symbols.KNOWN_CRYPTO_BASES:
            reasons.append("mirror_equities_only")
        if str(getattr(order.asset_class, "value", order.asset_class) or "equity") not in {
            "equity",
            "unknown",
        }:
            reasons.append("mirror_equities_only")
        stop_pct = stop_distance_pct(order.proposed_price, order.stop_loss)
        if order.stop_loss is None or order.take_profit is None:
            reasons.append("mirror_requires_stop_and_target")
        elif stop_pct is None:  # no entry price, or a "stop" at/above it: can't size the risk
            reasons.append("etoro_live_stop_distance_unknown")
        elif stop_pct > HARD_MAX_STOP_DISTANCE_PCT:
            reasons.append("etoro_live_stop_too_wide")
        metadata = (
            (getattr(proposal.signal, "metadata", None) or {})
            if proposal.signal is not None
            else {}
        )
        # The real timeframe lives in order.metadata (review 2026-10-05: proposals from the
        # scanner carry no signal, so the old "1d" default checked the wrong evidence key).
        timeframe = (getattr(order, "metadata", None) or {}).get("timeframe") or metadata.get(
            "timeframe"
        )
        strict = _StrictEvidence(settings)
        if not timeframe:
            reasons.append("mirror_timeframe_unknown")
        elif evidence := evidence_blocker(
            strict,
            self.state,
            strategy=str(order.strategy_name or ""),
            timeframe=str(timeframe),
            min_expectancy_r=MIRROR_MIN_EXPECTANCY_R,
        ):
            reasons.append(
                evidence if evidence == EDGE_BELOW_BAR else "strategy_lacks_oos_evidence"
            )
        state = self._state()
        if state["trades_today"] >= HARD_MAX_TRADES_PER_DAY:
            reasons.append("etoro_live_daily_trade_cap")
        from app.broker.etoro_live_room import bucket_blockers, effective_open_symbols

        held = effective_open_symbols(self.state, state)  # ids mapped, unfilled orders kept
        if order.symbol.upper() in held:
            reasons.append("etoro_live_symbol_already_open")
        if len(held) >= HARD_MAX_OPEN_POSITIONS:
            reasons.append("etoro_live_open_position_cap")
        reasons.extend(bucket_blockers(order.symbol, held))
        start, last = state.get("day_start_equity"), state.get("last_equity")
        if (
            start is not None
            and last is not None
            and float(start) - float(last) >= float(start) * HARD_DAILY_LOSS_STOP_PCT / 100.0
        ):
            reasons.append("etoro_live_daily_loss_stop")
        equity = state.get("last_equity")
        if equity is None or float(equity) <= 0:
            reasons.append("etoro_live_equity_unknown")
        amount = self._trade_amount(state, stop_pct)  # None: an unknown stop is refused above
        if amount is not None and amount < MIN_TRADE_USD:
            reasons.append("etoro_live_trade_below_minimum")
        cash = state.get("last_cash")
        if amount and cash is not None and float(cash) - CASH_RESERVE_USD < amount:
            reasons.append("etoro_live_insufficient_cash")  # only trade with money left
        return reasons

    def _trade_amount(self, state: dict[str, Any], stop_pct: float | None) -> float | None:
        """Exposure for one copy: a share of the last equity reading, cut so a stop
        ``stop_pct`` % below the entry costs at most ``HARD_MAX_RISK_PCT_OF_EQUITY`` of
        equity, capped in code. None when the equity or the stop distance is unknown."""

        equity = state.get("last_equity")
        if equity is None or float(equity) <= 0 or stop_pct is None or not stop_pct > 0:
            return None
        pct = float(getattr(self.settings, "etoro_live_trade_pct_of_equity", 10.0) or 0.0)
        pct = min(max(pct, 0.0), HARD_MAX_TRADE_PCT_OF_EQUITY)
        at_risk = float(equity) * HARD_MAX_RISK_PCT_OF_EQUITY / stop_pct  # loses 0.5% at stop
        return round(min(float(equity) * pct / 100.0, HARD_MAX_TRADE_USD, at_risk), 2)

    # -- mirror ----------------------------------------------------------------
    @_serialized
    def mirror(
        self, *, proposal: Any, primary_execution: Any, primary_broker: str
    ) -> dict[str, Any] | None:
        try:
            reasons = self.blockers(proposal, primary_broker)
        except Exception as exc:  # noqa: BLE001 - never disturb the paper bot
            reasons = [f"etoro_live_gate_error:{exc}"]
        if reasons == ["etoro_live_mirror_disabled"]:
            return None
        symbol = proposal.order.symbol.upper()
        if reasons:
            self.logs.log(
                "etoro_live_mirror_blocked",
                {"symbol": symbol, "proposal_id": proposal.id, "reasons": reasons},
            )
            return None
        state = self._state()
        leverage = (
            2
            if (
                LEVERAGE_TEST_ENABLED
                and not state["leverage_2x_done"]
                and state["successful_1x"] >= LEVERAGE_TEST_AFTER_1X_TRADES
            )
            else 1
        )
        order = proposal.order
        amount = self._trade_amount(state, stop_distance_pct(order.proposed_price, order.stop_loss))
        if not amount:  # backstop: blockers() already refuses an order it cannot size
            self.logs.log(
                "etoro_live_mirror_blocked",
                {"symbol": symbol, "proposal_id": proposal.id, "reasons": ["etoro_live_unsized"]},
            )
            return None
        if leverage > 1:
            amount = round(amount / leverage, 2)  # Amount is margin: keep the 1x exposure
        order = proposal.order.model_copy(update={"amount_usd": amount, "leverage": leverage})
        from app.broker.etoro_live_backup_stop import forget_stop, remember_stop
        from app.broker.etoro_live_room import note_copy_failed

        # Write-ahead (review 2026-10-05): the daily count, the one-time 2x flag and the
        # backup stop are saved BEFORE the POST, so a timeout whose order did fill still
        # leaves the position protected and can never allow a second 2x order.
        before = dict(state)
        state["trades_today"] += 1
        state["open_symbols"] = sorted(set(state["open_symbols"]) | {symbol})
        if leverage == 2:
            state["leverage_2x_done"] = True
        if state.get("last_cash") is not None:  # spent until the next reconcile re-reads it
            state["last_cash"] = round(float(state["last_cash"]) - float(amount or 0.0), 2)
        self._save(state)
        remember_stop(self, symbol, order.stop_loss, None)  # instrument id resolved at check
        try:
            response = self.client.open_market_order_by_amount(
                order, client_order_id=f"etoro-live:{proposal.id}"
            )
        except Exception as exc:  # noqa: BLE001 - halt the mirror, keep the paper bot running
            from app.broker.etoro_rate_limit import EToroRateLimitError

            if isinstance(exc, EToroRateLimitError) or "with status 4" in str(exc):
                self._save(before)  # refused or never sent: nothing was opened
                forget_stop(self, symbol)
            if isinstance(exc, EToroRateLimitError):
                self.logs.log(
                    "etoro_live_mirror_rate_limited", {"symbol": symbol, "error": str(exc)}
                )
                note_copy_failed(self, symbol, "rate_limited")
                return None
            self._halt(f"open_failed:{symbol}:{exc}")
            return None
        if str(getattr(response, "status", "") or "").lower() in {"rejected", "cancelled"}:
            self._save(before)  # a rejected order is not a trade and not a successful 1x
            forget_stop(self, symbol)
            note_copy_failed(self, symbol, str(response.status))
            self.logs.log(
                "etoro_live_mirror_rejected",
                {"symbol": symbol, "proposal_id": proposal.id, "status": response.status},
            )
            self._notify(f"eToro LIVE order for {symbol} was {response.status}; nothing opened.")
            return None
        if leverage == 1:
            state["successful_1x"] += 1
            self._save(state)
        record = {
            "symbol": symbol,
            "proposal_id": proposal.id,
            "primary_order_id": getattr(primary_execution, "broker_order_id", None),
            "etoro_order_id": response.order_id,
            "status": response.status,
            "amount_usd": amount,
            "equity_basis_usd": state.get("last_equity"),
            "leverage": leverage,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "strategy_name": order.strategy_name,
        }
        self.logs.log("etoro_live_mirror_submitted", record)
        try:  # live-vs-backtest scorecard; bookkeeping only, never blocks the trade
            from app.broker.etoro_live_scorecard import record_entry

            metadata = getattr(getattr(proposal, "signal", None), "metadata", None) or {}
            timeframe = (getattr(order, "metadata", None) or {}).get("timeframe") or metadata.get(
                "timeframe"
            )
            record_entry(self, record, timeframe=timeframe, entry_price=order.proposed_price)
        except Exception as exc:  # noqa: BLE001
            self.logs.log("etoro_live_scorecard_error", {"symbol": symbol, "error": str(exc)})
        self._notify(
            f"eToro LIVE test order: BUY ${amount:.0f} {symbol} x{leverage} "
            f"(stop {order.stop_loss}, target {order.take_profit}, {order.strategy_name})"
        )
        return record

    # -- reconcile (maintenance) -----------------------------------------------
    @_serialized
    def reconcile(self) -> dict[str, Any] | None:
        """Refresh equity and open positions; close any live position left without a stop loss."""

        if (
            not bool(getattr(self.settings, "etoro_live_mirror_enabled", False))
            or self.client is None
        ):
            return None
        problem = client_problem(self.client)
        if problem:
            self.logs.log("etoro_live_client_unusable", {"reason": problem})
            return {"unusable": problem}
        try:
            raw = self.client.fetch_raw_portfolio()
            portfolio = self.client.get_portfolio()
        except Exception as exc:  # noqa: BLE001
            from app.broker.etoro_rate_limit import EToroRateLimitError

            if isinstance(exc, EToroRateLimitError):  # local cooldown: skip, don't halt
                self.logs.log("etoro_live_reconcile_rate_limited", {"error": str(exc)})
                return None
            self._halt(f"reconcile_failed:{exc}")
            return None
        state = self._state()
        equity = account_value_at_cost(raw, fallback=float(portfolio.account.equity or 0.0))
        if state.get("day_start_equity") is None:
            state["day_start_equity"] = equity
        state["last_equity"] = equity
        credit = (raw.get("clientPortfolio", {}) or {}).get("credit")
        if credit is not None:
            state["last_cash"] = round(float(credit), 2)  # free cash, for the funds check
        state["open_symbols"] = sorted({str(p.symbol).upper() for p in portfolio.positions})
        by_position = {p.position_id: str(p.symbol).upper() for p in portfolio.positions}
        closed: list[str] = []
        for item in (raw.get("clientPortfolio", {}) or {}).get("positions", []) or []:
            if item.get("stopLossRate") and not bool(item.get("isNoStopLoss")):
                continue
            symbol = by_position.get(item.get("positionID"))
            if not symbol:
                continue
            try:
                self.client.close_position_by_id(
                    int(item["positionID"]), int(item.get("instrumentID") or 0)
                )
                closed.append(symbol)
            except Exception as exc:  # noqa: BLE001
                self._halt(f"close_unprotected_failed:{symbol}:{exc}")
        self._save(state)
        summary = {
            "equity": equity,
            "day_start_equity": state["day_start_equity"],
            "open_symbols": state["open_symbols"],
            "closed_unprotected": closed,
        }
        self.logs.log("etoro_live_reconciled", summary)
        return summary

    # -- helpers ---------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        today = utc_now().date().isoformat()
        try:
            state = json.loads(self.state.get(STATE_KEY) or "{}")
        except (TypeError, ValueError):
            state = {}
        state.setdefault("open_symbols", [])
        state.setdefault("successful_1x", 0)
        state.setdefault("leverage_2x_done", False)
        if state.get("day") != today:
            state.update(day=today, trades_today=0, day_start_equity=state.get("last_equity"))
        state.setdefault("trades_today", 0)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.state.set(STATE_KEY, json.dumps(state))

    def _halt(self, reason: str) -> None:
        self.state.set(HALTED_KEY, json.dumps({"reason": reason, "at": utc_now().isoformat()}))
        self.logs.log("etoro_live_mirror_halted", {"reason": reason})
        self._notify(f"eToro LIVE mirror HALTED: {reason}")

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        send = getattr(self.notifier, "send_text", None) or getattr(
            self.notifier, "send_message", None
        )
        if send is None:
            return
        with suppress(Exception):  # notification is best-effort
            send(text)


class _StrictEvidence:
    """Settings view that always enforces the Phase 2 evidence gate for the live mirror."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self.require_strategy_oos_evidence = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)
