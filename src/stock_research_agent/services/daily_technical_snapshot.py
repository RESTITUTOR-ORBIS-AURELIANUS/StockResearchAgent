"""技术分析 Agent 每日模式使用的确定性全市场横截面快照。"""

import asyncio
import math
import statistics
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from pydantic import Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.services.base import AsOfValue, normalize_as_of
from stock_research_agent.services.equity_market_data import EquityMarketDataService
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.instrument_reference import InstrumentReferenceService
from stock_research_agent.services.models import ServiceDataset

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = time(15, 0)
_EPSILON = 1e-9

_MARKET_INDICES: Final = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)
_BENCHMARK_INDICES: Final = (
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
)


class DailySnapshotCoverage(DomainModel):
    """各源表对当日股票目录的覆盖情况。"""

    listed_stock_count: int = Field(ge=0)
    traded_stock_count: int = Field(ge=0)
    valuation_stock_count: int = Field(ge=0)
    price_limit_stock_count: int = Field(ge=0)
    suspension_record_count: int = Field(ge=0)
    st_stock_count: int = Field(ge=0)
    industry_count: int = Field(ge=0)
    industry_member_row_count: int = Field(ge=0)
    benchmark_weight_row_count: int = Field(ge=0)
    traded_coverage_ratio: float | None = None
    valuation_coverage_ratio: float | None = None


class MarketBreadthSnapshot(DomainModel):
    """全市场涨跌、涨跌停、停牌和成交概况。"""

    advancing_count: int = Field(ge=0)
    declining_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    limit_up_count: int = Field(ge=0)
    limit_down_count: int = Field(ge=0)
    suspended_count: int = Field(ge=0)
    total_amount: float | None = None
    median_pct_change: float | None = None
    advance_decline_ratio: float | None = None


class MarketIndexSnapshot(DomainModel):
    ts_code: str
    name: str
    trade_date: date | None = None
    close: float | None = None
    pct_change: float | None = None
    amount: float | None = None


class IndustryBreadthSnapshot(DomainModel):
    index_code: str
    industry_name: str
    member_count: int = Field(ge=0)
    traded_member_count: int = Field(ge=0)
    advancing_count: int = Field(ge=0)
    declining_count: int = Field(ge=0)
    flat_count: int = Field(ge=0)
    median_member_pct_change: float | None = None
    index_trade_date: date | None = None
    index_pct_change: float | None = None
    index_amount: float | None = None
    leading_stock_code: str | None = None
    leading_stock_name: str | None = None
    leading_stock_pct_change: float | None = None
    lagging_stock_code: str | None = None
    lagging_stock_name: str | None = None
    lagging_stock_pct_change: float | None = None


class CandidateBenchmarkMembership(DomainModel):
    index_code: str
    index_name: str
    weight: float | None = None
    weight_trade_date: date | None = None


class DailyStockCandidate(DomainModel):
    ts_code: str
    name: str
    industry_l1_code: str | None = None
    industry_l1_name: str | None = None
    close: float | None = None
    pct_change: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    total_mv: float | None = None
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_st: bool = False
    benchmark_memberships: tuple[CandidateBenchmarkMembership, ...] = ()


class DailyCandidateGroups(DomainModel):
    """给 Agent 的少量查证候选；同一股票可以出现在多个组。"""

    top_gainers: tuple[DailyStockCandidate, ...]
    top_losers: tuple[DailyStockCandidate, ...]
    highest_amount: tuple[DailyStockCandidate, ...]
    highest_turnover: tuple[DailyStockCandidate, ...]
    highest_volume_ratio: tuple[DailyStockCandidate, ...]


class BenchmarkConstituentWeight(DomainModel):
    ts_code: str
    name: str | None = None
    weight: float | None = None


class BenchmarkCompositionSnapshot(DomainModel):
    index_code: str
    index_name: str
    weight_trade_date: date | None = None
    constituent_count: int = Field(ge=0)
    top_constituents: tuple[BenchmarkConstituentWeight, ...]


class DailyTechnicalSnapshot(DomainModel):
    """每日技术 Agent 可以直接阅读、但尚未转成 Evidence 的确定性快照。"""

    trade_date: date
    industry_standard: str = "SW2021"
    industry_level: str = "L1"
    coverage: DailySnapshotCoverage
    market_breadth: MarketBreadthSnapshot
    market_indices: tuple[MarketIndexSnapshot, ...]
    industries: tuple[IndustryBreadthSnapshot, ...]
    candidates: DailyCandidateGroups
    benchmarks: tuple[BenchmarkCompositionSnapshot, ...]


@dataclass(frozen=True, slots=True)
class DailyTechnicalSnapshotBuild:
    """聚合快照及其全部原始数据集；Tool 会把后者存入 run-scoped Store。"""

    snapshot: DailyTechnicalSnapshot
    datasets: Mapping[str, ServiceDataset]
    optional_failures: Mapping[str, BaseException]


type _DatasetQuery = tuple[str, Callable[[], Awaitable[ServiceDataset]]]


class DailyTechnicalSnapshotService:
    """组合多个源数据 Service，为技术分析每日模式生成一个可复现横截面。"""

    def __init__(
        self,
        instrument_reference: InstrumentReferenceService,
        equity_market_data: EquityMarketDataService,
        *,
        max_concurrency: int = 8,
    ) -> None:
        if max_concurrency < 1:
            raise ServiceInputError("max_concurrency 必须大于 0")
        self._instrument_reference = instrument_reference
        self._equity_market_data = equity_market_data
        self._max_concurrency = max_concurrency

    async def build_daily_snapshot(
        self,
        *,
        as_of: AsOfValue,
        candidate_count: int = 10,
    ) -> DailyTechnicalSnapshotBuild:
        """生成最近完整交易日的市场、行业和异常个股候选快照。"""

        if not 3 <= candidate_count <= 20:
            raise ServiceInputError("candidate_count 必须在 3 到 20 之间")
        cutoff_date = normalize_as_of(as_of)
        if cutoff_date is None:
            raise ServiceInputError("每日技术快照必须提供 as_of")

        calendar_start = cutoff_date - timedelta(days=20)
        calendar = await self._instrument_reference.get_trade_calendar(
            "SSE",
            calendar_start,
            cutoff_date,
            as_of=as_of,
        )
        trade_date = self._select_complete_trade_date(calendar, as_of)

        first_stage = await self._run_queries(
            (
                (
                    "stock_basic",
                    lambda: self._instrument_reference.get_all_stocks(list_status="L", as_of=as_of),
                ),
                (
                    "daily_bars",
                    lambda: self._equity_market_data.get_daily_market_bars(trade_date, as_of=as_of),
                ),
                (
                    "daily_valuation",
                    lambda: self._equity_market_data.get_daily_market_valuation(
                        trade_date, as_of=as_of
                    ),
                ),
                (
                    "daily_limits",
                    lambda: self._equity_market_data.get_daily_market_limits(
                        trade_date, as_of=as_of
                    ),
                ),
                (
                    "daily_suspensions",
                    lambda: self._equity_market_data.get_daily_market_suspensions(
                        trade_date, as_of=as_of
                    ),
                ),
                (
                    "st_stocks",
                    lambda: self._instrument_reference.get_st_list(trade_date, as_of=as_of),
                ),
                (
                    "sw_l1_classifications",
                    lambda: self._instrument_reference.get_industry_classifications(
                        src="SW2021", level="L1", as_of=as_of
                    ),
                ),
            )
        )
        self._require_nonempty(first_stage, "stock_basic", "daily_bars", "sw_l1_classifications")

        classifications = _unique_classifications(first_stage["sw_l1_classifications"].items)
        second_stage_queries: list[_DatasetQuery] = []
        industry_window_start = trade_date - timedelta(days=45)
        weight_window_start = trade_date - timedelta(days=62)
        optional_industry_queries: list[_DatasetQuery] = []

        for classification in classifications:
            index_code = classification["index_code"]
            suffix = _label_suffix(index_code)
            second_stage_queries.append(
                (
                    f"sw_members_{suffix}",
                    lambda code=index_code: self._instrument_reference.get_industry_members(
                        code, is_new="Y", as_of=as_of
                    ),
                )
            )
            optional_industry_queries.append(
                (
                    f"sw_daily_{suffix}",
                    lambda code=index_code: self._equity_market_data.get_sw_industry_bars(
                        code,
                        industry_window_start,
                        trade_date,
                        as_of=as_of,
                    ),
                )
            )

        for index_code, _ in _MARKET_INDICES:
            second_stage_queries.append(
                (
                    f"market_index_{_label_suffix(index_code)}",
                    lambda code=index_code: self._equity_market_data.get_index_bars(
                        code, trade_date, trade_date, as_of=as_of
                    ),
                )
            )

        for index_code, _ in _BENCHMARK_INDICES:
            second_stage_queries.append(
                (
                    f"benchmark_weight_{_label_suffix(index_code)}",
                    lambda code=index_code: self._instrument_reference.get_index_weights(
                        code, weight_window_start, trade_date, as_of=as_of
                    ),
                )
            )

        second_stage = await self._run_queries(tuple(second_stage_queries))
        industry_daily, optional_failures = await self._run_optional_queries(
            tuple(optional_industry_queries)
        )
        datasets = {
            "trade_calendar": calendar,
            **first_stage,
            **second_stage,
            **industry_daily,
        }
        snapshot = self._aggregate(
            trade_date=trade_date,
            datasets=datasets,
            classifications=classifications,
            candidate_count=candidate_count,
        )
        return DailyTechnicalSnapshotBuild(
            snapshot=snapshot,
            datasets=datasets,
            optional_failures=optional_failures,
        )

    async def _run_queries(self, queries: tuple[_DatasetQuery, ...]) -> dict[str, ServiceDataset]:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(
            label: str,
            query: Callable[[], Awaitable[ServiceDataset]],
        ) -> tuple[str, ServiceDataset]:
            async with semaphore:
                return label, await query()

        results = await asyncio.gather(*(run_one(label, query) for label, query in queries))
        return dict(results)

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

    @staticmethod
    def _select_complete_trade_date(calendar: ServiceDataset, as_of: AsOfValue) -> date:
        cutoff = normalize_as_of(as_of)
        if cutoff is None:
            raise ServiceInputError("每日技术快照必须提供 as_of")
        local_time: time | None = None
        if isinstance(as_of, datetime):
            local_time = as_of.astimezone(_SHANGHAI).time().replace(tzinfo=None)

        open_dates = sorted(
            parsed
            for row in calendar.items
            if _as_int(row.get("is_open")) == 1
            and (parsed := _parse_yyyymmdd(row.get("cal_date"))) is not None
            and parsed <= cutoff
            and not (parsed == cutoff and local_time is not None and local_time < _MARKET_CLOSE)
        )
        if not open_dates:
            raise ServiceDataValidationError("交易日历中找不到 as_of 之前的完整交易日")
        return open_dates[-1]

    @staticmethod
    def _require_nonempty(
        datasets: Mapping[str, ServiceDataset],
        *labels: str,
    ) -> None:
        empty = [label for label in labels if not datasets[label].items]
        if empty:
            raise ServiceDataValidationError(f"每日技术快照的必要数据集为空：{empty}")

    def _aggregate(
        self,
        *,
        trade_date: date,
        datasets: Mapping[str, ServiceDataset],
        classifications: tuple[dict[str, str], ...],
        candidate_count: int,
    ) -> DailyTechnicalSnapshot:
        basics = _rows_by_code(datasets["stock_basic"].items)
        bars = _rows_by_code(datasets["daily_bars"].items)
        valuations = _rows_by_code(datasets["daily_valuation"].items)
        limits = _rows_by_code(datasets["daily_limits"].items)
        suspension_codes = set(_rows_by_code(datasets["daily_suspensions"].items))
        st_codes = set(_rows_by_code(datasets["st_stocks"].items))

        industry_by_stock: dict[str, tuple[str, str]] = {}
        all_member_rows: list[dict[str, Any]] = []
        for classification in classifications:
            code = classification["index_code"]
            member_rows = datasets[f"sw_members_{_label_suffix(code)}"].items
            all_member_rows.extend(member_rows)
            for row in member_rows:
                stock_code = str(row.get("ts_code") or "").upper()
                if stock_code:
                    industry_by_stock.setdefault(
                        stock_code,
                        (code, classification["industry_name"]),
                    )

        latest_weights = _latest_benchmark_weights(datasets)
        candidate_factory = lambda code: _candidate_for_code(  # noqa: E731
            code,
            basics=basics,
            bars=bars,
            valuations=valuations,
            limits=limits,
            st_codes=st_codes,
            industry_by_stock=industry_by_stock,
            benchmark_weights=latest_weights,
        )
        pct_values = [_number(row.get("pct_chg")) for row in bars.values()]
        pct_values = [value for value in pct_values if value is not None]
        advancing = sum(value > _EPSILON for value in pct_values)
        declining = sum(value < -_EPSILON for value in pct_values)
        flat = len(pct_values) - advancing - declining
        limit_up_count = sum(
            _at_price_limit(row, limits.get(code), upper=True) for code, row in bars.items()
        )
        limit_down_count = sum(
            _at_price_limit(row, limits.get(code), upper=False) for code, row in bars.items()
        )
        total_amount = _sum_numbers(row.get("amount") for row in bars.values())

        industries = tuple(
            sorted(
                (
                    _build_industry_snapshot(
                        classification,
                        datasets,
                        basics=basics,
                        bars=bars,
                    )
                    for classification in classifications
                ),
                key=lambda item: (
                    item.index_pct_change is None,
                    -(item.index_pct_change or 0.0),
                    item.index_code,
                ),
            )
        )
        market_indices = tuple(
            _build_market_index_snapshot(code, name, datasets) for code, name in _MARKET_INDICES
        )
        benchmarks = tuple(
            _build_benchmark_snapshot(code, name, latest_weights, basics)
            for code, name in _BENCHMARK_INDICES
        )

        return DailyTechnicalSnapshot(
            trade_date=trade_date,
            coverage=DailySnapshotCoverage(
                listed_stock_count=len(basics),
                traded_stock_count=len(bars),
                valuation_stock_count=len(valuations),
                price_limit_stock_count=len(limits),
                suspension_record_count=len(datasets["daily_suspensions"].items),
                st_stock_count=len(st_codes),
                industry_count=len(classifications),
                industry_member_row_count=len(all_member_rows),
                benchmark_weight_row_count=sum(len(rows) for rows in latest_weights.values()),
                traded_coverage_ratio=_ratio(len(bars), len(basics)),
                valuation_coverage_ratio=_ratio(len(valuations), len(bars)),
            ),
            market_breadth=MarketBreadthSnapshot(
                advancing_count=advancing,
                declining_count=declining,
                flat_count=flat,
                limit_up_count=limit_up_count,
                limit_down_count=limit_down_count,
                suspended_count=len(suspension_codes),
                total_amount=total_amount,
                median_pct_change=statistics.median(pct_values) if pct_values else None,
                advance_decline_ratio=_ratio(advancing, declining),
            ),
            market_indices=market_indices,
            industries=industries,
            candidates=DailyCandidateGroups(
                top_gainers=_rank_candidates(
                    bars, candidate_factory, "pct_chg", candidate_count, reverse=True
                ),
                top_losers=_rank_candidates(
                    bars, candidate_factory, "pct_chg", candidate_count, reverse=False
                ),
                highest_amount=_rank_candidates(
                    bars, candidate_factory, "amount", candidate_count, reverse=True
                ),
                highest_turnover=_rank_candidates(
                    valuations,
                    candidate_factory,
                    "turnover_rate",
                    candidate_count,
                    reverse=True,
                ),
                highest_volume_ratio=_rank_candidates(
                    valuations,
                    candidate_factory,
                    "volume_ratio",
                    candidate_count,
                    reverse=True,
                ),
            ),
            benchmarks=benchmarks,
        )


def _unique_classifications(rows: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    classifications: dict[str, dict[str, str]] = {}
    for row in rows:
        index_code = str(row.get("index_code") or "").upper()
        industry_name = str(row.get("industry_name") or "").strip()
        if index_code and industry_name:
            classifications.setdefault(
                index_code,
                {"index_code": index_code, "industry_name": industry_name},
            )
    return tuple(classifications[code] for code in sorted(classifications))


def _rows_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row["ts_code"]).upper(): row for row in rows if row.get("ts_code") not in (None, "")
    }


def _latest_benchmark_weights(
    datasets: Mapping[str, ServiceDataset],
) -> dict[str, list[dict[str, Any]]]:
    latest: dict[str, list[dict[str, Any]]] = {}
    for index_code, _ in _BENCHMARK_INDICES:
        rows = datasets[f"benchmark_weight_{_label_suffix(index_code)}"].items
        dated_rows = [
            (parsed, row)
            for row in rows
            if (parsed := _parse_yyyymmdd(row.get("trade_date"))) is not None
        ]
        if not dated_rows:
            latest[index_code] = []
            continue
        latest_date = max(parsed for parsed, _ in dated_rows)
        latest[index_code] = [row for parsed, row in dated_rows if parsed == latest_date]
    return latest


def _build_market_index_snapshot(
    index_code: str,
    index_name: str,
    datasets: Mapping[str, ServiceDataset],
) -> MarketIndexSnapshot:
    rows = datasets[f"market_index_{_label_suffix(index_code)}"].items
    row = _latest_row(rows)
    return MarketIndexSnapshot(
        ts_code=index_code,
        name=index_name,
        trade_date=_parse_yyyymmdd(row.get("trade_date")) if row else None,
        close=_number(row.get("close")) if row else None,
        pct_change=_number(row.get("pct_chg")) if row else None,
        amount=_number(row.get("amount")) if row else None,
    )


def _build_industry_snapshot(
    classification: dict[str, str],
    datasets: Mapping[str, ServiceDataset],
    *,
    basics: Mapping[str, dict[str, Any]],
    bars: Mapping[str, dict[str, Any]],
) -> IndustryBreadthSnapshot:
    index_code = classification["index_code"]
    members = datasets[f"sw_members_{_label_suffix(index_code)}"].items
    member_codes = {
        str(row.get("ts_code") or "").upper()
        for row in members
        if row.get("ts_code") not in (None, "")
    }
    traded = [bars[code] for code in member_codes if code in bars]
    pct_rows = [
        (str(row.get("ts_code") or "").upper(), value)
        for row in traded
        if (value := _number(row.get("pct_chg"))) is not None
    ]
    values = [value for _, value in pct_rows]
    advancing = sum(value > _EPSILON for value in values)
    declining = sum(value < -_EPSILON for value in values)
    flat = len(values) - advancing - declining
    leader = max(pct_rows, key=lambda item: item[1], default=None)
    laggard = min(pct_rows, key=lambda item: item[1], default=None)
    index_dataset = datasets.get(f"sw_daily_{_label_suffix(index_code)}")
    index_row = _latest_row(index_dataset.items) if index_dataset is not None else None

    return IndustryBreadthSnapshot(
        index_code=index_code,
        industry_name=classification["industry_name"],
        member_count=len(member_codes),
        traded_member_count=len(traded),
        advancing_count=advancing,
        declining_count=declining,
        flat_count=flat,
        median_member_pct_change=statistics.median(values) if values else None,
        index_trade_date=_parse_yyyymmdd(index_row.get("trade_date")) if index_row else None,
        index_pct_change=_number(index_row.get("pct_change")) if index_row else None,
        index_amount=_number(index_row.get("amount")) if index_row else None,
        leading_stock_code=leader[0] if leader else None,
        leading_stock_name=_stock_name(leader[0], basics) if leader else None,
        leading_stock_pct_change=leader[1] if leader else None,
        lagging_stock_code=laggard[0] if laggard else None,
        lagging_stock_name=_stock_name(laggard[0], basics) if laggard else None,
        lagging_stock_pct_change=laggard[1] if laggard else None,
    )


def _build_benchmark_snapshot(
    index_code: str,
    index_name: str,
    latest_weights: Mapping[str, list[dict[str, Any]]],
    basics: Mapping[str, dict[str, Any]],
) -> BenchmarkCompositionSnapshot:
    rows = latest_weights.get(index_code, [])
    ordered = sorted(rows, key=lambda row: _number(row.get("weight")) or -math.inf, reverse=True)
    weight_date = _parse_yyyymmdd(ordered[0].get("trade_date")) if ordered else None
    return BenchmarkCompositionSnapshot(
        index_code=index_code,
        index_name=index_name,
        weight_trade_date=weight_date,
        constituent_count=len(rows),
        top_constituents=tuple(
            BenchmarkConstituentWeight(
                ts_code=str(row.get("con_code") or "").upper(),
                name=_stock_name(str(row.get("con_code") or "").upper(), basics),
                weight=_number(row.get("weight")),
            )
            for row in ordered[:5]
            if row.get("con_code") not in (None, "")
        ),
    )


def _rank_candidates(
    rows: Mapping[str, dict[str, Any]],
    candidate_factory: Callable[[str], DailyStockCandidate],
    field: str,
    count: int,
    *,
    reverse: bool,
) -> tuple[DailyStockCandidate, ...]:
    available = [
        (code, value)
        for code, row in rows.items()
        if (value := _number(row.get(field))) is not None
    ]
    available.sort(key=lambda item: (item[1], item[0]), reverse=reverse)
    return tuple(candidate_factory(code) for code, _ in available[:count])


def _candidate_for_code(
    code: str,
    *,
    basics: Mapping[str, dict[str, Any]],
    bars: Mapping[str, dict[str, Any]],
    valuations: Mapping[str, dict[str, Any]],
    limits: Mapping[str, dict[str, Any]],
    st_codes: set[str],
    industry_by_stock: Mapping[str, tuple[str, str]],
    benchmark_weights: Mapping[str, list[dict[str, Any]]],
) -> DailyStockCandidate:
    bar = bars.get(code, {})
    valuation = valuations.get(code, {})
    limit = limits.get(code)
    industry = industry_by_stock.get(code)
    memberships: list[CandidateBenchmarkMembership] = []
    for index_code, index_name in _BENCHMARK_INDICES:
        row = next(
            (
                item
                for item in benchmark_weights.get(index_code, [])
                if str(item.get("con_code") or "").upper() == code
            ),
            None,
        )
        if row is not None:
            memberships.append(
                CandidateBenchmarkMembership(
                    index_code=index_code,
                    index_name=index_name,
                    weight=_number(row.get("weight")),
                    weight_trade_date=_parse_yyyymmdd(row.get("trade_date")),
                )
            )
    return DailyStockCandidate(
        ts_code=code,
        name=_stock_name(code, basics) or code,
        industry_l1_code=industry[0] if industry else None,
        industry_l1_name=industry[1] if industry else None,
        close=_number(bar.get("close")),
        pct_change=_number(bar.get("pct_chg")),
        amount=_number(bar.get("amount")),
        turnover_rate=_number(valuation.get("turnover_rate")),
        volume_ratio=_number(valuation.get("volume_ratio")),
        total_mv=_number(valuation.get("total_mv")),
        is_limit_up=_at_price_limit(bar, limit, upper=True),
        is_limit_down=_at_price_limit(bar, limit, upper=False),
        is_st=code in st_codes,
        benchmark_memberships=tuple(memberships),
    )


def _at_price_limit(
    bar: Mapping[str, Any],
    limit: Mapping[str, Any] | None,
    *,
    upper: bool,
) -> bool:
    if limit is None:
        return False
    close = _number(bar.get("close"))
    boundary = _number(limit.get("up_limit" if upper else "down_limit"))
    if close is None or boundary is None:
        return False
    return math.isclose(close, boundary, rel_tol=1e-6, abs_tol=1e-4)


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    dated = [
        (parsed, row)
        for row in rows
        if (parsed := _parse_yyyymmdd(row.get("trade_date"))) is not None
    ]
    return max(dated, key=lambda item: item[0])[1] if dated else None


def _stock_name(code: str, basics: Mapping[str, dict[str, Any]]) -> str | None:
    value = basics.get(code, {}).get("name")
    return str(value).strip() if value not in (None, "") else None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sum_numbers(values: Any) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return sum(numbers) if numbers else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_yyyymmdd(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _label_suffix(code: str) -> str:
    return code.lower().replace(".", "_")
