"""Small support helpers for the universe scan orchestrator.

Timeout-bounded dependency calls and strategy-spec selection, factored out of
``service_scan`` so the orchestrator reads as a single flow. Pure helpers over
the passed ``service``; no module state.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any


class ScanTimeoutError(RuntimeError):
    """Raised when a bounded scan subtask exceeds its configured timeout."""


def _bounded_call(label: str, timeout_seconds: float, func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one blocking scan dependency behind a small timeout."""

    timeout = float(timeout_seconds or 0.0)
    if timeout <= 0:
        return func(*args, **kwargs)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener-timeout")
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise ScanTimeoutError(f"{label}_timeout_after_{timeout:g}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _normalize_spec_keys(strategy_spec_keys: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
    return {str(item).strip().lower() for item in strategy_spec_keys or [] if str(item).strip()}


def _spec_key(spec: Any) -> str:
    return f"{str(getattr(spec, 'name', '')).strip().lower()}:{str(getattr(spec, 'timeframe', '')).strip().lower()}"


def _strategy_specs_for_timeframe(service: Any, timeframe: str, requested_spec_keys: set[str]) -> list[Any]:
    try:
        return list(service._strategy_specs_for_timeframe(timeframe, strategy_spec_keys=requested_spec_keys or None))
    except TypeError:
        specs = list(service._strategy_specs_for_timeframe(timeframe))
        if requested_spec_keys:
            specs = [spec for spec in specs if _spec_key(spec) in requested_spec_keys]
        return specs

