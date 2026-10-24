"""A separate Alpaca PAPER learning account (operator option A, approved 2026-10-20/21).

What it trades: candidates the MAIN path refuses ONLY with ``['strategy_lacks_oos_evidence']``
(its own ``candidate_proposal_blockers``, asked on the learning thread) whose key is

* ``fee_band``: the pooled evidence passes WITHOUT eToro's fees but not with them, or
* ``lane``: it fails both, with >= 30 pooled OOS trades and a fee-free pooled OOS expectancy
  of at least -0.05R; fee-band first, then the nearest miss.

1m-15m keys are judged on the Stage 1 Alpaca cost profile instead of fee-free. While option B's
paper OOS bar is on, a key the main gate admits through it is the main path's and never reaches
this account (app/learning_account/verdicts.py). Only strict-valid, execution-ready,
alert-eligible longs above the auto score floor with ``metadata.backtest_validated``; never crypto.

Caps (code constants, app/learning_account/account.py): 3 new entries a New York trading day
(from the durable book, closed and unreported trades included), 6 entry orders a day, 4 open,
never two positions in one symbol (book and broker), half the normal paper risk size on the
learning account's OWN equity. The main kill switch or pause stops new learning entries (and an
unavailable automation service does too) but never flattens the learning account.

Separation. It is a second, independent client (app/learning_account/client.py), fixed to Alpaca
paper: never in the broker router, never the eToro mirror, never the main reconciliation, kill
switch or emergency flatten. Its records are runtime_state keys only
(app/learning_account/store.py) and its run logs are tagged ``learning_account: true``. Its trades
never reach the app's learning (ML) tables, strategy demotion or health, go-live readiness,
Phase 3, the eToro streak or any main limit.

Market data, stated plainly: its quotes and the market-direction bars are read read-only from
the MAIN account's data client (the shared market-data engine's Alpaca provider,
app/learning_account/market.py).

The learning account can hold a symbol the main account also holds (another key, an option B
paper-only entry, or a verdict that flipped between the two accounts' reads). Neither reaches
eToro: the live mirror and the exit copy read the main paper account only, so a learning
position or exit is never copied.

Residual risks (stated): if the route is disabled or the keys removed while positions are open,
nothing reconciles them: they keep their GTC brackets, intraday ones are not flattened; close
them on the Alpaca dashboard. A reconcile pass that loses the book race after a leg cancel or a
close redoes it next pass (a lost close id is looked up first), and can rarely record the trade
as ``closed_outside``.

The thread runs only in the process that runs the cadence (background jobs on, not Telegram
polling); in polling mode scripts/run_telegram_bot.py starts it (``run_learning_account_here``).
"""

from __future__ import annotations

from typing import Any


def attach(app: Any) -> Any:
    """Build the learning account for ``create_app`` when its settings configure it, hand it to
    the workflow service (``offer``) and start it with the cadence. Inert otherwise."""

    from app.learning_account.account import LearningAccount
    from app.learning_account.route import PENDING, settings_route

    state = app.state
    state.learning_account = None
    if settings_route(state.settings) != PENDING:
        return None
    account = LearningAccount(
        settings=state.settings,
        db=state.db,
        run_logs=state.run_log_repository,
        notifier=state.telegram_notifier,
        market_data_engine=state.market_data_engine,
        auto_trading=state.auto_trading_service,
        automation=state.automation_service,
    )
    state.learning_account = account
    state.workflow_service.learning_account = account

    def start_with_the_cadence() -> None:  # after create_app's own startup: no I/O here
        if getattr(state, "scheduler_worker", None) is not None:
            account.start()

    app.add_event_handler("startup", start_with_the_cadence)
    app.add_event_handler("shutdown", account.stop)
    return account


def offer(service: Any, response: Any) -> int:
    """The main path's one call (the end of ``run_scan_task``): copy the scan's candidates."""

    account = getattr(service, "learning_account", None)
    if account is None:
        return 0
    return account.offer(list(getattr(response, "candidates", None) or []), getattr(response, "generated_at", None))


def run_learning_account_here(app: Any) -> Any:
    """Polling mode: start the learning thread in this process; returns the stop call."""

    account = getattr(app.state, "learning_account", None)
    if account is None:
        return lambda: None
    account.start()
    return account.stop
