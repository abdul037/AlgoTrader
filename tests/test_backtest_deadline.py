"""The batch backtester must bound each run by a wall-clock deadline and rotate
its start offset, so the scheduled full-universe pass stops cleanly under the
scheduler's hard job cap (instead of being killed) and successive runs sweep the
whole universe instead of only its leading symbols."""

from __future__ import annotations

from types import SimpleNamespace

from app.backtesting.batch import BatchBacktestService


def _service(get_history):
    return BatchBacktestService(
        settings=SimpleNamespace(
            primary_market_data_provider="test", max_risk_per_trade_pct=1.0
        ),
        market_data_engine=SimpleNamespace(get_history=get_history),
        backtest_repository=None,
        run_log_repository=SimpleNamespace(log=lambda *a, **k: None),
    )


def _raise(_symbol=None, **_kwargs):
    # Skip the (heavy) per-symbol backtest; the deadline/rotation bookkeeping
    # runs before get_history and is what we're exercising here.
    raise RuntimeError("skip heavy backtest")


def test_run_stops_at_deadline_and_reports_partial_coverage(monkeypatch) -> None:
    # Clock: started_at, then one check per symbol. Trips on the third symbol.
    clock = iter([0.0, 0.0, 0.0, 100.0])
    monkeypatch.setattr("app.backtesting.batch.time.monotonic", lambda: next(clock))

    summary = _service(_raise).run(
        symbols=["A", "B", "C", "D"], timeframes=["1d"], deadline_seconds=50.0
    )

    # Covered the first two symbols, then broke before touching C/D.
    assert summary.symbols_evaluated == 2
    assert "backtest_deadline_exceeded" in summary.errors


def test_run_without_deadline_covers_whole_universe() -> None:
    summary = _service(_raise).run(symbols=["A", "B", "C", "D"], timeframes=["1d"])

    assert summary.symbols_evaluated == 4
    assert "backtest_deadline_exceeded" not in summary.errors


def test_run_deadline_trips_inside_a_single_symbol(monkeypatch) -> None:
    # The production failure: one symbol's full strategy sweep exceeds the budget.
    # The top-of-symbol check alone can't catch that (it only runs between
    # symbols), so the job was hard-killed mid-symbol and never advanced. The
    # in-loop check must bail mid-symbol, still count the symbol, and return.
    from app.backtesting import batch as batch_mod

    monkeypatch.setattr(
        batch_mod,
        "strategy_specs_for",
        lambda settings, timeframe, requested: [
            SimpleNamespace(name="s1"),
            SimpleNamespace(name="s2"),
            SimpleNamespace(name="s3"),
        ],
    )
    monkeypatch.setattr(batch_mod, "strategy_kwargs_for", lambda settings, spec: {})
    monkeypatch.setattr(batch_mod, "get_strategy", lambda name, **kw: SimpleNamespace())
    monkeypatch.setattr(batch_mod, "leakage_tripwire_triggered", lambda summary: (False, ""))
    # started_at, symbol-top, s1, s2 all under budget; s3 trips.
    clock = iter([0.0, 0.0, 0.0, 0.0, 100.0])
    monkeypatch.setattr(batch_mod.time, "monotonic", lambda: next(clock))

    history = SimpleNamespace(copy=lambda: SimpleNamespace())
    service = BatchBacktestService(
        settings=SimpleNamespace(primary_market_data_provider="test", max_risk_per_trade_pct=1.0),
        market_data_engine=SimpleNamespace(get_history=lambda *a, **k: history),
        backtest_repository=None,
        run_log_repository=SimpleNamespace(log=lambda *a, **k: None),
    )
    monkeypatch.setattr(
        service,
        "_run_strategy",
        lambda **kw: {"strategy_name": "s", "annualized_return_pct": 0.0},
    )

    summary = service.run(symbols=["ONLY"], timeframes=["1d"], deadline_seconds=50.0)

    assert summary.symbols_evaluated == 1  # the symbol is still counted (cursor advances)
    assert summary.strategy_runs == 2  # s1, s2 ran; bailed before s3
    assert "backtest_deadline_exceeded" in summary.errors


def test_run_rotates_universe_by_start_offset() -> None:
    seen: list[str] = []

    def record(symbol, **_kwargs):
        seen.append(symbol)
        raise RuntimeError("skip heavy backtest")

    _service(record).run(symbols=["A", "B", "C", "D"], timeframes=["1d"], start_offset=2)

    # Start offset rotates the processing order without dropping any symbol.
    assert seen == ["C", "D", "A", "B"]


def test_run_start_offset_wraps_modulo_universe_size() -> None:
    seen: list[str] = []

    def record(symbol, **_kwargs):
        seen.append(symbol)
        raise RuntimeError("skip heavy backtest")

    # offset 6 over 4 symbols -> effective offset 2.
    _service(record).run(symbols=["A", "B", "C", "D"], timeframes=["1d"], start_offset=6)

    assert seen == ["C", "D", "A", "B"]


def _unit_service(monkeypatch, clock_values):
    from app.backtesting import batch as batch_mod

    monkeypatch.setattr(
        batch_mod,
        "strategy_specs_for",
        lambda settings, timeframe, requested: [SimpleNamespace(name=f"{timeframe}-s{i}") for i in (1, 2, 3)],
    )
    monkeypatch.setattr(batch_mod, "strategy_kwargs_for", lambda settings, spec: {})
    monkeypatch.setattr(batch_mod, "get_strategy", lambda name, **kw: SimpleNamespace(name=name))
    monkeypatch.setattr(batch_mod, "leakage_tripwire_triggered", lambda summary: (False, ""))
    clock = iter(clock_values)
    monkeypatch.setattr(batch_mod.time, "monotonic", lambda: next(clock, 1e9))
    ran: list[str] = []
    service = BatchBacktestService(
        settings=SimpleNamespace(primary_market_data_provider="test", max_risk_per_trade_pct=1.0),
        market_data_engine=SimpleNamespace(get_history=lambda *a, **k: SimpleNamespace(copy=lambda: None)),
        backtest_repository=None,
        run_log_repository=SimpleNamespace(log=lambda *a, **k: None),
    )

    def fake_run(**kw):
        ran.append(kw["strategy"].name)
        return {"strategy_name": kw["strategy"].name, "annualized_return_pct": 0.0}

    monkeypatch.setattr(service, "_run_strategy", fake_run)
    return service, ran


def test_mid_symbol_stop_reports_resume_point_and_resumes_there(monkeypatch) -> None:
    # 2026-10-03: one symbol's intraday sweep (strategies x variants) exceeds a
    # pass's deadline; advancing by whole symbols would never reach its later
    # strategies. Pass 1: started_at, symbol-top, then units 1-4 under budget, 5th trips.
    service, ran = _unit_service(monkeypatch, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0])
    first = service.run(symbols=["ONLY"], timeframes=["1d", "5m"], deadline_seconds=50.0)
    assert ran == ["1d-s1", "1d-s2", "1d-s3", "5m-s1"]
    assert first.stopped_mid_symbol and first.resume_unit == 4

    service, ran = _unit_service(monkeypatch, [0.0] * 20)
    second = service.run(symbols=["ONLY"], timeframes=["1d", "5m"], deadline_seconds=50.0, start_unit=4)
    assert ran == ["5m-s2", "5m-s3"]
    assert not second.stopped_mid_symbol


def test_stop_before_first_unit_is_still_mid_symbol(monkeypatch) -> None:
    service, ran = _unit_service(monkeypatch, [0.0, 0.0, 100.0])
    summary = service.run(symbols=["ONLY"], timeframes=["5m"], deadline_seconds=50.0)
    assert ran == [] and summary.stopped_mid_symbol and summary.resume_unit == 0
