"""The production qualification audit must deflate the observed Sharpe for the
number of strategies tested, not pass the raw Sharpe straight through."""

from __future__ import annotations

from app.strategies.qualification_routes import _deflated_sharpe_for_ranking


def test_deflated_sharpe_is_a_probability_below_the_raw_sharpe() -> None:
    ranking = {"average_sharpe_like": 1.5, "total_trades": 250}
    dsr = _deflated_sharpe_for_ranking(ranking, n_trials=20)
    # DSR is a confidence in [0, 1], never the raw 1.5 that used to be passed.
    assert 0.0 <= dsr <= 1.0


def test_more_trials_deflates_confidence() -> None:
    ranking = {"average_sharpe_like": 1.2, "total_trades": 200}
    few = _deflated_sharpe_for_ranking(ranking, n_trials=2)
    many = _deflated_sharpe_for_ranking(ranking, n_trials=200)
    # Testing more strategies makes any single winner less trustworthy.
    assert many <= few


def test_non_positive_or_thin_samples_return_zero() -> None:
    assert _deflated_sharpe_for_ranking({"average_sharpe_like": 0.0, "total_trades": 100}, n_trials=5) == 0.0
    assert _deflated_sharpe_for_ranking({"average_sharpe_like": 2.0, "total_trades": 1}, n_trials=5) == 0.0


def test_raw_high_sharpe_no_longer_trivially_clears_095_gate() -> None:
    # A single-trial, few-observation "great" Sharpe used to be written as 2.0
    # and sail past the 0.95 production gate. Deflated, thin evidence is punished.
    ranking = {"average_sharpe_like": 2.0, "total_trades": 5}
    dsr = _deflated_sharpe_for_ranking(ranking, n_trials=50)
    assert dsr < 0.95
