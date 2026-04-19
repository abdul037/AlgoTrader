"""Simple synchronous scheduler scaffold."""

from __future__ import annotations

from app.data.market_data import MarketDataService
from app.execution.trader import TraderService
from app.strategies import get_strategy


class StrategyScheduler:
    """Run a single strategy scan and create a proposal when a buy signal appears."""

    def __init__(self, market_data: MarketDataService, trader: TraderService):
        self.market_data = market_data
        self.trader = trader

    def scan_once(
        self,
        *,
        symbol: str,
        strategy_name: str,
        file_path: str,
        amount_usd: float | None = None,
        leverage: int = 1,
    ):
        data = self.market_data.load_csv(file_path)
        strategy = get_strategy(strategy_name)
        signal = strategy.generate_signal(data, symbol)
        if signal is None or signal.action.value != "buy":
            return None
        return self.trader.propose_from_signal(signal, amount_usd=amount_usd, leverage=leverage)
