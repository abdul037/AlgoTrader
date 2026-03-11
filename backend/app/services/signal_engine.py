from __future__ import annotations

from datetime import UTC, datetime

from app.schemas import (
    IndicatorSnapshot,
    PriceBar,
    SignalComponent,
    SignalDirection,
    SignalReport,
    SignalRiskLevels,
    SupportedInterval,
)
from app.services.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    macd,
    relative_strength_index,
    simple_moving_average,
)


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4)


def _latest_value(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _summarize(signal: SignalDirection, score: int, components: list[SignalComponent]) -> str:
    drivers = sorted(components, key=lambda item: abs(item.score), reverse=True)[:3]
    reasons = "; ".join(f"{item.name}: {item.note.lower()}" for item in drivers)
    if signal == "buy":
        return f"Bias is bullish with score {score}. Key drivers: {reasons}."
    if signal == "sell":
        return f"Bias is bearish with score {score}. Key drivers: {reasons}."
    return f"Signal is neutral with score {score}. Strongest readings: {reasons}."


def generate_signal_report(
    symbol: str,
    interval: SupportedInterval,
    provider: str,
    mode: str,
    bars: list[PriceBar],
) -> SignalReport:
    if len(bars) < 60:
        raise RuntimeError("At least 60 candles are required to generate a signal.")

    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]

    sma20_series = simple_moving_average(closes, 20)
    sma50_series = simple_moving_average(closes, 50)
    ema12_series = exponential_moving_average(closes, 12)
    ema26_series = exponential_moving_average(closes, 26)
    rsi14_series = relative_strength_index(closes, 14)
    volume_sma20_series = simple_moving_average([float(volume) for volume in volumes], 20)
    atr14_series = average_true_range(bars, 14)
    macd_line, signal_line, histogram = macd(closes)
    middle, upper, lower = bollinger_bands(closes)

    latest_bar = bars[-1]
    latest_price = latest_bar.close

    indicators = IndicatorSnapshot(
        sma20=_latest_value(sma20_series),
        sma50=_latest_value(sma50_series),
        ema12=_latest_value(ema12_series),
        ema26=_latest_value(ema26_series),
        rsi14=_latest_value(rsi14_series),
        macd=_latest_value(macd_line),
        macdSignal=_latest_value(signal_line),
        macdHistogram=_latest_value(histogram),
        bollingerUpper=_latest_value(upper),
        bollingerMiddle=_latest_value(middle),
        bollingerLower=_latest_value(lower),
        atr14=_latest_value(atr14_series),
        volumeSma20=_latest_value(volume_sma20_series),
    )

    components: list[SignalComponent] = []

    def add_component(name: str, weight: int, raw_score: float, value: float | str | None, note: str) -> None:
        score = round(_clamp(raw_score, -weight, weight))
        stance = "neutral"
        if score > 0:
            stance = "bullish"
        elif score < 0:
            stance = "bearish"
        components.append(
            SignalComponent(
                name=name,
                stance=stance,
                weight=weight,
                score=score,
                value=value,
                note=note,
            )
        )

    if indicators.sma20 is not None and indicators.sma50 is not None:
        delta_pct = ((indicators.sma20 - indicators.sma50) / indicators.sma50) * 100
        add_component(
            "Trend",
            25,
            delta_pct * 8,
            _round(delta_pct),
            "short trend remains above long trend" if delta_pct >= 0 else "short trend remains below long trend",
        )

    if indicators.ema12 is not None and indicators.ema26 is not None:
        delta_pct = ((indicators.ema12 - indicators.ema26) / indicators.ema26) * 100
        add_component(
            "Momentum",
            20,
            delta_pct * 12,
            _round(delta_pct),
            "fast EMA is leading price acceleration"
            if delta_pct >= 0
            else "slow EMA is overpowering the fast EMA",
        )

    if indicators.rsi14 is not None:
        rsi = indicators.rsi14
        raw_score = 0.0
        note = "RSI is balanced"
        if rsi <= 30:
            raw_score = 18
            note = "RSI is oversold and can support a rebound"
        elif rsi >= 70:
            raw_score = -18
            note = "RSI is overbought and vulnerable to a pullback"
        elif rsi >= 55:
            raw_score = 8
            note = "RSI is above neutral and confirms positive momentum"
        elif rsi <= 45:
            raw_score = -8
            note = "RSI is below neutral and momentum is softening"
        add_component("RSI", 18, raw_score, _round(rsi), note)

    if indicators.macd_histogram is not None:
        add_component(
            "MACD",
            16,
            indicators.macd_histogram * 40,
            indicators.macd_histogram,
            "MACD histogram is positive" if indicators.macd_histogram >= 0 else "MACD histogram is negative",
        )

    if (
        indicators.bollinger_upper is not None
        and indicators.bollinger_lower is not None
        and indicators.bollinger_middle is not None
    ):
        band_width = indicators.bollinger_upper - indicators.bollinger_lower
        raw_score = 0.0
        note = "price is trading inside the Bollinger range"
        if latest_price < indicators.bollinger_lower:
            raw_score = 10
            note = "price closed below the lower Bollinger band"
        elif latest_price > indicators.bollinger_upper:
            raw_score = -10
            note = "price closed above the upper Bollinger band"
        elif band_width > 0:
            raw_score = ((latest_price - indicators.bollinger_middle) / band_width) * 10
            note = (
                "price is holding in the upper half of the band"
                if raw_score >= 0
                else "price is holding in the lower half of the band"
            )
        add_component("Bollinger", 10, raw_score, _round(latest_price), note)

    if indicators.volume_sma20 is not None:
        volume_ratio = latest_bar.volume / max(indicators.volume_sma20, 1)
        trend_component = next((item for item in components if item.name == "Trend"), None)
        raw_score = 0.0
        if volume_ratio >= 1.2 and trend_component and trend_component.score > 0:
            raw_score = 8
        elif volume_ratio >= 1.2 and trend_component and trend_component.score < 0:
            raw_score = -8
        add_component(
            "Volume",
            8,
            raw_score,
            _round(volume_ratio),
            "volume is above its recent average" if volume_ratio >= 1 else "volume is below its recent average",
        )

    score = sum(component.score for component in components)
    confidence = max(0, min(100, round((abs(score) / 97) * 100)))
    signal: SignalDirection = "hold"
    if score >= 25:
        signal = "buy"
    elif score <= -25:
        signal = "sell"

    atr = indicators.atr14
    risk = SignalRiskLevels(
        longStopLoss=_round(latest_price - atr * 1.5) if atr is not None else None,
        longTakeProfit=_round(latest_price + atr * 3) if atr is not None else None,
        shortStopLoss=_round(latest_price + atr * 1.5) if atr is not None else None,
        shortTakeProfit=_round(latest_price - atr * 3) if atr is not None else None,
    )

    return SignalReport(
        symbol=symbol,
        interval=interval,
        provider=provider,
        mode=mode,
        generatedAt=datetime.now(UTC).isoformat(),
        latestPrice=latest_price,
        latestBarTime=latest_bar.time,
        score=score,
        confidence=confidence,
        signal=signal,
        indicators=indicators,
        components=components,
        risk=risk,
        bars=bars,
        summary=_summarize(signal, score, components),
    )
