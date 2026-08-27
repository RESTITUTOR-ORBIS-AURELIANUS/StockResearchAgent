"""情绪与资金研究员每日模式使用的确定性全市场快照。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pydantic import Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.services.base import AsOfValue
from stock_research_agent.services.daily_technical_snapshot import (
    DailyTechnicalSnapshot,
    DailyTechnicalSnapshotService,
)
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.models import ServiceDataset
from stock_research_agent.services.trading_behavior import TradingBehaviorService


class HsgtFlowPoint(DomainModel):
    trade_date: date
    hgt: float | None = None
    sgt: float | None = None
    north_money: float | None = None
    south_money: float | None = None


class MarketMoneyflowSnapshot(DomainModel):
    trade_date: date
    close_sh: float | None = None
    pct_change_sh: float | None = None
    close_sz: float | None = None
    pct_change_sz: float | None = None
    net_amount: float | None = None
    net_amount_rate: float | None = None
    buy_elg_amount: float | None = None
    buy_lg_amount: float | None = None
    buy_md_amount: float | None = None
    buy_sm_amount: float | None = None


class MarginMarketSnapshot(DomainModel):
    exchange_id: str
    trade_date: date
    rzye: float | None = None
    rzmre: float | None = None
    rzche: float | None = None
    rqye: float | None = None
    rqmcl: float | None = None
    rzrqye: float | None = None


class DailyMarketFlowSnapshot(DomainModel):
    hsgt_history: tuple[HsgtFlowPoint, ...]
    market_moneyflow_dc: MarketMoneyflowSnapshot | None = None
    margin_markets: tuple[MarginMarketSnapshot, ...]


class IndustryFlowSnapshot(DomainModel):
    ts_code: str
    industry: str
    pct_change: float | None = None
    company_num: int | None = Field(default=None, ge=0)
    lead_stock: str | None = None
    lead_stock_pct_change: float | None = None
    net_buy_amount: float | None = None
    net_sell_amount: float | None = None
    net_amount: float | None = None


class StockFlowCandidate(DomainModel):
    source_api: str
    ts_code: str
    name: str
    pct_change: float | None = None
    net_amount: float
    net_amount_rate: float | None = None
    net_d5_amount: float | None = None


class LimitEventCandidate(DomainModel):
    ts_code: str
    name: str
    industry: str | None = None
    limit: str | None = None
    pct_change: float | None = None
    fd_amount: float | None = None
    open_times: int | None = Field(default=None, ge=0)
    up_stat: str | None = None


class DailyStockFlowCandidateGroups(DomainModel):
    ths_top_inflows: tuple[StockFlowCandidate, ...]
    ths_top_outflows: tuple[StockFlowCandidate, ...]
    dc_top_inflows: tuple[StockFlowCandidate, ...]
    dc_top_outflows: tuple[StockFlowCandidate, ...]
    strongest_limit_events: tuple[LimitEventCandidate, ...]
    most_opened_limit_events: tuple[LimitEventCandidate, ...]


class DailySentimentFlowCoverage(DomainModel):
    source_dataset_count: int = Field(ge=0)
    optional_failure_count: int = Field(ge=0)
    ths_stock_flow_count: int = Field(ge=0)
    dc_stock_flow_count: int = Field(ge=0)
    industry_flow_count: int = Field(ge=0)
    limit_event_count: int = Field(ge=0)


class DailySentimentFlowSnapshot(DomainModel):
    """给情绪资金 Agent 阅读的市场背景、资金横截面与少量查证候选。"""

    trade_date: date
    technical_context: DailyTechnicalSnapshot
    market_flow: DailyMarketFlowSnapshot
    industry_top_inflows: tuple[IndustryFlowSnapshot, ...]
    industry_top_outflows: tuple[IndustryFlowSnapshot, ...]
    stock_candidates: DailyStockFlowCandidateGroups
    coverage: DailySentimentFlowCoverage


@dataclass(frozen=True, slots=True)
class DailySentimentFlowSnapshotBuild:
    snapshot: DailySentimentFlowSnapshot
    datasets: Mapping[str, ServiceDataset]
    optional_failures: Mapping[str, BaseException]


type _DatasetQuery = tuple[str, Callable[[], Awaitable[ServiceDataset]]]


class DailySentimentFlowSnapshotService:
    """复用技术背景快照，再聚合市场、行业和个股资金行为。"""

    def __init__(
        self,
        technical_snapshot: DailyTechnicalSnapshotService,
        trading_behavior: TradingBehaviorService,
        *,
        max_concurrency: int = 6,
    ) -> None:
        if max_concurrency < 1:
            raise ServiceInputError("max_concurrency 必须大于 0")
        self._technical_snapshot = technical_snapshot
        self._trading_behavior = trading_behavior
        self._max_concurrency = max_concurrency

    async def build_daily_snapshot(
        self,
        *,
        as_of: AsOfValue,
        candidate_count: int = 10,
    ) -> DailySentimentFlowSnapshotBuild:
        if not 3 <= candidate_count <= 20:
            raise ServiceInputError("candidate_count 必须在 3 到 20 之间")

        technical_build = await self._technical_snapshot.build_daily_snapshot(
            as_of=as_of,
            candidate_count=candidate_count,
        )
        trade_date = technical_build.snapshot.trade_date
        history_start = trade_date - timedelta(days=14)
        queries: tuple[_DatasetQuery, ...] = (
            (
                "sentiment_stock_flow_ths",
                lambda: self._trading_behavior.get_daily_stock_moneyflow_ths(
                    trade_date, as_of=as_of
                ),
            ),
            (
                "sentiment_stock_flow_dc",
                lambda: self._trading_behavior.get_daily_stock_moneyflow_dc(
                    trade_date, as_of=as_of
                ),
            ),
            (
                "sentiment_industry_flow_ths",
                lambda: self._trading_behavior.get_daily_industry_moneyflow_ths(
                    trade_date, as_of=as_of
                ),
            ),
            (
                "sentiment_market_flow_dc",
                lambda: self._trading_behavior.get_daily_market_moneyflow_dc(
                    trade_date, as_of=as_of
                ),
            ),
            (
                "sentiment_hsgt_flow",
                lambda: self._trading_behavior.get_hsgt_moneyflow(
                    history_start, trade_date, as_of=as_of
                ),
            ),
            (
                "sentiment_limit_list",
                lambda: self._trading_behavior.get_daily_limit_list(trade_date, as_of=as_of),
            ),
            (
                "sentiment_margin_sse",
                lambda: self._trading_behavior.get_margin_market(
                    trade_date, exchange_id="SSE", as_of=as_of
                ),
            ),
            (
                "sentiment_margin_szse",
                lambda: self._trading_behavior.get_margin_market(
                    trade_date, exchange_id="SZSE", as_of=as_of
                ),
            ),
        )
        sentiment_datasets, sentiment_failures = await self._run_optional_queries(queries)
        if not any(
            sentiment_datasets.get(label) and sentiment_datasets[label].items
            for label in (
                "sentiment_stock_flow_ths",
                "sentiment_stock_flow_dc",
                "sentiment_industry_flow_ths",
                "sentiment_market_flow_dc",
                "sentiment_hsgt_flow",
                "sentiment_limit_list",
            )
        ):
            raise ServiceDataValidationError("每日情绪资金快照没有取得任何资金或情绪数据")

        datasets = {**technical_build.datasets, **sentiment_datasets}
        failures = {**technical_build.optional_failures, **sentiment_failures}
        snapshot = _aggregate_snapshot(
            trade_date=trade_date,
            technical_context=technical_build.snapshot,
            datasets=sentiment_datasets,
            optional_failure_count=len(failures),
            source_dataset_count=len(datasets),
            candidate_count=candidate_count,
        )
        return DailySentimentFlowSnapshotBuild(
            snapshot=snapshot,
            datasets=datasets,
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
    technical_context: DailyTechnicalSnapshot,
    datasets: Mapping[str, ServiceDataset],
    optional_failure_count: int,
    source_dataset_count: int,
    candidate_count: int,
) -> DailySentimentFlowSnapshot:
    ths_rows = _items(datasets, "sentiment_stock_flow_ths")
    dc_rows = _items(datasets, "sentiment_stock_flow_dc")
    industry_rows = _items(datasets, "sentiment_industry_flow_ths")
    limit_rows = _items(datasets, "sentiment_limit_list")
    industry_flows = [_industry_flow(row) for row in industry_rows]
    industry_flows = [item for item in industry_flows if item is not None]

    return DailySentimentFlowSnapshot(
        trade_date=trade_date,
        technical_context=technical_context,
        market_flow=DailyMarketFlowSnapshot(
            hsgt_history=tuple(
                item
                for row in _sorted_by_date(_items(datasets, "sentiment_hsgt_flow"))
                if (item := _hsgt_point(row)) is not None
            ),
            market_moneyflow_dc=_market_flow(
                _latest_row(_items(datasets, "sentiment_market_flow_dc"))
            ),
            margin_markets=tuple(
                item
                for label in ("sentiment_margin_sse", "sentiment_margin_szse")
                for row in _items(datasets, label)
                if (item := _margin_flow(row)) is not None
            ),
        ),
        industry_top_inflows=tuple(
            _rank_by_net_amount(industry_flows, candidate_count, reverse=True)
        ),
        industry_top_outflows=tuple(
            _rank_by_net_amount(industry_flows, candidate_count, reverse=False)
        ),
        stock_candidates=DailyStockFlowCandidateGroups(
            ths_top_inflows=tuple(
                _stock_candidates(ths_rows, "moneyflow_ths", candidate_count, True)
            ),
            ths_top_outflows=tuple(
                _stock_candidates(ths_rows, "moneyflow_ths", candidate_count, False)
            ),
            dc_top_inflows=tuple(_stock_candidates(dc_rows, "moneyflow_dc", candidate_count, True)),
            dc_top_outflows=tuple(
                _stock_candidates(dc_rows, "moneyflow_dc", candidate_count, False)
            ),
            strongest_limit_events=tuple(_strongest_limit_events(limit_rows, candidate_count)),
            most_opened_limit_events=tuple(_most_opened_limit_events(limit_rows, candidate_count)),
        ),
        coverage=DailySentimentFlowCoverage(
            source_dataset_count=source_dataset_count,
            optional_failure_count=optional_failure_count,
            ths_stock_flow_count=len(ths_rows),
            dc_stock_flow_count=len(dc_rows),
            industry_flow_count=len(industry_rows),
            limit_event_count=len(limit_rows),
        ),
    )


def _items(datasets: Mapping[str, ServiceDataset], label: str) -> list[dict[str, Any]]:
    dataset = datasets.get(label)
    return dataset.items if dataset is not None else []


def _stock_candidates(
    rows: list[dict[str, Any]],
    source_api: str,
    count: int,
    reverse: bool,
) -> list[StockFlowCandidate]:
    candidates: list[StockFlowCandidate] = []
    for row in rows:
        net_amount = _number(row.get("net_amount"))
        if row.get("ts_code") in (None, "") or net_amount is None:
            continue
        if (reverse and net_amount <= 0) or (not reverse and net_amount >= 0):
            continue
        candidates.append(
            StockFlowCandidate(
                source_api=source_api,
                ts_code=str(row["ts_code"]).upper(),
                name=str(row.get("name") or row["ts_code"]).strip(),
                pct_change=_number(row.get("pct_change")),
                net_amount=net_amount,
                net_amount_rate=_number(row.get("net_amount_rate")),
                net_d5_amount=_number(row.get("net_d5_amount")),
            )
        )
    return sorted(candidates, key=lambda item: (item.net_amount, item.ts_code), reverse=reverse)[
        :count
    ]


def _industry_flow(row: Mapping[str, Any]) -> IndustryFlowSnapshot | None:
    code = str(row.get("ts_code") or "").upper()
    name = str(row.get("industry") or "").strip()
    if not code or not name:
        return None
    return IndustryFlowSnapshot(
        ts_code=code,
        industry=name,
        pct_change=_number(row.get("pct_change")),
        company_num=_integer(row.get("company_num")),
        lead_stock=_optional_text(row.get("lead_stock")),
        lead_stock_pct_change=_number(row.get("pct_change_stock")),
        net_buy_amount=_number(row.get("net_buy_amount")),
        net_sell_amount=_number(row.get("net_sell_amount")),
        net_amount=_number(row.get("net_amount")),
    )


def _rank_by_net_amount(
    rows: list[IndustryFlowSnapshot],
    count: int,
    *,
    reverse: bool,
) -> list[IndustryFlowSnapshot]:
    available = [
        item
        for item in rows
        if item.net_amount is not None
        and ((reverse and item.net_amount > 0) or (not reverse and item.net_amount < 0))
    ]
    return sorted(
        available,
        key=lambda item: (item.net_amount or 0.0, item.ts_code),
        reverse=reverse,
    )[:count]


def _limit_event(row: Mapping[str, Any]) -> LimitEventCandidate | None:
    code = str(row.get("ts_code") or "").upper()
    if not code:
        return None
    return LimitEventCandidate(
        ts_code=code,
        name=str(row.get("name") or code).strip(),
        industry=_optional_text(row.get("industry")),
        limit=_optional_text(row.get("limit")),
        pct_change=_number(row.get("pct_chg")),
        fd_amount=_number(row.get("fd_amount")),
        open_times=_integer(row.get("open_times")),
        up_stat=_optional_text(row.get("up_stat")),
    )


def _strongest_limit_events(rows: list[dict[str, Any]], count: int) -> list[LimitEventCandidate]:
    events = [item for row in rows if (item := _limit_event(row)) is not None]
    return sorted(
        events,
        key=lambda item: (item.fd_amount is not None, item.fd_amount or 0.0, item.ts_code),
        reverse=True,
    )[:count]


def _most_opened_limit_events(rows: list[dict[str, Any]], count: int) -> list[LimitEventCandidate]:
    events = [item for row in rows if (item := _limit_event(row)) is not None]
    return sorted(
        events,
        key=lambda item: (item.open_times or 0, abs(item.pct_change or 0.0), item.ts_code),
        reverse=True,
    )[:count]


def _hsgt_point(row: Mapping[str, Any]) -> HsgtFlowPoint | None:
    trade_date = _date(row.get("trade_date"))
    if trade_date is None:
        return None
    return HsgtFlowPoint(
        trade_date=trade_date,
        hgt=_number(row.get("hgt")),
        sgt=_number(row.get("sgt")),
        north_money=_number(row.get("north_money")),
        south_money=_number(row.get("south_money")),
    )


def _market_flow(row: Mapping[str, Any] | None) -> MarketMoneyflowSnapshot | None:
    if row is None or (trade_date := _date(row.get("trade_date"))) is None:
        return None
    return MarketMoneyflowSnapshot(
        trade_date=trade_date,
        close_sh=_number(row.get("close_sh")),
        pct_change_sh=_number(row.get("pct_change_sh")),
        close_sz=_number(row.get("close_sz")),
        pct_change_sz=_number(row.get("pct_change_sz")),
        net_amount=_number(row.get("net_amount")),
        net_amount_rate=_number(row.get("net_amount_rate")),
        buy_elg_amount=_number(row.get("buy_elg_amount")),
        buy_lg_amount=_number(row.get("buy_lg_amount")),
        buy_md_amount=_number(row.get("buy_md_amount")),
        buy_sm_amount=_number(row.get("buy_sm_amount")),
    )


def _margin_flow(row: Mapping[str, Any]) -> MarginMarketSnapshot | None:
    trade_date = _date(row.get("trade_date"))
    exchange_id = str(row.get("exchange_id") or "").upper()
    if trade_date is None or not exchange_id:
        return None
    return MarginMarketSnapshot(
        exchange_id=exchange_id,
        trade_date=trade_date,
        rzye=_number(row.get("rzye")),
        rzmre=_number(row.get("rzmre")),
        rzche=_number(row.get("rzche")),
        rqye=_number(row.get("rqye")),
        rqmcl=_number(row.get("rqmcl")),
        rzrqye=_number(row.get("rzrqye")),
    )


def _sorted_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: _date(row.get("trade_date")) or date.min)


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = _sorted_by_date(rows)
    return ordered[-1] if ordered else None


def _date(value: Any) -> date | None:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
