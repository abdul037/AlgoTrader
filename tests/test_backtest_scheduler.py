from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.main import create_app
from tests.conftest import make_settings


def _job_names(app):
    return [job.name for job in app.state.build_scheduler_worker().jobs]


def test_backtest_job_absent_by_default(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), enable_background_jobs=False)
    assert "backtest_gate_refresh" not in _job_names(app)


def test_backtest_job_registered_when_enabled(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(tmp_path, backtest_scheduler_enabled=True),
        enable_background_jobs=False,
    )
    assert "backtest_gate_refresh" in _job_names(app)


def test_backtest_job_calls_service_with_walk_forward(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(
            tmp_path,
            backtest_scheduler_enabled=True,
            backtest_scheduler_symbol_limit=5,
        ),
        enable_background_jobs=False,
    )
    captured: dict = {}
    app.state.batch_backtest_service.run = lambda **kwargs: captured.update(kwargs)

    worker = app.state.build_scheduler_worker()
    job = next(j for j in worker.jobs if j.name == "backtest_gate_refresh")
    job.func()

    assert captured["walk_forward"] is True
    assert captured["timeframes"] == ["1d"]
    assert captured["limit"] == 5


def test_backtest_job_full_universe_when_limit_zero(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(tmp_path, backtest_scheduler_enabled=True),
        enable_background_jobs=False,
    )
    captured: dict = {}
    app.state.batch_backtest_service.run = lambda **kwargs: captured.update(kwargs)

    worker = app.state.build_scheduler_worker()
    next(j for j in worker.jobs if j.name == "backtest_gate_refresh").func()

    assert captured["limit"] is None  # 0 -> None -> full universe


def test_backtest_job_passes_soft_deadline(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(
            tmp_path,
            backtest_scheduler_enabled=True,
            backtest_scheduler_deadline_seconds=123.0,
        ),
        enable_background_jobs=False,
    )
    captured: dict = {}
    app.state.batch_backtest_service.run = lambda **kwargs: captured.update(kwargs)

    worker = app.state.build_scheduler_worker()
    next(j for j in worker.jobs if j.name == "backtest_gate_refresh").func()

    # The soft budget must stay under the hard scheduler job cap so the job
    # finishes cleanly instead of being killed at the wall-clock timeout.
    assert captured["deadline_seconds"] == 123.0


def test_backtest_job_rotates_cursor_across_runs(tmp_path: Path) -> None:
    # Each scheduled run must advance a persisted cursor by the number of
    # symbols it covered, so consecutive runs sweep the universe rather than
    # re-backtesting the same leading symbols forever.
    app = create_app(
        settings=make_settings(
            tmp_path,
            backtest_scheduler_enabled=True,
            backtest_scheduler_symbol_limit=50,
        ),
        enable_background_jobs=False,
    )
    offsets: list[int] = []

    def fake_run(**kwargs):
        offsets.append(kwargs.get("start_offset"))
        return SimpleNamespace(symbols_evaluated=3)

    app.state.batch_backtest_service.run = fake_run

    worker = app.state.build_scheduler_worker()
    job = next(j for j in worker.jobs if j.name == "backtest_gate_refresh")
    job.func()
    job.func()

    assert offsets == [0, 3]  # first run starts at 0; cursor advances by coverage
