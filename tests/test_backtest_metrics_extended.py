import pytest

from app.backtesting.metrics import calmar, deflated_sharpe, expectancy_R, sortino


def test_sortino_zero_when_no_downside():
    assert sortino([0.01, 0.02, 0.005]) == 0.0


def test_sortino_positive_when_mean_positive_with_downside():
    result = sortino([0.01, -0.005, 0.02, -0.01, 0.015])
    assert result > 0.0


def test_calmar_positive_for_growing_curve_with_drawdown():
    returns = [0.01, -0.005, 0.02, -0.01, 0.015]
    equity = [10000, 10100, 10049, 10250, 10148, 10300]
    assert calmar(returns, equity) > 0.0


def test_calmar_zero_for_zero_drawdown():
    returns = [0.01, 0.02, 0.005]
    equity = [10000, 10100, 10302, 10353]
    assert calmar(returns, equity) == 0.0


def test_expectancy_R_positive_for_winning_trades():
    trades = [{"pnl_usd": 100}, {"pnl_usd": -50}, {"pnl_usd": 200}]
    # mean pnl = 250/3 ~= 83.33; risk = 100; expectancy_R = 0.8333
    assert expectancy_R(trades, 100.0) == pytest.approx(250.0 / 300.0, rel=1e-9)


def test_expectancy_R_zero_for_no_trades():
    assert expectancy_R([], 100.0) == 0.0


def test_deflated_sharpe_n_trials_one_no_deflation():
    # n_trials=1, SR=0.1, n=252, normal returns
    # numerator = 0.1 x sqrt(251) ~= 1.5843
    # denominator = sqrt(1 + 0.5 x 0.01) = sqrt(1.005) ~= 1.00249
    # result ~= Phi(1.5803) ~= 0.943
    result = deflated_sharpe(0.1, n_trials=1, n_observations=252)
    assert 0.93 < result < 0.95


def test_deflated_sharpe_high_n_trials_penalizes():
    single = deflated_sharpe(1.0, n_trials=1, n_observations=252)
    many = deflated_sharpe(1.0, n_trials=14, n_observations=252)
    assert many < single
    assert single - many > 0.0001


def test_deflated_sharpe_handles_small_n_observations():
    assert deflated_sharpe(1.0, n_trials=14, n_observations=1) == 0.0
