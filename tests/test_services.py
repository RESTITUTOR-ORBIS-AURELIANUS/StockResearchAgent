"""源数据 Service 的确定性行为测试，不访问真实上游。"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_research_agent.providers.errors import ProviderError, ProviderErrorCode
from stock_research_agent.providers.models import (
    ProviderQuery,
    ProviderResult,
    ProviderSource,
)
from stock_research_agent.providers.routes import SUPPORTED_APIS
from stock_research_agent.services import build_data_services
from stock_research_agent.services.catalog import SERVICE_API_GROUPS
from stock_research_agent.services.errors import (
    ServiceDataValidationError,
    ServiceInputError,
    ServicePaginationError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
FETCHED_AT = datetime(2026, 8, 18, 15, 30, tzinfo=SHANGHAI)


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
    has_more: bool = False,
    provider: ProviderSource = ProviderSource.PRIMARY,
    returned_fields: tuple[str, ...] | None = None,
    normalize_items: bool = True,
) -> ProviderResult:
    fields = returned_fields if returned_fields is not None else request.fields
    raw_items = items or []
    normalized_items = (
        [{**{field: row.get(field) for field in fields}, **row} for row in raw_items]
        if normalize_items
        else raw_items
    )
    return ProviderResult(
        api_name=request.api_name,
        provider=provider,
        fetched_at=FETCHED_AT,
        fields=fields,
        items=normalized_items,
        provider_code=0,
        has_more=has_more,
        response_bytes=123,
    )


def test_eight_source_services_partition_all_89_provider_apis() -> None:
    groups = [set(specs) for specs in SERVICE_API_GROUPS.values()]

    assert [len(group) for group in groups] == [14, 10, 5, 17, 19, 8, 13, 3]
    assert set().union(*groups) == set(SUPPORTED_APIS)
    assert sum(len(group) for group in groups) == len(set().union(*groups)) == 89


def test_factory_injects_one_provider_into_all_services() -> None:
    provider = RecordingProvider()
    public_news_provider = RecordingProvider()
    services = build_data_services(
        provider,
        public_news_provider=public_news_provider,
    )

    assert services.instrument_reference._provider is provider
    assert services.equity_market_data._provider is provider
    assert services.cross_asset_market_data._provider is provider
    assert services.fundamental_data._provider is provider
    assert services.macro_data._provider is provider
    assert services.ownership_event._provider is provider
    assert services.trading_behavior._provider is provider
    assert services.news_event._provider is provider
    assert services.public_news_event._provider is public_news_provider
    assert services.daily_technical_snapshot._instrument_reference is services.instrument_reference
    assert services.daily_technical_snapshot._equity_market_data is services.equity_market_data
    assert (
        services.daily_fundamental_snapshot._instrument_reference is services.instrument_reference
    )
    assert services.daily_fundamental_snapshot._equity_market_data is services.equity_market_data
    assert services.daily_fundamental_snapshot._fundamental_data is services.fundamental_data
    assert services.daily_fundamental_snapshot._macro_data is services.macro_data
    assert services.daily_event_snapshot._public_news_event is services.public_news_event
    assert services.daily_event_snapshot._news_event is services.news_event
    assert (
        services.daily_event_snapshot._instrument_reference
        is services.instrument_reference
    )


def test_major_news_uses_frozen_window_without_offset_pagination() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {
                    "title": "窗口内新闻",
                    "content": "正文",
                    "pub_time": "2026-08-18 15:30:00",
                    "src": "新浪财经",
                },
                {
                    "title": "窗口外新闻",
                    "content": "正文",
                    "pub_time": "2026-08-18 16:00:01",
                    "src": "新浪财经",
                },
            ],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = build_data_services(provider).news_event
        dataset = await service.get_market_news(
            datetime(2026, 8, 18, 15, 0, tzinfo=SHANGHAI),
            datetime(2026, 8, 18, 16, 0, tzinfo=SHANGHAI),
            source="新浪财经",
            as_of=datetime(2026, 8, 18, 16, 0, tzinfo=SHANGHAI),
        )

        assert len(provider.requests) == 1
        assert provider.requests[0].params == {
            "start_date": "2026-08-18 15:00:00",
            "end_date": "2026-08-18 16:00:00",
            "src": "新浪财经",
        }
        assert "limit" not in provider.requests[0].params
        assert "offset" not in provider.requests[0].params
        assert [row["title"] for row in dataset.items] == ["窗口内新闻"]
        assert dataset.received_item_count == 2
        assert dataset.discarded_item_count == 1
        assert dataset.data_as_of == date(2026, 8, 18)

    asyncio.run(scenario())


def test_major_news_rejects_naive_time_and_oversized_window() -> None:
    async def scenario() -> None:
        service = build_data_services(RecordingProvider()).news_event
        with pytest.raises(ServiceInputError, match="带时区"):
            await service.get_market_news(
                datetime(2026, 8, 18, 15, 0),
                datetime(2026, 8, 18, 16, 0, tzinfo=SHANGHAI),
            )
        with pytest.raises(ServiceInputError, match="24 小时"):
            await service.get_market_news(
                datetime(2026, 8, 18, 15, 0, tzinfo=SHANGHAI),
                datetime(2026, 8, 19, 15, 0, 1, tzinfo=SHANGHAI),
            )

    asyncio.run(scenario())


def test_sell_side_research_methods_validate_cutoff_scope_and_stock_filter() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.api_name == "report_rc":
            return provider_result(
                request,
                items=[
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "report_date": "20260818",
                        "report_title": "盈利能力跟踪",
                        "org_name": "测试证券",
                        "author_name": "研究员",
                        "quarter": "2026Q4",
                        "rating": "增持",
                    }
                ],
            )
        if request.api_name == "broker_recommend":
            return provider_result(
                request,
                items=[
                    {
                        "month": "202608",
                        "broker": "测试证券",
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                    },
                    {
                        "month": "202608",
                        "broker": "其他证券",
                        "ts_code": "600000.SH",
                        "name": "浦发银行",
                    },
                ],
            )
        raise AssertionError(request.api_name)

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = build_data_services(provider).news_event
        reports = await service.get_sell_side_reports(
            date(2026, 8, 1),
            date(2026, 8, 18),
            ts_code="000001.sz",
            as_of=date(2026, 8, 18),
        )
        daily_reports = await service.get_daily_sell_side_reports(
            date(2026, 8, 18),
            as_of=date(2026, 8, 18),
        )
        recommendations = await service.get_broker_recommendations(
            "202608",
            ts_code="000001.sz",
            as_of=date(2026, 8, 18),
        )

        assert reports.items[0]["report_title"] == "盈利能力跟踪"
        assert provider.requests[0].params == {
            "start_date": "20260801",
            "end_date": "20260818",
            "ts_code": "000001.SZ",
        }
        assert provider.requests[1].params == {"report_date": "20260818"}
        assert daily_reports.items[0]["report_date"] == "20260818"
        assert [row["ts_code"] for row in recommendations.items] == ["000001.SZ"]
        assert provider.requests[2].params == {"month": "202608"}
        assert all("limit" not in request.params for request in provider.requests)

        with pytest.raises(ServiceInputError, match="31 天"):
            await service.get_sell_side_reports(
                date(2026, 1, 1),
                date(2026, 3, 1),
                as_of=date(2026, 3, 1),
            )
        with pytest.raises(ServiceInputError, match="366 天"):
            await service.get_sell_side_reports(
                date(2025, 1, 1),
                date(2026, 8, 18),
                ts_code="000001.SZ",
                as_of=date(2026, 8, 18),
            )
        with pytest.raises(ServiceInputError, match="不能晚于"):
            await service.get_broker_recommendations(
                "202609",
                as_of=date(2026, 8, 18),
            )
        with pytest.raises(ServiceDataValidationError, match="历史成分快照"):
            await service.get_broker_recommendations(
                "202608",
                as_of=date(2026, 8, 1),
            )

    asyncio.run(scenario())


def test_stock_bars_are_fully_paginated_and_keep_item_provenance() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        offset = int(request.params["offset"])
        if offset == 0:
            return provider_result(
                request,
                items=[
                    {"ts_code": "000001.SZ", "trade_date": "20260818", "close": 12.0},
                    {"ts_code": "000001.SZ", "trade_date": "20260817", "close": 11.8},
                ],
                has_more=True,
            )
        assert offset == 2
        return provider_result(
            request,
            items=[{"ts_code": "000001.SZ", "trade_date": "20260816", "close": 11.7}],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = build_data_services(provider, page_size=2).equity_market_data
        dataset = await service.get_stock_bars(
            "000001.sz",
            date(2026, 8, 1),
            date(2026, 8, 18),
            as_of=date(2026, 8, 18),
        )

        assert [request.params["offset"] for request in provider.requests] == [0, 2]
        assert all(request.params["limit"] == 2 for request in provider.requests)
        assert dataset.query_params == {
            "ts_code": "000001.SZ",
            "start_date": "20260801",
            "end_date": "20260818",
        }
        assert len(dataset.items) == 3
        assert dataset.data_as_of == date(2026, 8, 18)
        assert [page.provider for page in dataset.pages] == [ProviderSource.PRIMARY] * 2
        assert [trace.page_index for trace in dataset.item_traces] == [0, 0, 1]
        assert [trace.source_offset for trace in dataset.item_traces] == [0, 1, 2]
        assert [trace.provider for trace in dataset.item_traces] == [ProviderSource.PRIMARY] * 3
        assert dataset.complete is True

    asyncio.run(scenario())


def test_pagination_rejects_switching_between_provider_snapshots() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        if request.params["offset"] == 0:
            return provider_result(
                request,
                items=[{"ts_code": "000001.SZ", "trade_date": "20260818"}],
                has_more=True,
            )
        return provider_result(
            request,
            items=[{"ts_code": "000001.SZ", "trade_date": "20260817"}],
            provider=ProviderSource.BACKUP,
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder), page_size=1).equity_market_data
        with pytest.raises(ServicePaginationError, match="不同快照"):
            await service.get_stock_bars(
                "000001.SZ",
                date(2026, 8, 1),
                date(2026, 8, 18),
            )

    asyncio.run(scenario())


def test_as_of_filters_future_financial_announcements() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {"ts_code": "000001.SZ", "ann_date": "20260818", "end_date": "20260630"},
                {"ts_code": "000001.SZ", "ann_date": "20260819", "end_date": "20260630"},
            ],
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).fundamental_data
        dataset = await service.get_income_statement(
            "000001.SZ",
            "20260630",
            as_of=date(2026, 8, 18),
        )

        assert [row["ann_date"] for row in dataset.items] == ["20260818"]
        assert dataset.received_item_count == 2
        assert dataset.discarded_item_count == 1

    asyncio.run(scenario())


def test_single_stock_financial_queries_fail_closed_on_wrong_stock_or_period() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260818",
                    "end_date": "20260630",
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20260818",
                    "end_date": "20260630",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260818",
                    "end_date": "20260331",
                },
            ],
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).fundamental_data
        datasets = (
            await service.get_earnings_forecast(
                "000001.SZ", "20260630", as_of=date(2026, 8, 18)
            ),
            await service.get_earnings_express(
                "000001.SZ", "20260630", as_of=date(2026, 8, 18)
            ),
            await service.get_disclosure_schedule(
                "000001.SZ", "20260630", as_of=date(2026, 8, 18)
            ),
        )

        assert all(len(dataset.items) == 1 for dataset in datasets)
        assert all(dataset.items[0]["ts_code"] == "000001.SZ" for dataset in datasets)
        assert all(dataset.items[0]["end_date"] == "20260630" for dataset in datasets)
        assert all(dataset.discarded_item_count == 2 for dataset in datasets)

    asyncio.run(scenario())


def test_dividends_apply_the_declared_announcement_window_locally() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260730",
                    "end_date": "20260630",
                    "div_proc": "预案",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260810",
                    "end_date": "20260630",
                    "div_proc": "实施",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260819",
                    "end_date": "20260630",
                    "div_proc": "实施",
                },
            ],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = build_data_services(provider).fundamental_data
        dataset = await service.get_dividends(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 18),
            as_of=date(2026, 8, 18),
        )

        assert [row["ann_date"] for row in dataset.items] == ["20260810"]
        assert dataset.received_item_count == 3
        assert dataset.discarded_item_count == 2
        assert provider.requests[0].params["ts_code"] == "000001.SZ"
        assert "start_date" not in provider.requests[0].params
        assert "end_date" not in provider.requests[0].params

        with pytest.raises(ServiceInputError, match="必须同时提供"):
            await service.get_dividends("000001.SZ", date(2026, 8, 1))

    asyncio.run(scenario())


def test_as_of_fails_closed_when_response_has_no_verifiable_date() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(request, items=[{"ts_code": "000001.SZ", "close": 12.0}])

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).equity_market_data
        with pytest.raises(ServiceDataValidationError):
            await service.get_stock_bars(
                "000001.SZ",
                date(2026, 8, 1),
                date(2026, 8, 18),
                as_of=date(2026, 8, 18),
            )

    asyncio.run(scenario())


def test_nonempty_response_must_contain_all_fixed_service_fields() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[{"ts_code": "000001.SZ", "trade_date": "20260818"}],
            returned_fields=("ts_code", "trade_date"),
            normalize_items=False,
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).equity_market_data
        with pytest.raises(ServiceDataValidationError, match="必需字段"):
            await service.get_stock_bars(
                "000001.SZ",
                date(2026, 8, 1),
                date(2026, 8, 18),
            )

    asyncio.run(scenario())


def test_historical_as_of_is_rejected_for_current_only_or_delayed_sources() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[{"ts_code": "000001.SZ", "name": "平安银行"}],
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).instrument_reference
        with pytest.raises(ServiceDataValidationError, match="历史成分快照"):
            await service.get_stock_basic("000001.SZ", as_of=date(2026, 8, 17))

    asyncio.run(scenario())


def test_full_market_event_is_filtered_locally_without_sending_stock_code() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {"ts_code": "000001.SZ", "ann_date": "20260818", "proc": "实施"},
                {"ts_code": "600000.SH", "ann_date": "20260818", "proc": "实施"},
            ],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = build_data_services(provider).ownership_event
        dataset = await service.get_repurchase_events(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 18),
            as_of=date(2026, 8, 18),
        )

        sent_params = provider.requests[0].params
        assert "ts_code" not in sent_params
        assert sent_params["start_date"] == "20260801"
        assert [row["ts_code"] for row in dataset.items] == ["000001.SZ"]

    asyncio.run(scenario())


def test_corporate_action_services_recheck_event_dates_locally() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        date_field = "float_date" if request.api_name == "share_float" else "ann_date"
        announcement_dates = (
            ["20260720"] * 4
            if request.api_name == "share_float"
            else ["20260731", "20260810", "20260819", "20260810"]
        )
        event_dates = ["20260731", "20260810", "20260819", "20260810"]
        return provider_result(
            request,
            items=[
                {
                    "ts_code": ts_code,
                    "ann_date": announcement_date,
                    date_field: event_date,
                }
                for ts_code, announcement_date, event_date in zip(
                    ["000001.SZ", "000001.SZ", "000001.SZ", "600000.SH"],
                    announcement_dates,
                    event_dates,
                    strict=True,
                )
            ],
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).ownership_event
        datasets = (
            await service.get_repurchase_events(
                "000001.SZ", date(2026, 8, 1), date(2026, 8, 18), as_of=date(2026, 8, 18)
            ),
            await service.get_unlock_events(
                "000001.SZ", date(2026, 8, 1), date(2026, 8, 18), as_of=date(2026, 8, 18)
            ),
            await service.get_holder_trades(
                "000001.SZ", date(2026, 8, 1), date(2026, 8, 18), as_of=date(2026, 8, 18)
            ),
        )

        assert all(len(dataset.items) == 1 for dataset in datasets)
        assert all(dataset.items[0]["ts_code"] == "000001.SZ" for dataset in datasets)
        assert datasets[0].items[0]["ann_date"] == "20260810"
        assert datasets[1].items[0]["float_date"] == "20260810"
        assert datasets[2].items[0]["ann_date"] == "20260810"

    asyncio.run(scenario())


def test_compatibility_methods_do_not_send_unverified_parameters() -> None:
    async def scenario() -> None:
        provider = RecordingProvider()
        service = build_data_services(provider).instrument_reference
        await service.get_option_contracts("SSE")
        await service.get_hsgt_stock("000001.SZ")
        await build_data_services(provider).equity_market_data.get_suspensions(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 18),
        )
        await build_data_services(provider).equity_market_data.get_price_limits(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 18),
        )

        assert provider.requests[0].params == {
            "exchange": "SSE",
            "limit": 1_000,
            "offset": 0,
        }
        assert provider.requests[1].params == {
            "ts_code": "000001.SZ",
            "limit": 1_000,
            "offset": 0,
        }
        assert provider.requests[2].params == {
            "start_date": "20260801",
            "end_date": "20260818",
            "limit": 1_000,
            "offset": 0,
        }
        assert provider.requests[3].params == {
            "ts_code": "000001.SZ",
            "start_date": "20260801",
            "end_date": "20260818",
            "limit": 1_000,
            "offset": 0,
        }

    asyncio.run(scenario())


def test_stock_hsgt_is_filtered_locally_even_if_upstream_ignores_code() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            items=[
                {"ts_code": "000001.SZ", "name": "平安银行", "type": "深股通"},
                {"ts_code": "600000.SH", "name": "浦发银行", "type": "沪股通"},
            ],
        )

    async def scenario() -> None:
        service = build_data_services(RecordingProvider(responder)).instrument_reference
        dataset = await service.get_hsgt_stock("000001.SZ")
        assert [row["ts_code"] for row in dataset.items] == ["000001.SZ"]
        assert len(dataset.item_traces) == 1

    asyncio.run(scenario())


def test_targeted_ownership_events_send_stock_code_except_repurchase() -> None:
    async def scenario() -> None:
        provider = RecordingProvider()
        service = build_data_services(provider).ownership_event
        await service.get_unlock_events("000001.SZ", date(2026, 8, 1), date(2026, 8, 31))
        await service.get_holder_trades("000001.SZ", date(2026, 8, 1), date(2026, 8, 31))
        await service.get_repurchase_events("000001.SZ", date(2026, 8, 1), date(2026, 8, 31))

        assert provider.requests[0].params["ts_code"] == "000001.SZ"
        assert provider.requests[1].params["ts_code"] == "000001.SZ"
        assert "ts_code" not in provider.requests[2].params

    asyncio.run(scenario())


def test_naive_as_of_and_future_observation_window_are_rejected() -> None:
    async def scenario() -> None:
        service = build_data_services(RecordingProvider()).equity_market_data
        with pytest.raises(ServiceInputError, match="时区"):
            await service.get_stock_bars(
                "000001.SZ",
                date(2026, 8, 1),
                date(2026, 8, 18),
                as_of=datetime(2026, 8, 18, 15, 0),
            )
        with pytest.raises(ServiceInputError, match="end_date"):
            await service.get_stock_bars(
                "000001.SZ",
                date(2026, 8, 1),
                date(2026, 8, 19),
                as_of=date(2026, 8, 18),
            )

    asyncio.run(scenario())


def test_provider_errors_are_not_disguised_as_empty_business_data() -> None:
    class FailingProvider:
        async def query(self, request: ProviderQuery) -> ProviderResult:
            raise ProviderError(
                ProviderErrorCode.DATA_SOURCE_UNAVAILABLE,
                request.api_name,
                "both providers unavailable",
            )

    async def scenario() -> None:
        service = build_data_services(FailingProvider()).instrument_reference
        with pytest.raises(ProviderError):
            await service.get_stock_basic("000001.SZ")

    asyncio.run(scenario())
