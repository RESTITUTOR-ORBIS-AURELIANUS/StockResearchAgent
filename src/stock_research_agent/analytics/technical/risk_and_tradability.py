"""Deterministic risk and A-share tradability calculator."""

import math
from statistics import fmean
from typing import Any

from stock_research_agent.analytics.technical.helpers import (
    scalar_available,
    scalar_unavailable,
    window_available,
    window_unavailable,
)
from stock_research_agent.analytics.technical.math import (
    annualized_volatility,
    consecutive_returns,
    maximum_drawdown,
)
from stock_research_agent.analytics.technical.models import (
    CalculationIssue,
    CalculationIssueCode,
    MetricStatus,
    RiskAndTradabilityInput,
    RiskAndTradabilityResult,
    TechnicalInstrumentKind,
    TradabilitySummary,
)
from stock_research_agent.analytics.technical.preparation import (
    finite_number,
    metadata_from_series,
    parse_trade_date,
    prepare_price_series,
)


def calculate_risk_and_tradability(
    request: RiskAndTradabilityInput,
) -> RiskAndTradabilityResult:
    series = prepare_price_series(request.series)
    points = list(series.points)
    closes = [point.close for point in points]
    returns = consecutive_returns(closes)
    issues: list[CalculationIssue] = []

    if len(returns) < request.volatility_window:
        volatility = window_unavailable(
            request.volatility_window,
            MetricStatus.INSUFFICIENT_HISTORY,
            len(returns),
            f"波动率需要 {request.volatility_window} 个收益率观测",
        )
        downside = window_unavailable(
            request.volatility_window,
            MetricStatus.INSUFFICIENT_HISTORY,
            len(returns),
            f"下行波动率需要 {request.volatility_window} 个收益率观测",
        )
    else:
        selected = returns[-request.volatility_window :]
        calculated = annualized_volatility(selected, request.annualization_periods)
        assert calculated is not None
        volatility = window_available(request.volatility_window, calculated, len(selected))
        downside_value = math.sqrt(fmean([min(value, 0.0) ** 2 for value in selected]))
        downside = window_available(
            request.volatility_window,
            downside_value * math.sqrt(request.annualization_periods),
            len(selected),
        )

    true_ranges = _true_ranges(points)
    if len(true_ranges) < request.atr_period:
        atr = window_unavailable(
            request.atr_period,
            MetricStatus.INSUFFICIENT_HISTORY,
            len(true_ranges),
            f"ATR 需要至少 {request.atr_period} 个价格点",
        )
    else:
        atr = window_available(
            request.atr_period,
            fmean(true_ranges[-request.atr_period :]),
            request.atr_period,
        )

    maximum = maximum_drawdown(closes)
    peak = max(closes)
    current = closes[-1] / peak - 1.0
    if len(points) < 2:
        largest_up_gap = scalar_unavailable(
            MetricStatus.INSUFFICIENT_HISTORY, 1, "跳空计算至少需要两个价格点"
        )
        largest_down_gap = largest_up_gap.model_copy()
    else:
        gaps = [
            current_point.open / previous_point.close - 1.0
            for previous_point, current_point in zip(points, points[1:], strict=False)
        ]
        largest_up_gap = scalar_available(max([0.0, *gaps]), len(gaps))
        largest_down_gap = scalar_available(min([0.0, *gaps]), len(gaps))

    if request.instrument_kind is TechnicalInstrumentKind.STOCK:
        if not request.calendar_rows:
            issues.append(
                CalculationIssue(
                    code=CalculationIssueCode.MISSING_OPTIONAL_DATA,
                    message="未提供交易日历，无法区分停牌、休市和未解释的数据缺口",
                )
            )
        if not request.price_limit_rows:
            issues.append(
                CalculationIssue(
                    code=CalculationIssueCode.MISSING_OPTIONAL_DATA,
                    message="未提供涨跌停价格，涨跌停触及与封板次数不可观测",
                )
            )
        tradability = _calculate_tradability(request, series.start_date, series.end_date)
    else:
        tradability = TradabilitySummary(
            status=MetricStatus.NOT_APPLICABLE,
            reason=(
                f"{request.instrument_kind.value} 行情只计算通用价格风险；"
                "A 股个股涨跌停、停牌和交易日缺口统计不适用"
            ),
        )

    return RiskAndTradabilityResult(
        metadata=metadata_from_series(series, additional_issues=tuple(issues)),
        annualized_volatility=volatility,
        annualized_downside_volatility=downside,
        average_true_range=atr,
        maximum_drawdown_ratio=scalar_available(maximum, len(closes)),
        current_drawdown_ratio=scalar_available(current, len(closes)),
        largest_up_gap_ratio=largest_up_gap,
        largest_down_gap_ratio=largest_down_gap,
        tradability=tradability,
    )


def _true_ranges(points) -> list[float]:
    values = [points[0].high - points[0].low]
    for previous, current in zip(points, points[1:], strict=False):
        values.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return values


def _calculate_tradability(
    request: RiskAndTradabilityInput,
    start_date,
    end_date,
) -> TradabilitySummary:
    raw_bars = {parse_trade_date(row.get("trade_date")): row for row in request.series.price_rows}
    observed_dates = set(raw_bars)
    suspension_dates = {
        parse_trade_date(row.get("trade_date"))
        for row in request.suspension_rows
        if start_date <= parse_trade_date(row.get("trade_date")) <= end_date
    }
    if request.calendar_rows:
        expected_dates = {
            parse_trade_date(row.get("cal_date"), field_name="cal_date")
            for row in request.calendar_rows
            if _is_open(row.get("is_open"))
            and start_date
            <= parse_trade_date(row.get("cal_date"), field_name="cal_date")
            <= end_date
        }
    else:
        expected_dates = observed_dates | suspension_dates

    missing = expected_dates - observed_dates
    unexplained = missing - suspension_dates
    limits = {parse_trade_date(row.get("trade_date")): row for row in request.price_limit_rows}
    up_touches = down_touches = up_closes = down_closes = 0
    for trade_date, limit_row in limits.items():
        bar = raw_bars.get(trade_date)
        if bar is None:
            continue
        up_limit = finite_number(limit_row.get("up_limit"), field_name="up_limit")
        down_limit = finite_number(limit_row.get("down_limit"), field_name="down_limit")
        high = finite_number(bar.get("high"), field_name="high")
        low = finite_number(bar.get("low"), field_name="low")
        close = finite_number(bar.get("close"), field_name="close")
        assert None not in (up_limit, down_limit, high, low, close)
        tolerance = max(abs(up_limit), abs(down_limit)) * 1e-8
        up_touches += int(high >= up_limit - tolerance)
        down_touches += int(low <= down_limit + tolerance)
        up_closes += int(abs(close - up_limit) <= tolerance)
        down_closes += int(abs(close - down_limit) <= tolerance)

    expected_count = len(expected_dates)
    ratio = None if expected_count == 0 else min(len(observed_dates) / expected_count, 1.0)
    return TradabilitySummary(
        expected_open_sessions=expected_count,
        observed_sessions=len(observed_dates),
        observed_session_ratio=ratio,
        missing_open_dates=tuple(sorted(missing)),
        suspension_dates=tuple(sorted(suspension_dates)),
        unexplained_missing_dates=tuple(sorted(unexplained)),
        limit_up_touches=up_touches,
        limit_down_touches=down_touches,
        limit_up_closes=up_closes,
        limit_down_closes=down_closes,
    )


def _is_open(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "open"}
    return False
