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
Operator 2026-10-18 (post-mortem: AMD/META/MSFT/SPY were all weak, failed their own backtest
check, the planner said wait/watchlist/no_trade, the evidence flickered, AMD was +23% in 20
days the day after a record), four more rules for the live copy only -- paper trades, sizes
and gates exactly as before so evidence keeps coming in:

1. the planner's ``trade_plan.timing_label`` is a buy-now label
   (``MIRROR_BUY_NOW_TIMING_LABELS``: only "immediate");
2. the signal's own per-signal backtest check passed (``backtest_validated`` is True);
3. the strategy+timeframe has passed its pooled evidence check at every refresh for at least
   ``MIRROR_MIN_EVIDENCE_STREAK_HOURS`` without a break (``passing_since`` in the verdicts
   cache), counted up to the last refresh -- the time since it is unchecked and does not
   count; a refresh older than ``EVIDENCE_STREAK_MAX_GAP_HOURS`` reads as unknown;
4. the move was not chased: at most ``HARD_MAX_RETURN_20D_PCT`` % rise over the 20 sessions to
   the signal bar, RSI(14) below ``HARD_MAX_RSI_14``, and no 252-session closing high on the
   signal bar or the bar before it (``metadata["entry_extension"]``, computed by the scan on
   daily bars: ``app/screener/extension.py``; an intraday signal has none, so it is not copied).

Each rule fails CLOSED: a missing or unreadable input refuses the copy with its own reason
(``COPY_RULE_REASONS``). None of them is a room reason, so ``room_verdict`` returns None for
such an order and paper's own room checks decide it (``etoro_live_room``).

Real money. The mirror only acts when ALL of these hold:

* ``ETORO_LIVE_MIRROR_ENABLED=true`` (operator-set);
* ``ETORO_LIVE_API_KEY`` / ``ETORO_LIVE_USER_KEY`` set (operator-set Railway
  secrets; never committed, never pasted in chat);
* ``ETORO_LIVE_ACKNOWLEDGEMENT`` equals ``LIVE_OPERATOR_ACKNOWLEDGEMENT``;
* the primary trade is an Alpaca *paper* long equity entry, with stop and
  target, from a strategy that passes the Phase 2 evidence gate by at least
  ``MIRROR_MIN_EXPECTANCY_R``, with its stop at most ``HARD_MAX_STOP_DISTANCE_PCT``
  below the entry;
* the four 2026-10-18 rules above hold (planner, per-signal backtest, evidence streak,
  no chase);
* the order is not tagged paper-only (operator option B, 2026-10-21: admitted on paper's
  OOS bar alone; app/performance/paper_only.py), refused as ``etoro_live_paper_only``;
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

Overnight adverse-move alert (operator 2026-10-18, after AMD gapped to its stop before the
15 Oct open; alert only, no trading change): ``etoro_live_overnight_alert.py``, run by the
guard every minute, Telegrams the operator when, outside 09:30-16:00 New York on weekdays
(and all weekend for crypto), a live position's fresh eToro rate is ``ALERT_DROP_PCT`` (3%)
or more below the previous close, or within ``ALERT_STOP_ATR_MULT`` (1) ATR of its stop, or a
tracked signal on the same symbol hits its own stop. Code constants; at most one alert per
symbol per kind per Dubai date (the operator's day: a whole weekday night in New York is one
Dubai date in summer and winter time); it never blocks, sizes or closes a copy and adds no
refusal reason here. Setting ``etoro_live_overnight_alert_enabled`` (default on).
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
# Operator 2026-10-18 (post-mortem of the AMD/META/MSFT/SPY losers): four more copy rules.
# (1) The trade planner's only buy-now label (app/intelligence/trade_plan.py); "conditional"
# waits for a trigger, "watchlist", "no_trade" and "data_quality_insufficient" do not buy.
MIRROR_BUY_NOW_TIMING_LABELS = frozenset({"immediate"})
# (3) The pooled evidence must have passed every refresh for this long without a break,
# measured from the run's start to the last refresh (not to now).
MIRROR_MIN_EVIDENCE_STREAK_HOURS = 24.0
# (4) Don't chase: a rise above this over the 20 sessions to the signal bar, an RSI(14) at or
# above this, or a 252-session closing high on the signal bar or the bar before it.
HARD_MAX_RETURN_20D_PCT = 15.0
HARD_MAX_RSI_14 = 68.0
COPY_RULE_REASONS = frozenset(  # none is a room reason: paper keeps its own room checks
    {
        "etoro_live_planner_unknown",
        "etoro_live_planner_not_buy_now",
        "etoro_live_backtest_check_unknown",
        "etoro_live_backtest_check_failed",
        "etoro_live_evidence_streak_unknown",
        "etoro_live_evidence_streak_too_short",
        "etoro_live_extension_unknown",
        "etoro_live_extended_20d_rise",
        "etoro_live_extended_rsi",
        "etoro_live_extended_new_high",
    }
)
PAPER_ONLY_REASON = "etoro_live_paper_only"  # a tagged paper-only order; not a room reason
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


def planner_blocker(metadata: dict[str, Any]) -> str | None:
    """Rule 1: copy only what the trade planner says to buy now."""

    plan = metadata.get("trade_plan")
    label = plan.get("timing_label") if isinstance(plan, dict) else None
    if not isinstance(label, str) or not label.strip():
        return "etoro_live_planner_unknown"
    if label.strip().lower() not in MIRROR_BUY_NOW_TIMING_LABELS:
        return "etoro_live_planner_not_buy_now"
    return None


def backtest_check_blocker(metadata: dict[str, Any]) -> str | None:
    """Rule 2: copy only a signal whose own (per-signal) backtest check passed."""

    validated = metadata.get("backtest_validated")
    if validated is True:
        return None
    if validated is False:
        return "etoro_live_backtest_check_failed"
    return "etoro_live_backtest_check_unknown"  # missing, or not a plain True/False


def evidence_streak_blocker(hours: float | None) -> str | None:
    """Rule 3: the pooled evidence passed for MIRROR_MIN_EVIDENCE_STREAK_HOURS unbroken."""

    if hours is None or not math.isfinite(hours):
        return "etoro_live_evidence_streak_unknown"
    return (
        "etoro_live_evidence_streak_too_short" if hours < MIRROR_MIN_EVIDENCE_STREAK_HOURS else None
    )


def extension_blockers(extension: Any, timeframe: Any) -> list[str]:
    """Rule 4: no copy of a move that has already run (inputs: app/screener/extension.py)."""

    if (
        not isinstance(extension, dict)
        or str(timeframe or "").lower() != "1d"
        or extension.get("timeframe") != "1d"
    ):
        return ["etoro_live_extension_unknown"]
    rise, rsi = _number(extension.get("return_20d_pct")), _number(extension.get("rsi_14"))
    new_high = extension.get("new_high_252_recent")
    reasons = []
    if rise is None or rsi is None or not isinstance(new_high, bool):
        reasons.append("etoro_live_extension_unknown")
    if rise is not None and rise > HARD_MAX_RETURN_20D_PCT:
        reasons.append("etoro_live_extended_20d_rise")
    if rsi is not None and rsi >= HARD_MAX_RSI_14:
        reasons.append("etoro_live_extended_rsi")
    if new_high is True:
        reasons.append("etoro_live_extended_new_high")
    return reasons


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


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
        from app.performance.strategy_evidence import (
            EDGE_BELOW_BAR,
            VERDICTS_KEY,
            evidence_blocker,
            passing_hours,
        )
        from app.screener.extension import EXTENSION_FIELD

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
        from app.performance.paper_only import is_paper_only

        # Option B (2026-10-21): an entry admitted on paper's OOS bar alone is never copied,
        # whatever the other checks say. Not a room reason: paper's own room rules decide it.
        if is_paper_only(getattr(order, "metadata", None)):
            reasons.append(PAPER_ONLY_REASON)
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
        else:
            cached = self.state.get(VERDICTS_KEY)  # one read: the gate and the streak agree
            if evidence := evidence_blocker(
                strict,
                _OneKey(VERDICTS_KEY, cached),
                strategy=str(order.strategy_name or ""),
                timeframe=str(timeframe),
                min_expectancy_r=MIRROR_MIN_EXPECTANCY_R,
            ):
                reasons.append(
                    evidence if evidence == EDGE_BELOW_BAR else "strategy_lacks_oos_evidence"
                )
            hours = passing_hours(
                cached, strategy=str(order.strategy_name or ""), timeframe=str(timeframe)
            )
            if streak := evidence_streak_blocker(hours):
                reasons.append(streak)
        # Rules 1, 2 and 4 read the scan's metadata (the order's own wins over the signal's).
        copy_inputs = {**metadata, **(getattr(order, "metadata", None) or {})}
        for blocker in (planner_blocker(copy_inputs), backtest_check_blocker(copy_inputs)):
            if blocker:
                reasons.append(blocker)
        reasons.extend(extension_blockers(copy_inputs.get(EXTENSION_FIELD), timeframe))
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


class _OneKey:
    """A read-only runtime_state holding one value already read (no second database read)."""

    def __init__(self, key: str, value: Any) -> None:
        self._key, self._value = key, value

    def get(self, key: str, default: Any = None) -> Any:
        return self._value if key == self._key else default
