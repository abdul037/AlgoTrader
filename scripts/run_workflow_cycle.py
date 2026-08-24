"""Run one workflow cycle manually or from an external cron scheduler.

This builds the FULL application wiring via ``create_app`` (with background jobs
disabled) so a single cron-driven cycle has the same proposal, automation, and
auto-trading services the long-running process has. A previous version wired a
partial ``SignalWorkflowService`` by hand, without those services, so the
``scheduled`` task could scan and alert but could never propose or execute --
an "autonomy-blind" cron path. Driving the real wiring keeps cron behaviour in
lockstep with the in-process scheduler worker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402 - import after sys.path bootstrap


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one workflow cycle.")
    parser.add_argument(
        "--task",
        choices=["scheduled", "swing", "intraday", "open-check", "daily-summary"],
        default="scheduled",
    )
    parser.add_argument("--no-notify", action="store_true", help="Do not send Telegram messages.")
    args = parser.parse_args()

    # Full production wiring, but do not spawn the background scheduler thread --
    # this is a single-shot cycle.
    app = create_app(enable_background_jobs=False)
    workflow = app.state.workflow_service

    notify = not args.no_notify
    if args.task == "scheduled":
        # Drive the exact same jobs the in-process worker runs, once. This
        # covers scans, proposals, queue processing, reconciliation, and paper
        # position refresh -- the real autonomous cadence, not scans only.
        worker = app.state.build_scheduler_worker()
        ran = worker.run_due_jobs()
        result = {"ran_jobs": ran, "worker": worker.status()}
    elif args.task == "swing":
        result = workflow.run_swing_scan(notify=notify, force_refresh=True).model_dump()
    elif args.task == "intraday":
        result = workflow.run_intraday_scan(notify=notify, force_refresh=True).model_dump()
    elif args.task == "open-check":
        result = workflow.check_open_signals(notify=notify, force_refresh=True).model_dump()
    else:
        result = workflow.send_daily_summary(notify=notify).model_dump()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
