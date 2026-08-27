"""LLM Tool 的受控输入、输出与错误模型。"""

from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.providers.models import ProviderParam, ProviderSource
from stock_research_agent.services.daily_event_snapshot import DailyEventSnapshot
from stock_research_agent.services.daily_fundamental_snapshot import DailyFundamentalSnapshot
from stock_research_agent.services.daily_sentiment_flow_snapshot import (
    DailySentimentFlowSnapshot,
)
from stock_research_agent.services.daily_technical_snapshot import DailyTechnicalSnapshot
from stock_research_agent.services.equity_market_data import StockBarFrequency
from stock_research_agent.services.fundamental_data import BusinessCompositionType
from stock_research_agent.services.macro_data import RateRangeSeries
from stock_research_agent.services.public_news_event import AnnouncementCategory

SecurityCode = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{6}\.(?:SH|SZ|BJ)$",
        description="Tushare 格式的 A 股个股或场内基金代码，例如 000001.SZ、510300.SH",
    ),
]
IndexCode = Annotated[
    str,
    Field(
        pattern=r"^(?:[0-9]{6}\.(?:SH|SZ|BJ|SI)|[A-Za-z0-9]{1,20}\.CSI)$",
        description=(
            "Tushare 市场、中证或申万指数代码，例如 000300.SH、399006.SZ、801780.SI、000013.CSI"
        ),
    ),
]
TechnicalInstrumentCode = Annotated[
    str,
    Field(
        pattern=r"^(?:[0-9]{6}\.(?:SH|SZ|BJ|SI)|[A-Za-z0-9]{1,20}\.CSI)$",
        description="股票、基金、市场指数或行业指数代码",
    ),
]
ContextRef = Annotated[
    str,
    Field(
        pattern=r"^ctx_[A-Za-z0-9_-]{32}$",
        description="当前研究运行内由原始数据 Tool 返回的不透明数据引用",
    ),
]
ReportPeriod = Annotated[
    str,
    Field(pattern=r"^[0-9]{8}$", description="报告期，格式 YYYYMMDD，例如 20251231"),
]
MonthValue = Annotated[
    str,
    Field(pattern=r"^[0-9]{6}$", description="月份，格式 YYYYMM，例如 202607"),
]
QuarterValue = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}Q[1-4]$", description="季度，格式 YYYYQ1～YYYYQ4"),
]


class DateRangeToolInput(DomainModel):
    start_date: date = Field(description="查询起始日期（含）")
    end_date: date = Field(description="查询结束日期（含）")

    @model_validator(mode="after")
    def validate_range(self) -> "DateRangeToolInput":
        if self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return self


class StockDateRangeToolInput(DateRangeToolInput):
    ts_code: SecurityCode


class StockIdentityInput(DomainModel):
    ts_code: SecurityCode
    list_status: Literal["L", "D", "P"] = Field(
        default="L",
        description="上市状态：L 上市、D 退市、P 暂停上市",
    )


class StockCodeInput(DomainModel):
    ts_code: SecurityCode


class TradeCalendarInput(DateRangeToolInput):
    exchange: str = Field(min_length=1, max_length=20, description="交易所代码，例如 SSE")


class StockPriceContextInput(StockDateRangeToolInput):
    frequency: StockBarFrequency = Field(
        default="daily",
        description="K 线频率：daily、weekly 或 monthly",
    )


class IndexMarketContextInput(DateRangeToolInput):
    ts_code: IndexCode = Field(
        description="市场或中证指数代码，例如 000300.SH、000013.CSI、000012CNY030.CSI"
    )


class FundMarketContextInput(StockDateRangeToolInput):
    include_adjustment_factors: bool = Field(
        default=True,
        description="是否同时读取基金复权因子",
    )
    include_share_history: bool = Field(
        default=False,
        description="是否同时读取 ETF 份额；部分上游可能没有该权限",
    )


class DailyTechnicalSnapshotInput(DomainModel):
    candidate_count: int = Field(
        default=10,
        ge=3,
        le=20,
        description=("每个异常候选组最多保留多少只股票；交易日、行业层级和基准指数由程序固定"),
    )


class DailySentimentFlowSnapshotInput(DomainModel):
    candidate_count: int = Field(
        default=10,
        ge=3,
        le=20,
        description="每个资金流/涨跌停候选分组最多保留的证券数量",
    )


class DailyFundamentalSnapshotInput(DomainModel):
    candidate_count: int = Field(
        default=10,
        ge=3,
        le=20,
        description="每个估值、业绩或财务质量候选分组最多保留的股票数量",
    )
    announcement_lookback_days: int = Field(
        default=14,
        ge=7,
        le=60,
        description="业绩预告和快报的公告回看天数",
    )


class DailyEventSnapshotInput(DomainModel):
    candidate_count: int = Field(
        default=10,
        ge=3,
        le=20,
        description="分别保留的新闻候选与高优先级公告候选数量",
    )
    news_lookback_hours: int = Field(
        default=24,
        ge=1,
        le=24,
        description="从本次研究冻结时间向前抓取最近快讯的小时数",
    )
    announcement_lookback_days: int = Field(
        default=3,
        ge=1,
        le=7,
        description="从冻结日期向前扫描公告索引的自然日数量",
    )
    research_lookback_days: int = Field(
        default=7,
        ge=1,
        le=14,
        description=(
            "按单日 report_date 回看全市场卖方研报摘要的自然日数；每个日期独立查询并可单独失败"
        ),
    )


class ReturnAndTrendCalculatorInput(DomainModel):
    context_ref: ContextRef
    windows: tuple[int, ...] = Field(
        default=(5, 20, 60),
        min_length=1,
        max_length=8,
        description="收益率和均线窗口，单位是当前数据频率的期数",
    )

    @model_validator(mode="after")
    def validate_windows(self) -> "ReturnAndTrendCalculatorInput":
        _validate_periods(self.windows, "windows", minimum=2, maximum=500)
        return self


class MomentumCalculatorInput(DomainModel):
    context_ref: ContextRef
    rsi_period: int = Field(default=14, ge=2, le=100)
    macd_fast: int = Field(default=12, ge=2, le=100)
    macd_slow: int = Field(default=26, ge=3, le=200)
    macd_signal: int = Field(default=9, ge=2, le=100)
    roc_periods: tuple[int, ...] = Field(default=(5, 20), min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_parameters(self) -> "MomentumCalculatorInput":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast 必须小于 macd_slow")
        _validate_periods(self.roc_periods, "roc_periods", minimum=1, maximum=500)
        return self


class RiskAndTradabilityCalculatorInput(DomainModel):
    context_ref: ContextRef
    volatility_window: int = Field(default=20, ge=2, le=500)
    atr_period: int = Field(default=14, ge=2, le=200)


class VolumeAndLiquidityCalculatorInput(DomainModel):
    context_ref: ContextRef
    windows: tuple[int, ...] = Field(
        default=(5, 20),
        min_length=1,
        max_length=8,
        description="成交量、成交额和换手率统计窗口",
    )

    @model_validator(mode="after")
    def validate_windows(self) -> "VolumeAndLiquidityCalculatorInput":
        _validate_periods(self.windows, "windows", minimum=2, maximum=500)
        return self


class RelativeStrengthCalculatorInput(DomainModel):
    target_context_ref: ContextRef
    benchmark_context_ref: ContextRef
    windows: tuple[int, ...] = Field(default=(20, 60), min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_references_and_windows(self) -> "RelativeStrengthCalculatorInput":
        if self.target_context_ref == self.benchmark_context_ref:
            raise ValueError("target_context_ref 和 benchmark_context_ref 不能相同")
        _validate_periods(self.windows, "windows", minimum=2, maximum=500)
        return self


def _validate_periods(
    periods: tuple[int, ...],
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if len(set(periods)) != len(periods):
        raise ValueError(f"{field_name} 不能包含重复值")
    if any(period < minimum or period > maximum for period in periods):
        raise ValueError(f"{field_name} 的每个值必须在 {minimum} 到 {maximum} 之间")


class StockPeriodInput(DomainModel):
    ts_code: SecurityCode
    period: ReportPeriod


class FinancialQualityInput(StockPeriodInput):
    composition_type: BusinessCompositionType = Field(
        default="P",
        description="主营构成口径：P 产品、D 地区、I 行业",
    )


class DividendOwnershipInput(DateRangeToolInput):
    ts_code: SecurityCode
    period: ReportPeriod


class ChinaMacroContextInput(DomainModel):
    start_month: MonthValue
    end_month: MonthValue
    start_quarter: QuarterValue
    end_quarter: QuarterValue

    @model_validator(mode="after")
    def validate_ranges(self) -> "ChinaMacroContextInput":
        if self.start_month > self.end_month:
            raise ValueError("start_month 不能晚于 end_month")
        if self.start_quarter > self.end_quarter:
            raise ValueError("start_quarter 不能晚于 end_quarter")
        return self


class InterestRateContextInput(DateRangeToolInput):
    series: RateRangeSeries = Field(
        description=(
            "利率序列：shibor、shibor_lpr、wz_index、gz_index、us_tycr、"
            "us_trycr、us_tbr、us_tltr 或 us_trltr"
        )
    )


class NewsWindowInput(DomainModel):
    start_at: AwareDatetime = Field(description="新闻窗口起点，必须含时区")
    end_at: AwareDatetime = Field(description="新闻窗口终点，必须含时区")
    source: Literal["ALL", "EASTMONEY", "THS", "CLS"] = Field(
        default="ALL",
        description="新闻聚合来源；ALL 会同时查询东方财富、同花顺和财联社",
    )

    @model_validator(mode="after")
    def validate_window(self) -> "NewsWindowInput":
        if self.start_at > self.end_at:
            raise ValueError("start_at 不能晚于 end_at")
        if self.end_at - self.start_at > timedelta(days=1):
            raise ValueError("单次新闻窗口不能超过 24 小时")
        return self


class TargetedNewsDisclosureInput(DateRangeToolInput):
    ts_code: SecurityCode
    announcement_category: AnnouncementCategory = Field(
        default=AnnouncementCategory.ALL,
        description=(
            "公告分类：全部、重大事项、财务报告、融资公告、风险提示、资产重组、信息变更或持股变动"
        ),
    )

    @model_validator(mode="after")
    def validate_targeted_window(self) -> "TargetedNewsDisclosureInput":
        # 首尾都包含；相差 30 天正好覆盖 31 个自然日。
        if self.end_date - self.start_date >= timedelta(days=31):
            raise ValueError("指定股票新闻与公告查证窗口不能超过 31 天")
        return self


class SellSideResearchContextInput(StockDateRangeToolInput):
    """指定股票的卖方研报与券商月度荐股查证窗口。"""

    @model_validator(mode="after")
    def validate_research_window(self) -> "SellSideResearchContextInput":
        if self.end_date - self.start_date > timedelta(days=366):
            raise ValueError("指定股票的卖方研究查询窗口不能超过 366 天")
        return self


class CorporateActionInput(StockDateRangeToolInput):
    pass


class EconomicCalendarInput(DateRangeToolInput):
    pass


class CapitalFlowContextInput(StockDateRangeToolInput):
    exchange_id: str = Field(
        min_length=1,
        max_length=20,
        description="融资融券市场代码，例如 SSE 或 SZSE",
    )


class UnusualTradingInput(DomainModel):
    ts_code: SecurityCode
    trade_date: date = Field(description="需要检查异常交易行为的交易日")


class ToolResultStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"
    TOO_LARGE = "too_large"


class ToolIssueCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    CALCULATION_INCOMPLETE = "CALCULATION_INCOMPLETE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolIssue(DomainModel):
    dataset_label: str | None = None
    code: ToolIssueCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    suggested_action: str = Field(min_length=1, max_length=500)
    correlation_id: str | None = None


class ToolRowSource(DomainModel):
    provider: ProviderSource
    from_cache: bool
    fetched_at: AwareDatetime
    page_index: int = Field(ge=0)
    source_offset: int = Field(ge=0)


class ToolDataRow(DomainModel):
    data: dict[str, Any]
    source: ToolRowSource


class ToolSourceSummary(DomainModel):
    provider: ProviderSource
    from_cache: bool
    page_count: int = Field(ge=1)
    item_count: int = Field(ge=0)
    response_bytes: int = Field(ge=0)
    latest_fetched_at: AwareDatetime


class ToolDatasetResult(DomainModel):
    label: str
    api_name: str
    query_params: dict[str, ProviderParam]
    requested_fields: tuple[str, ...]
    rows: list[ToolDataRow]
    received_item_count: int = Field(ge=0)
    returned_item_count: int = Field(ge=0)
    discarded_item_count: int = Field(ge=0)
    data_as_of: date | None = None
    complete: bool
    source_summary: tuple[ToolSourceSummary, ...]


class StoredToolDatasetResult(DomainModel):
    """存储型原始 Tool 返回的数据清单和少量预览。"""

    label: str
    api_name: str
    query_params: dict[str, ProviderParam]
    requested_fields: tuple[str, ...]
    preview_rows: list[ToolDataRow]
    received_item_count: int = Field(ge=0)
    stored_item_count: int = Field(ge=0)
    preview_item_count: int = Field(ge=0)
    discarded_item_count: int = Field(ge=0)
    data_as_of: date | None = None
    complete: bool
    preview_complete: bool
    preview_strategy: Literal["provider_order_head"] = "provider_order_head"
    source_summary: tuple[ToolSourceSummary, ...]

    @model_validator(mode="after")
    def validate_preview_counts(self) -> "StoredToolDatasetResult":
        if self.preview_item_count != len(self.preview_rows):
            raise ValueError("preview_item_count 必须等于 preview_rows 长度")
        if self.preview_item_count > self.stored_item_count:
            raise ValueError("preview_item_count 不能超过 stored_item_count")
        if self.preview_complete != (self.preview_item_count == self.stored_item_count):
            raise ValueError("preview_complete 与预览/存储行数不一致")
        return self


class ResearchToolResult(DomainModel):
    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    datasets: list[ToolDatasetResult]
    issues: list[ToolIssue]
    total_returned_items: int = Field(ge=0)
    complete: bool


class StoredResearchToolResult(DomainModel):
    """完整数据已进入 ResearchDataStore 的原始 Tool 返回值。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    context_ref: ContextRef | None = None
    datasets: list[StoredToolDatasetResult]
    issues: list[ToolIssue]
    total_stored_items: int = Field(ge=0)
    total_preview_items: int = Field(ge=0)
    complete: bool


class DailyTechnicalSnapshotToolResult(DomainModel):
    """每日技术快照 Tool 的小型语义结果；完整源表只保存在 context_ref 中。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    context_ref: ContextRef | None = None
    snapshot: DailyTechnicalSnapshot | None = None
    issues: list[ToolIssue]
    source_dataset_count: int = Field(ge=0)
    total_stored_items: int = Field(ge=0)
    complete: bool


class DailySentimentFlowSnapshotToolResult(DomainModel):
    """每日情绪资金快照 Tool 的小型语义结果；完整源表只保存在 context_ref 中。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    context_ref: ContextRef | None = None
    snapshot: DailySentimentFlowSnapshot | None = None
    issues: list[ToolIssue]
    source_dataset_count: int = Field(ge=0)
    total_stored_items: int = Field(ge=0)
    complete: bool


class DailyFundamentalSnapshotToolResult(DomainModel):
    """每日基本面快照 Tool 的小型语义结果；完整源表只保存在 context_ref 中。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    context_ref: ContextRef | None = None
    snapshot: DailyFundamentalSnapshot | None = None
    issues: list[ToolIssue]
    source_dataset_count: int = Field(ge=0)
    total_stored_items: int = Field(ge=0)
    complete: bool


class DailyEventSnapshotToolResult(DomainModel):
    """每日新闻事件快照 Tool 的有界候选；完整源表只保存在 context_ref 中。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    context_ref: ContextRef | None = None
    snapshot: DailyEventSnapshot | None = None
    issues: list[ToolIssue]
    source_dataset_count: int = Field(ge=0)
    total_stored_items: int = Field(ge=0)
    complete: bool


class TechnicalCalculationSubject(DomainModel):
    """计算结果对应的可读标的身份，避免只靠不透明 context_ref 猜测对象。"""

    context_ref: ContextRef
    bundle_kind: Literal[
        "stock_price_context",
        "index_market_context",
        "fund_market_context",
    ]
    ts_code: TechnicalInstrumentCode
    frequency: StockBarFrequency


class TechnicalCalculationToolResult(DomainModel):
    """五个确定性技术计算器共享的 Tool 外层信封。"""

    tool_name: str
    status: ToolResultStatus
    as_of: AwareDatetime
    source_context_refs: tuple[ContextRef, ...]
    source_subjects: tuple[TechnicalCalculationSubject, ...] = ()
    calculation: dict[str, Any] | None = None
    issues: list[ToolIssue]
    complete: bool
