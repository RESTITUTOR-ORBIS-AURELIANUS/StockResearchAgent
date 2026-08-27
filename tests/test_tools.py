"""面向四位证据研究员的 Tool 白名单、边界与委托行为测试。"""

import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.errors import ProviderError, ProviderErrorCode
from stock_research_agent.providers.models import (
    ProviderQuery,
    ProviderResult,
    ProviderSource,
)
from stock_research_agent.research_data import InMemoryResearchDataStore
from stock_research_agent.services import build_data_services
from stock_research_agent.tools import (
    EvidenceAgentRole,
    ResearchToolContext,
    ToolLimits,
    build_agent_tool_registry,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 15, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 1, tzinfo=SHANGHAI)
RUN_A = "run_20260820_150000_000001_SZ_aaaaaaaa"
RUN_B = "run_20260820_150000_600000_SH_bbbbbbbb"
CALCULATOR_TOOL_NAMES = {
    "calculate_return_and_trend",
    "calculate_momentum",
    "calculate_risk_and_tradability",
    "calculate_volume_and_liquidity",
    "calculate_relative_strength",
}


class RecordingProvider:
    def __init__(
        self,
        responder: Callable[[ProviderQuery], ProviderResult] | None = None,
    ) -> None:
        self.requests: list[ProviderQuery] = []
        self._responder = responder or self._empty_response

    async def query(self, request: ProviderQuery) -> ProviderResult:
        self.requests.append(request)
        return self._responder(request)

    @staticmethod
    def _empty_response(request: ProviderQuery) -> ProviderResult:
        return provider_result(request)


def provider_result(
    request: ProviderQuery,
    *,
    items: list[dict[str, Any]] | None = None,
    provider: ProviderSource = ProviderSource.PRIMARY,
) -> ProviderResult:
    rows = [{**{field: row.get(field) for field in request.fields}, **row} for row in (items or [])]
    return ProviderResult(
        api_name=request.api_name,
        provider=provider,
        fetched_at=FETCHED_AT,
        fields=request.fields,
        items=rows,
        provider_code=0,
        response_bytes=128,
    )


def build_context(
    provider: RecordingProvider,
    *,
    limits: ToolLimits | None = None,
    run_id: str = RUN_A,
    data_store: InMemoryResearchDataStore | None = None,
) -> ResearchToolContext:
    return ResearchToolContext(
        services=build_data_services(provider, public_news_provider=provider),
        as_of=AS_OF,
        limits=limits or ToolLimits(),
        run_id=run_id,
        data_store=data_store or InMemoryResearchDataStore(),
    )


def build_registry(
    provider: RecordingProvider,
    *,
    limits: ToolLimits | None = None,
):
    return build_agent_tool_registry(build_context(provider, limits=limits))


def market_rows(
    ts_code: str,
    *,
    count: int = 70,
    daily_growth: float = 0.003,
) -> list[dict[str, Any]]:
    start = date(2026, 6, 1)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        close = 100.0 * (1.0 + daily_growth) ** index
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": (start + timedelta(days=index)).strftime("%Y%m%d"),
                "open": close * 0.997,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "vol": 1_000_000.0 + index * 10_000.0,
                "amount": close * (1_000_000.0 + index * 10_000.0),
            }
        )
    return rows


def technical_responder(request: ProviderQuery) -> ProviderResult:
    api_name = request.api_name
    ts_code = str(request.params.get("ts_code", "000001.SZ"))
    growth = 0.001 if api_name == "index_daily" else 0.003
    prices = market_rows(ts_code, daily_growth=growth)

    if api_name in {"daily", "index_daily", "fund_daily"}:
        return provider_result(request, items=prices)
    if api_name in {"adj_factor", "fund_adj"}:
        return provider_result(
            request,
            items=[
                {
                    "ts_code": ts_code,
                    "trade_date": row["trade_date"],
                    "adj_factor": 1.0,
                }
                for row in prices
            ],
        )
    if api_name == "daily_basic":
        return provider_result(
            request,
            items=[
                {
                    "ts_code": ts_code,
                    "trade_date": row["trade_date"],
                    "turnover_rate": 1.0 + index / 100,
                    "volume_ratio": 1.2,
                    "pe_ttm": 8.0,
                    "pb": 1.0,
                    "dv_ttm": 2.0,
                    "total_mv": 100_000_000.0,
                    "circ_mv": 80_000_000.0,
                }
                for index, row in enumerate(prices)
            ],
        )
    if api_name == "stk_limit":
        return provider_result(
            request,
            items=[
                {
                    "ts_code": ts_code,
                    "trade_date": row["trade_date"],
                    "pre_close": row["close"] / 1.003,
                    "up_limit": row["close"] * 1.1,
                    "down_limit": row["close"] * 0.9,
                }
                for row in prices
            ],
        )
    if api_name == "index_dailybasic":
        return provider_result(
            request,
            items=[
                {
                    "ts_code": ts_code,
                    "trade_date": "20260820",
                    "turnover_rate": 0.8,
                    "pe_ttm": 12.0,
                }
            ],
        )
    if api_name == "trade_cal":
        return provider_result(
            request,
            items=[
                {
                    "exchange": str(request.params.get("exchange", "SSE")),
                    "cal_date": row["trade_date"],
                    "is_open": 1,
                    "pretrade_date": prices[max(index - 1, 0)]["trade_date"],
                }
                for index, row in enumerate(prices)
            ],
        )
    return provider_result(request)


def tool_by_name(registry, name: str):
    return next(tool for tool in registry.all_tools if tool.name == name)


def test_registry_is_an_exact_business_allowlist() -> None:
    registry = build_registry(RecordingProvider())

    assert {tool.name for tool in registry.all_tools} == {
        "resolve_stock_identity",
        "get_trade_calendar",
        "get_daily_technical_market_snapshot",
        "get_daily_sentiment_flow_snapshot",
        "get_daily_fundamental_snapshot",
        "get_daily_event_snapshot",
        "get_stock_price_context",
        "get_index_market_context",
        "get_fund_market_context",
        "get_financial_statements",
        "get_financial_quality",
        "get_earnings_and_disclosure",
        "get_dividend_and_ownership_context",
        "get_pledge_risk_context",
        "get_valuation_context",
        "get_china_macro_context",
        "get_interest_rate_context",
        "search_market_news",
        "get_targeted_news_and_disclosures",
        "get_corporate_action_events",
        "get_sell_side_research_context",
        "get_economic_calendar",
        "get_capital_flow_context",
        "get_unusual_trading_activity",
        "get_stock_active_money_flow_context",
        *CALCULATOR_TOOL_NAMES,
    }
    assert len(registry.all_tools) == 30
    assert all("batch" not in tool.name and "vip" not in tool.name for tool in registry.all_tools)
    assert {tool.name for tool in registry.for_role(EvidenceAgentRole.TECHNICAL)} == {
        "resolve_stock_identity",
        "get_trade_calendar",
        "get_daily_technical_market_snapshot",
        "get_stock_price_context",
        "get_index_market_context",
        "get_fund_market_context",
        *CALCULATOR_TOOL_NAMES,
    }
    assert len(registry.for_role(EvidenceAgentRole.TECHNICAL)) == 11


def test_tool_schema_hides_routing_pagination_and_as_of() -> None:
    registry = build_registry(RecordingProvider())
    forbidden = {
        "api_name",
        "provider",
        "fields",
        "limit",
        "offset",
        "as_of",
        "run_id",
        "datasets",
    }

    for tool in registry.all_tools:
        schema_fields = set(tool.get_input_schema().model_fields)
        assert schema_fields.isdisjoint(forbidden)

    raw_data_fields = {
        "raw_rows",
        "rows",
        "price_rows",
        "adjustment_rows",
        "valuation_rows",
        "price_limit_rows",
        "suspension_rows",
        "calendar_rows",
    }
    for calculator_name in CALCULATOR_TOOL_NAMES:
        calculator = tool_by_name(registry, calculator_name)
        schema_fields = set(calculator.get_input_schema().model_fields)
        assert schema_fields.isdisjoint(raw_data_fields | forbidden)

    async def scenario() -> None:
        result = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-01",
                "end_date": "2026-08-20",
                "api_name": "income_vip",
            }
        )
        parsed = json.loads(result)
        assert parsed["issues"][0]["code"] == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_stock_price_tool_delegates_to_six_services_and_keeps_provenance() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "daily":
            return provider_result(
                request,
                items=[
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260820",
                        "close": 12.34,
                    }
                ],
            )
        return provider_result(request)

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        context = build_context(provider)
        registry = build_agent_tool_registry(context)
        result = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-01",
                "end_date": "2026-08-20",
                "frequency": "daily",
            }
        )

        assert result["status"] == "ok"
        assert result["as_of"] == "2026-08-20T15:00:00+08:00"
        assert isinstance(result["context_ref"], str)
        assert {request.api_name for request in provider.requests} == {
            "daily",
            "adj_factor",
            "daily_basic",
            "stk_limit",
            "suspend_d",
            "trade_cal",
        }
        price_dataset = next(
            dataset for dataset in result["datasets"] if dataset["label"] == "price_bars"
        )
        assert "rows" not in price_dataset
        assert price_dataset["preview_rows"][0]["data"]["close"] == 12.34
        assert price_dataset["preview_rows"][0]["source"]["provider"] == "PRIMARY"
        assert price_dataset["preview_rows"][0]["source"]["from_cache"] is False

        bundle = await context.data_store.get(context.run_id, result["context_ref"])
        assert bundle.kind == "stock_price_context"
        assert bundle.metadata["ts_code"] == "000001.SZ"
        assert bundle.datasets["price_bars"].items[0]["close"] == 12.34

    asyncio.run(scenario())


def test_three_market_context_tools_store_full_data_but_only_return_previews() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        cases = (
            (
                "get_stock_price_context",
                {
                    "ts_code": "000001.SZ",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
                "stock_price_context",
                "price_bars",
            ),
            (
                "get_index_market_context",
                {
                    "ts_code": "000300.SH",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
                "index_market_context",
                "index_price_bars",
            ),
            (
                "get_fund_market_context",
                {
                    "ts_code": "510300.SH",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
                "fund_market_context",
                "fund_price_bars",
            ),
        )

        for tool_name, arguments, expected_kind, price_label in cases:
            result = await tool_by_name(registry, tool_name).ainvoke(arguments)
            assert result["status"] == "ok"
            assert isinstance(result["context_ref"], str)
            exposed = next(
                dataset for dataset in result["datasets"] if dataset["label"] == price_label
            )
            assert "rows" not in exposed
            assert exposed["preview_item_count"] == 5
            assert exposed["stored_item_count"] == 70
            assert len(exposed["preview_rows"]) == 5
            assert exposed["preview_complete"] is False

            bundle = await context.data_store.get(context.run_id, result["context_ref"])
            assert bundle.kind == expected_kind
            assert len(bundle.datasets[price_label].items) == 70
            assert bundle.datasets[price_label].items[-1]["trade_date"] == "20260809"

    asyncio.run(scenario())


def test_index_context_routes_csi_and_sw_codes_to_their_respective_interfaces() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        tool = tool_by_name(registry, "get_index_market_context")

        for ts_code in ("000013.CSI", "000012CNY030.CSI"):
            supported = await tool.ainvoke(
                {
                    "ts_code": ts_code,
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                }
            )
            assert supported["status"] == "ok"
            assert supported["context_ref"] is not None

        request_count_before_sw = len(context.services.equity_market_data._provider.requests)
        sw_result = await tool.ainvoke(
            {
                "ts_code": "801010.SI",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        assert sw_result["status"] == "empty"
        assert sw_result["context_ref"] is not None
        sw_requests = context.services.equity_market_data._provider.requests[
            request_count_before_sw:
        ]
        assert [request.api_name for request in sw_requests] == ["sw_daily"]

    asyncio.run(scenario())


def test_five_technical_calculator_tools_use_context_refs_end_to_end() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        stock_result = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        index_result = await tool_by_name(registry, "get_index_market_context").ainvoke(
            {
                "ts_code": "000300.SH",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        stock_ref = stock_result["context_ref"]
        index_ref = index_result["context_ref"]
        assert isinstance(stock_ref, str)
        assert isinstance(index_ref, str)

        calls = {
            "calculate_return_and_trend": {
                "context_ref": stock_ref,
                "windows": [5, 20, 60],
            },
            "calculate_momentum": {
                "context_ref": stock_ref,
                "rsi_period": 14,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "roc_periods": [5, 20],
            },
            "calculate_risk_and_tradability": {
                "context_ref": stock_ref,
                "volatility_window": 20,
                "atr_period": 14,
            },
            "calculate_volume_and_liquidity": {
                "context_ref": stock_ref,
                "windows": [5, 20],
            },
            "calculate_relative_strength": {
                "target_context_ref": stock_ref,
                "benchmark_context_ref": index_ref,
                "windows": [20, 60],
            },
        }

        results: dict[str, dict[str, Any]] = {}
        for tool_name, arguments in calls.items():
            result = await tool_by_name(registry, tool_name).ainvoke(arguments)
            assert result["status"] == "ok", (tool_name, result)
            assert result["complete"] is True
            assert result["calculation"] is not None
            assert set(result["source_context_refs"]).issubset({stock_ref, index_ref})
            assert {subject["context_ref"] for subject in result["source_subjects"]} == set(
                result["source_context_refs"]
            )
            results[tool_name] = result

        assert (
            results["calculate_return_and_trend"]["calculation"]["interval_return_ratio"]["status"]
            == "available"
        )
        assert results["calculate_momentum"]["calculation"]["rsi"]["status"] == "available"
        assert (
            results["calculate_risk_and_tradability"]["calculation"]["annualized_volatility"][
                "status"
            ]
            == "available"
        )
        assert (
            results["calculate_volume_and_liquidity"]["calculation"]["latest_turnover_rate"][
                "status"
            ]
            == "available"
        )
        assert (
            results["calculate_relative_strength"]["calculation"]["alignment"][
                "common_observation_count"
            ]
            == 70
        )

    asyncio.run(scenario())


def test_single_target_calculators_reuse_stock_index_and_fund_contexts() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        source_cases = (
            (
                "stock",
                "get_stock_price_context",
                {
                    "ts_code": "000001.SZ",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
            ),
            (
                "index",
                "get_index_market_context",
                {
                    "ts_code": "000300.SH",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
            ),
            (
                "fund",
                "get_fund_market_context",
                {
                    "ts_code": "510300.SH",
                    "start_date": "2026-06-01",
                    "end_date": "2026-08-20",
                },
            ),
        )

        for instrument_kind, source_tool_name, arguments in source_cases:
            source = await tool_by_name(registry, source_tool_name).ainvoke(arguments)
            context_ref = source["context_ref"]
            assert isinstance(context_ref, str)

            for calculator_name in (
                "calculate_return_and_trend",
                "calculate_momentum",
                "calculate_risk_and_tradability",
                "calculate_volume_and_liquidity",
            ):
                result = await tool_by_name(registry, calculator_name).ainvoke(
                    {"context_ref": context_ref}
                )
                assert result["status"] == "ok", (instrument_kind, calculator_name, result)
                assert result["complete"] is True
                assert result["source_subjects"] == [
                    {
                        "context_ref": context_ref,
                        "bundle_kind": {
                            "stock": "stock_price_context",
                            "index": "index_market_context",
                            "fund": "fund_market_context",
                        }[instrument_kind],
                        "ts_code": arguments["ts_code"],
                        "frequency": "daily",
                    }
                ]

            risk = await tool_by_name(registry, "calculate_risk_and_tradability").ainvoke(
                {"context_ref": context_ref}
            )
            volume = await tool_by_name(registry, "calculate_volume_and_liquidity").ainvoke(
                {"context_ref": context_ref}
            )
            expected_stock_status = "available" if instrument_kind == "stock" else "not_applicable"
            assert risk["calculation"]["tradability"]["status"] == expected_stock_status
            assert volume["calculation"]["latest_turnover_rate"]["status"] == expected_stock_status

    asyncio.run(scenario())


def test_relative_strength_accepts_index_and_sector_etf_pair() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        sector_etf = await tool_by_name(registry, "get_fund_market_context").ainvoke(
            {
                "ts_code": "512480.SH",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        broad_index = await tool_by_name(registry, "get_index_market_context").ainvoke(
            {
                "ts_code": "000300.SH",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )

        result = await tool_by_name(registry, "calculate_relative_strength").ainvoke(
            {
                "target_context_ref": sector_etf["context_ref"],
                "benchmark_context_ref": broad_index["context_ref"],
                "windows": [20, 60],
            }
        )

        assert result["status"] == "ok"
        assert result["complete"] is True
        assert result["calculation"]["alignment"]["common_observation_count"] == 70
        assert [subject["bundle_kind"] for subject in result["source_subjects"]] == [
            "fund_market_context",
            "index_market_context",
        ]

    asyncio.run(scenario())


def test_fund_raw_opt_out_and_calculator_completeness_are_explicit() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        fund_result = await tool_by_name(registry, "get_fund_market_context").ainvoke(
            {
                "ts_code": "510300.SH",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
                "include_adjustment_factors": False,
            }
        )
        assert fund_result["status"] == "ok"

        raw_calculation = await tool_by_name(registry, "calculate_return_and_trend").ainvoke(
            {"context_ref": fund_result["context_ref"], "windows": [5, 20]}
        )
        assert raw_calculation["status"] == "ok"
        assert raw_calculation["calculation"]["metadata"]["adjustment_mode"] == "raw"
        assert raw_calculation["calculation"]["metadata"]["adjustment_applied"] is False

        short_history = await tool_by_name(registry, "calculate_return_and_trend").ainvoke(
            {"context_ref": fund_result["context_ref"], "windows": [100]}
        )
        assert short_history["status"] == "partial"
        assert short_history["complete"] is False
        assert short_history["issues"][0]["code"] == "CALCULATION_INCOMPLETE"

    asyncio.run(scenario())


def test_daily_only_calculators_reject_weekly_context() -> None:
    async def scenario() -> None:
        context = build_context(RecordingProvider(technical_responder))
        registry = build_agent_tool_registry(context)
        source = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
                "frequency": "weekly",
            }
        )
        assert isinstance(source["context_ref"], str)

        for calculator_name in (
            "calculate_risk_and_tradability",
            "calculate_volume_and_liquidity",
        ):
            result = await tool_by_name(registry, calculator_name).ainvoke(
                {"context_ref": source["context_ref"]}
            )
            assert result["status"] == "error"
            assert result["issues"][0]["code"] == "INVALID_ARGUMENT"
            assert "daily" in result["issues"][0]["message"]

    asyncio.run(scenario())


def test_missing_core_price_dataset_is_retryable_source_error() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "daily":
            raise ProviderError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "行情暂时不可用",
                provider=ProviderSource.PRIMARY,
            )
        return technical_responder(request)

    async def scenario() -> None:
        context = build_context(RecordingProvider(responder))
        registry = build_agent_tool_registry(context)
        source = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        assert source["status"] == "partial"
        assert isinstance(source["context_ref"], str)

        result = await tool_by_name(registry, "calculate_return_and_trend").ainvoke(
            {"context_ref": source["context_ref"]}
        )
        assert result["status"] == "error"
        assert result["issues"][0]["code"] == "DATA_INTEGRITY"
        assert result["issues"][0]["retryable"] is True

    asyncio.run(scenario())


def test_calculator_rejects_invalid_missing_and_cross_run_context_refs() -> None:
    async def scenario() -> None:
        data_store = InMemoryResearchDataStore()
        provider = RecordingProvider(technical_responder)
        context_a = build_context(provider, run_id=RUN_A, data_store=data_store)
        context_b = build_context(provider, run_id=RUN_B, data_store=data_store)
        registry_a = build_agent_tool_registry(context_a)
        registry_b = build_agent_tool_registry(context_b)

        source = await tool_by_name(registry_a, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-06-01",
                "end_date": "2026-08-20",
            }
        )
        context_ref = source["context_ref"]
        assert isinstance(context_ref, str)

        malformed = await tool_by_name(registry_a, "calculate_return_and_trend").ainvoke(
            {"context_ref": "ctx_guessable"}
        )
        malformed_payload = json.loads(malformed)
        assert malformed_payload["issues"][0]["code"] == "INVALID_ARGUMENT"

        missing = await tool_by_name(registry_a, "calculate_return_and_trend").ainvoke(
            {"context_ref": f"ctx_{'a' * 32}"}
        )
        assert missing["status"] == "error"
        assert missing["issues"][0]["code"] == "INVALID_ARGUMENT"

        cross_run = await tool_by_name(registry_b, "calculate_return_and_trend").ainvoke(
            {"context_ref": context_ref}
        )
        assert cross_run["status"] == "error"
        assert cross_run["issues"][0]["code"] == "INVALID_ARGUMENT"
        assert cross_run["calculation"] is None

    asyncio.run(scenario())


def test_partial_failure_is_not_misreported_as_empty_data() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "fina_mainbz":
            raise ProviderError(
                ProviderErrorCode.PERMISSION_DENIED,
                request.api_name,
                "当前账号无权限",
                provider=ProviderSource.BACKUP,
            )
        return provider_result(request)

    async def scenario() -> None:
        registry = build_registry(RecordingProvider(responder))
        result = await tool_by_name(registry, "get_financial_quality").ainvoke(
            {
                "ts_code": "000001.SZ",
                "period": "20251231",
                "composition_type": "P",
            }
        )

        assert result["status"] == "partial"
        assert result["complete"] is False
        assert result["issues"][0]["dataset_label"] == "business_composition"
        assert result["issues"][0]["code"] == "CAPABILITY_UNAVAILABLE"
        assert len(result["datasets"]) == 2

    asyncio.run(scenario())


def test_empty_success_is_distinct_from_error() -> None:
    async def scenario() -> None:
        registry = build_registry(RecordingProvider())
        result = await tool_by_name(registry, "get_unusual_trading_activity").ainvoke(
            {"ts_code": "000001.SZ", "trade_date": "2026-08-20"}
        )

        assert result["status"] == "empty"
        assert result["issues"] == []
        assert result["complete"] is True
        assert result["total_returned_items"] == 0

    asyncio.run(scenario())


def test_oversized_market_preview_keeps_full_data_reference() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "daily":
            return provider_result(
                request,
                items=[
                    {"ts_code": "000001.SZ", "trade_date": "20260820", "close": 12.3},
                    {"ts_code": "000001.SZ", "trade_date": "20260819", "close": 12.1},
                ],
            )
        return provider_result(request)

    async def scenario() -> None:
        context = build_context(
            RecordingProvider(responder),
            limits=ToolLimits(max_items=1),
        )
        registry = build_agent_tool_registry(context)
        result = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-01",
                "end_date": "2026-08-20",
            }
        )

        assert result["status"] == "too_large"
        assert isinstance(result["context_ref"], str)
        assert result["datasets"] == []
        assert result["complete"] is False
        assert result["issues"][0]["code"] == "RESULT_TOO_LARGE"
        assert result["issues"][0]["retryable"] is False

        bundle = await context.data_store.get(context.run_id, result["context_ref"])
        assert len(bundle.datasets["price_bars"].items) == 2
        assert [row["close"] for row in bundle.datasets["price_bars"].items] == [12.3, 12.1]

    asyncio.run(scenario())


def test_frozen_as_of_rejects_future_observations() -> None:
    async def scenario() -> None:
        registry = build_registry(RecordingProvider())
        result = await tool_by_name(registry, "get_stock_price_context").ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-20",
                "end_date": "2026-08-21",
            }
        )

        assert result["status"] == "error"
        assert all(issue["code"] == "INVALID_ARGUMENT" for issue in result["issues"])
        assert all(issue["retryable"] is False for issue in result["issues"])

    asyncio.run(scenario())


def test_news_tool_requires_timezone_aware_datetimes() -> None:
    async def scenario() -> None:
        registry = build_registry(RecordingProvider())
        result = await tool_by_name(registry, "search_market_news").ainvoke(
            {
                "start_at": "2026-08-20T14:00:00",
                "end_at": "2026-08-20T15:00:00",
            }
        )

        parsed = json.loads(result)
        assert parsed["issues"][0]["code"] == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_oversized_news_body_is_not_silently_truncated() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {
                    "record_key": "ak_test_long_news",
                    "title": "长新闻",
                    "content": "正文" * 100,
                    "published_at": "2026-08-20 14:30:00",
                    "source_name": "测试来源",
                    "source_url": "https://example.com/news",
                    "source_kind": "market_news",
                    "citable": True,
                }
            ],
        )

    async def scenario() -> None:
        registry = build_registry(
            RecordingProvider(responder),
            limits=ToolLimits(max_item_chars=50),
        )
        result = await tool_by_name(registry, "search_market_news").ainvoke(
            {
                "start_at": "2026-08-20T14:00:00+08:00",
                "end_at": "2026-08-20T15:00:00+08:00",
            }
        )

        assert result["status"] == "too_large"
        assert result["datasets"] == []
        assert result["issues"][0]["code"] == "RESULT_TOO_LARGE"
        assert "正文" not in json.dumps(result, ensure_ascii=False)

    asyncio.run(scenario())


def test_concurrent_invocations_do_not_mix_results() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "stock_basic":
            code = str(request.params["ts_code"])
            return provider_result(
                request,
                items=[
                    {
                        "ts_code": code,
                        "name": "平安银行" if code == "000001.SZ" else "贵州茅台",
                    }
                ],
            )
        return provider_result(request)

    async def scenario() -> None:
        registry = build_registry(RecordingProvider(responder))
        tool = tool_by_name(registry, "resolve_stock_identity")
        first, second = await asyncio.gather(
            tool.ainvoke({"ts_code": "000001.SZ"}),
            tool.ainvoke({"ts_code": "600519.SH"}),
        )

        first_code = first["datasets"][0]["rows"][0]["data"]["ts_code"]
        second_code = second["datasets"][0]["rows"][0]["data"]["ts_code"]
        assert (first_code, second_code) == ("000001.SZ", "600519.SH")

    asyncio.run(scenario())
