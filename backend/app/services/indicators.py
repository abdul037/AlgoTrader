from __future__ import annotations

import math

from app.schemas import PriceBar


def _round(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return round(value, 4)


def simple_moving_average(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
      return result

    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index >= period - 1:
            result[index] = _round(total / period)
    return result


def exponential_moving_average(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if not values or period <= 0:
        return result

    multiplier = 2 / (period + 1)
    ema = values[0]
    result[0] = _round(ema)
    for index in range(1, len(values)):
        ema = (values[index] - ema) * multiplier + ema
        result[index] = _round(ema)
    return result


def relative_strength_index(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period or period <= 0:
        return result

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change

    average_gain = gains / period
    average_loss = losses / period
    denominator = average_loss if average_loss > 0 else 1e-9
    result[period] = _round(100 - 100 / (1 + average_gain / denominator))

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        if average_loss == 0:
            result[index] = 100.0
            continue
        rs = average_gain / average_loss
        result[index] = _round(100 - 100 / (1 + rs))

    return result


def macd(
    values: list[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast = exponential_moving_average(values, fast_period)
    slow = exponential_moving_average(values, slow_period)
    macd_line: list[float | None] = []

    for fast_value, slow_value in zip(fast, slow, strict=True):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
        else:
            macd_line.append(_round(fast_value - slow_value))

    macd_values = [value if value is not None else 0.0 for value in macd_line]
    signal_line = exponential_moving_average(macd_values, signal_period)
    signal_line = [
        value if macd_line[index] is not None else None
        for index, value in enumerate(signal_line)
    ]
    histogram: list[float | None] = []

    for index, value in enumerate(macd_line):
        signal_value = signal_line[index]
        if value is None or signal_value is None:
            histogram.append(None)
        else:
            histogram.append(_round(value - signal_value))

    return macd_line, signal_line, histogram


def bollinger_bands(
    values: list[float], period: int = 20, multiplier: int = 2
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    middle = simple_moving_average(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)

    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mean = middle[index]
        if mean is None:
            continue
        variance = sum((value - mean) ** 2 for value in window) / period
        deviation = math.sqrt(variance) * multiplier
        upper[index] = _round(mean + deviation)
        lower[index] = _round(mean - deviation)

    return middle, upper, lower


def average_true_range(bars: list[PriceBar], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return result

    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )

    atr = sum(true_ranges[:period]) / period
    result[period - 1] = _round(atr)
    for index in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[index]) / period
        result[index] = _round(atr)
    return result
