"""AKShare 公开新闻 Service 的日期、代码与完整性边界测试。"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.public_news_event import (
    AnnouncementCategory,
    MarketNewsSource,
    PublicNewsEventService,
)
from stock_research_agent.tools.models import TargetedNewsDisclosureInput

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 24, 15, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI)


class RecordingProvider:
    def __init__(self, responder: Callable[[ProviderQuery], ProviderResult]) -> None:
        self.requests: list[ProviderQuery] = []
        self._responder = responder

    async def query(self, request: ProviderQuery) -> ProviderResult:
        self.requests.append(request)
        return self._responder(request)


def provider_result(
    request: ProviderQuery,
    items: list[dict[str, Any]],
    *,
    provider: ProviderSource = ProviderSource.AKSHARE_EASTMONEY,
) -> ProviderResult:
    rows = [{**{field: row.get(field) for field in request.fields}, **row} for row in items]
    return ProviderResult(
        api_name=request.api_name,
        provider=provider,
        fetched_at=FETCHED_AT,
        fields=request.fields,
        items=rows,
        provider_code=0,
        response_bytes=123,
    )


def news_row(
    title: str,
    published_at: str,
    *,
    ts_code: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_key": f"ak_{title}",
        "title": title,
        "content": "正文",
        "published_at": published_at,
        "source_name": "东方财富",
        "source_url": f"https://example.com/{title}",
        "source_kind": "stock_news" if ts_code else "market_news",
        "citable": True,
    }
    if ts_code:
        row.update(ts_code=ts_code, keywords=title)
    return row


def notice_row(title: str, announcement_date: str) -> dict[str, Any]:
    return {
        "record_key": f"ak_{title}",
        "security_code": "000001",
        "ts_code": "000001.SZ",
        "stock_name": "平安银行",
        "title": title,
        "announcement_type": "重大事项",
        "announcement_date": announcement_date,
        "source_url": "https://example.com/notice",
        "source_kind": "announcement",
        "citable": True,
    }


def test_market_feed_filters_exact_time_and_declares_recent_window_incomplete() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            [
                news_row("窗口内", "2026-08-24 14:30:00"),
                news_row("未来新闻", "2026-08-24 15:00:01"),
                news_row("窗口外", "2026-08-24 13:59:59"),
            ],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = PublicNewsEventService(provider)
        dataset = await service.get_market_news(
            MarketNewsSource.EASTMONEY,
            datetime(2026, 8, 24, 14, 0, tzinfo=SHANGHAI),
            AS_OF,
            as_of=AS_OF,
        )

        assert [item["title"] for item in dataset.items] == ["窗口内"]
        assert dataset.received_item_count == 3
        assert dataset.discarded_item_count == 2
        assert dataset.complete is False
        assert provider.requests[0].api_name == "stock_info_global_em"
        assert provider.requests[0].params == {}

    asyncio.run(scenario())


def test_targeted_news_converts_code_and_rejects_historical_replay() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            [news_row("个股新闻", "2026-08-24 10:00:00", ts_code="000001.SZ")],
        )

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = PublicNewsEventService(provider)
        dataset = await service.get_stock_news(
            "000001.sz",
            datetime(2026, 8, 20, 0, 0, tzinfo=SHANGHAI),
            AS_OF,
            as_of=AS_OF,
        )
        assert dataset.items[0]["ts_code"] == "000001.SZ"
        assert provider.requests[0].params == {"symbol": "000001"}
        assert dataset.complete is False

        with pytest.raises(ServiceDataValidationError, match="不能在"):
            await service.get_stock_news(
                "000001.SZ",
                datetime(2026, 8, 19, 0, 0, tzinfo=SHANGHAI),
                datetime(2026, 8, 20, 15, 0, tzinfo=SHANGHAI),
                as_of=datetime(2026, 8, 20, 15, 0, tzinfo=SHANGHAI),
            )

    asyncio.run(scenario())


def test_daily_and_targeted_announcements_use_explicit_dates_and_categories() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(request, [notice_row("回购公告", "2026-08-24")])

    async def scenario() -> None:
        provider = RecordingProvider(responder)
        service = PublicNewsEventService(provider)
        daily = await service.get_daily_announcements(
            date(2026, 8, 24),
            category=AnnouncementCategory.MATERIAL_EVENT,
            as_of=AS_OF,
        )
        targeted = await service.get_stock_announcements(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 24),
            category=AnnouncementCategory.ALL,
            as_of=AS_OF,
        )

        assert daily.items[0]["title"] == "回购公告"
        assert provider.requests[0].api_name == "stock_notice_report"
        assert provider.requests[0].params == {"symbol": "重大事项", "date": "20260824"}
        assert provider.requests[1].api_name == "stock_individual_notice_report"
        assert provider.requests[1].params == {
            "security": "000001",
            "symbol": "全部",
            "begin_date": "20260801",
            "end_date": "20260824",
        }
        assert targeted.items[0]["ts_code"] == "000001.SZ"

    asyncio.run(scenario())


def test_announcement_queries_recheck_dates_and_stock_locally() -> None:
    def responder(request: ProviderQuery) -> ProviderResult:
        return provider_result(
            request,
            [
                notice_row("窗口前公告", "2026-07-31"),
                notice_row("窗口内公告", "2026-08-10"),
                notice_row("窗口后公告", "2026-08-25"),
                {**notice_row("其他股票公告", "2026-08-10"), "ts_code": "600000.SH"},
            ],
        )

    async def scenario() -> None:
        service = PublicNewsEventService(RecordingProvider(responder))
        targeted = await service.get_stock_announcements(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 24),
            as_of=AS_OF,
        )
        daily = await service.get_daily_announcements(
            date(2026, 8, 10),
            as_of=AS_OF,
        )

        assert [row["title"] for row in targeted.items] == ["窗口内公告"]
        assert {row["title"] for row in daily.items} == {"窗口内公告", "其他股票公告"}

    asyncio.run(scenario())


def test_targeted_news_tool_schema_counts_both_endpoint_dates() -> None:
    TargetedNewsDisclosureInput(
        ts_code="000001.SZ",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )
    with pytest.raises(ValidationError, match="不能超过 31 天"):
        TargetedNewsDisclosureInput(
            ts_code="000001.SZ",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 9, 1),
        )


def test_public_news_service_rejects_naive_or_future_windows() -> None:
    service = PublicNewsEventService(
        RecordingProvider(lambda request: provider_result(request, []))
    )

    async def scenario() -> None:
        with pytest.raises(ServiceInputError, match="带时区"):
            await service.get_market_news(
                MarketNewsSource.THS,
                datetime(2026, 8, 24, 14, 0),
                AS_OF,
                as_of=AS_OF,
            )
        with pytest.raises(ServiceInputError, match="不能晚于 as_of"):
            await service.get_daily_announcements(
                date(2026, 8, 25),
                as_of=AS_OF,
            )

    asyncio.run(scenario())
