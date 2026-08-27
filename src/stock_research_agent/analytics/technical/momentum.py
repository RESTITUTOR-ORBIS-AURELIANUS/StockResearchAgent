"""Deterministic momentum calculator."""

from stock_research_agent.analytics.technical.helpers import (
    window_available,
    window_unavailable,
)
from stock_research_agent.analytics.technical.math import (
    exponential_moving_average_series,
    period_return,
    rsi_series,
)
from stock_research_agent.analytics.technical.models import (
    MacdMetric,
    MetricStatus,
    MomentumDivergence,
    MomentumInput,
    MomentumResult,
    WindowMetric,
)
from stock_research_agent.analytics.technical.preparation import (
    metadata_from_series,
    prepare_price_series,
)


def calculate_momentum(request: MomentumInput) -> MomentumResult:
    series = prepare_price_series(request.series)
    closes = [point.close for point in series.points]
    sample_size = len(closes)

    all_rsi = rsi_series(closes, request.rsi_period)
    latest_rsi = all_rsi[-1]
    if latest_rsi is None:
        rsi = window_unavailable(
            request.rsi_period,
            MetricStatus.INSUFFICIENT_HISTORY,
            sample_size,
            f"RSI({request.rsi_period}) 需要至少 {request.rsi_period + 1} 个价格点",
        )
    else:
        rsi = window_available(request.rsi_period, latest_rsi, request.rsi_period + 1)

    macd = _calculate_macd(closes, request)
    rate_of_change: list[WindowMetric] = []
    for window in request.roc_windows:
        value = period_return(closes, window)
        if value is None:
            rate_of_change.append(
                window_unavailable(
                    window,
                    MetricStatus.INSUFFICIENT_HISTORY,
                    sample_size,
                    f"ROC({window}) 需要至少 {window + 1} 个价格点",
                )
            )
        else:
            rate_of_change.append(window_available(window, value, window + 1))

    divergence = _detect_divergence(
        closes,
        all_rsi,
        request.divergence_lookback,
    )
    return MomentumResult(
        metadata=metadata_from_series(series),
        rsi=rsi,
        macd=macd,
        rate_of_change=tuple(rate_of_change),
        divergence=divergence,
    )


def _calculate_macd(closes: list[float], request: MomentumInput) -> MacdMetric:
    required = request.macd_slow + request.macd_signal - 1
    if len(closes) < required:
        return MacdMetric(
            fast_period=request.macd_fast,
            slow_period=request.macd_slow,
            signal_period=request.macd_signal,
            status=MetricStatus.INSUFFICIENT_HISTORY,
            sample_size=len(closes),
            reason=f"成熟 MACD 结果需要至少 {required} 个价格点",
        )
    fast = exponential_moving_average_series(closes, request.macd_fast)
    slow = exponential_moving_average_series(closes, request.macd_slow)
    macd_series = [left - right for left, right in zip(fast, slow, strict=True)]
    signal_series = exponential_moving_average_series(macd_series, request.macd_signal)
    return MacdMetric(
        fast_period=request.macd_fast,
        slow_period=request.macd_slow,
        signal_period=request.macd_signal,
        macd=macd_series[-1],
        signal=signal_series[-1],
        histogram=macd_series[-1] - signal_series[-1],
        status=MetricStatus.AVAILABLE,
        sample_size=len(closes),
    )


def _detect_divergence(
    closes: list[float],
    rsi_values: list[float | None],
    lookback: int,
) -> MomentumDivergence:
    if len(closes) < lookback + 1 or rsi_values[-1] is None:
        return MomentumDivergence(
            direction="unavailable",
            lookback=lookback,
            reason=f"背离判断需要至少 {lookback + 1} 个价格点和可用 RSI",
        )

    previous = closes[-lookback - 1 : -1]
    previous_rsi = rsi_values[-lookback - 1 : -1]
    high_index = max(range(len(previous)), key=previous.__getitem__)
    low_index = min(range(len(previous)), key=previous.__getitem__)
    high_rsi = previous_rsi[high_index]
    low_rsi = previous_rsi[low_index]
    latest_rsi = rsi_values[-1]
    assert latest_rsi is not None
    if closes[-1] > previous[high_index] and high_rsi is not None and latest_rsi < high_rsi:
        return MomentumDivergence(
            direction="bearish",
            lookback=lookback,
            reason="价格创观察窗新高，但 RSI 低于前一价格高点对应值",
        )
    if closes[-1] < previous[low_index] and low_rsi is not None and latest_rsi > low_rsi:
        return MomentumDivergence(
            direction="bullish",
            lookback=lookback,
            reason="价格创观察窗新低，但 RSI 高于前一价格低点对应值",
        )
    return MomentumDivergence(
        direction="none",
        lookback=lookback,
        reason="未满足基于价格极值与 RSI 的确定性背离条件",
    )
