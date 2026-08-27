"""基本面研究员每日模式使用的确定性全市场快照。"""

import asyncio
import hashlib
import statistics
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.services.base import AsOfValue, normalize_as_of
from stock_research_agent.services.equity_market_data import EquityMarketDataService
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.fundamental_data import FundamentalDataService
from stock_research_agent.services.instrument_reference import InstrumentReferenceService
from stock_research_agent.services.macro_data import MacroDataService
from stock_research_agent.services.models import ServiceDataset

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = time(15, 0)
_MIN_SECTOR_METRIC_SAMPLE = 5
_SECTOR_RANK_SIZE = 2


class FundamentalStockCandidate(DomainModel):
    """基本面候选的稳定证券身份。"""

    ts_code: str
    name: str
    industry: str | None = None


class ValuationCandidate(FundamentalStockCandidate):
    trade_date: date
    pe_ttm: float | None = None
    pb: float | None = None
    dividend_yield_ttm: float | None = None
    total_market_value: float | None = None


class ValuationCandidateGroups(DomainModel):
    """只陈述估值横截面极值，不把低估值直接解释成低估。"""

    lowest_positive_pe: tuple[ValuationCandidate, ...]
    highest_pe: tuple[ValuationCandidate, ...]
    highest_pb: tuple[ValuationCandidate, ...]
    highest_dividend_yield: tuple[ValuationCandidate, ...]


class EarningsForecastCandidate(FundamentalStockCandidate):
    announcement_date: date | None = None
    report_period: str | None = None
    forecast_type: str | None = None
    profit_change_min: float | None = None
    profit_change_max: float | None = None
    profit_change_midpoint: float | None = None


class EarningsExpressCandidate(FundamentalStockCandidate):
    announcement_date: date | None = None
    report_period: str | None = None
    revenue: float | None = None


class EarningsEventSnapshot(DomainModel):
    strongest_forecast_improvements: tuple[EarningsForecastCandidate, ...]
    strongest_forecast_deteriorations: tuple[EarningsForecastCandidate, ...]
    recent_earnings_express: tuple[EarningsExpressCandidate, ...]


class FinancialQualityCandidate(FundamentalStockCandidate):
    announcement_date: date | None = None
    report_period: str | None = None
    roe: float | None = None
    roa: float | None = None
    gross_profit_margin: float | None = None
    debt_to_assets: float | None = None
    operating_cash_flow_to_revenue: float | None = None
    prior_roe: float | None = None
    roe_change: float | None = None


class FinancialQualityCandidateGroups(DomainModel):
    highest_roe: tuple[FinancialQualityCandidate, ...]
    largest_roe_improvements: tuple[FinancialQualityCandidate, ...]
    largest_roe_deteriorations: tuple[FinancialQualityCandidate, ...]
    highest_debt_to_assets: tuple[FinancialQualityCandidate, ...]
    lowest_cash_flow_to_revenue: tuple[FinancialQualityCandidate, ...]


class SectorRepresentativeStock(FundamentalStockCandidate):
    """行业聚合中用于提示 Agent 继续查证的少量代表性公司。"""

    selection_signals: tuple[str, ...]


class SectorFundamentalCandidate(DomainModel):
    """按 ``stock_basic.industry`` 汇总的行业横截面；不是申万行业。"""

    sector_code: str
    sector_name: str
    member_count: int = Field(ge=1)
    valuation_sample_count: int = Field(ge=0)
    positive_pe_sample_count: int = Field(ge=0)
    pb_sample_count: int = Field(ge=0)
    dividend_sample_count: int = Field(ge=0)
    roe_sample_count: int = Field(ge=0)
    roe_change_sample_count: int = Field(ge=0)
    median_positive_pe_ttm: float | None = None
    median_pb: float | None = None
    median_dividend_yield_ttm: float | None = None
    median_roe: float | None = None
    median_roe_change: float | None = None
    recent_positive_forecast_count: int = Field(ge=0)
    recent_negative_forecast_count: int = Field(ge=0)
    recent_express_count: int = Field(ge=0)
    recent_positive_forecast_rate: float = Field(ge=0)
    recent_negative_forecast_rate: float = Field(ge=0)
    recent_express_rate: float = Field(ge=0)
    recent_reporting_event_rate: float = Field(ge=0)
    representative_stocks: tuple[SectorRepresentativeStock, ...]
    selection_signals: tuple[str, ...] = ()


class SectorFundamentalGroups(DomainModel):
    """只保留各维度少量极值行业，控制每日 Prompt 体积。"""

    classification_basis: str = "stock_basic.industry"
    classification_note: str = (
        "按 stock_basic.industry 聚合，不是申万行业分类；行业之间的会计口径可能不同"
    )
    minimum_metric_sample_size: int = Field(ge=1)
    valuation_extremes: tuple[SectorFundamentalCandidate, ...]
    financial_quality_extremes: tuple[SectorFundamentalCandidate, ...]
    recent_reporting_activity: tuple[SectorFundamentalCandidate, ...]


class MacroSeriesSnapshot(DomainModel):
    """不同宏观接口的最近两条原始观测；不擅自统一不同量纲。"""

    series: str
    observation_count: int = Field(ge=0)
    latest_period: str | None = None
    latest: dict[str, Any] | None = None
    previous_period: str | None = None
    previous: dict[str, Any] | None = None


class FundamentalSnapshotCoverage(DomainModel):
    listed_stock_count: int = Field(ge=0)
    classified_stock_count: int = Field(ge=0)
    sector_count: int = Field(ge=0)
    rank_eligible_sector_count: int = Field(ge=0)
    valuation_stock_count: int = Field(ge=0)
    recent_forecast_count: int = Field(ge=0)
    recent_express_count: int = Field(ge=0)
    current_indicator_count: int = Field(ge=0)
    comparable_indicator_count: int = Field(ge=0)
    macro_series_count: int = Field(ge=0)
    source_dataset_count: int = Field(ge=0)
    optional_failure_count: int = Field(ge=0)


class DailyFundamentalSnapshot(DomainModel):
    """给基本面 Agent 直接阅读、但尚未转成 Evidence 的确定性快照。"""

    trade_date: date
    report_period: str
    comparison_period: str
    announcement_lookback_days: int = Field(ge=1)
    valuations: ValuationCandidateGroups
    earnings_events: EarningsEventSnapshot
    financial_quality: FinancialQualityCandidateGroups
    sector_fundamentals: SectorFundamentalGroups
    macro_and_rates: tuple[MacroSeriesSnapshot, ...]
    coverage: FundamentalSnapshotCoverage


@dataclass(frozen=True, slots=True)
class DailyFundamentalSnapshotBuild:
    snapshot: DailyFundamentalSnapshot
    datasets: Mapping[str, ServiceDataset]
    optional_failures: Mapping[str, BaseException]


type _DatasetQuery = tuple[str, Callable[[], Awaitable[ServiceDataset]]]


class DailyFundamentalSnapshotService:
    """聚合市场、行业和代表性个股的每日基本面背景。"""

    def __init__(
        self,
        instrument_reference: InstrumentReferenceService,
        equity_market_data: EquityMarketDataService,
        fundamental_data: FundamentalDataService,
        macro_data: MacroDataService,
        *,
        max_concurrency: int = 6,
    ) -> None:
        if max_concurrency < 1:
            raise ServiceInputError("max_concurrency 必须大于 0")
        self._instrument_reference = instrument_reference
        self._equity_market_data = equity_market_data
        self._fundamental_data = fundamental_data
        self._macro_data = macro_data
        self._max_concurrency = max_concurrency

    async def build_daily_snapshot(
        self,
        *,
        as_of: AsOfValue,
        candidate_count: int = 10,
        announcement_lookback_days: int = 14,
    ) -> DailyFundamentalSnapshotBuild:
        if not 3 <= candidate_count <= 20:
            raise ServiceInputError("candidate_count 必须在 3 到 20 之间")
        if not 7 <= announcement_lookback_days <= 60:
            raise ServiceInputError("announcement_lookback_days 必须在 7 到 60 之间")
        cutoff_date = normalize_as_of(as_of)
        if cutoff_date is None:
            raise ServiceInputError("每日基本面快照必须提供 as_of")

        calendar = await self._instrument_reference.get_trade_calendar(
            "SSE",
            cutoff_date - timedelta(days=20),
            cutoff_date,
            as_of=as_of,
        )
        trade_date = _select_complete_trade_date(calendar, as_of)
        report_period = _latest_reporting_period(cutoff_date)
        comparison_period = f"{int(report_period[:4]) - 1}{report_period[4:]}"
        start_month = _shift_month(cutoff_date, -12).strftime("%Y%m")
        end_month = cutoff_date.strftime("%Y%m")
        start_quarter, end_quarter = _quarter_window(cutoff_date, quarters=8)
        rate_start = trade_date - timedelta(days=400)

        queries: tuple[_DatasetQuery, ...] = (
            (
                "stock_basic",
                lambda: self._instrument_reference.get_all_stocks(list_status="L", as_of=as_of),
            ),
            (
                "daily_valuation",
                lambda: self._equity_market_data.get_daily_market_valuation(
                    trade_date, as_of=as_of
                ),
            ),
            (
                "earnings_forecast",
                lambda: self._fundamental_data.get_earnings_forecast_batch(
                    report_period, as_of=as_of
                ),
            ),
            (
                "earnings_express",
                lambda: self._fundamental_data.get_earnings_express_batch(
                    report_period, as_of=as_of
                ),
            ),
            (
                "financial_indicators_current",
                lambda: self._fundamental_data.get_financial_indicators_batch(
                    report_period, as_of=as_of
                ),
            ),
            (
                "financial_indicators_comparison",
                lambda: self._fundamental_data.get_financial_indicators_batch(
                    comparison_period, as_of=as_of
                ),
            ),
            (
                "macro_gdp",
                lambda: self._macro_data.get_gdp(start_quarter, end_quarter, as_of=as_of),
            ),
            *(
                (
                    f"macro_{series}",
                    lambda item=series: self._macro_data.get_monthly_indicator(
                        item, start_month, end_month, as_of=as_of
                    ),
                )
                for series in ("cn_cpi", "cn_ppi", "cn_m", "sf_month", "cn_pmi")
            ),
            (
                "rate_shibor",
                lambda: self._macro_data.get_rate_range(
                    "shibor", rate_start, trade_date, as_of=as_of
                ),
            ),
            (
                "rate_lpr",
                lambda: self._macro_data.get_rate_range(
                    "shibor_lpr", rate_start, trade_date, as_of=as_of
                ),
            ),
            (
                "rate_us_treasury",
                lambda: self._macro_data.get_rate_range(
                    "us_tycr", rate_start, trade_date, as_of=as_of
                ),
            ),
        )
        datasets, failures = await self._run_optional_queries(queries)
        analytic_labels = set(datasets).difference({"stock_basic"})
        if not any(datasets[label].items for label in analytic_labels):
            raise ServiceDataValidationError("每日基本面快照没有取得任何分析数据")

        all_datasets = {"trade_calendar": calendar, **datasets}
        snapshot = _aggregate_snapshot(
            trade_date=trade_date,
            report_period=report_period,
            comparison_period=comparison_period,
            announcement_lookback_days=announcement_lookback_days,
            cutoff_date=cutoff_date,
            datasets=datasets,
            failure_count=len(failures),
            source_dataset_count=len(all_datasets),
            candidate_count=candidate_count,
        )
        return DailyFundamentalSnapshotBuild(
            snapshot=snapshot,
            datasets=all_datasets,
            optional_failures=failures,
        )

    async def _run_optional_queries(
        self,
        queries: tuple[_DatasetQuery, ...],
    ) -> tuple[dict[str, ServiceDataset], dict[str, BaseException]]:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(
            label: str,
            query: Callable[[], Awaitable[ServiceDataset]],
        ) -> tuple[str, ServiceDataset | BaseException]:
            async with semaphore:
                try:
                    return label, await query()
                except Exception as exc:
                    return label, exc

        outcomes = await asyncio.gather(*(run_one(label, query) for label, query in queries))
        datasets: dict[str, ServiceDataset] = {}
        failures: dict[str, BaseException] = {}
        for label, outcome in outcomes:
            if isinstance(outcome, BaseException):
                failures[label] = outcome
            else:
                datasets[label] = outcome
        return datasets, failures


def _aggregate_snapshot(
    *,
    trade_date: date,
    report_period: str,
    comparison_period: str,
    announcement_lookback_days: int,
    cutoff_date: date,
    datasets: Mapping[str, ServiceDataset],
    failure_count: int,
    source_dataset_count: int,
    candidate_count: int,
) -> DailyFundamentalSnapshot:
    stock_rows = _items(datasets, "stock_basic")
    names = {
        str(row.get("ts_code") or "").upper(): str(row.get("name") or "").strip()
        for row in stock_rows
        if row.get("ts_code")
    }
    industries = {
        str(row.get("ts_code") or "").upper(): industry
        for row in stock_rows
        if row.get("ts_code") and (industry := _text(row.get("industry"))) is not None
    }
    valuation_rows = _items(datasets, "daily_valuation")
    recent_start = cutoff_date - timedelta(days=announcement_lookback_days)
    forecast_rows = _recent_announcements(
        _items(datasets, "earnings_forecast"), recent_start, cutoff_date
    )
    express_rows = _recent_announcements(
        _items(datasets, "earnings_express"), recent_start, cutoff_date
    )
    current_rows = _latest_by_code(_items(datasets, "financial_indicators_current"))
    comparison_rows = _latest_by_code(_items(datasets, "financial_indicators_comparison"))

    return DailyFundamentalSnapshot(
        trade_date=trade_date,
        report_period=report_period,
        comparison_period=comparison_period,
        announcement_lookback_days=announcement_lookback_days,
        valuations=_valuation_groups(
            valuation_rows, names, industries, trade_date, candidate_count
        ),
        earnings_events=_earnings_events(
            forecast_rows, express_rows, names, industries, candidate_count
        ),
        financial_quality=_quality_groups(
            current_rows, comparison_rows, names, industries, candidate_count
        ),
        sector_fundamentals=_sector_fundamental_groups(
            names=names,
            industries=industries,
            valuations=valuation_rows,
            current_indicators=current_rows,
            comparison_indicators=comparison_rows,
            forecasts=forecast_rows,
            expresses=express_rows,
        ),
        macro_and_rates=tuple(
            _macro_series(dataset.api_name, dataset.items)
            for label, dataset in sorted(datasets.items())
            if label.startswith(("macro_", "rate_"))
        ),
        coverage=FundamentalSnapshotCoverage(
            listed_stock_count=len(names),
            classified_stock_count=len(industries),
            sector_count=len(set(industries.values())),
            rank_eligible_sector_count=sum(
                member_count >= _MIN_SECTOR_METRIC_SAMPLE
                for member_count in _industry_member_counts(industries).values()
            ),
            valuation_stock_count=len(valuation_rows),
            recent_forecast_count=len(forecast_rows),
            recent_express_count=len(express_rows),
            current_indicator_count=len(current_rows),
            comparable_indicator_count=len(set(current_rows).intersection(comparison_rows)),
            macro_series_count=sum(
                bool(dataset.items)
                for label, dataset in datasets.items()
                if label.startswith(("macro_", "rate_"))
            ),
            source_dataset_count=source_dataset_count,
            optional_failure_count=failure_count,
        ),
    )


def _valuation_groups(
    rows: list[dict[str, Any]],
    names: Mapping[str, str],
    industries: Mapping[str, str],
    trade_date: date,
    count: int,
) -> ValuationCandidateGroups:
    candidates = [
        _valuation_candidate(row, names, industries, trade_date)
        for row in rows
        if row.get("ts_code") not in (None, "")
    ]
    positive_pe = [item for item in candidates if item.pe_ttm is not None and item.pe_ttm > 0]
    with_pb = [item for item in candidates if item.pb is not None and item.pb > 0]
    with_dividend = [item for item in candidates if item.dividend_yield_ttm is not None]
    return ValuationCandidateGroups(
        lowest_positive_pe=tuple(sorted(positive_pe, key=lambda item: item.pe_ttm)[:count]),
        highest_pe=tuple(
            sorted(positive_pe, key=lambda item: item.pe_ttm or 0.0, reverse=True)[:count]
        ),
        highest_pb=tuple(sorted(with_pb, key=lambda item: item.pb or 0.0, reverse=True)[:count]),
        highest_dividend_yield=tuple(
            sorted(
                with_dividend,
                key=lambda item: item.dividend_yield_ttm or 0.0,
                reverse=True,
            )[:count]
        ),
    )


def _valuation_candidate(
    row: Mapping[str, Any],
    names: Mapping[str, str],
    industries: Mapping[str, str],
    trade_date: date,
) -> ValuationCandidate:
    code = str(row.get("ts_code") or "").upper()
    return ValuationCandidate(
        ts_code=code,
        name=names.get(code, code),
        industry=industries.get(code),
        trade_date=_parse_date(row.get("trade_date")) or trade_date,
        pe_ttm=_number(row.get("pe_ttm")),
        pb=_number(row.get("pb")),
        dividend_yield_ttm=_number(row.get("dv_ttm")),
        total_market_value=_number(row.get("total_mv")),
    )


def _earnings_events(
    forecasts: list[dict[str, Any]],
    expresses: list[dict[str, Any]],
    names: Mapping[str, str],
    industries: Mapping[str, str],
    count: int,
) -> EarningsEventSnapshot:
    forecast_candidates = [_forecast_candidate(row, names, industries) for row in forecasts]
    improvements = [
        item
        for item in forecast_candidates
        if item.profit_change_midpoint is not None and item.profit_change_midpoint > 0
    ]
    deteriorations = [
        item
        for item in forecast_candidates
        if item.profit_change_midpoint is not None and item.profit_change_midpoint < 0
    ]
    express_candidates = [_express_candidate(row, names, industries) for row in expresses]
    express_candidates.sort(
        key=lambda item: (item.announcement_date or date.min, item.ts_code), reverse=True
    )
    return EarningsEventSnapshot(
        strongest_forecast_improvements=tuple(
            sorted(
                improvements,
                key=lambda item: item.profit_change_midpoint or 0.0,
                reverse=True,
            )[:count]
        ),
        strongest_forecast_deteriorations=tuple(
            sorted(deteriorations, key=lambda item: item.profit_change_midpoint or 0.0)[:count]
        ),
        recent_earnings_express=tuple(express_candidates[:count]),
    )


def _forecast_candidate(
    row: Mapping[str, Any], names: Mapping[str, str], industries: Mapping[str, str]
) -> EarningsForecastCandidate:
    code = str(row.get("ts_code") or "").upper()
    lower = _number(row.get("p_change_min"))
    upper = _number(row.get("p_change_max"))
    midpoint = None
    if lower is not None and upper is not None:
        midpoint = (lower + upper) / 2
    elif lower is not None:
        midpoint = lower
    elif upper is not None:
        midpoint = upper
    return EarningsForecastCandidate(
        ts_code=code,
        name=names.get(code, code),
        industry=industries.get(code),
        announcement_date=_parse_date(row.get("ann_date")),
        report_period=_text(row.get("end_date")),
        forecast_type=_text(row.get("type")),
        profit_change_min=lower,
        profit_change_max=upper,
        profit_change_midpoint=midpoint,
    )


def _express_candidate(
    row: Mapping[str, Any], names: Mapping[str, str], industries: Mapping[str, str]
) -> EarningsExpressCandidate:
    code = str(row.get("ts_code") or "").upper()
    return EarningsExpressCandidate(
        ts_code=code,
        name=names.get(code, code),
        industry=industries.get(code),
        announcement_date=_parse_date(row.get("ann_date")),
        report_period=_text(row.get("end_date")),
        revenue=_number(row.get("revenue")),
    )


def _quality_groups(
    current: Mapping[str, dict[str, Any]],
    comparison: Mapping[str, dict[str, Any]],
    names: Mapping[str, str],
    industries: Mapping[str, str],
    count: int,
) -> FinancialQualityCandidateGroups:
    candidates = [
        _quality_candidate(code, row, comparison.get(code), names, industries)
        for code, row in current.items()
    ]
    return FinancialQualityCandidateGroups(
        highest_roe=tuple(_rank_quality(candidates, "roe", count, reverse=True)),
        largest_roe_improvements=tuple(
            _rank_quality(candidates, "roe_change", count, reverse=True, positive_only=True)
        ),
        largest_roe_deteriorations=tuple(
            _rank_quality(candidates, "roe_change", count, reverse=False, negative_only=True)
        ),
        highest_debt_to_assets=tuple(
            _rank_quality(candidates, "debt_to_assets", count, reverse=True)
        ),
        lowest_cash_flow_to_revenue=tuple(
            _rank_quality(candidates, "operating_cash_flow_to_revenue", count, reverse=False)
        ),
    )


def _quality_candidate(
    code: str,
    row: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    names: Mapping[str, str],
    industries: Mapping[str, str],
) -> FinancialQualityCandidate:
    roe = _number(row.get("roe"))
    prior_roe = _number(prior.get("roe")) if prior is not None else None
    return FinancialQualityCandidate(
        ts_code=code,
        name=names.get(code, code),
        industry=industries.get(code),
        announcement_date=_parse_date(row.get("ann_date")),
        report_period=_text(row.get("end_date")),
        roe=roe,
        roa=_number(row.get("roa")),
        gross_profit_margin=_number(row.get("grossprofit_margin")),
        debt_to_assets=_number(row.get("debt_to_assets")),
        operating_cash_flow_to_revenue=_number(row.get("ocf_to_or")),
        prior_roe=prior_roe,
        roe_change=(roe - prior_roe if roe is not None and prior_roe is not None else None),
    )


def _rank_quality(
    candidates: list[FinancialQualityCandidate],
    field: str,
    count: int,
    *,
    reverse: bool,
    positive_only: bool = False,
    negative_only: bool = False,
) -> list[FinancialQualityCandidate]:
    usable = [item for item in candidates if getattr(item, field) is not None]
    if positive_only:
        usable = [item for item in usable if getattr(item, field) > 0]
    if negative_only:
        usable = [item for item in usable if getattr(item, field) < 0]
    return sorted(usable, key=lambda item: getattr(item, field), reverse=reverse)[:count]


def _sector_fundamental_groups(
    *,
    names: Mapping[str, str],
    industries: Mapping[str, str],
    valuations: list[dict[str, Any]],
    current_indicators: Mapping[str, dict[str, Any]],
    comparison_indicators: Mapping[str, dict[str, Any]],
    forecasts: list[dict[str, Any]],
    expresses: list[dict[str, Any]],
) -> SectorFundamentalGroups:
    """将个股原始行确定性聚合为紧凑的 stock_basic 行业横截面。"""

    member_counts = _industry_member_counts(industries)
    valuation_by_sector = _rows_by_industry(valuations, industries)
    current_by_sector = _mapping_rows_by_industry(current_indicators, industries)
    latest_forecasts = _latest_by_code(forecasts)
    latest_expresses = _latest_by_code(expresses)
    forecast_by_sector = _mapping_rows_by_industry(latest_forecasts, industries)
    express_by_sector = _mapping_rows_by_industry(latest_expresses, industries)

    candidates = [
        _sector_candidate(
            sector_name=sector_name,
            member_count=member_count,
            names=names,
            industries=industries,
            valuations=valuation_by_sector.get(sector_name, []),
            current_indicators=current_by_sector.get(sector_name, []),
            comparison_indicators=comparison_indicators,
            forecasts=forecast_by_sector.get(sector_name, []),
            expresses=express_by_sector.get(sector_name, []),
        )
        for sector_name, member_count in sorted(member_counts.items())
    ]

    valuation_extremes = _rank_sector_candidates(
        candidates,
        (
            (
                "lowest_median_positive_pe",
                "median_positive_pe_ttm",
                "positive_pe_sample_count",
                False,
                False,
                False,
            ),
            (
                "highest_median_positive_pe",
                "median_positive_pe_ttm",
                "positive_pe_sample_count",
                True,
                False,
                False,
            ),
            ("highest_median_pb", "median_pb", "pb_sample_count", True, False, False),
            (
                "highest_median_dividend_yield",
                "median_dividend_yield_ttm",
                "dividend_sample_count",
                True,
                True,
                False,
            ),
        ),
    )
    financial_quality_extremes = _rank_sector_candidates(
        candidates,
        (
            ("highest_median_roe", "median_roe", "roe_sample_count", True, False, False),
            (
                "largest_median_roe_improvement",
                "median_roe_change",
                "roe_change_sample_count",
                True,
                True,
                False,
            ),
            (
                "largest_median_roe_deterioration",
                "median_roe_change",
                "roe_change_sample_count",
                False,
                False,
                True,
            ),
        ),
    )
    reporting_activity = _rank_sector_candidates(
        candidates,
        (
            (
                "highest_recent_positive_forecast_rate",
                "recent_positive_forecast_rate",
                "member_count",
                True,
                True,
                False,
            ),
            (
                "highest_recent_negative_forecast_rate",
                "recent_negative_forecast_rate",
                "member_count",
                True,
                True,
                False,
            ),
            (
                "highest_recent_express_rate",
                "recent_express_rate",
                "member_count",
                True,
                True,
                False,
            ),
        ),
    )
    return SectorFundamentalGroups(
        minimum_metric_sample_size=_MIN_SECTOR_METRIC_SAMPLE,
        valuation_extremes=valuation_extremes,
        financial_quality_extremes=financial_quality_extremes,
        recent_reporting_activity=reporting_activity,
    )


def _sector_candidate(
    *,
    sector_name: str,
    member_count: int,
    names: Mapping[str, str],
    industries: Mapping[str, str],
    valuations: list[dict[str, Any]],
    current_indicators: list[dict[str, Any]],
    comparison_indicators: Mapping[str, dict[str, Any]],
    forecasts: list[dict[str, Any]],
    expresses: list[dict[str, Any]],
) -> SectorFundamentalCandidate:
    positive_pe = _numbers(valuations, "pe_ttm", positive_only=True)
    pb_values = _numbers(valuations, "pb", positive_only=True)
    dividend_values = _numbers(valuations, "dv_ttm")
    roe_values = _numbers(current_indicators, "roe")
    roe_changes = [
        current_roe - prior_roe
        for row in current_indicators
        if (code := _text(row.get("ts_code"))) is not None
        and (current_roe := _number(row.get("roe"))) is not None
        and (prior := comparison_indicators.get(code.upper())) is not None
        and (prior_roe := _number(prior.get("roe"))) is not None
    ]
    positive_forecast_count = sum(
        (midpoint := _forecast_midpoint(row)) is not None and midpoint > 0 for row in forecasts
    )
    negative_forecast_count = sum(
        (midpoint := _forecast_midpoint(row)) is not None and midpoint < 0 for row in forecasts
    )
    express_count = len(expresses)
    valuation_codes = {
        str(row.get("ts_code") or "").upper()
        for row in valuations
        if row.get("ts_code")
        and any(_number(row.get(field)) is not None for field in ("pe_ttm", "pb", "dv_ttm"))
    }
    denominator = float(member_count)
    return SectorFundamentalCandidate(
        sector_code=_stable_sector_code(sector_name),
        sector_name=sector_name,
        member_count=member_count,
        valuation_sample_count=len(valuation_codes),
        positive_pe_sample_count=len(positive_pe),
        pb_sample_count=len(pb_values),
        dividend_sample_count=len(dividend_values),
        roe_sample_count=len(roe_values),
        roe_change_sample_count=len(roe_changes),
        median_positive_pe_ttm=_median(positive_pe),
        median_pb=_median(pb_values),
        median_dividend_yield_ttm=_median(dividend_values),
        median_roe=_median(roe_values),
        median_roe_change=_median(roe_changes),
        recent_positive_forecast_count=positive_forecast_count,
        recent_negative_forecast_count=negative_forecast_count,
        recent_express_count=express_count,
        recent_positive_forecast_rate=positive_forecast_count / denominator,
        recent_negative_forecast_rate=negative_forecast_count / denominator,
        recent_express_rate=express_count / denominator,
        recent_reporting_event_rate=(
            positive_forecast_count + negative_forecast_count + express_count
        )
        / denominator,
        representative_stocks=_sector_representatives(
            sector_name=sector_name,
            names=names,
            industries=industries,
            valuations=valuations,
            current_indicators=current_indicators,
            comparison_indicators=comparison_indicators,
            forecasts=forecasts,
            expresses=expresses,
        ),
    )


def _rank_sector_candidates(
    candidates: list[SectorFundamentalCandidate],
    rankings: tuple[tuple[str, str, str, bool, bool, bool], ...],
) -> tuple[SectorFundamentalCandidate, ...]:
    selected: dict[str, SectorFundamentalCandidate] = {}
    signals: dict[str, list[str]] = {}
    for signal, value_field, sample_field, reverse, positive_only, negative_only in rankings:
        usable = [
            candidate
            for candidate in candidates
            if getattr(candidate, value_field) is not None
            and getattr(candidate, sample_field) >= _MIN_SECTOR_METRIC_SAMPLE
            and (not positive_only or getattr(candidate, value_field) > 0)
            and (not negative_only or getattr(candidate, value_field) < 0)
        ]
        usable.sort(key=lambda item: item.sector_code)
        usable.sort(key=lambda item: getattr(item, value_field), reverse=reverse)
        for candidate in usable[:_SECTOR_RANK_SIZE]:
            selected.setdefault(candidate.sector_code, candidate)
            signals.setdefault(candidate.sector_code, []).append(signal)
    return tuple(
        candidate.model_copy(update={"selection_signals": tuple(signals[sector_code])})
        for sector_code, candidate in selected.items()
    )


def _sector_representatives(
    *,
    sector_name: str,
    names: Mapping[str, str],
    industries: Mapping[str, str],
    valuations: list[dict[str, Any]],
    current_indicators: list[dict[str, Any]],
    comparison_indicators: Mapping[str, dict[str, Any]],
    forecasts: list[dict[str, Any]],
    expresses: list[dict[str, Any]],
) -> tuple[SectorRepresentativeStock, ...]:
    scores: dict[str, float] = {}
    signals: dict[str, set[str]] = {}

    def add(code: str, signal: str, score: float) -> None:
        normalized = code.upper()
        if not normalized or industries.get(normalized) != sector_name:
            return
        scores[normalized] = max(scores.get(normalized, 0.0), score)
        signals.setdefault(normalized, set()).add(signal)

    for row in forecasts:
        code = str(row.get("ts_code") or "")
        midpoint = _forecast_midpoint(row)
        if midpoint is not None and midpoint != 0:
            signal = "recent_positive_forecast" if midpoint > 0 else "recent_negative_forecast"
            add(code, signal, 400.0 + min(abs(midpoint), 100.0))
    for row in expresses:
        add(str(row.get("ts_code") or ""), "recent_earnings_express", 350.0)
    for row in current_indicators:
        code = str(row.get("ts_code") or "").upper()
        current_roe = _number(row.get("roe"))
        prior = comparison_indicators.get(code)
        prior_roe = _number(prior.get("roe")) if prior is not None else None
        if current_roe is not None and prior_roe is not None:
            change = current_roe - prior_roe
            add(code, "large_absolute_roe_change", 200.0 + min(abs(change), 100.0))

    positive_pe_rows = [row for row in valuations if (_number(row.get("pe_ttm")) or 0) > 0]
    if positive_pe_rows:
        lowest_pe = min(positive_pe_rows, key=lambda row: _number(row.get("pe_ttm")) or 0.0)
        highest_pe = max(positive_pe_rows, key=lambda row: _number(row.get("pe_ttm")) or 0.0)
        add(str(lowest_pe.get("ts_code") or ""), "sector_low_positive_pe", 110.0)
        add(str(highest_pe.get("ts_code") or ""), "sector_high_positive_pe", 105.0)
    dividend_rows = [row for row in valuations if _number(row.get("dv_ttm")) is not None]
    if dividend_rows:
        highest_dividend = max(dividend_rows, key=lambda row: _number(row.get("dv_ttm")) or 0.0)
        add(str(highest_dividend.get("ts_code") or ""), "sector_high_dividend_yield", 100.0)

    ordered_codes = sorted(
        scores,
        key=lambda code: (-scores[code], -len(signals[code]), code),
    )[:3]
    return tuple(
        SectorRepresentativeStock(
            ts_code=code,
            name=names.get(code, code),
            industry=sector_name,
            selection_signals=tuple(sorted(signals[code])),
        )
        for code in ordered_codes
    )


def _industry_member_counts(industries: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for industry in industries.values():
        counts[industry] = counts.get(industry, 0) + 1
    return counts


def _rows_by_industry(
    rows: list[dict[str, Any]], industries: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("ts_code") or "").upper()
        if industry := industries.get(code):
            grouped.setdefault(industry, []).append(row)
    return grouped


def _mapping_rows_by_industry(
    rows: Mapping[str, dict[str, Any]], industries: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    return _rows_by_industry(list(rows.values()), industries)


def _numbers(rows: list[dict[str, Any]], field: str, *, positive_only: bool = False) -> list[float]:
    values = [number for row in rows if (number := _number(row.get(field))) is not None]
    return [number for number in values if number > 0] if positive_only else values


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _forecast_midpoint(row: Mapping[str, Any]) -> float | None:
    lower = _number(row.get("p_change_min"))
    upper = _number(row.get("p_change_max"))
    if lower is not None and upper is not None:
        return (lower + upper) / 2
    return lower if lower is not None else upper


def _stable_sector_code(sector_name: str) -> str:
    digest = hashlib.sha256(sector_name.encode("utf-8")).hexdigest()[:12].upper()
    return f"SBI-{digest}"


def _macro_series(series: str, rows: list[dict[str, Any]]) -> MacroSeriesSnapshot:
    ordered = sorted(rows, key=_observation_key)
    latest = ordered[-1] if ordered else None
    previous = ordered[-2] if len(ordered) > 1 else None
    return MacroSeriesSnapshot(
        series=series,
        observation_count=len(rows),
        latest_period=_observation_period(latest),
        latest=dict(latest) if latest is not None else None,
        previous_period=_observation_period(previous),
        previous=dict(previous) if previous is not None else None,
    )


def _latest_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("ts_code") or "").upper()
        if code:
            grouped.setdefault(code, []).append(row)
    return {
        code: max(
            entries,
            key=lambda row: (
                _text(row.get("ann_date")) or "",
                _number(row.get("update_flag")) or 0.0,
            ),
        )
        for code, entries in grouped.items()
    }


def _recent_announcements(
    rows: list[dict[str, Any]], start: date, end: date
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (ann_date := _parse_date(row.get("ann_date"))) is not None and start <= ann_date <= end
    ]


def _items(datasets: Mapping[str, ServiceDataset], label: str) -> list[dict[str, Any]]:
    dataset = datasets.get(label)
    return dataset.items if dataset is not None else []


def _select_complete_trade_date(calendar: ServiceDataset, as_of: AsOfValue) -> date:
    cutoff = normalize_as_of(as_of)
    if cutoff is None:
        raise ServiceInputError("每日基本面快照必须提供 as_of")
    local_time: time | None = None
    if isinstance(as_of, datetime):
        local_time = as_of.astimezone(_SHANGHAI).time().replace(tzinfo=None)
    open_dates = sorted(
        parsed
        for row in calendar.items
        if _integer(row.get("is_open")) == 1
        and (parsed := _parse_date(row.get("cal_date"))) is not None
        and parsed <= cutoff
        and not (parsed == cutoff and local_time is not None and local_time < _MARKET_CLOSE)
    )
    if not open_dates:
        raise ServiceDataValidationError("交易日历中找不到 as_of 之前的完整交易日")
    return open_dates[-1]


def _latest_reporting_period(cutoff: date) -> str:
    """选择通常已经进入披露窗口的最近报告期，避免季末第一天就查空新季度。"""

    if cutoff.month <= 4:
        return f"{cutoff.year - 1}1231"
    if cutoff.month <= 6:
        return f"{cutoff.year}0331"
    if cutoff.month <= 10:
        return f"{cutoff.year}0630"
    return f"{cutoff.year}0930"


def _shift_month(value: date, offset: int) -> date:
    absolute = value.year * 12 + value.month - 1 + offset
    year, zero_based_month = divmod(absolute, 12)
    return date(year, zero_based_month + 1, 1)


def _quarter_window(value: date, *, quarters: int) -> tuple[str, str]:
    end_index = value.year * 4 + (value.month - 1) // 3
    start_index = end_index - quarters + 1

    def render(index: int) -> str:
        year, zero_based_quarter = divmod(index, 4)
        return f"{year}Q{zero_based_quarter + 1}"

    return render(start_index), render(end_index)


def _observation_key(row: Mapping[str, Any]) -> str:
    return _observation_period(row) or ""


def _observation_period(row: Mapping[str, Any] | None) -> str | None:
    if row is None:
        return None
    for field in ("date", "trade_date", "month", "quarter"):
        value = _text(row.get(field))
        if value:
            return value
    return None


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("-", "")[:8]
    if len(normalized) != 8 or not normalized.isdigit():
        return None
    try:
        return datetime.strptime(normalized, "%Y%m%d").date()
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None
