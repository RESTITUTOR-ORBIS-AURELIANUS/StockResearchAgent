"""Price normalization, adjustment, validation and date alignment."""

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from stock_research_agent.analytics.technical.errors import (
    TechnicalAnalyticsError,
    TechnicalAnalyticsErrorCode,
)
from stock_research_agent.analytics.technical.models import (
    AdjustmentMode,
    CalculationIssue,
    CalculationIssueCode,
    CalculationMetadata,
    PreparedPriceSeries,
    PricePoint,
    TechnicalSeriesInput,
)


def parse_trade_date(value: Any, *, field_name: str = "trade_date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        for pattern in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(stripped, pattern).date()
            except ValueError:
                continue
    raise TechnicalAnalyticsError(
        TechnicalAnalyticsErrorCode.INVALID_ROW,
        f"{field_name} 不是 YYYYMMDD、YYYY-MM-DD 或 date",
        details={"field": field_name, "value": repr(value)},
    )


def finite_number(
    value: Any,
    *,
    field_name: str,
    required: bool = True,
) -> float | None:
    if value is None or value == "":
        if not required:
            return None
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.INVALID_ROW,
            f"{field_name} 不能为空",
            details={"field": field_name},
        )
    if isinstance(value, bool):
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.INVALID_ROW,
            f"{field_name} 不能是布尔值",
            details={"field": field_name, "value": value},
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.INVALID_ROW,
            f"{field_name} 不是有效数字",
            details={"field": field_name, "value": repr(value)},
        ) from exc
    if not math.isfinite(number):
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.INVALID_ROW,
            f"{field_name} 必须是有限数字",
            details={"field": field_name, "value": repr(value)},
        )
    return number


def _rows_by_date(
    rows: list[dict[str, Any]],
    *,
    date_field: str = "trade_date",
) -> dict[date, Mapping[str, Any]]:
    indexed: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        trade_date = parse_trade_date(row.get(date_field), field_name=date_field)
        if trade_date in indexed:
            raise TechnicalAnalyticsError(
                TechnicalAnalyticsErrorCode.DUPLICATE_DATE,
                f"{date_field} 出现重复日期 {trade_date.isoformat()}",
                details={"date": trade_date.isoformat()},
            )
        indexed[trade_date] = row
    return indexed


def prepare_price_series(series: TechnicalSeriesInput) -> PreparedPriceSeries:
    """Build an ascending, validated and optionally adjusted OHLC series."""

    if not series.price_rows:
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.EMPTY_SERIES,
            "price_rows 为空，无法进行技术计算",
        )

    price_by_date = _rows_by_date(series.price_rows)
    factor_by_date: dict[date, float] = {}
    for trade_date, row in _rows_by_date(series.adjustment_rows).items():
        factor = finite_number(row.get("adj_factor"), field_name="adj_factor")
        assert factor is not None
        if factor <= 0:
            raise TechnicalAnalyticsError(
                TechnicalAnalyticsErrorCode.INVALID_ROW,
                "adj_factor 必须大于 0",
                details={"date": trade_date.isoformat(), "value": factor},
            )
        factor_by_date[trade_date] = factor

    should_adjust = series.adjustment_mode != AdjustmentMode.RAW
    if series.require_adjustment and (not should_adjust or not factor_by_date):
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.MISSING_ADJUSTMENT_FACTOR,
            "调用方要求复权，但没有可用的复权因子",
        )

    issues: list[CalculationIssue] = []
    adjustment_applied = should_adjust and bool(factor_by_date)
    if should_adjust and not factor_by_date:
        issues.append(
            CalculationIssue(
                code=CalculationIssueCode.UNADJUSTED_SERIES,
                message="未提供复权因子，计算基于原始价格；跨除权日的收益可能失真",
            )
        )

    if adjustment_applied:
        missing = sorted(set(price_by_date) - set(factor_by_date))
        if missing:
            raise TechnicalAnalyticsError(
                TechnicalAnalyticsErrorCode.MISSING_ADJUSTMENT_FACTOR,
                "部分价格日期缺少复权因子，拒绝混用复权与未复权价格",
                details={"missing_dates": [item.isoformat() for item in missing]},
            )
        ordered_factors = [factor_by_date[item] for item in sorted(price_by_date)]
        anchor = (
            ordered_factors[-1]
            if series.adjustment_mode == AdjustmentMode.FORWARD
            else ordered_factors[0]
        )
    else:
        anchor = 1.0

    points: list[PricePoint] = []
    for trade_date in sorted(price_by_date):
        row = price_by_date[trade_date]
        factor = factor_by_date.get(trade_date)
        multiplier = factor / anchor if adjustment_applied and factor is not None else 1.0
        try:
            point = PricePoint(
                trade_date=trade_date,
                open=_required_number(row, "open") * multiplier,
                high=_required_number(row, "high") * multiplier,
                low=_required_number(row, "low") * multiplier,
                close=_required_number(row, "close") * multiplier,
                volume=finite_number(row.get("vol"), field_name="vol", required=False),
                amount=finite_number(row.get("amount"), field_name="amount", required=False),
                adjustment_factor=factor,
            )
        except ValidationError as exc:
            raise TechnicalAnalyticsError(
                TechnicalAnalyticsErrorCode.INVALID_ROW,
                f"{trade_date.isoformat()} 的 OHLC 数据不合法",
                details={"date": trade_date.isoformat(), "errors": exc.errors()},
            ) from exc
        points.append(point)

    return PreparedPriceSeries(
        points=tuple(points),
        adjustment_mode=series.adjustment_mode,
        adjustment_applied=adjustment_applied,
        issues=tuple(issues),
    )


def _required_number(row: Mapping[str, Any], field_name: str) -> float:
    number = finite_number(row.get(field_name), field_name=field_name)
    assert number is not None
    return number


def metadata_from_series(
    series: PreparedPriceSeries,
    *,
    additional_issues: tuple[CalculationIssue, ...] = (),
) -> CalculationMetadata:
    return CalculationMetadata(
        observation_count=len(series.points),
        start_date=series.start_date,
        end_date=series.end_date,
        adjustment_mode=series.adjustment_mode,
        adjustment_applied=series.adjustment_applied,
        issues=series.issues + additional_issues,
    )


def align_price_series(
    target: PreparedPriceSeries,
    benchmark: PreparedPriceSeries,
) -> tuple[tuple[PricePoint, PricePoint], ...]:
    target_by_date = {point.trade_date: point for point in target.points}
    benchmark_by_date = {point.trade_date: point for point in benchmark.points}
    common_dates = sorted(set(target_by_date) & set(benchmark_by_date))
    if len(common_dates) < 2:
        raise TechnicalAnalyticsError(
            TechnicalAnalyticsErrorCode.NO_OVERLAPPING_DATES,
            "目标与基准至少需要两个共同交易日",
            details={"common_date_count": len(common_dates)},
        )
    return tuple((target_by_date[item], benchmark_by_date[item]) for item in common_dates)
