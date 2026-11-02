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


def test_short_research_runs_only_after_the_long_pass_and_its_cursor(tmp_path: Path) -> None:
    # Short research (step 1) shares the job but must never delay, shrink or replace the
    # long pass: the long run keeps its deadline and its cursor is saved before shorts start.
    app = create_app(
        settings=make_settings(
            tmp_path,
            backtest_scheduler_enabled=True,
            backtest_scheduler_symbol_limit=50,
            backtest_scheduler_deadline_seconds=123.0,
            backtest_short_research_enabled=True,
        ),
        enable_background_jobs=False,
    )
    from app.storage.repositories import RuntimeStateRepository

    state = RuntimeStateRepository(app.state.db)
    service = app.state.batch_backtest_service
    order: list[tuple] = []

    def fake_run(**kwargs):
        order.append(("long", kwargs["deadline_seconds"]))
        return SimpleNamespace(symbols_evaluated=4, stopped_mid_symbol=True, resume_unit=7)

    def fake_short(runtime_state):
        cursor = runtime_state.get("backtest_gate_refresh:cursor")
        unit = runtime_state.get("backtest_gate_refresh:unit")
        order.append(("short", cursor, unit))

    service.run = fake_run
    service.run_short_research = fake_short
    job = next(j for j in app.state.build_scheduler_worker().jobs if j.name == "backtest_gate_refresh")
    job.func()

    assert order == [("long", 123.0), ("short", "3", "7")]
    assert state.get("backtest_gate_refresh:cursor") == "3"


def test_a_short_research_failure_never_fails_the_backtest_job(tmp_path: Path, monkeypatch) -> None:
    import time

    from app.backtesting import short_research

    app = create_app(
        settings=make_settings(tmp_path, backtest_scheduler_enabled=True, backtest_short_research_enabled=True),
        enable_background_jobs=False,
    )
    service = app.state.batch_backtest_service

    def fake_run(**kwargs):
        service._pass_started.value = time.monotonic()  # what the real long pass records
        return SimpleNamespace(symbols_evaluated=1)

    def boom(*args, **kwargs):
        raise RuntimeError("short research bug")

    service.run = fake_run
    monkeypatch.setattr(short_research, "run_short_pass", boom)
    job = next(j for j in app.state.build_scheduler_worker().jobs if j.name == "backtest_gate_refresh")
    job.func()  # does not raise

    with app.state.db.connect() as connection:
        events = [row["event_type"] for row in connection.execute("SELECT event_type FROM run_logs").fetchall()]
    assert "backtest_short_research_failed" in events
