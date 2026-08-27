"""Deterministic return and trend calculator."""

from stock_research_agent.analytics.technical.helpers import (
    scalar_available,
    scalar_unavailable,
    window_available,
    window_unavailable,
)
from stock_research_agent.analytics.technical.math import (
    exponential_moving_average_series,
    period_return,
    simple_moving_average,
)
from stock_research_agent.analytics.technical.models import (
    BreakoutSignal,
    MetricStatus,
    MovingAverageCross,
    ReturnAndTrendInput,
    ReturnAndTrendResult,
    ScalarMetric,
    WindowMetric,
)
from stock_research_agent.analytics.technical.preparation import (
    metadata_from_series,
    prepare_price_series,
)


def calculate_return_and_trend(request: ReturnAndTrendInput) -> ReturnAndTrendResult:
    series = prepare_price_series(request.series)
    closes = [point.close for point in series.points]
    observation_count = len(closes)

    interval_return: ScalarMetric
    if observation_count >= 2:
        interval_return = scalar_available(closes[-1] / closes[0] - 1.0, observation_count)
    else:
        interval_return = scalar_unavailable(
            MetricStatus.INSUFFICIENT_HISTORY,
            observation_count,
            "区间收益至少需要两个价格点",
        )

    period_returns: list[WindowMetric] = []
    simple_averages: list[WindowMetric] = []
    exponential_averages: list[WindowMetric] = []
    slopes: list[WindowMetric] = []
    close_positions: list[WindowMetric] = []
    breakouts: list[BreakoutSignal] = []

    for window in request.windows:
        calculated_return = period_return(closes, window)
        if calculated_return is None:
            period_returns.append(_insufficient(window, observation_count, window + 1))
        else:
            period_returns.append(window_available(window, calculated_return, window + 1))

        average = simple_moving_average(closes, window)
        if average is None:
            unavailable = _insufficient(window, observation_count, window)
            simple_averages.append(unavailable)
            close_positions.append(unavailable.model_copy())
        else:
            simple_averages.append(window_available(window, average, window))
            close_positions.append(window_available(window, closes[-1] / average - 1.0, window))

        if observation_count < window:
            exponential_averages.append(_insufficient(window, observation_count, window))
        else:
            ema = exponential_moving_average_series(closes, window)[-1]
            exponential_averages.append(window_available(window, ema, observation_count))

        if observation_count < window + 1:
            slopes.append(_insufficient(window, observation_count, window + 1))
        else:
            previous_average = simple_moving_average(closes[:-1], window)
            assert average is not None and previous_average is not None
            slopes.append(window_available(window, average / previous_average - 1.0, window + 1))

        if observation_count < window + 1:
            breakouts.append(
                BreakoutSignal(
                    window=window,
                    direction="unavailable",
                    reason=f"突破判断需要至少 {window + 1} 个价格点",
                )
            )
        else:
            reference_high = max(closes[-window - 1 : -1])
            reference_low = min(closes[-window - 1 : -1])
            if closes[-1] > reference_high:
                direction = "up"
                reference = reference_high
            elif closes[-1] < reference_low:
                direction = "down"
                reference = reference_low
            else:
                direction = "none"
                reference = reference_high if closes[-1] >= closes[-2] else reference_low
            breakouts.append(
                BreakoutSignal(
                    window=window,
                    direction=direction,
                    reference_price=reference,
                    trade_date=series.end_date,
                )
            )

    crossovers = tuple(
        _moving_average_cross(closes, fast, slow, series.end_date)
        for fast, slow in zip(request.windows, request.windows[1:], strict=False)
    )
    range_high = max(point.high for point in series.points)
    range_low = min(point.low for point in series.points)

    return ReturnAndTrendResult(
        metadata=metadata_from_series(series),
        latest_close=closes[-1],
        interval_return_ratio=interval_return,
        period_returns=tuple(period_returns),
        simple_moving_averages=tuple(simple_averages),
        exponential_moving_averages=tuple(exponential_averages),
        moving_average_slopes=tuple(slopes),
        close_to_moving_average_ratios=tuple(close_positions),
        range_high=range_high,
        range_low=range_low,
        distance_to_range_high_ratio=closes[-1] / range_high - 1.0,
        distance_to_range_low_ratio=closes[-1] / range_low - 1.0,
        crossovers=crossovers,
        breakouts=tuple(breakouts),
    )


def _insufficient(window: int, observed: int, required: int) -> WindowMetric:
    return window_unavailable(
        window,
        MetricStatus.INSUFFICIENT_HISTORY,
        observed,
        f"需要至少 {required} 个价格点，当前只有 {observed} 个",
    )


def _moving_average_cross(
    closes: list[float],
    fast_window: int,
    slow_window: int,
    trade_date,
) -> MovingAverageCross:
    required = slow_window + 1
    if len(closes) < required:
        return MovingAverageCross(
            fast_window=fast_window,
            slow_window=slow_window,
            direction="unavailable",
            reason=f"均线交叉判断需要至少 {required} 个价格点",
        )
    fast_previous = simple_moving_average(closes[:-1], fast_window)
    slow_previous = simple_moving_average(closes[:-1], slow_window)
    fast_current = simple_moving_average(closes, fast_window)
    slow_current = simple_moving_average(closes, slow_window)
    assert None not in (fast_previous, slow_previous, fast_current, slow_current)
    if fast_previous <= slow_previous and fast_current > slow_current:
        direction = "bullish"
    elif fast_previous >= slow_previous and fast_current < slow_current:
        direction = "bearish"
    else:
        direction = "none"
    return MovingAverageCross(
        fast_window=fast_window,
        slow_window=slow_window,
        direction=direction,
        trade_date=trade_date,
    )
