"""新闻事件每日快照与 Event Tool 链的离线契约测试。"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    ProviderTransportError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.research_data import InMemoryResearchDataStore
from stock_research_agent.services import build_data_services
from stock_research_agent.tools import (
    EvidenceAgentRole,
    ResearchToolContext,
    build_agent_tool_registry,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 24, 15, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 24, 15, 1, tzinfo=SHANGHAI)
RUN_ID = "run_20260824_150000_event_snapshot_aaaaaaaa"


class StructuredEventProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderQuery] = []

    async def query(self, request: ProviderQuery) -> ProviderResult:
        self.requests.append(request)
        if request.api_name == "stock_basic":
            return make_result(
                request,
                [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "industry": "银行",
                        "market": "主板",
                        "list_date": "19910403",
                    },
                    # 歧义名称故意对应两个代码，不应被新闻自动映射。
                    {
                        "ts_code": "000002.SZ",
                        "name": "同名公司",
                        "industry": "测试",
                        "market": "主板",
                        "list_date": "19910129",
                    },
                    {
                        "ts_code": "600002.SH",
                        "name": "同名公司",
                        "industry": "测试",
                        "market": "主板",
                        "list_date": "20000101",
                    },
                ],
                ProviderSource.PRIMARY,
            )
        if request.api_name == "report_rc":
            if (
                request.params.get("report_date") != "20260824"
                and request.params.get("ts_code") != "000001.SZ"
            ):
                return make_result(request, [], ProviderSource.BACKUP)
            return make_result(
                request,
                [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "report_date": "20260824",
                        "report_title": "净息差与资产质量跟踪",
                        "report_type": "公司",
                        "classify": "点评",
                        "org_name": "测试证券",
                        "author_name": "研究员",
                        "quarter": "2026Q4",
                        "eps": 2.0,
                        "pe": 6.0,
                        "rating": "增持",
                        "min_price": 12.0,
                        "max_price": 15.0,
                    },
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "report_date": "20260824",
                        "report_title": "净息差与资产质量跟踪",
                        "report_type": "公司",
                        "classify": "点评",
                        "org_name": "测试证券",
                        "author_name": "研究员",
                        "quarter": "2027Q4",
                        "eps": 2.2,
                        "pe": 5.5,
                        "rating": "增持",
                        "min_price": 12.0,
                        "max_price": 15.0,
                    },
                ],
                ProviderSource.BACKUP,
            )
        if request.api_name == "broker_recommend":
            return make_result(
                request,
                [
                    {
                        "month": str(request.params["month"]),
                        "broker": "甲证券",
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                    },
                    {
                        "month": str(request.params["month"]),
                        "broker": "乙证券",
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                    },
                ],
                ProviderSource.PRIMARY,
            )
        return make_result(request, [], ProviderSource.PRIMARY)


class PartiallyFailingStructuredEventProvider(StructuredEventProvider):
    async def query(self, request: ProviderQuery) -> ProviderResult:
        if request.api_name == "report_rc" and request.params.get("report_date") == "20260823":
            self.requests.append(request)
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "simulated report endpoint failure",
                provider=ProviderSource.BACKUP,
            )
        return await super().query(request)


class PublicNewsProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderQuery] = []

    async def query(self, request: ProviderQuery) -> ProviderResult:
        self.requests.append(request)
        if request.api_name == "stock_info_global_cls":
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "simulated timeout",
                provider=ProviderSource.AKSHARE_CLS,
            )
        if request.api_name in {"stock_info_global_em", "stock_info_global_ths"}:
            source = (
                ProviderSource.AKSHARE_EASTMONEY
                if request.api_name.endswith("_em")
                else ProviderSource.AKSHARE_THS
            )
            return make_result(
                request,
                [
                    market_news_row(
                        "同名公司与平安银行突发事件",
                        "2026-08-24 14:30:00",
                        source.value,
                    ),
                    market_news_row(
                        (
                            "宇树科技机器人撞墙损坏"
                            if request.api_name.endswith("_em")
                            else f"{source.value} 独家"
                        ),
                        "2026-08-24 14:00:00",
                        source.value,
                    ),
                ],
                source,
            )
        if request.api_name == "stock_notice_report":
            day = str(request.params["date"])
            rows = [
                notice_row(index, day)
                for index in range(12)
            ]
            return make_result(request, rows, ProviderSource.AKSHARE_EASTMONEY)
        if request.api_name == "stock_news_em":
            return make_result(
                request,
                [
                    {
                        **market_news_row(
                            "指定股票新闻",
                            "2026-08-24 10:00:00",
                            "东方财富",
                        ),
                        "ts_code": "000001.SZ",
                        "keywords": "平安银行",
                        "source_kind": "stock_news",
                    }
                ],
                ProviderSource.AKSHARE_EASTMONEY,
            )
        if request.api_name == "stock_individual_notice_report":
            return make_result(
                request,
                [notice_row(0, "20260824")],
                ProviderSource.AKSHARE_EASTMONEY,
            )
        raise AssertionError(f"unexpected public API: {request.api_name}")


class FullyRespondingPublicNewsProvider(PublicNewsProvider):
    async def query(self, request: ProviderQuery) -> ProviderResult:
        if request.api_name == "stock_info_global_cls":
            self.requests.append(request)
            return make_result(
                request,
                [market_news_row("财联社快讯", "2026-08-24 14:10:00", "财联社")],
                ProviderSource.AKSHARE_CLS,
            )
        return await super().query(request)


def make_result(
    request: ProviderQuery,
    rows: list[dict[str, Any]],
    provider: ProviderSource,
) -> ProviderResult:
    normalized = [{**{field: row.get(field) for field in request.fields}, **row} for row in rows]
    return ProviderResult(
        api_name=request.api_name,
        provider=provider,
        fetched_at=FETCHED_AT,
        fields=request.fields,
        items=normalized,
        provider_code=0,
        response_bytes=256,
    )


def market_news_row(title: str, published_at: str, source_name: str) -> dict[str, Any]:
    safe_title = title.replace(" ", "_")
    return {
        "record_key": f"ak_news_{safe_title}",
        "title": title,
        "content": f"{title}的正文摘要",
        "published_at": published_at,
        "source_name": source_name,
        "source_url": f"https://example.com/news/{safe_title}",
        "source_kind": "market_news",
        "citable": True,
    }


def notice_row(index: int, raw_day: str) -> dict[str, Any]:
    day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:8]}"
    title = "风险提示公告" if index == 0 else f"普通公告 {index}"
    return {
        "record_key": f"ak_notice_{raw_day}_{index}",
        "security_code": f"{index:06d}",
        "ts_code": f"{index:06d}.SZ",
        "stock_name": f"公司 {index}",
        "title": title,
        "announcement_type": "风险提示" if index == 0 else "重大事项",
        "announcement_date": day,
        "source_url": f"https://example.com/notice/{raw_day}/{index}",
        "source_kind": "announcement",
        "citable": True,
    }


def build_context(
    public_provider: PublicNewsProvider,
    structured_provider: StructuredEventProvider | None = None,
) -> ResearchToolContext:
    return ResearchToolContext(
        services=build_data_services(
            structured_provider or StructuredEventProvider(),
            public_news_provider=public_provider,
        ),
        as_of=AS_OF,
        run_id=RUN_ID,
        data_store=InMemoryResearchDataStore(),
    )


def tool_by_name(registry, name: str):
    return next(tool for tool in registry.all_tools if tool.name == name)


def test_daily_event_snapshot_is_bounded_stored_and_partial_on_one_source_failure() -> None:
    async def scenario() -> None:
        public_provider = PublicNewsProvider()
        context = build_context(public_provider)
        registry = build_agent_tool_registry(context)
        result = await tool_by_name(registry, "get_daily_event_snapshot").ainvoke(
            {
                "candidate_count": 3,
                "news_lookback_hours": 24,
                "announcement_lookback_days": 3,
            }
        )

        assert result["status"] == "partial"
        assert result["context_ref"] is not None
        assert result["snapshot"]["coverage"]["successful_market_source_count"] == 2
        assert result["snapshot"]["coverage"]["optional_failure_count"] == 1
        assert result["snapshot"]["coverage"]["recent_feed_is_complete_history"] is False
        assert result["snapshot"]["coverage"]["stock_catalog_available"] is True
        assert result["snapshot"]["coverage"]["mapped_market_news_count"] == 1
        assert result["snapshot"]["coverage"]["raw_sell_side_report_count"] == 2
        assert result["snapshot"]["coverage"]["raw_broker_recommendation_count"] == 2
        assert len(result["snapshot"]["market_news"]) == 3
        assert len(result["snapshot"]["announcements"]) == 3
        assert len(result["snapshot"]["sell_side_reports"]) == 1
        assert len(result["snapshot"]["broker_recommendations"]) == 1
        mapped_news = next(
            item
            for item in result["snapshot"]["market_news"]
            if item["title"] == "同名公司与平安银行突发事件"
        )
        assert mapped_news["related_stocks"] == [
            {
                "ts_code": "000001.SZ",
                "stock_name": "平安银行",
                "matched_name": "平安银行",
                "supporting_record_keys": ["ak_news_同名公司与平安银行突发事件"],
            }
        ]
        assert all(
            related["stock_name"] != "同名公司"
            for related in mapped_news["related_stocks"]
        )
        unitree_news = next(
            item
            for item in result["snapshot"]["market_news"]
            if item["title"] == "宇树科技机器人撞墙损坏"
        )
        assert unitree_news["related_stocks"] == []
        report = result["snapshot"]["sell_side_reports"][0]
        assert report["ts_code"] == "000001.SZ"
        assert report["citable"] is True
        assert report["prediction_is_ground_truth"] is False
        assert len(report["forecast_points"]) == 2
        assert len(report["supporting_record_keys"]) == 2
        assert {
            point["source_record_key"] for point in report["forecast_points"]
        } == set(report["supporting_record_keys"])
        recommendation = result["snapshot"]["broker_recommendations"][0]
        assert recommendation["ts_code"] == "000001.SZ"
        assert recommendation["broker_count"] == 2
        assert len(recommendation["supporting_record_keys"]) == 2
        assert len(set(recommendation["supporting_record_keys"])) == 2
        assert recommendation["recommendation_is_outcome_ground_truth"] is False
        assert result["snapshot"]["announcements"][0]["selection_signals"] == ["风险提示"]
        assert result["issues"][0]["code"] == "UPSTREAM_UNAVAILABLE"

        bundle = await context.data_store.get(context.run_id, result["context_ref"])
        assert bundle.kind == "daily_event_snapshot"
        assert len(bundle.datasets) == 14
        assert sum(len(dataset.items) for dataset in bundle.datasets.values()) == 47
        assert "major_news" not in {request.api_name for request in public_provider.requests}

    asyncio.run(scenario())


def test_successful_recent_feeds_still_do_not_claim_complete_history() -> None:
    async def scenario() -> None:
        context = build_context(FullyRespondingPublicNewsProvider())
        result = await tool_by_name(
            build_agent_tool_registry(context),
            "get_daily_event_snapshot",
        ).ainvoke({"candidate_count": 3})

        assert result["status"] == "ok"
        assert result["issues"] == []
        assert result["complete"] is False
        assert result["snapshot"]["coverage"]["recent_feed_is_complete_history"] is False

    asyncio.run(scenario())


def test_event_role_uses_akshare_news_tools_and_never_calls_major_news() -> None:
    async def scenario() -> None:
        public_provider = PublicNewsProvider()
        context = build_context(public_provider)
        registry = build_agent_tool_registry(context)
        event_names = {tool.name for tool in registry.for_role(EvidenceAgentRole.EVENT)}
        assert event_names == {
            "resolve_stock_identity",
            "get_trade_calendar",
            "get_daily_event_snapshot",
            "search_market_news",
            "get_targeted_news_and_disclosures",
            "get_corporate_action_events",
            "get_sell_side_research_context",
            "get_economic_calendar",
            "get_earnings_and_disclosure",
        }

        search = await tool_by_name(registry, "search_market_news").ainvoke(
            {
                "start_at": "2026-08-24T14:00:00+08:00",
                "end_at": "2026-08-24T15:00:00+08:00",
            }
        )
        assert search["status"] == "partial"
        assert {dataset["api_name"] for dataset in search["datasets"]} == {
            "stock_info_global_em",
            "stock_info_global_ths",
        }

        targeted = await tool_by_name(
            registry,
            "get_targeted_news_and_disclosures",
        ).ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-08-20",
                "end_date": "2026-08-24",
            }
        )
        assert targeted["status"] == "ok"
        assert {dataset["api_name"] for dataset in targeted["datasets"]} == {
            "stock_news_em",
            "stock_individual_notice_report",
        }
        assert "major_news" not in {request.api_name for request in public_provider.requests}

        research = await tool_by_name(
            registry,
            "get_sell_side_research_context",
        ).ainvoke(
            {
                "ts_code": "000001.SZ",
                "start_date": "2026-07-20",
                "end_date": "2026-08-24",
            }
        )
        assert research["status"] == "ok"
        assert {dataset["api_name"] for dataset in research["datasets"]} == {
            "report_rc",
            "broker_recommend",
        }
        assert len(
            [
                dataset
                for dataset in research["datasets"]
                if dataset["api_name"] == "broker_recommend"
            ]
        ) == 2

    asyncio.run(scenario())


def test_daily_event_snapshot_fails_soft_per_report_day() -> None:
    async def scenario() -> None:
        context = build_context(
            PublicNewsProvider(),
            PartiallyFailingStructuredEventProvider(),
        )
        result = await tool_by_name(
            build_agent_tool_registry(context),
            "get_daily_event_snapshot",
        ).ainvoke(
            {
                "candidate_count": 3,
                "research_lookback_days": 3,
            }
        )

        assert result["status"] == "partial"
        coverage = result["snapshot"]["coverage"]
        assert coverage["configured_sell_side_report_day_count"] == 3
        assert coverage["successful_sell_side_report_day_count"] == 2
        assert coverage["broker_recommendation_available"] is True
        failure_labels = {issue["dataset_label"] for issue in result["issues"]}
        assert "sell_side_reports_20260823" in failure_labels
        assert result["snapshot"]["sell_side_reports"][0]["ts_code"] == "000001.SZ"

    asyncio.run(scenario())
