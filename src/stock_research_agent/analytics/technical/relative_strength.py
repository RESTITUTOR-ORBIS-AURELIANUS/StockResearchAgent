"""Deterministic target-versus-benchmark calculator."""

from statistics import fmean

from stock_research_agent.analytics.technical.helpers import (
    scalar_available,
    scalar_unavailable,
    window_available,
    window_unavailable,
)
from stock_research_agent.analytics.technical.math import (
    beta,
    consecutive_returns,
    pearson_correlation,
)
from stock_research_agent.analytics.technical.models import (
    AlignmentSummary,
    CalculationIssue,
    CalculationIssueCode,
    MetricStatus,
    RelativePeriodMetric,
    RelativeStrengthInput,
    RelativeStrengthResult,
)
from stock_research_agent.analytics.technical.preparation import (
    align_price_series,
    metadata_from_series,
    prepare_price_series,
)


def calculate_relative_strength(request: RelativeStrengthInput) -> RelativeStrengthResult:
    target = prepare_price_series(request.target)
    benchmark = prepare_price_series(request.benchmark)
    aligned = align_price_series(target, benchmark)
    target_points = [pair[0] for pair in aligned]
    benchmark_points = [pair[1] for pair in aligned]
    target_closes = [point.close for point in target_points]
    benchmark_closes = [point.close for point in benchmark_points]
    target_returns = consecutive_returns(target_closes)
    benchmark_returns = consecutive_returns(benchmark_closes)

    target_dates = {point.trade_date for point in target.points}
    benchmark_dates = {point.trade_date for point in benchmark.points}
    target_only_count = len(target_dates - benchmark_dates)
    benchmark_only_count = len(benchmark_dates - target_dates)
    alignment_issues: tuple[CalculationIssue, ...] = ()
    if target_only_count or benchmark_only_count:
        alignment_issues = (
            CalculationIssue(
                code=CalculationIssueCode.PARTIAL_DATE_ALIGNMENT,
                message=(
                    f"目标独有 {target_only_count} 日、基准独有 {benchmark_only_count} 日；"
                    "相对指标只使用共同交易日"
                ),
            ),
        )

    interval_target = target_closes[-1] / target_closes[0] - 1.0
    interval_benchmark = benchmark_closes[-1] / benchmark_closes[0] - 1.0
    period_metrics: list[RelativePeriodMetric] = []
    correlations = []
    betas = []
    for window in request.windows:
        if len(target_closes) <= window:
            reason = f"需要至少 {window + 1} 个共同交易日"
            period_metrics.append(
                RelativePeriodMetric(
                    window=window,
                    status=MetricStatus.INSUFFICIENT_HISTORY,
                    sample_size=len(target_closes),
                    reason=reason,
                )
            )
            correlations.append(
                window_unavailable(
                    window,
                    MetricStatus.INSUFFICIENT_HISTORY,
                    len(target_returns),
                    reason,
                )
            )
            betas.append(correlations[-1].model_copy())
            continue

        target_period = target_closes[-1] / target_closes[-window - 1] - 1.0
        benchmark_period = benchmark_closes[-1] / benchmark_closes[-window - 1] - 1.0
        period_metrics.append(
            RelativePeriodMetric(
                window=window,
                target_return_ratio=target_period,
                benchmark_return_ratio=benchmark_period,
                excess_return_ratio=target_period - benchmark_period,
                status=MetricStatus.AVAILABLE,
                sample_size=window + 1,
            )
        )
        selected_target_returns = target_returns[-window:]
        selected_benchmark_returns = benchmark_returns[-window:]
        correlation = pearson_correlation(
            selected_target_returns,
            selected_benchmark_returns,
        )
        if correlation is None:
            correlations.append(
                window_unavailable(
                    window,
                    MetricStatus.NOT_APPLICABLE,
                    window,
                    "目标或基准收益率没有方差，相关系数无定义",
                )
            )
        else:
            correlations.append(window_available(window, correlation, window))
        calculated_beta = beta(selected_target_returns, selected_benchmark_returns)
        if calculated_beta is None:
            betas.append(
                window_unavailable(
                    window,
                    MetricStatus.NOT_APPLICABLE,
                    window,
                    "基准收益率没有方差，Beta 无定义",
                )
            )
        else:
            betas.append(window_available(window, calculated_beta, window))

    upside_excess = [
        target_return - benchmark_return
        for target_return, benchmark_return in zip(target_returns, benchmark_returns, strict=True)
        if benchmark_return > 0
    ]
    downside_excess = [
        target_return - benchmark_return
        for target_return, benchmark_return in zip(target_returns, benchmark_returns, strict=True)
        if benchmark_return < 0
    ]

    return RelativeStrengthResult(
        target_metadata=metadata_from_series(target, additional_issues=alignment_issues),
        benchmark_metadata=metadata_from_series(benchmark, additional_issues=alignment_issues),
        alignment=AlignmentSummary(
            common_observation_count=len(aligned),
            target_only_count=target_only_count,
            benchmark_only_count=benchmark_only_count,
            start_date=target_points[0].trade_date,
            end_date=target_points[-1].trade_date,
        ),
        interval_target_return_ratio=interval_target,
        interval_benchmark_return_ratio=interval_benchmark,
        interval_excess_return_ratio=interval_target - interval_benchmark,
        period_metrics=tuple(period_metrics),
        rolling_correlations=tuple(correlations),
        rolling_betas=tuple(betas),
        upside_average_excess_return_ratio=_average_or_unavailable(
            upside_excess, "共同区间内没有基准上涨交易日"
        ),
        downside_average_excess_return_ratio=_average_or_unavailable(
            downside_excess, "共同区间内没有基准下跌交易日"
        ),
        target_new_high_without_benchmark=(
            len(target_closes) >= 2
            and target_closes[-1] > max(target_closes[:-1])
            and benchmark_closes[-1] <= max(benchmark_closes[:-1])
        ),
    )


def _average_or_unavailable(values: list[float], reason: str):
    if not values:
        return scalar_unavailable(MetricStatus.NOT_APPLICABLE, 0, reason)
    return scalar_available(fmean(values), len(values))
