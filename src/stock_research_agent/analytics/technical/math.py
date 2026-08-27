"""Small dependency-free numerical helpers for technical analytics."""

import math
from statistics import fmean, pstdev


def simple_moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return fmean(values[-window:])


def exponential_moving_average_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def period_return(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    return values[-1] / values[-period - 1] - 1.0


def consecutive_returns(values: list[float]) -> list[float]:
    return [current / previous - 1.0 for previous, current in zip(values, values[1:], strict=False)]


def annualized_volatility(returns: list[float], annualization_periods: int) -> float | None:
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(annualization_periods)


def maximum_drawdown(values: list[float]) -> float:
    peak = values[0]
    maximum = 0.0
    for value in values:
        peak = max(peak, value)
        maximum = min(maximum, value / peak - 1.0)
    return maximum


def pearson_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator == 0:
        return None
    return numerator / denominator


def beta(target_returns: list[float], benchmark_returns: list[float]) -> float | None:
    if len(target_returns) != len(benchmark_returns) or len(target_returns) < 2:
        return None
    target_mean = fmean(target_returns)
    benchmark_mean = fmean(benchmark_returns)
    covariance = fmean(
        [
            (target - target_mean) * (benchmark - benchmark_mean)
            for target, benchmark in zip(target_returns, benchmark_returns, strict=True)
        ]
    )
    variance = fmean([(item - benchmark_mean) ** 2 for item in benchmark_returns])
    if variance == 0:
        return None
    return covariance / variance


def rsi_series(values: list[float], period: int) -> list[float | None]:
    """Wilder RSI with unavailable warm-up entries."""

    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = fmean(gains[:period])
    average_loss = fmean(losses[:period])
    result[period] = _rsi_from_averages(average_gain, average_loss)
    for index in range(period, len(changes)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
        result[index + 1] = _rsi_from_averages(average_gain, average_loss)
    return result


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)
