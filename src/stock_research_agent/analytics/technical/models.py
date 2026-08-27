"""Structured inputs and outputs for deterministic technical calculations."""

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from stock_research_agent.domain.base import DomainModel


class AdjustmentMode(StrEnum):
    RAW = "raw"
    FORWARD = "forward"
    BACKWARD = "backward"


class MetricStatus(StrEnum):
    AVAILABLE = "available"
    INSUFFICIENT_HISTORY = "insufficient_history"
    MISSING_INPUT = "missing_input"
    NOT_APPLICABLE = "not_applicable"


class TechnicalInstrumentKind(StrEnum):
    """行情数据包代表的标的类型。

    行业指数沿用 ``INDEX``，行业 ETF 沿用 ``FUND``。计算器据此区分
    通用价格/量价指标与只对 A 股个股有意义的换手、涨跌停和停牌字段。
    """

    STOCK = "stock"
    INDEX = "index"
    FUND = "fund"


class CalculationIssueCode(StrEnum):
    UNADJUSTED_SERIES = "UNADJUSTED_SERIES"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MISSING_OPTIONAL_DATA = "MISSING_OPTIONAL_DATA"
    PARTIAL_DATE_ALIGNMENT = "PARTIAL_DATE_ALIGNMENT"
    ZERO_OR_MISSING_AMOUNT = "ZERO_OR_MISSING_AMOUNT"


class CalculationIssue(DomainModel):
    code: CalculationIssueCode
    message: str = Field(min_length=1, max_length=500)


class ScalarMetric(DomainModel):
    value: float | None = None
    status: MetricStatus
    sample_size: int = Field(ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def align_value_and_status(self) -> "ScalarMetric":
        if self.status == MetricStatus.AVAILABLE and self.value is None:
            raise ValueError("available 指标必须有 value")
        if self.status != MetricStatus.AVAILABLE and self.value is not None:
            raise ValueError("不可用指标不能携带 value")
        return self


class WindowMetric(ScalarMetric):
    window: int = Field(gt=0)


class TechnicalSeriesInput(DomainModel):
    """Raw rows read from a run-scoped data bundle, never supplied by the LLM."""

    price_rows: list[dict[str, Any]]
    adjustment_rows: list[dict[str, Any]] = Field(default_factory=list)
    adjustment_mode: AdjustmentMode = AdjustmentMode.FORWARD
    require_adjustment: bool = False


def _validate_windows(value: tuple[int, ...]) -> tuple[int, ...]:
    if not value:
        raise ValueError("windows 不能为空")
    if any(window <= 0 for window in value):
        raise ValueError("window 必须大于 0")
    if len(set(value)) != len(value):
        raise ValueError("windows 不能重复")
    return tuple(sorted(value))


class ReturnAndTrendInput(DomainModel):
    series: TechnicalSeriesInput
    windows: tuple[int, ...] = (5, 10, 20, 60)

    _windows = field_validator("windows")(_validate_windows)


class MomentumInput(DomainModel):
    series: TechnicalSeriesInput
    rsi_period: int = Field(default=14, gt=1, le=200)
    macd_fast: int = Field(default=12, gt=1, le=200)
    macd_slow: int = Field(default=26, gt=2, le=400)
    macd_signal: int = Field(default=9, gt=1, le=200)
    roc_windows: tuple[int, ...] = (5, 20)
    divergence_lookback: int = Field(default=20, gt=2, le=250)

    _roc_windows = field_validator("roc_windows")(_validate_windows)

    @model_validator(mode="after")
    def validate_macd_periods(self) -> "MomentumInput":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast 必须小于 macd_slow")
        return self


class RiskAndTradabilityInput(DomainModel):
    series: TechnicalSeriesInput
    instrument_kind: TechnicalInstrumentKind = TechnicalInstrumentKind.STOCK
    price_limit_rows: list[dict[str, Any]] = Field(default_factory=list)
    suspension_rows: list[dict[str, Any]] = Field(default_factory=list)
    calendar_rows: list[dict[str, Any]] = Field(default_factory=list)
    volatility_window: int = Field(default=20, gt=1, le=250)
    atr_period: int = Field(default=14, gt=1, le=250)
    annualization_periods: int = Field(default=252, gt=0, le=366)


class VolumeAndLiquidityInput(DomainModel):
    series: TechnicalSeriesInput
    instrument_kind: TechnicalInstrumentKind = TechnicalInstrumentKind.STOCK
    valuation_rows: list[dict[str, Any]] = Field(default_factory=list)
    windows: tuple[int, ...] = (5, 20)
    amihud_window: int = Field(default=20, gt=1, le=250)

    _windows = field_validator("windows")(_validate_windows)


class RelativeStrengthInput(DomainModel):
    target: TechnicalSeriesInput
    benchmark: TechnicalSeriesInput
    windows: tuple[int, ...] = (20, 60)

    _windows = field_validator("windows")(_validate_windows)


class PricePoint(DomainModel):
    trade_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    adjustment_factor: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "PricePoint":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high 不能低于 open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low 不能高于 open/close/high")
        return self


class PreparedPriceSeries(DomainModel):
    points: tuple[PricePoint, ...]
    adjustment_mode: AdjustmentMode
    adjustment_applied: bool
    issues: tuple[CalculationIssue, ...] = ()

    @property
    def start_date(self) -> date:
        return self.points[0].trade_date

    @property
    def end_date(self) -> date:
        return self.points[-1].trade_date


class CalculationMetadata(DomainModel):
    observation_count: int = Field(gt=0)
    start_date: date
    end_date: date
    adjustment_mode: AdjustmentMode
    adjustment_applied: bool
    issues: tuple[CalculationIssue, ...] = ()


class MovingAverageCross(DomainModel):
    fast_window: int = Field(gt=0)
    slow_window: int = Field(gt=0)
    direction: str = Field(pattern=r"^(bullish|bearish|none|unavailable)$")
    trade_date: date | None = None
    reason: str | None = None


class BreakoutSignal(DomainModel):
    window: int = Field(gt=0)
    direction: str = Field(pattern=r"^(up|down|none|unavailable)$")
    reference_price: float | None = None
    trade_date: date | None = None
    reason: str | None = None


class ReturnAndTrendResult(DomainModel):
    metadata: CalculationMetadata
    latest_close: float
    interval_return_ratio: ScalarMetric
    period_returns: tuple[WindowMetric, ...]
    simple_moving_averages: tuple[WindowMetric, ...]
    exponential_moving_averages: tuple[WindowMetric, ...]
    moving_average_slopes: tuple[WindowMetric, ...]
    close_to_moving_average_ratios: tuple[WindowMetric, ...]
    range_high: float
    range_low: float
    distance_to_range_high_ratio: float
    distance_to_range_low_ratio: float
    crossovers: tuple[MovingAverageCross, ...]
    breakouts: tuple[BreakoutSignal, ...]


class MacdMetric(DomainModel):
    fast_period: int = Field(gt=0)
    slow_period: int = Field(gt=0)
    signal_period: int = Field(gt=0)
    macd: float | None = None
    signal: float | None = None
    histogram: float | None = None
    status: MetricStatus
    sample_size: int = Field(ge=0)
    reason: str | None = None


class MomentumDivergence(DomainModel):
    direction: str = Field(pattern=r"^(bearish|bullish|none|unavailable)$")
    lookback: int = Field(gt=0)
    reason: str


class MomentumResult(DomainModel):
    metadata: CalculationMetadata
    rsi: WindowMetric
    macd: MacdMetric
    rate_of_change: tuple[WindowMetric, ...]
    divergence: MomentumDivergence


class TradabilitySummary(DomainModel):
    status: MetricStatus = MetricStatus.AVAILABLE
    reason: str | None = Field(default=None, min_length=1, max_length=300)
    expected_open_sessions: int | None = Field(default=None, ge=0)
    observed_sessions: int | None = Field(default=None, ge=0)
    observed_session_ratio: float | None = Field(default=None, ge=0, le=1)
    missing_open_dates: tuple[date, ...] = ()
    suspension_dates: tuple[date, ...] = ()
    unexplained_missing_dates: tuple[date, ...] = ()
    limit_up_touches: int | None = Field(default=None, ge=0)
    limit_down_touches: int | None = Field(default=None, ge=0)
    limit_up_closes: int | None = Field(default=None, ge=0)
    limit_down_closes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_availability(self) -> "TradabilitySummary":
        detailed_values = (
            self.expected_open_sessions,
            self.observed_sessions,
            self.limit_up_touches,
            self.limit_down_touches,
            self.limit_up_closes,
            self.limit_down_closes,
        )
        if self.status is MetricStatus.AVAILABLE and any(
            value is None for value in detailed_values
        ):
            raise ValueError("available 的可交易性摘要必须包含全部计数字段")
        if self.status is not MetricStatus.AVAILABLE:
            if self.reason is None:
                raise ValueError("不可用的可交易性摘要必须说明 reason")
            if any(value is not None for value in detailed_values):
                raise ValueError("不可用的可交易性摘要不能携带计数值")
        return self


class RiskAndTradabilityResult(DomainModel):
    metadata: CalculationMetadata
    annualized_volatility: WindowMetric
    annualized_downside_volatility: WindowMetric
    average_true_range: WindowMetric
    maximum_drawdown_ratio: ScalarMetric
    current_drawdown_ratio: ScalarMetric
    largest_up_gap_ratio: ScalarMetric
    largest_down_gap_ratio: ScalarMetric
    tradability: TradabilitySummary


class VolumePriceRegimeCounts(DomainModel):
    volume_up_price_up: int = Field(ge=0)
    volume_down_price_up: int = Field(ge=0)
    volume_up_price_down: int = Field(ge=0)
    volume_down_price_down: int = Field(ge=0)


class VolumeAndLiquidityResult(DomainModel):
    metadata: CalculationMetadata
    latest_volume: ScalarMetric
    latest_amount: ScalarMetric
    volume_moving_averages: tuple[WindowMetric, ...]
    amount_moving_averages: tuple[WindowMetric, ...]
    relative_volume: tuple[WindowMetric, ...]
    latest_turnover_rate: ScalarMetric
    turnover_rate_averages: tuple[WindowMetric, ...]
    turnover_rate_percentile: ScalarMetric
    provider_volume_ratio: ScalarMetric
    on_balance_volume: ScalarMetric
    amihud_illiquidity: WindowMetric
    regime_counts: VolumePriceRegimeCounts


class AlignmentSummary(DomainModel):
    common_observation_count: int = Field(gt=0)
    target_only_count: int = Field(ge=0)
    benchmark_only_count: int = Field(ge=0)
    start_date: date
    end_date: date


class RelativePeriodMetric(DomainModel):
    window: int = Field(gt=0)
    target_return_ratio: float | None = None
    benchmark_return_ratio: float | None = None
    excess_return_ratio: float | None = None
    status: MetricStatus
    sample_size: int = Field(ge=0)
    reason: str | None = None


class RelativeStrengthResult(DomainModel):
    target_metadata: CalculationMetadata
    benchmark_metadata: CalculationMetadata
    alignment: AlignmentSummary
    interval_target_return_ratio: float
    interval_benchmark_return_ratio: float
    interval_excess_return_ratio: float
    period_metrics: tuple[RelativePeriodMetric, ...]
    rolling_correlations: tuple[WindowMetric, ...]
    rolling_betas: tuple[WindowMetric, ...]
    upside_average_excess_return_ratio: ScalarMetric
    downside_average_excess_return_ratio: ScalarMetric
    target_new_high_without_benchmark: bool
