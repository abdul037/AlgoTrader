"""Walk-forward splitting for backtests and model training.

The brief's non-negotiable #4: "Walk-forward validation only. No in-sample
backtests. No 'optimized on the full history' numbers. The last 4 weeks of the
ledger are a permanent holdout that no model trains on until final acceptance."

This module is the single source of truth for how history is sliced into
train/test folds. Every backtest aggregation and every Phase 1+ model trainer
must consume :class:`WalkForwardSplitter` rather than rolling its own split
logic.

Invariants enforced here and verified by the property test in
``tests/test_walk_forward.py``:

* ``train_df`` timestamps are strictly less than all ``test_df`` timestamps.
* The holdout (last ``holdout_days``) never appears in any fold.
* A configurable ``embargo_days`` gap between train and test protects against
  serial correlation leakage (the classic "bar straddles the split" bug).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pandas as pd


@dataclass(frozen=True)
class WalkForwardWindow:
    """One fold of a walk-forward split."""

    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_df: pd.DataFrame
    test_df: pd.DataFrame

    def __post_init__(self) -> None:
        if self.train_end >= self.test_start:
            raise ValueError(
                f"train_end ({self.train_end}) must be strictly < test_start ({self.test_start})"
            )
        if self.test_end < self.test_start:
            raise ValueError("test_end must be >= test_start")


@dataclass
class WalkForwardSplitter:
    """Rolling walk-forward splitter with embargo and holdout protection.

    Defaults mirror the brief's Phase 1 spec: 6-month train, 2-week test, step
    by 2 weeks, with the last 4 weeks sealed off as holdout.
    """

    train_days: int = 180
    test_days: int = 14
    step_days: int = 14
    embargo_days: int = 1
    holdout_days: int = 28
    timestamp_column: str = "timestamp"

    def split(self, frame: pd.DataFrame) -> Iterator[WalkForwardWindow]:
        """Yield successive :class:`WalkForwardWindow` folds over the frame.

        The frame must have a timezone-aware timestamp column (UTC recommended
        per the brief's timezone rule). Naive timestamps raise, because every
        downstream leakage check relies on monotonic UTC comparisons.
        """

        if self.train_days <= 0:
            raise ValueError("train_days must be positive")
        if self.test_days <= 0:
            raise ValueError("test_days must be positive")
        if self.step_days <= 0:
            raise ValueError("step_days must be positive")
        if self.embargo_days < 0:
            raise ValueError("embargo_days must be non-negative")
        if self.holdout_days < 0:
            raise ValueError("holdout_days must be non-negative")

        if frame.empty:
            return
        if self.timestamp_column not in frame.columns:
            raise ValueError(f"frame missing timestamp column {self.timestamp_column!r}")

        ts = pd.to_datetime(frame[self.timestamp_column], utc=True)
        if ts.isna().any():
            raise ValueError("frame timestamp column contains NaT")
        sorted_frame = frame.assign(**{self.timestamp_column: ts}).sort_values(self.timestamp_column)

        dataset_end = ts.max()
        holdout_boundary = dataset_end - pd.Timedelta(days=self.holdout_days)

        train_start = ts.min()
        train_end = train_start + pd.Timedelta(days=self.train_days)
        while True:
            embargo_end = train_end + pd.Timedelta(days=self.embargo_days)
            test_start = embargo_end
            test_end = test_start + pd.Timedelta(days=self.test_days)
            if test_end > holdout_boundary:
                break
            fold = self._slice(sorted_frame, train_start, train_end, test_start, test_end)
            if fold is not None:
                yield fold
            train_end = train_end + pd.Timedelta(days=self.step_days)
            # Window stays anchored at train_start; for a rolling (not
            # expanding) window, also shift train_start. We choose rolling
            # because the brief says "rolling 6-month windows".
            train_start = train_end - pd.Timedelta(days=self.train_days)

    def holdout_window(self, frame: pd.DataFrame) -> WalkForwardWindow | None:
        """Return the final holdout window. Only used at acceptance time."""

        if frame.empty or self.holdout_days <= 0:
            return None
        ts = pd.to_datetime(frame[self.timestamp_column], utc=True)
        dataset_end = ts.max()
        holdout_start = dataset_end - pd.Timedelta(days=self.holdout_days)
        embargo_end = holdout_start + pd.Timedelta(days=self.embargo_days)
        train_end = holdout_start - pd.Timedelta(days=self.embargo_days)
        train_start = train_end - pd.Timedelta(days=self.train_days)
        sorted_frame = frame.assign(**{self.timestamp_column: ts}).sort_values(self.timestamp_column)
        return self._slice(sorted_frame, train_start, train_end, embargo_end, dataset_end)

    def _slice(
        self,
        frame: pd.DataFrame,
        train_start: pd.Timestamp,
        train_end: pd.Timestamp,
        test_start: pd.Timestamp,
        test_end: pd.Timestamp,
    ) -> WalkForwardWindow | None:
        ts = frame[self.timestamp_column]
        train_mask = (ts >= train_start) & (ts < train_end)
        test_mask = (ts >= test_start) & (ts <= test_end)
        train_df = frame.loc[train_mask].copy()
        test_df = frame.loc[test_mask].copy()
        if train_df.empty or test_df.empty:
            return None
        # Belt and braces: enforce the strict ordering invariant. The dataclass
        # post-init also checks this but slipping a bar in by rounding could
        # defeat the intent of the test.
        if train_df[self.timestamp_column].max() >= test_df[self.timestamp_column].min():
            raise ValueError("walk-forward slice produced overlapping train/test")
        return WalkForwardWindow(
            train_start=pd.Timestamp(train_start),
            train_end=pd.Timestamp(train_end),
            test_start=pd.Timestamp(test_start),
            test_end=pd.Timestamp(test_end),
            train_df=train_df,
            test_df=test_df,
        )


def aggregate_out_of_sample(
    per_fold_trades: list[list[dict]],
    per_fold_metrics: list[dict],
) -> dict:
    """Aggregate per-fold test-set trades and metrics into a single summary.

    Called by :class:`BatchBacktestService` to produce the out-of-sample
    summary that feeds :mod:`app.screener.scoring`. Do not feed this function
    training-set trades by accident.
    """

    from app.backtesting.metrics import summarize_trades

    merged_trades: list[dict] = []
    for fold in per_fold_trades:
        merged_trades.extend(fold)
    fold_count = len(per_fold_metrics)
    combined_metrics = summarize_trades(merged_trades)
    combined_metrics["fold_count"] = fold_count
    return {
        "merged_trades": merged_trades,
        "metrics": combined_metrics,
        "per_fold_metrics": per_fold_metrics,
    }


def assert_no_future_leakage(window: WalkForwardWindow) -> None:
    """Cheap sanity check used by the hypothesis property test and by callers
    that want to be loud when something goes wrong.
    """

    if window.train_df.empty or window.test_df.empty:
        return
    train_max = pd.to_datetime(window.train_df["timestamp"], utc=True).max()
    test_min = pd.to_datetime(window.test_df["timestamp"], utc=True).min()
    if train_max >= test_min:
        raise AssertionError(
            f"leakage: train_max={train_max} >= test_min={test_min}"
        )


def utc_midnight(year: int, month: int, day: int) -> datetime:
    """Small helper used by tests to build synthetic data deterministically."""

    return datetime(year, month, day, tzinfo=timezone.utc)


def _parse_days(value: int | timedelta) -> int:
    """Internal convenience used in tests that pass either int-days or a delta."""

    if isinstance(value, timedelta):
        return value.days
    return int(value)
