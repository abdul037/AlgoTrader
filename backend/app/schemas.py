from typing import Literal

from pydantic import BaseModel, Field


SupportedInterval = Literal["1min", "5min", "15min", "30min", "60min", "daily"]
SignalDirection = Literal["buy", "sell", "hold"]
SignalStance = Literal["bullish", "bearish", "neutral"]
DataMode = Literal["live", "delayed", "demo"]


class PriceBar(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class IndicatorSnapshot(BaseModel):
    sma20: float | None = None
    sma50: float | None = None
    ema12: float | None = None
    ema26: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = Field(default=None, alias="macdSignal")
    macd_histogram: float | None = Field(default=None, alias="macdHistogram")
    bollinger_upper: float | None = Field(default=None, alias="bollingerUpper")
    bollinger_middle: float | None = Field(default=None, alias="bollingerMiddle")
    bollinger_lower: float | None = Field(default=None, alias="bollingerLower")
    atr14: float | None = None
    volume_sma20: float | None = Field(default=None, alias="volumeSma20")

    model_config = {"populate_by_name": True}


class SignalComponent(BaseModel):
    name: str
    stance: SignalStance
    weight: int
    score: int
    value: float | str | None = None
    note: str


class SignalRiskLevels(BaseModel):
    long_stop_loss: float | None = Field(default=None, alias="longStopLoss")
    long_take_profit: float | None = Field(default=None, alias="longTakeProfit")
    short_stop_loss: float | None = Field(default=None, alias="shortStopLoss")
    short_take_profit: float | None = Field(default=None, alias="shortTakeProfit")

    model_config = {"populate_by_name": True}


class SignalReport(BaseModel):
    symbol: str
    interval: SupportedInterval
    provider: str
    mode: DataMode
    generated_at: str = Field(alias="generatedAt")
    latest_price: float = Field(alias="latestPrice")
    latest_bar_time: str = Field(alias="latestBarTime")
    score: int
    confidence: int
    signal: SignalDirection
    indicators: IndicatorSnapshot
    components: list[SignalComponent]
    risk: SignalRiskLevels
    bars: list[PriceBar]
    summary: str

    model_config = {"populate_by_name": True}


class BacktestTrade(BaseModel):
    entry_time: str = Field(alias="entryTime")
    exit_time: str = Field(alias="exitTime")
    entry_price: float = Field(alias="entryPrice")
    exit_price: float = Field(alias="exitPrice")
    quantity: int
    profit_loss: float = Field(alias="profitLoss")
    return_pct: float = Field(alias="returnPct")
    bars_held: int = Field(alias="barsHeld")

    model_config = {"populate_by_name": True}


class EquityPoint(BaseModel):
    time: str
    equity: float


class BacktestResult(BaseModel):
    symbol: str
    interval: SupportedInterval
    starting_capital: float = Field(alias="startingCapital")
    ending_capital: float = Field(alias="endingCapital")
    total_return_pct: float = Field(alias="totalReturnPct")
    buy_hold_return_pct: float = Field(alias="buyHoldReturnPct")
    max_drawdown_pct: float = Field(alias="maxDrawdownPct")
    win_rate_pct: float = Field(alias="winRatePct")
    sharpe_ratio: float = Field(alias="sharpeRatio")
    trades: list[BacktestTrade]
    equity_curve: list[EquityPoint] = Field(alias="equityCurve")
    generated_at: str = Field(alias="generatedAt")

    model_config = {"populate_by_name": True}


class ProviderStatus(BaseModel):
    provider: str
    ready: bool
    mode: DataMode
    note: str


class AnalysisMode(BaseModel):
    execution_enabled: bool = Field(alias="executionEnabled")
    note: str

    model_config = {"populate_by_name": True}


class Recommendation(BaseModel):
    area: str
    choice: str
    reason: str


class TradingConfig(BaseModel):
    app_name: str = Field(alias="appName")
    market_data: ProviderStatus = Field(alias="marketData")
    analysis_mode: AnalysisMode = Field(alias="analysisMode")
    recommendations: list[Recommendation]

    model_config = {"populate_by_name": True}


class BacktestRequest(BaseModel):
    symbol: str
    interval: SupportedInterval = "15min"
    lookback: int = 320
    starting_capital: int = Field(default=10000, alias="startingCapital")

    model_config = {"populate_by_name": True}
