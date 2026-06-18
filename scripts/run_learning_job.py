"""Run one continuous-learning cron task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one governed learning task.")
    parser.add_argument(
        "task",
        choices=["counterfactual", "nightly", "weekly", "digest", "process"],
    )
    args = parser.parse_args()

    app = create_app(enable_background_jobs=False)
    service = app.state.learning_service
    models = app.state.learning_model_service
    result: object
    if args.task == "counterfactual":
        result = {"created": service.generate_counterfactuals(limit=1000)}
    elif args.task == "nightly":
        result = models.train_challenger().model_dump()
    elif args.task == "weekly":
        evaluations = []
        for model in service.repository.list_models(limit=100):
            if model.status == "challenger":
                evaluations.append(models.evaluate_challenger(model.id).model_dump())
        result = {"evaluations": evaluations, "synthesis": service.weekly_synthesis()}
    elif args.task == "digest":
        message = service.daily_digest()
        app.state.telegram_notifier.send_text(message)
        result = {"sent": True, "message": message}
    else:
        service.schedule_due_jobs()
        result = {"jobs": [item.model_dump() for item in service.process_jobs(limit=100)]}
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
