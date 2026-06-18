"""Run the PostgreSQL-backed continuous-learning worker."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> int:
    app = create_app(enable_background_jobs=False)
    settings = app.state.settings
    service = app.state.learning_service
    if not settings.learning_worker_enabled:
        raise RuntimeError("LEARNING_WORKER_ENABLED must be true for the learning worker")

    while True:
        try:
            service.schedule_due_jobs()
            completed = service.process_jobs(limit=20)
            if completed:
                logger.info("Processed %s learning jobs", len(completed))
        except Exception:
            logger.exception("Learning worker cycle failed")
        time.sleep(max(int(settings.learning_worker_poll_seconds), 5))


if __name__ == "__main__":
    raise SystemExit(main())
