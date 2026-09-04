"""技术分析每日模式 Service → Tool 链的确定性测试。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    ProviderPermissionDeniedError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.research_data import InMemoryResearchDataStore
from stock_research_agent.services import build_data_services
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 16, 1, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_daily_technical_aaaaaaaa"


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


def daily_snapshot_responder(request: ProviderQuery) -> ProviderResult:
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
            [
                {
                    "ts_code": "000001.SZ",
                    "name": "甲公司",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910403",
                },
                {
                    "ts_code": "000002.SZ",
                    "name": "乙公司",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910129",
                },
                {
                    "ts_code": "000003.SZ",
                    "name": "丙公司",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910703",
                },
                {
                    "ts_code": "000004.SZ",
                    "name": "停牌公司",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910114",
                },
            ],
        )
    if request.api_name == "daily":
        return provider_result(
            request,
            [
                _bar("000001.SZ", close=11.0, pct_chg=10.0, amount=300.0),
                _bar("000002.SZ", close=9.0, pct_chg=-10.0, amount=200.0),
                _bar("000003.SZ", close=10.0, pct_chg=0.0, amount=100.0),
            ],
        )
    if request.api_name == "daily_basic":
        return provider_result(
            request,
            [
                _valuation("000001.SZ", 4.0, 2.0),
                _valuation("000002.SZ", 8.0, 1.0),
                _valuation("000003.SZ", 2.0, 3.0),
            ],
        )
    if request.api_name == "stk_limit":
        return provider_result(
            request,
            [
                _limit("000001.SZ", 11.0, 9.0),
                _limit("000002.SZ", 11.0, 9.0),
                _limit("000003.SZ", 11.0, 9.0),
            ],
        )
    if request.api_name == "suspend_d":
        return provider_result(
            request,
            [
                {
                    "ts_code": "000004.SZ",
                    "trade_date": "20260820",
                    "suspend_timing": "09:30",
                    "suspend_type": "S",
                }
            ],
        )
    if request.api_name == "stock_st":
        return provider_result(
            request,
            [
                {
                    "ts_code": "000003.SZ",
                    "name": "ST丙公司",
                    "trade_date": "20260820",
                    "type": "ST",
                    "type_name": "ST",
                }
            ],
        )
    if request.api_name == "index_classify":
        return provider_result(
            request,
            [
                {
                    "index_code": "801780.SI",
                    "industry_name": "银行",
                    "level": "L1",
                    "industry_code": "610000",
                    "is_pub": "1",
                    "parent_code": "0",
                    "src": "SW2021",
                }
            ],
        )
    if request.api_name == "index_member_all":
        return provider_result(
            request,
            [
                {
                    "l1_code": "801780.SI",
                    "l1_name": "银行",
                    "l2_code": "850000.SI",
                    "l2_name": "银行Ⅱ",
                    "l3_code": "850001.SI",
                    "l3_name": "银行Ⅲ",
                    "ts_code": code,
                    "name": name,
                    "in_date": "20210101",
                    "out_date": None,
                    "is_new": "Y",
                }
                for code, name in (
                    ("000001.SZ", "甲公司"),
                    ("000002.SZ", "乙公司"),
                    ("000003.SZ", "丙公司"),
                )
            ],
        )
    if request.api_name == "sw_daily":
        return provider_result(
            request,
            [
                {
                    "ts_code": "801780.SI",
                    "trade_date": "20260820",
                    "name": "银行",
                    "open": 100.0,
                    "low": 99.0,
                    "high": 102.0,
                    "close": 101.0,
                    "change": 1.0,
                    "pct_change": 1.0,
                    "vol": 1000.0,
                    "amount": 10000.0,
                    "pe": 7.0,
                    "pb": 0.8,
                    "float_mv": 100000.0,
                    "total_mv": 120000.0,
                }
            ],
        )
    if request.api_name == "index_daily":
        code = str(request.params["ts_code"])
        return provider_result(
            request,
            [
                {
                    "ts_code": code,
                    "trade_date": "20260820",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "pre_close": 100.0,
                    "change": 1.0,
                    "pct_chg": 1.0,
                    "vol": 1000.0,
                    "amount": 10000.0,
                }
            ],
        )
    if request.api_name == "index_weight":
        index_code = str(request.params["index_code"])
        return provider_result(
            request,
            [
                {
                    "index_code": index_code,
                    "con_code": "000001.SZ",
                    "trade_date": "20260801",
                    "weight": 2.5,
                }
            ],
        )
    raise AssertionError(f"unexpected API: {request.api_name}")


def sentiment_snapshot_responder(request: ProviderQuery) -> ProviderResult:
    if request.api_name == "moneyflow_ths":
        return provider_result(
            request,
            [
                _stock_flow("000001.SZ", "甲公司", 120.0, rate=None),
                _stock_flow("000002.SZ", "乙公司", -180.0, rate=None),
                _stock_flow("000003.SZ", "丙公司", 30.0, rate=None),
            ],
        )
    if request.api_name == "moneyflow_dc":
        return provider_result(
            request,
            [
                _stock_flow("000001.SZ", "甲公司", 150.0, rate=4.5),
                _stock_flow("000002.SZ", "乙公司", -160.0, rate=-5.0),
                _stock_flow("000003.SZ", "丙公司", 20.0, rate=1.0),
            ],
        )
    if request.api_name == "moneyflow_ind_ths":
        return provider_result(
            request,
            [
                {
                    "trade_date": "20260820",
                    "ts_code": "881155.TI",
                    "industry": "银行",
                    "lead_stock": "甲公司",
                    "close": 100.0,
                    "pct_change": 1.2,
                    "company_num": 42,
                    "pct_change_stock": 10.0,
                    "close_price": 11.0,
                    "net_buy_amount": 20.0,
                    "net_sell_amount": 10.0,
                    "net_amount": 10.0,
                },
                {
                    "trade_date": "20260820",
                    "ts_code": "881156.TI",
                    "industry": "地产",
                    "lead_stock": "乙公司",
                    "close": 90.0,
                    "pct_change": -1.0,
                    "company_num": 30,
                    "pct_change_stock": -10.0,
                    "close_price": 9.0,
                    "net_buy_amount": 5.0,
                    "net_sell_amount": 12.0,
                    "net_amount": -7.0,
                },
            ],
        )
    if request.api_name == "moneyflow_mkt_dc":
        return provider_result(
            request,
            [
                {
                    "trade_date": "20260820",
                    "close_sh": 3500.0,
                    "pct_change_sh": 0.5,
                    "close_sz": 11000.0,
                    "pct_change_sz": -0.2,
                    "net_amount": 80.0,
                    "net_amount_rate": 1.5,
                    "buy_elg_amount": 20.0,
                    "buy_elg_amount_rate": 0.5,
                    "buy_lg_amount": 30.0,
                    "buy_lg_amount_rate": 0.7,
                    "buy_md_amount": 10.0,
                    "buy_md_amount_rate": 0.2,
                    "buy_sm_amount": 20.0,
                    "buy_sm_amount_rate": 0.1,
                }
            ],
        )
    if request.api_name == "moneyflow_hsgt":
        return provider_result(
            request,
            [
                {
                    "trade_date": "20260819",
                    "ggt_ss": 1.0,
                    "ggt_sz": 2.0,
                    "hgt": 3.0,
                    "sgt": 4.0,
                    "north_money": 7.0,
                    "south_money": 3.0,
                },
                {
                    "trade_date": "20260820",
                    "ggt_ss": 2.0,
                    "ggt_sz": 3.0,
                    "hgt": 5.0,
                    "sgt": 6.0,
                    "north_money": 11.0,
                    "south_money": 5.0,
                },
            ],
        )
    if request.api_name == "limit_list_d":
        return provider_result(
            request,
            [
                _limit_event("000001.SZ", "甲公司", "U", 1000.0, 0),
                _limit_event("000002.SZ", "乙公司", "D", 100.0, 4),
            ],
        )
    if request.api_name == "margin":
        exchange_id = str(request.params["exchange_id"])
        return provider_result(
            request,
            [
                {
                    "trade_date": "20260820",
                    "exchange_id": exchange_id,
                    "rzye": 1000.0,
                    "rzmre": 100.0,
                    "rzche": 80.0,
                    "rqye": 10.0,
                    "rqmcl": 2.0,
                    "rzrqye": 1010.0,
                }
            ],
        )
    return daily_snapshot_responder(request)


def _bar(ts_code: str, *, close: float, pct_chg: float, amount: float) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "trade_date": "20260820",
        "open": 10.0,
        "high": max(10.0, close),
        "low": min(10.0, close),
        "close": close,
        "pre_close": 10.0,
        "change": close - 10.0,
        "pct_chg": pct_chg,
        "vol": 10.0,
        "amount": amount,
    }


def _valuation(ts_code: str, turnover: float, volume_ratio: float) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "trade_date": "20260820",
        "turnover_rate": turnover,
        "volume_ratio": volume_ratio,
        "pe_ttm": 10.0,
        "pb": 1.0,
        "dv_ttm": 2.0,
        "total_mv": 1000.0,
        "circ_mv": 800.0,
    }


def _limit(ts_code: str, up_limit: float, down_limit: float) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "trade_date": "20260820",
        "pre_close": 10.0,
        "up_limit": up_limit,
        "down_limit": down_limit,
    }


def _stock_flow(
    ts_code: str,
    name: str,
    net_amount: float,
    *,
    rate: float | None,
) -> dict[str, Any]:
    return {
        "trade_date": "20260820",
        "ts_code": ts_code,
        "name": name,
        "pct_change": 1.0,
        "latest": 10.0,
        "close": 10.0,
        "net_amount": net_amount,
        "net_amount_rate": rate,
        "net_d5_amount": net_amount * 2,
        "buy_elg_amount": 1.0,
        "buy_elg_amount_rate": 0.1,
        "buy_lg_amount": 2.0,
        "buy_lg_amount_rate": 0.2,
        "buy_md_amount": 3.0,
        "buy_md_amount_rate": 0.3,
        "buy_sm_amount": 4.0,
        "buy_sm_amount_rate": 0.4,
    }


def _limit_event(
    ts_code: str,
    name: str,
    limit: str,
    fd_amount: float,
    open_times: int,
) -> dict[str, Any]:
    return {
        "trade_date": "20260820",
        "ts_code": ts_code,
        "industry": "银行",
        "name": name,
        "close": 10.0,
        "pct_chg": 10.0 if limit == "U" else -10.0,
        "amount": 100.0,
        "limit_amount": 50.0,
        "float_mv": 1000.0,
        "total_mv": 1200.0,
        "turnover_ratio": 5.0,
        "fd_amount": fd_amount,
        "first_time": "093000",
        "last_time": "145000",
        "open_times": open_times,
        "up_stat": "1/1",
        "limit_times": 1,
        "limit": limit,
    }


def test_new_compatibility_services_hide_unverified_pagination_and_level_parameters() -> None:
    async def scenario() -> None:
        provider = RecordingProvider(daily_snapshot_responder)
        services = build_data_services(provider)

        classifications = await services.instrument_reference.get_industry_classifications(
            level="L1", as_of=AS_OF
        )
        await services.instrument_reference.get_industry_members("801780.SI", as_of=AS_OF)
        await services.instrument_reference.get_index_weights(
            "000300.SH",
            AS_OF.date(),
            AS_OF.date(),
            as_of=AS_OF,
        )

        assert classifications.items[0]["industry_name"] == "银行"
        assert provider.requests[0].params == {"src": "SW2021"}
        assert provider.requests[1].params == {"l1_code": "801780.SI", "is_new": "Y"}
        assert provider.requests[2].params == {
            "index_code": "000300.SH",
            "start_date": "20260820",
            "end_date": "20260820",
        }

    asyncio.run(scenario())


def test_daily_technical_service_aggregates_market_industry_and_candidates() -> None:
    async def scenario() -> None:
        provider = RecordingProvider(daily_snapshot_responder)
        services = build_data_services(provider)
        build = await services.daily_technical_snapshot.build_daily_snapshot(
            as_of=AS_OF,
            candidate_count=3,
        )

        snapshot = build.snapshot
        assert snapshot.trade_date.isoformat() == "2026-08-20"
        assert snapshot.coverage.listed_stock_count == 4
        assert snapshot.coverage.traded_stock_count == 3
        assert snapshot.market_breadth.advancing_count == 1
        assert snapshot.market_breadth.declining_count == 1
        assert snapshot.market_breadth.flat_count == 1
        assert snapshot.market_breadth.limit_up_count == 1
        assert snapshot.market_breadth.limit_down_count == 1
        assert snapshot.market_breadth.suspended_count == 1
        assert snapshot.industries[0].industry_name == "银行"
        assert snapshot.industries[0].index_pct_change == 1.0
        assert snapshot.candidates.top_gainers[0].ts_code == "000001.SZ"
        assert snapshot.candidates.top_losers[0].ts_code == "000002.SZ"
        assert snapshot.candidates.highest_turnover[0].ts_code == "000002.SZ"
        assert snapshot.candidates.highest_volume_ratio[0].ts_code == "000003.SZ"
        assert snapshot.candidates.top_gainers[0].benchmark_memberships
        assert len(build.datasets) == 16
        full_market_requests = [
            request
            for request in provider.requests
            if request.api_name in {"daily", "daily_basic", "stk_limit", "suspend_d"}
            and "trade_date" in request.params
        ]
        assert len(full_market_requests) == 4
        assert all("limit" not in request.params for request in full_market_requests)
        assert all("offset" not in request.params for request in full_market_requests)

    asyncio.run(scenario())


def test_daily_technical_tool_stores_raw_sources_and_returns_small_snapshot() -> None:
    async def scenario() -> None:
        data_store = InMemoryResearchDataStore()
        context = ResearchToolContext(
            services=build_data_services(RecordingProvider(daily_snapshot_responder)),
            as_of=AS_OF,
            run_id=RUN_ID,
            data_store=data_store,
        )
        registry = build_agent_tool_registry(context)
        tool = next(
            item
            for item in registry.technical
            if item.name == "get_daily_technical_market_snapshot"
        )
        result = await tool.ainvoke({"candidate_count": 3})

        assert result["status"] == "ok"
        assert result["snapshot"]["trade_date"] == "2026-08-20"
        assert result["source_dataset_count"] == 16
        assert result["total_stored_items"] > 0
        assert result["context_ref"] is not None
        assert "datasets" not in result

        bundle = await data_store.get(RUN_ID, result["context_ref"])
        assert bundle.kind == "daily_technical_market_snapshot"
        assert bundle.metadata["trade_date"] == "2026-08-20"
        assert "daily_bars" in bundle.datasets
        assert len(bundle.datasets["daily_bars"].items) == 3

    asyncio.run(scenario())


def test_daily_technical_tool_keeps_member_breadth_when_sw_daily_is_unavailable() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "sw_daily":
            raise ProviderPermissionDeniedError(
                ProviderErrorCode.PERMISSION_DENIED,
                request.api_name,
                "upstream capability is unavailable",
                provider=ProviderSource.PRIMARY,
            )
        return daily_snapshot_responder(request)

    async def scenario() -> None:
        data_store = InMemoryResearchDataStore()
        context = ResearchToolContext(
            services=build_data_services(RecordingProvider(responder)),
            as_of=AS_OF,
            run_id=RUN_ID,
            data_store=data_store,
        )
        tool = next(
            item
            for item in build_agent_tool_registry(context).technical
            if item.name == "get_daily_technical_market_snapshot"
        )
        result = await tool.ainvoke({"candidate_count": 3})

        assert result["status"] == "partial"
        assert result["complete"] is False
        assert result["source_dataset_count"] == 15
        assert result["snapshot"]["industries"][0]["industry_name"] == "银行"
        assert result["snapshot"]["industries"][0]["advancing_count"] == 1
        assert result["snapshot"]["industries"][0]["index_pct_change"] is None
        assert result["issues"] == [
            {
                "dataset_label": "sw_daily_801780_si",
                "code": "CAPABILITY_UNAVAILABLE",
                "message": "upstream capability is unavailable",
                "retryable": False,
                "suggested_action": "不要把它当成空数据；改用现有能力或通知程序维护者",
                "correlation_id": None,
            }
        ]

        bundle = await data_store.get(RUN_ID, result["context_ref"])
        assert "sw_members_801780_si" in bundle.datasets
        assert "sw_daily_801780_si" not in bundle.datasets
        assert bundle.metadata["optional_failure_count"] == 1

    asyncio.run(scenario())


def test_daily_sentiment_flow_service_and_tools_build_cross_sectional_snapshot() -> None:
    async def scenario() -> None:
        provider = RecordingProvider(sentiment_snapshot_responder)
        services = build_data_services(provider)
        build = await services.daily_sentiment_flow_snapshot.build_daily_snapshot(
            as_of=AS_OF,
            candidate_count=3,
        )

        snapshot = build.snapshot
        assert snapshot.trade_date.isoformat() == "2026-08-20"
        assert snapshot.technical_context.market_breadth.advancing_count == 1
        assert snapshot.market_flow.hsgt_history[-1].north_money == 11.0
        assert snapshot.market_flow.market_moneyflow_dc.net_amount == 80.0
        assert len(snapshot.market_flow.margin_markets) == 2
        assert snapshot.industry_top_inflows[0].industry == "银行"
        assert snapshot.industry_top_outflows[0].industry == "地产"
        assert snapshot.stock_candidates.ths_top_inflows[0].ts_code == "000001.SZ"
        assert snapshot.stock_candidates.ths_top_outflows[0].ts_code == "000002.SZ"
        assert snapshot.stock_candidates.dc_top_inflows[0].net_amount == 150.0
        assert all(item.net_amount > 0 for item in snapshot.stock_candidates.ths_top_inflows)
        assert all(item.net_amount < 0 for item in snapshot.stock_candidates.ths_top_outflows)
        assert all(item.net_amount > 0 for item in snapshot.industry_top_inflows)
        assert all(item.net_amount < 0 for item in snapshot.industry_top_outflows)
        assert snapshot.stock_candidates.most_opened_limit_events[0].ts_code == "000002.SZ"
        authorized = {(item.type.value, item.code) for item in snapshot.authorized_targets}
        assert ("MARKET", "A_SHARE") in authorized
        assert ("SECTOR", "801780.SI") in authorized
        assert ("STOCK", "000001.SZ") in authorized
        assert ("STOCK", "000002.SZ") in authorized
        assert snapshot.coverage.optional_failure_count == 0
        assert len(build.datasets) == 24

        data_store = InMemoryResearchDataStore()
        context = ResearchToolContext(
            services=services,
            as_of=AS_OF,
            run_id=RUN_ID,
            data_store=data_store,
        )
        registry = build_agent_tool_registry(context)
        sentiment_names = {tool.name for tool in registry.sentiment_flow}
        assert "get_daily_sentiment_flow_snapshot" in sentiment_names
        assert "get_stock_active_money_flow_context" in sentiment_names

        daily_tool = next(
            tool
            for tool in registry.sentiment_flow
            if tool.name == "get_daily_sentiment_flow_snapshot"
        )
        result = await daily_tool.ainvoke({"candidate_count": 3})
        assert result["status"] == "ok"
        assert result["snapshot"]["industry_top_inflows"][0]["industry"] == "银行"
        assert {
            (item["type"], item["code"]) for item in result["snapshot"]["authorized_targets"]
        } >= {
            ("MARKET", "A_SHARE"),
            ("STOCK", "000001.SZ"),
        }
        assert result["context_ref"] is not None
        bundle = await data_store.get(RUN_ID, result["context_ref"])
        assert bundle.kind == "daily_sentiment_flow_snapshot"
        assert "sentiment_stock_flow_dc" in bundle.datasets

        stock_tool = next(
            tool
            for tool in registry.sentiment_flow
            if tool.name == "get_stock_active_money_flow_context"
        )
        stock_result = await stock_tool.ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-01",
                "end_date": "2026-08-20",
            }
        )
        assert stock_result["status"] == "ok"
        assert {item["label"] for item in stock_result["datasets"]} == {
            "moneyflow_ths",
            "moneyflow_dc",
        }

    asyncio.run(scenario())
