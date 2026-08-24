from __future__ import annotations

from pathlib import Path

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
