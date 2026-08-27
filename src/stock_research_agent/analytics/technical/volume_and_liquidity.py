"""Deterministic volume, turnover and liquidity calculator."""

from statistics import fmean

from stock_research_agent.analytics.technical.helpers import (
    scalar_available,
    scalar_unavailable,
    window_available,
    window_unavailable,
)
from stock_research_agent.analytics.technical.models import (
    CalculationIssue,
    CalculationIssueCode,
    MetricStatus,
    TechnicalInstrumentKind,
    VolumeAndLiquidityInput,
    VolumeAndLiquidityResult,
    VolumePriceRegimeCounts,
    WindowMetric,
)
from stock_research_agent.analytics.technical.preparation import (
    finite_number,
    metadata_from_series,
    parse_trade_date,
    prepare_price_series,
)


def calculate_volume_and_liquidity(
    request: VolumeAndLiquidityInput,
) -> VolumeAndLiquidityResult:
    series = prepare_price_series(request.series)
    points = list(series.points)
    volumes = [point.volume for point in points]
    amounts = [point.amount for point in points]
    issues: list[CalculationIssue] = []

    latest_volume = _optional_latest(volumes, "最新成交量缺失")
    latest_amount = _optional_latest(amounts, "最新成交额缺失")
    volume_averages: list[WindowMetric] = []
    amount_averages: list[WindowMetric] = []
    relative_volumes: list[WindowMetric] = []
    for window in request.windows:
        volume_average = _optional_average(volumes, window, "成交量")
        amount_average = _optional_average(amounts, window, "成交额")
        volume_averages.append(volume_average)
        amount_averages.append(amount_average)
        if volume_average.value is None or volumes[-1] is None:
            relative_volumes.append(
                window_unavailable(
                    window,
                    MetricStatus.MISSING_INPUT,
                    min(len(volumes), window),
                    f"最近 {window} 期成交量不完整",
                )
            )
        elif volume_average.value == 0:
            relative_volumes.append(
                window_unavailable(
                    window,
                    MetricStatus.NOT_APPLICABLE,
                    window,
                    "平均成交量为 0，无法计算相对成交量",
                )
            )
        else:
            relative_volumes.append(
                window_available(window, volumes[-1] / volume_average.value, window)
            )

    if request.instrument_kind is TechnicalInstrumentKind.STOCK:
        valuation_by_date = {
            parse_trade_date(row.get("trade_date")): row for row in request.valuation_rows
        }
        turnover_values: list[float | None] = []
        provider_volume_ratios: list[float | None] = []
        for point in points:
            row = valuation_by_date.get(point.trade_date)
            turnover_values.append(
                None
                if row is None
                else finite_number(
                    row.get("turnover_rate"), field_name="turnover_rate", required=False
                )
            )
            provider_volume_ratios.append(
                None
                if row is None
                else finite_number(
                    row.get("volume_ratio"), field_name="volume_ratio", required=False
                )
            )
        if not request.valuation_rows:
            issues.append(
                CalculationIssue(
                    code=CalculationIssueCode.MISSING_OPTIONAL_DATA,
                    message="未提供 daily_basic，换手率及上游量比不可观测",
                )
            )

        turnover_averages = tuple(
            _optional_average(turnover_values, window, "换手率") for window in request.windows
        )
        available_turnover = [value for value in turnover_values if value is not None]
        latest_turnover = _optional_latest(turnover_values, "最新换手率缺失")
        if latest_turnover.value is None or not available_turnover:
            turnover_percentile = scalar_unavailable(
                MetricStatus.MISSING_INPUT,
                len(available_turnover),
                "没有足够的换手率数据计算历史百分位",
            )
        else:
            percentile = sum(value <= latest_turnover.value for value in available_turnover) / len(
                available_turnover
            )
            turnover_percentile = scalar_available(percentile, len(available_turnover))
        provider_volume_ratio = _optional_latest(
            provider_volume_ratios, "最新上游 volume_ratio 缺失"
        )
    else:
        reason = (
            f"{request.instrument_kind.value} 行情没有 A 股个股 daily_basic 换手率口径，"
            "该指标不适用"
        )
        turnover_averages = tuple(
            window_unavailable(window, MetricStatus.NOT_APPLICABLE, 0, reason)
            for window in request.windows
        )
        latest_turnover = scalar_unavailable(MetricStatus.NOT_APPLICABLE, 0, reason)
        turnover_percentile = scalar_unavailable(MetricStatus.NOT_APPLICABLE, 0, reason)
        provider_volume_ratio = scalar_unavailable(MetricStatus.NOT_APPLICABLE, 0, reason)

    obv = _calculate_obv(points)
    amihud, amihud_issue = _calculate_amihud(points, request.amihud_window)
    if amihud_issue is not None:
        issues.append(amihud_issue)

    return VolumeAndLiquidityResult(
        metadata=metadata_from_series(series, additional_issues=tuple(issues)),
        latest_volume=latest_volume,
        latest_amount=latest_amount,
        volume_moving_averages=tuple(volume_averages),
        amount_moving_averages=tuple(amount_averages),
        relative_volume=tuple(relative_volumes),
        latest_turnover_rate=latest_turnover,
        turnover_rate_averages=turnover_averages,
        turnover_rate_percentile=turnover_percentile,
        provider_volume_ratio=provider_volume_ratio,
        on_balance_volume=obv,
        amihud_illiquidity=amihud,
        regime_counts=_regime_counts(points),
    )


def _optional_latest(values: list[float | None], reason: str):
    value = values[-1]
    if value is None:
        return scalar_unavailable(MetricStatus.MISSING_INPUT, len(values), reason)
    return scalar_available(value, len(values))


def _optional_average(values: list[float | None], window: int, label: str) -> WindowMetric:
    if len(values) < window:
        return window_unavailable(
            window,
            MetricStatus.INSUFFICIENT_HISTORY,
            len(values),
            f"{label}均值需要 {window} 个观测",
        )
    selected = values[-window:]
    if any(value is None for value in selected):
        return window_unavailable(
            window,
            MetricStatus.MISSING_INPUT,
            len([value for value in selected if value is not None]),
            f"最近 {window} 期{label}存在空值",
        )
    complete = [value for value in selected if value is not None]
    return window_available(window, fmean(complete), window)


def _calculate_obv(points):
    if any(point.volume is None for point in points):
        return scalar_unavailable(
            MetricStatus.MISSING_INPUT,
            len([point for point in points if point.volume is not None]),
            "存在缺失成交量，拒绝计算不完整 OBV",
        )
    value = 0.0
    for previous, current in zip(points, points[1:], strict=False):
        assert current.volume is not None
        if current.close > previous.close:
            value += current.volume
        elif current.close < previous.close:
            value -= current.volume
    return scalar_available(value, len(points))


def _calculate_amihud(points, window: int):
    if len(points) <= window:
        return (
            window_unavailable(
                window,
                MetricStatus.INSUFFICIENT_HISTORY,
                len(points),
                f"Amihud 指标需要至少 {window + 1} 个价格点",
            ),
            None,
        )
    selected = points[-window - 1 :]
    values: list[float] = []
    for previous, current in zip(selected, selected[1:], strict=False):
        if current.amount is None or current.amount <= 0:
            return (
                window_unavailable(
                    window,
                    MetricStatus.MISSING_INPUT,
                    len(values),
                    "计算窗口存在缺失或为 0 的成交额",
                ),
                CalculationIssue(
                    code=CalculationIssueCode.ZERO_OR_MISSING_AMOUNT,
                    message="Amihud 指标未计算：窗口内成交额缺失或为 0",
                ),
            )
        values.append(abs(current.close / previous.close - 1.0) / current.amount)
    return window_available(window, fmean(values), window), None


def _regime_counts(points) -> VolumePriceRegimeCounts:
    counts = {
        "volume_up_price_up": 0,
        "volume_down_price_up": 0,
        "volume_up_price_down": 0,
        "volume_down_price_down": 0,
    }
    for previous, current in zip(points, points[1:], strict=False):
        if previous.volume is None or current.volume is None or current.close == previous.close:
            continue
        volume_up = current.volume >= previous.volume
        price_up = current.close > previous.close
        if volume_up and price_up:
            counts["volume_up_price_up"] += 1
        elif not volume_up and price_up:
            counts["volume_down_price_up"] += 1
        elif volume_up and not price_up:
            counts["volume_up_price_down"] += 1
        else:
            counts["volume_down_price_down"] += 1
    return VolumePriceRegimeCounts(**counts)
