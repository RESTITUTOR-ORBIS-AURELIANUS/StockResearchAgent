"""基本面每日快照 Service → Tool 链的确定性测试。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.errors import ProviderErrorCode, ProviderTransportError
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.research_data import InMemoryResearchDataStore
from stock_research_agent.services import build_data_services
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 16, 1, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_daily_fundamental_aaaaa"


class RecordingProvider:
    def __init__(self, responder: Callable[[ProviderQuery], ProviderResult]) -> None:
        self.requests: list[ProviderQuery] = []
        self._responder = responder

    async def query(self, request: ProviderQuery) -> ProviderResult:
        self.requests.append(request)
        return self._responder(request)


def provider_result(
    request: ProviderQuery,
    items: list[dict[str, Any]] | None = None,
) -> ProviderResult:
    rows = [{**{field: row.get(field) for field in request.fields}, **row} for row in (items or [])]
    return ProviderResult(
        api_name=request.api_name,
        provider=ProviderSource.PRIMARY,
        fetched_at=FETCHED_AT,
        fields=request.fields,
        items=rows,
        provider_code=0,
        response_bytes=256,
    )


def fundamental_snapshot_responder(request: ProviderQuery) -> ProviderResult:
    if request.api_name == "trade_cal":
        return provider_result(
            request,
            [
                {
                    "exchange": "SSE",
                    "cal_date": "20260819",
                    "is_open": 1,
                    "pretrade_date": "20260818",
                },
                {
                    "exchange": "SSE",
                    "cal_date": "20260820",
                    "is_open": 1,
                    "pretrade_date": "20260819",
                },
            ],
        )
    if request.api_name == "stock_basic":
        return provider_result(
            request,
            [_stock(f"{index:06d}.SZ", f"公司{index}", _industry(index)) for index in range(1, 16)],
        )
    if request.api_name == "daily_basic":
        return provider_result(
            request,
            [
                _valuation("000001.SZ", pe=5.0, pb=0.8, dividend=4.0),
                _valuation("000002.SZ", pe=80.0, pb=8.0, dividend=0.0),
                _valuation("000003.SZ", pe=20.0, pb=2.0, dividend=2.0),
                _valuation("000004.SZ", pe=7.0, pb=1.2, dividend=1.0),
                *(
                    _valuation(
                        f"{index:06d}.SZ",
                        pe=_sector_value(index, first=(6.0, 60.0, 20.0)),
                        pb=_sector_value(index, first=(1.0, 6.0, 2.0)),
                        dividend=_sector_value(index, first=(3.0, 0.5, 2.0)),
                    )
                    for index in range(5, 16)
                ),
            ],
        )
    if request.api_name == "forecast_vip":
        return provider_result(
            request,
            [
                _forecast("000001.SZ", "20260818", 40.0, 60.0),
                _forecast("000002.SZ", "20260817", -80.0, -60.0),
                _forecast("000005.SZ", "20260816", 20.0, 30.0),
                _forecast("000008.SZ", "20260816", -30.0, -20.0),
                _forecast("000003.SZ", "20260701", 10.0, 20.0),
            ],
        )
    if request.api_name == "express_vip":
        return provider_result(
            request,
            [
                {
                    "ts_code": "000003.SZ",
                    "ann_date": "20260819",
                    "end_date": "20260630",
                    "revenue": 300.0,
                },
                {
                    "ts_code": "000012.SZ",
                    "ann_date": "20260818",
                    "end_date": "20260630",
                    "revenue": 500.0,
                },
            ],
        )
    if request.api_name == "fina_indicator_vip":
        period = str(request.params["period"])
        if period == "20260630":
            return provider_result(
                request,
                [
                    _indicator(
                        f"{index:06d}.SZ",
                        period,
                        roe=_sector_value(index, first=(13.0, 5.0, 8.0)),
                        debt=_sector_value(index, first=(40.0, 75.0, 55.0)),
                        cash=_sector_value(index, first=(12.0, -2.0, 6.0)),
                    )
                    for index in range(1, 16)
                ],
            )
        return provider_result(
            request,
            [
                _indicator(
                    f"{index:06d}.SZ",
                    period,
                    roe=_sector_value(index, first=(10.0, 8.0, 8.0)),
                    debt=_sector_value(index, first=(42.0, 72.0, 54.0)),
                    cash=_sector_value(index, first=(10.0, 1.0, 5.0)),
                )
                for index in range(1, 16)
            ],
        )
    if request.api_name == "cn_gdp":
        return provider_result(
            request,
            [
                {"quarter": "2026Q1", "gdp": 100.0},
                {"quarter": "2026Q2", "gdp": 105.0},
            ],
        )
    if request.api_name.startswith("cn_") or request.api_name == "sf_month":
        return provider_result(
            request,
            [
                {"month": "202606", "value": 100.0},
                {"month": "202607", "value": 101.0},
            ],
        )
    if request.api_name in {"shibor", "shibor_lpr", "us_tycr"}:
        return provider_result(
            request,
            [
                {"date": "20260819", "value": 2.0},
                {"date": "20260820", "value": 2.1},
            ],
        )
    return provider_result(request)


def _stock(ts_code: str, name: str, industry: str) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "name": name,
        "industry": industry,
        "market": "主板",
        "list_date": "20200101",
    }


def _industry(index: int) -> str:
    if index in {1, 4, 5, 6, 7}:
        return "甲行业"
    if index in {2, 8, 9, 10, 11}:
        return "乙行业"
    return "丙行业"


def _sector_value(index: int, *, first: tuple[float, float, float]) -> float:
    return first[("甲行业", "乙行业", "丙行业").index(_industry(index))]


def _valuation(ts_code: str, *, pe: float, pb: float, dividend: float) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "trade_date": "20260820",
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "pe_ttm": pe,
        "pb": pb,
        "dv_ttm": dividend,
        "total_mv": 1000.0,
        "circ_mv": 800.0,
    }


def _forecast(ts_code: str, ann_date: str, lower: float, upper: float) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "ann_date": ann_date,
        "end_date": "20260630",
        "type": "预增" if lower > 0 else "预减",
        "p_change_min": lower,
        "p_change_max": upper,
    }


def _indicator(
    ts_code: str,
    period: str,
    *,
    roe: float,
    debt: float,
    cash: float,
) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "ann_date": "20260818" if period == "20260630" else "20250818",
        "end_date": period,
        "roe": roe,
        "roa": roe / 2,
        "grossprofit_margin": 30.0,
        "debt_to_assets": debt,
        "ocf_to_or": cash,
        "update_flag": 1,
    }


def test_daily_fundamental_service_builds_four_source_snapshot() -> None:
    async def scenario() -> None:
        provider = RecordingProvider(fundamental_snapshot_responder)
        build = await build_data_services(provider).daily_fundamental_snapshot.build_daily_snapshot(
            as_of=AS_OF,
            candidate_count=3,
            announcement_lookback_days=14,
        )

        snapshot = build.snapshot
        assert snapshot.trade_date.isoformat() == "2026-08-20"
        assert snapshot.report_period == "20260630"
        assert snapshot.comparison_period == "20250630"
        assert snapshot.valuations.lowest_positive_pe[0].ts_code == "000001.SZ"
        assert snapshot.valuations.highest_pe[0].ts_code == "000002.SZ"
        assert snapshot.earnings_events.strongest_forecast_improvements[0].name == "公司1"
        assert snapshot.earnings_events.strongest_forecast_deteriorations[0].name == "公司2"
        assert snapshot.earnings_events.recent_earnings_express[0].name == "公司3"
        assert snapshot.valuations.lowest_positive_pe[0].industry == "甲行业"
        assert snapshot.financial_quality.largest_roe_improvements[0].industry == "甲行业"
        assert snapshot.financial_quality.largest_roe_deteriorations[0].industry == "乙行业"
        assert snapshot.financial_quality.highest_debt_to_assets[0].ts_code == "000002.SZ"
        sectors = snapshot.sector_fundamentals
        assert sectors.classification_basis == "stock_basic.industry"
        assert "不是申万行业" in sectors.classification_note
        assert sectors.minimum_metric_sample_size == 5
        valuation_by_name = {item.sector_name: item for item in sectors.valuation_extremes}
        assert "lowest_median_positive_pe" in valuation_by_name["甲行业"].selection_signals
        assert "highest_median_positive_pe" in valuation_by_name["乙行业"].selection_signals
        assert valuation_by_name["甲行业"].median_positive_pe_ttm == 6.0
        assert valuation_by_name["乙行业"].median_positive_pe_ttm == 60.0
        quality_by_name = {item.sector_name: item for item in sectors.financial_quality_extremes}
        assert "largest_median_roe_improvement" in quality_by_name["甲行业"].selection_signals
        assert "largest_median_roe_deterioration" in quality_by_name["乙行业"].selection_signals
        reporting_by_name = {item.sector_name: item for item in sectors.recent_reporting_activity}
        assert reporting_by_name["甲行业"].recent_positive_forecast_count == 2
        assert reporting_by_name["乙行业"].recent_negative_forecast_count == 2
        assert reporting_by_name["丙行业"].recent_express_count == 2
        assert all(
            representative.industry == item.sector_name
            for item in reporting_by_name.values()
            for representative in item.representative_stocks
        )
        assert {item.series for item in snapshot.macro_and_rates} == {
            "cn_gdp",
            "cn_cpi",
            "cn_ppi",
            "cn_m",
            "sf_month",
            "cn_pmi",
            "shibor",
            "shibor_lpr",
            "us_tycr",
        }
        assert all(item.latest_period is not None for item in snapshot.macro_and_rates)
        assert snapshot.coverage.optional_failure_count == 0
        assert snapshot.coverage.classified_stock_count == 15
        assert snapshot.coverage.sector_count == 3
        assert snapshot.coverage.rank_eligible_sector_count == 3
        assert len(build.datasets) == 16

    asyncio.run(scenario())


def test_daily_fundamental_tool_is_role_scoped_and_stores_complete_sources() -> None:
    async def scenario() -> None:
        data_store = InMemoryResearchDataStore()
        context = ResearchToolContext(
            services=build_data_services(RecordingProvider(fundamental_snapshot_responder)),
            as_of=AS_OF,
            run_id=RUN_ID,
            data_store=data_store,
        )
        registry = build_agent_tool_registry(context)
        fundamental_names = {tool.name for tool in registry.fundamental}
        assert "get_daily_fundamental_snapshot" in fundamental_names
        assert "get_daily_fundamental_snapshot" not in {tool.name for tool in registry.technical}

        tool = next(
            item for item in registry.fundamental if item.name == "get_daily_fundamental_snapshot"
        )
        result = await tool.ainvoke({"candidate_count": 3, "announcement_lookback_days": 14})

        assert result["status"] == "ok"
        assert result["snapshot"]["report_period"] == "20260630"
        assert result["source_dataset_count"] == 16
        assert result["context_ref"] is not None
        assert "datasets" not in result
        bundle = await data_store.get(RUN_ID, result["context_ref"])
        assert bundle.kind == "daily_fundamental_snapshot"
        assert bundle.metadata["comparison_period"] == "20250630"
        assert "financial_indicators_current" in bundle.datasets
        assert len(bundle.datasets["daily_valuation"].items) == 15

    asyncio.run(scenario())


def test_daily_fundamental_tool_discloses_optional_source_failure() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "us_tycr":
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "美国利率源暂时不可用",
                provider=ProviderSource.PRIMARY,
            )
        return fundamental_snapshot_responder(request)

    async def scenario() -> None:
        context = ResearchToolContext(
            services=build_data_services(RecordingProvider(responder)),
            as_of=AS_OF,
            run_id=RUN_ID,
        )
        tool = next(
            item
            for item in build_agent_tool_registry(context).fundamental
            if item.name == "get_daily_fundamental_snapshot"
        )
        result = await tool.ainvoke({"candidate_count": 3, "announcement_lookback_days": 14})

        assert result["status"] == "partial"
        assert result["complete"] is False
        assert result["snapshot"] is not None
        assert result["snapshot"]["coverage"]["optional_failure_count"] == 1
        assert result["issues"][0]["dataset_label"] == "rate_us_treasury"
        assert result["issues"][0]["message"] == "美国利率源暂时不可用"

    asyncio.run(scenario())


def test_sector_rankings_exclude_industries_with_fewer_than_five_members() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "stock_basic":
            return provider_result(
                request,
                [
                    _stock(f"{index:06d}.SZ", f"微型公司{index}", "微型行业")
                    for index in range(1, 5)
                ],
            )
        return fundamental_snapshot_responder(request)

    async def scenario() -> None:
        build = await build_data_services(
            RecordingProvider(responder)
        ).daily_fundamental_snapshot.build_daily_snapshot(
            as_of=AS_OF,
            candidate_count=3,
            announcement_lookback_days=14,
        )

        sectors = build.snapshot.sector_fundamentals
        assert sectors.valuation_extremes == ()
        assert sectors.financial_quality_extremes == ()
        assert sectors.recent_reporting_activity == ()
        assert build.snapshot.coverage.sector_count == 1
        assert build.snapshot.coverage.rank_eligible_sector_count == 0

    asyncio.run(scenario())
