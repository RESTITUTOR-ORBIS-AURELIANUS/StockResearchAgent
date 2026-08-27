"""Deterministic technical analytics tests."""

from datetime import date, timedelta

import pytest

from stock_research_agent.analytics.technical import (
    MomentumInput,
    RelativeStrengthInput,
    ReturnAndTrendInput,
    RiskAndTradabilityInput,
    TechnicalAnalyticsError,
    TechnicalAnalyticsErrorCode,
    TechnicalInstrumentKind,
    TechnicalSeriesInput,
    VolumeAndLiquidityInput,
    calculate_momentum,
    calculate_relative_strength,
    calculate_return_and_trend,
    calculate_risk_and_tradability,
    calculate_volume_and_liquidity,
    prepare_price_series,
)

START = date(2026, 1, 1)


def price_rows(
    count: int,
    *,
    growth: float = 0.01,
    missing_indices: set[int] | None = None,
) -> list[dict[str, object]]:
    missing = missing_indices or set()
    rows: list[dict[str, object]] = []
    for index in range(count):
        if index in missing:
            continue
        close = 100.0 * (1.0 + growth) ** index
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": (START + timedelta(days=index)).strftime("%Y%m%d"),
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "vol": 1_000.0 + index * 10.0,
                "amount": close * (1_000.0 + index * 10.0),
            }
        )
    return rows


def raw_series(count: int, *, growth: float = 0.01) -> TechnicalSeriesInput:
    return TechnicalSeriesInput(
        price_rows=price_rows(count, growth=growth),
        adjustment_mode="raw",
    )


def test_preparation_sorts_and_forward_adjusts_without_fake_split_return() -> None:
    rows = [
        {
            "trade_date": "20260102",
            "open": 50,
            "high": 51,
            "low": 49,
            "close": 50,
            "vol": 200,
            "amount": 10_000,
        },
        {
            "trade_date": "20260101",
            "open": 100,
            "high": 102,
            "low": 98,
            "close": 100,
            "vol": 100,
            "amount": 10_000,
        },
    ]
    factors = [
        {"trade_date": "20260102", "adj_factor": 2},
        {"trade_date": "20260101", "adj_factor": 1},
    ]

    prepared = prepare_price_series(TechnicalSeriesInput(price_rows=rows, adjustment_rows=factors))

    assert [point.trade_date for point in prepared.points] == [date(2026, 1, 1), date(2026, 1, 2)]
    assert [point.close for point in prepared.points] == [50.0, 50.0]
    assert prepared.adjustment_applied is True


def test_preparation_rejects_duplicate_dates_and_partial_adjustment() -> None:
    duplicate = price_rows(2)
    duplicate[1]["trade_date"] = duplicate[0]["trade_date"]
    with pytest.raises(TechnicalAnalyticsError) as duplicate_error:
        prepare_price_series(TechnicalSeriesInput(price_rows=duplicate))
    assert duplicate_error.value.code == TechnicalAnalyticsErrorCode.DUPLICATE_DATE

    rows = price_rows(2)
    with pytest.raises(TechnicalAnalyticsError) as factor_error:
        prepare_price_series(
            TechnicalSeriesInput(
                price_rows=rows,
                adjustment_rows=[{"trade_date": rows[0]["trade_date"], "adj_factor": 1}],
            )
        )
    assert factor_error.value.code == TechnicalAnalyticsErrorCode.MISSING_ADJUSTMENT_FACTOR


def test_return_and_trend_reports_available_and_short_window_metrics() -> None:
    result = calculate_return_and_trend(
        ReturnAndTrendInput(series=raw_series(70), windows=(5, 20, 60))
    )

    assert result.interval_return_ratio.value is not None
    assert result.interval_return_ratio.value > 0
    assert all(metric.status == "available" for metric in result.period_returns)
    assert result.close_to_moving_average_ratios[-1].value > 0
    assert result.breakouts[-1].direction == "up"

    short = calculate_return_and_trend(ReturnAndTrendInput(series=raw_series(3), windows=(5,)))
    assert short.period_returns[0].status == "insufficient_history"
    assert short.simple_moving_averages[0].value is None
    assert short.breakouts[0].direction == "unavailable"


def test_momentum_is_deterministic_and_marks_immature_macd() -> None:
    result = calculate_momentum(MomentumInput(series=raw_series(60)))

    assert result.rsi.value == pytest.approx(100.0)
    assert result.macd.status == "available"
    assert result.macd.histogram is not None
    assert all(metric.value > 0 for metric in result.rate_of_change)

    short = calculate_momentum(MomentumInput(series=raw_series(10)))
    assert short.rsi.status == "insufficient_history"
    assert short.macd.status == "insufficient_history"
    assert short.divergence.direction == "unavailable"


def test_risk_and_tradability_distinguishes_suspension_and_limit_lock() -> None:
    rows = price_rows(30, growth=-0.002, missing_indices={10})
    missing_date = START + timedelta(days=10)
    calendar = [
        {"cal_date": (START + timedelta(days=index)).strftime("%Y%m%d"), "is_open": 1}
        for index in range(30)
    ]
    last = rows[-1]
    limits = [
        {
            "trade_date": last["trade_date"],
            "up_limit": last["close"],
            "down_limit": float(last["close"]) * 0.8,
        }
    ]
    result = calculate_risk_and_tradability(
        RiskAndTradabilityInput(
            series=TechnicalSeriesInput(price_rows=rows, adjustment_mode="raw"),
            price_limit_rows=limits,
            suspension_rows=[{"trade_date": missing_date.strftime("%Y%m%d"), "suspend_type": "S"}],
            calendar_rows=calendar,
            volatility_window=5,
            atr_period=5,
        )
    )

    assert result.maximum_drawdown_ratio.value < 0
    assert result.tradability.missing_open_dates == (missing_date,)
    assert result.tradability.suspension_dates == (missing_date,)
    assert result.tradability.unexplained_missing_dates == ()
    assert result.tradability.limit_up_touches == 1
    assert result.tradability.limit_up_closes == 1


def test_risk_short_series_has_explicit_unavailable_metrics() -> None:
    result = calculate_risk_and_tradability(
        RiskAndTradabilityInput(series=raw_series(1), volatility_window=5, atr_period=5)
    )
    assert result.annualized_volatility.status == "insufficient_history"
    assert result.average_true_range.status == "insufficient_history"
    assert result.largest_up_gap_ratio.status == "insufficient_history"
    assert {issue.code for issue in result.metadata.issues} >= {"MISSING_OPTIONAL_DATA"}


@pytest.mark.parametrize(
    "instrument_kind",
    [TechnicalInstrumentKind.INDEX, TechnicalInstrumentKind.FUND],
)
def test_price_risk_is_reusable_for_index_and_fund_without_stock_tradability(
    instrument_kind: TechnicalInstrumentKind,
) -> None:
    result = calculate_risk_and_tradability(
        RiskAndTradabilityInput(
            series=raw_series(30),
            instrument_kind=instrument_kind,
            volatility_window=5,
            atr_period=5,
        )
    )

    assert result.annualized_volatility.status == "available"
    assert result.maximum_drawdown_ratio.status == "available"
    assert result.tradability.status == "not_applicable"
    assert result.tradability.expected_open_sessions is None
    assert result.tradability.limit_up_touches is None
    assert result.tradability.reason is not None
    assert "MISSING_OPTIONAL_DATA" not in {issue.code for issue in result.metadata.issues}


def test_volume_and_liquidity_uses_turnover_and_rejects_partial_amihud_window() -> None:
    rows = price_rows(25)
    valuation = [
        {
            "trade_date": row["trade_date"],
            "turnover_rate": 1.0 + index / 10,
            "volume_ratio": 1.2,
        }
        for index, row in enumerate(rows)
    ]
    result = calculate_volume_and_liquidity(
        VolumeAndLiquidityInput(
            series=TechnicalSeriesInput(price_rows=rows, adjustment_mode="raw"),
            valuation_rows=valuation,
            windows=(5, 20),
            amihud_window=20,
        )
    )

    assert result.latest_turnover_rate.value == pytest.approx(3.4)
    assert result.turnover_rate_percentile.value == pytest.approx(1.0)
    assert result.relative_volume[0].value > 1
    assert result.on_balance_volume.value > 0
    assert result.amihud_illiquidity.status == "available"

    rows[-1]["amount"] = 0
    missing_amount = calculate_volume_and_liquidity(
        VolumeAndLiquidityInput(
            series=TechnicalSeriesInput(price_rows=rows, adjustment_mode="raw"),
            amihud_window=20,
        )
    )
    assert missing_amount.amihud_illiquidity.status == "missing_input"
    assert "ZERO_OR_MISSING_AMOUNT" in {issue.code for issue in missing_amount.metadata.issues}


@pytest.mark.parametrize(
    "instrument_kind",
    [TechnicalInstrumentKind.INDEX, TechnicalInstrumentKind.FUND],
)
def test_volume_and_liquidity_reuses_price_volume_for_non_stock_targets(
    instrument_kind: TechnicalInstrumentKind,
) -> None:
    result = calculate_volume_and_liquidity(
        VolumeAndLiquidityInput(
            series=raw_series(30),
            instrument_kind=instrument_kind,
            windows=(5, 20),
        )
    )

    assert result.latest_volume.status == "available"
    assert result.relative_volume[0].status == "available"
    assert result.on_balance_volume.status == "available"
    assert result.amihud_illiquidity.status == "available"
    assert result.latest_turnover_rate.status == "not_applicable"
    assert result.turnover_rate_averages[0].status == "not_applicable"
    assert result.provider_volume_ratio.status == "not_applicable"
    assert "MISSING_OPTIONAL_DATA" not in {issue.code for issue in result.metadata.issues}


def test_relative_strength_aligns_dates_and_calculates_excess_beta() -> None:
    target_rows = price_rows(40, growth=0.02, missing_indices={5})
    benchmark_rows = price_rows(40, growth=0.01, missing_indices={8})
    for index, row in enumerate(target_rows):
        _rescale_bar(row, 1.0 + (index % 3) * 0.002)
    for index, row in enumerate(benchmark_rows):
        _rescale_bar(row, 1.0 + (index % 4) * 0.001)
    result = calculate_relative_strength(
        RelativeStrengthInput(
            target=TechnicalSeriesInput(price_rows=target_rows, adjustment_mode="raw"),
            benchmark=TechnicalSeriesInput(
                price_rows=benchmark_rows,
                adjustment_mode="raw",
            ),
            windows=(5, 20),
        )
    )

    assert result.alignment.common_observation_count == 38
    assert result.alignment.target_only_count == 1
    assert result.alignment.benchmark_only_count == 1
    assert result.interval_excess_return_ratio > 0
    assert result.period_metrics[0].excess_return_ratio > 0
    assert result.rolling_correlations[0].status == "available"
    assert result.rolling_betas[0].status == "available"
    assert "PARTIAL_DATE_ALIGNMENT" in {issue.code for issue in result.target_metadata.issues}


def _rescale_bar(row: dict[str, object], multiplier: float) -> None:
    for field in ("open", "high", "low", "close", "amount"):
        row[field] = float(row[field]) * multiplier


def test_relative_strength_rejects_non_overlapping_series() -> None:
    target = price_rows(2)
    benchmark = price_rows(2)
    for index, row in enumerate(benchmark):
        row["trade_date"] = (START + timedelta(days=10 + index)).strftime("%Y%m%d")

    with pytest.raises(TechnicalAnalyticsError) as error:
        calculate_relative_strength(
            RelativeStrengthInput(
                target=TechnicalSeriesInput(price_rows=target, adjustment_mode="raw"),
                benchmark=TechnicalSeriesInput(price_rows=benchmark, adjustment_mode="raw"),
            )
        )
    assert error.value.code == TechnicalAnalyticsErrorCode.NO_OVERLAPPING_DATES
