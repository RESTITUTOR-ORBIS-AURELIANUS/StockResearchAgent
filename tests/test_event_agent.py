"""新闻事件 Agent 的每日快照、逐行引用、定向查证和主图接入测试。"""

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_research_agent.agents.event import (
    EventAgentLimits,
    EventAgentRunSummary,
    EventCheck,
    EventResearchMode,
    build_event_agent_graph,
)
from stock_research_agent.agents.event.models import (
    DailyEventAnalysis,
    EventEvidenceDraft,
    EventReviewDecision,
    EventVerificationRequestDraft,
    TargetedEventPlan,
)
from stock_research_agent.agents.event.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.domain import ResearchRequest, ResearchTarget, TimeRange
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph import build_research_graph
from stock_research_agent.providers.models import ProviderSource
from stock_research_agent.research_data import InMemoryResearchDataStore, ResearchDataBundle
from stock_research_agent.services import build_data_services
from stock_research_agent.services.daily_event_snapshot import (
    event_broker_recommendation_record_key,
    event_report_record_key,
)
from stock_research_agent.services.models import (
    ServiceDataset,
    ServiceItemTrace,
    ServicePageTrace,
)
from stock_research_agent.tools import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool
from stock_research_agent.tools.models import (
    CorporateActionInput,
    DailyEventSnapshotInput,
    StockDateRangeToolInput,
    StockIdentityInput,
    StockPeriodInput,
    TargetedNewsDisclosureInput,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 40, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_A_SHARE_event0001"
MARKET_TARGET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
OTHER_STOCK = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


class NoopProvider:
    async def query(self, request):  # pragma: no cover - fake Tools bypass provider
        raise AssertionError(f"unexpected provider call: {request.api_name}")


class SellSideInput(StockDateRangeToolInput):
    pass


class ScriptedEventModel:
    def __init__(
        self,
        *,
        daily: DailyEventAnalysis | None = None,
        targeted: TargetedEventPlan | None = None,
        reviews: tuple[EventReviewDecision, ...] = (),
    ) -> None:
        self.daily = daily
        self.targeted = targeted
        self.reviews = list(reviews)
        self.daily_inputs: list[Any] = []
        self.targeted_inputs: list[Any] = []

    async def analyze_daily(self, request):
        self.daily_inputs.append(request)
        assert request.snapshot_result["snapshot"]["market_news"]
        assert self.daily is not None
        return self.daily

    async def plan_targeted(self, request):
        self.targeted_inputs.append(request)
        assert self.targeted is not None
        return self.targeted

    async def review_verification(self, request):
        assert request.observations
        return self.reviews.pop(0)


def test_daily_news_can_form_direct_stock_evidence_with_row_level_source() -> None:
    async def scenario() -> None:
        context, tools, calls = await _fake_runtime()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="一条明确点名上市公司的突发新闻已记录。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=ResearchTarget(
                            type=TargetType.STOCK,
                            code=STOCK_TARGET.code,
                            name="模型自由写的别名",
                        ),
                        title="媒体报道平安银行发生示例事件",
                        description="东方财富在冻结时间前报道平安银行发生示例事件。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=("news:stock:1",),
                        limitations=("这是媒体报道事实，不等同于事件全部细节已证实",),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )

        assert calls == ["get_daily_event_snapshot"]
        assert len(model.daily_inputs) == 1
        evidence = result["evidence_records"][0]
        assert evidence.target == STOCK_TARGET
        assert evidence.domain is EvidenceDomain.EVENT
        assert evidence.verification_status is VerificationStatus.VERIFIED
        assert evidence.source_refs[0].record_key == "news:stock:1"
        assert evidence.source_refs[0].provider == "AKSHARE_EASTMONEY"
        assert evidence.source_refs[0].url == "https://example.test/news/1"
        assert evidence.raw_payload_ref is not None

    asyncio.run(scenario())


def test_unlisted_company_news_cannot_be_mapped_to_concept_stock() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="未上市公司新闻不能映射 A 股。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=OTHER_STOCK,
                        title="错误映射的机器人概念股证据",
                        description="宇树科技新闻不能自动变成万科A事件。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=("news:unlisted:1",),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )
        assert result["evidence_records"] == []
        assert any("未授权标的" in message for message in result["errors"])

    asyncio.run(scenario())


def test_citable_false_raw_row_is_hard_rejected_even_if_group_says_citable() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="不可引用来源不能独立成证据。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=STOCK_TARGET,
                        title="不可引用新闻",
                        description="该行没有可回溯 URL。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=("news:false:1",),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )
        assert result["evidence_records"] == []
        assert any("record_key" in message for message in result["errors"])

    asyncio.run(scenario())


def test_grouped_news_stock_permission_applies_only_to_supporting_raw_row() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="聚合新闻内只有一条原始行真正点名上市公司。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=STOCK_TARGET,
                        title="错误引用未点名公司的聚合来源",
                        description="该原始行不能支持具体股票证据。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=("news:group:other",),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )
        assert result["evidence_records"] == []
        assert any("record_key" in message for message in result["errors"])

    asyncio.run(scenario())


def test_grouped_report_fact_rejects_incomplete_constituent_citation() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        report_keys = _report_record_keys()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="聚合研报事实必须引用所有组成行。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=STOCK_TARGET,
                        title="研报包含两个预测期",
                        description="同一份研报包含两个预测期，但这里只引用了一行。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=(report_keys[0],),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )

        assert result["evidence_records"] == []
        assert any("record_key" in message for message in result["errors"])

    asyncio.run(scenario())


def test_grouped_report_fact_maps_every_constituent_to_its_raw_trace() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        report_keys = _report_record_keys()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="聚合研报事实逐行可回溯。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=STOCK_TARGET,
                        title="研报包含两个预测期",
                        description="同一机构同一作者的研报包含两个独立预测期。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=report_keys,
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )

        evidence = result["evidence_records"][0]
        refs = {ref.record_key: ref.provider for ref in evidence.source_refs}
        assert refs == {
            report_keys[0]: ProviderSource.PRIMARY.value,
            report_keys[1]: ProviderSource.BACKUP.value,
        }

    asyncio.run(scenario())


def test_grouped_broker_recommendation_requires_every_broker_row() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime()
        broker_keys = _broker_record_keys()
        model = ScriptedEventModel(
            daily=DailyEventAnalysis(
                market_summary="多家券商推荐是多行聚合事实。",
                snapshot_evidence=(
                    EventEvidenceDraft(
                        target=STOCK_TARGET,
                        title="两家券商将该股列入月度名单",
                        description="该聚合描述不能只引用其中一家券商的原始行。",
                        source_call_ids=("ec_daily_snapshot_1",),
                        source_record_keys=(broker_keys[0],),
                    ),
                ),
            )
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.DAILY,
            }
        )

        assert result["evidence_records"] == []
        assert any("record_key" in message for message in result["errors"])

    asyncio.run(scenario())


def test_verification_mode_resolves_identity_then_calls_news_and_research() -> None:
    async def scenario() -> None:
        context, tools, calls = await _fake_runtime()
        request = _research_request()
        plan_request = EventVerificationRequestDraft(
            target=STOCK_TARGET,
            question="突发消息是否有公告和卖方研究可以交叉核对？",
            requested_evidence="读取同一股票的新闻公告及研报元数据。",
            checks=(EventCheck.NEWS_DISCLOSURES, EventCheck.SELL_SIDE_RESEARCH),
            lookback_days=14,
            reason="需要区分公司披露与第三方判断。",
        )
        model = ScriptedEventModel(
            targeted=TargetedEventPlan(
                planning_summary="先核对身份，再查新闻公告和卖方研究。",
                verification_requests=(plan_request,),
            ),
            reviews=(
                EventReviewDecision(
                    review_summary="公告行可以形成来源事实。",
                    evidence=(
                        EventEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行发布风险提示公告",
                            description="公司在查询窗口内发布风险提示公告。",
                            source_call_ids=("ec_r1_1_news_disclosures",),
                            source_record_keys=("notice:000001:1",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.VERIFICATION,
                "research_request": request,
            }
        )

        assert calls == [
            "resolve_stock_identity",
            "get_targeted_news_and_disclosures",
            "get_sell_side_research_context",
        ]
        assert "get_daily_event_snapshot" not in calls
        assert result["completed_research_request"].status is ResearchRequestStatus.COMPLETED
        assert result["evidence_records"][0].source_refs[0].record_key == "notice:000001:1"

    asyncio.run(scenario())


def test_identity_mismatch_stops_all_event_queries() -> None:
    async def scenario() -> None:
        context, tools, calls = await _fake_runtime(identity_code=OTHER_STOCK.code)
        model = ScriptedEventModel(
            targeted=TargetedEventPlan(
                planning_summary="核对个股新闻。",
                verification_requests=(
                    EventVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="是否有相关公告？",
                        requested_evidence="读取公告。",
                        checks=(EventCheck.NEWS_DISCLOSURES,),
                        reason="验证身份闸门。",
                    ),
                ),
            ),
            reviews=(EventReviewDecision(review_summary="身份不匹配。"),),
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.VERIFICATION,
                "research_request": _research_request(),
            }
        )
        assert calls == ["resolve_stock_identity"]
        assert result["evidence_records"] == []
        assert any("身份核对失败" in message for message in result["errors"])

    asyncio.run(scenario())


def test_failed_shared_call_cannot_push_actual_invocations_past_hard_budget() -> None:
    async def scenario() -> None:
        context, tools, calls = await _fake_runtime(news_failure=True)
        model = ScriptedEventModel(
            targeted=TargetedEventPlan(
                planning_summary="两个问题复用同一新闻查询。",
                verification_requests=(
                    EventVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="新闻与研报是否相互印证？",
                        requested_evidence="同时查询新闻和卖方研究。",
                        checks=(
                            EventCheck.NEWS_DISCLOSURES,
                            EventCheck.SELL_SIDE_RESEARCH,
                        ),
                        reason="第一项查证。",
                    ),
                    EventVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="公告是否确认新闻？",
                        requested_evidence="再次核对相同新闻公告窗口。",
                        checks=(EventCheck.NEWS_DISCLOSURES,),
                        reason="第二项查证，共享但失败的调用不能免费重试。",
                    ),
                ),
            ),
            reviews=(EventReviewDecision(review_summary="预算内完成可执行部分。"),),
        )
        graph = build_event_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
            limits=EventAgentLimits(max_total_tool_calls=3),
        )
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.VERIFICATION,
                "research_request": _research_request(),
            }
        )

        assert calls == [
            "resolve_stock_identity",
            "get_targeted_news_and_disclosures",
            "get_sell_side_research_context",
        ]
        assert result["run_summary"].tool_call_count == 3
        assert result["run_summary"].budget_exhausted is True
        assert len(result["run_summary"].skipped_task_ids) == 1

    asyncio.run(scenario())


def test_partial_tool_result_automatically_marks_evidence_incomplete() -> None:
    async def scenario() -> None:
        context, tools, _ = await _fake_runtime(news_partial=True)
        model = ScriptedEventModel(
            targeted=TargetedEventPlan(
                planning_summary="核对近期新闻和公告。",
                verification_requests=(
                    EventVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="公告是否确认事件？",
                        requested_evidence="读取新闻和公告。",
                        checks=(EventCheck.NEWS_DISCLOSURES,),
                        reason="需要原始公告索引。",
                    ),
                ),
            ),
            reviews=(
                EventReviewDecision(
                    review_summary="成功部分包含一条公告。",
                    evidence=(
                        EventEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行发布风险提示公告",
                            description="公告索引显示该公司发布风险提示公告。",
                            source_call_ids=("ec_r1_1_news_disclosures",),
                            source_record_keys=("notice:000001:1",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_event_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": EventResearchMode.VERIFICATION,
                "research_request": _research_request(),
            }
        )

        evidence = result["evidence_records"][0]
        assert evidence.verification_status is VerificationStatus.UNVERIFIED
        assert "仅返回 partial" in evidence.description
        assert "缺失数据集：stock_news" in evidence.description
        assert "未声明结果完整" in evidence.description

    asyncio.run(scenario())


def test_main_graph_runs_event_stage_after_other_configured_stages() -> None:
    stages: list[str] = []

    class StubEventGraph:
        async def ainvoke(self, state):
            stages.append("event")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": EventAgentRunSummary(
                    mode=EventResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    graph = build_research_graph(event_agent_graph_factory=lambda **_: StubEventGraph())
    result = asyncio.run(graph.ainvoke({"target": MARKET_TARGET, "as_of": AS_OF}))
    assert stages == ["event"]
    assert result["event_run_summary"].mode is EventResearchMode.DAILY
    assert result["event_request_count"] == 0


def test_main_graph_orders_event_after_fundamental_stage() -> None:
    stages: list[str] = []

    class StubGraph:
        def __init__(self, label: str) -> None:
            self.label = label

        async def ainvoke(self, state):
            stages.append(self.label)
            return {"evidence_records": [], "errors": []}

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: StubGraph("technical"),
        sentiment_flow_agent_graph_factory=lambda **_: StubGraph("sentiment_flow"),
        fundamental_agent_graph_factory=lambda **_: StubGraph("fundamental"),
        event_agent_graph_factory=lambda **_: StubGraph("event"),
    )
    asyncio.run(graph.ainvoke({"target": MARKET_TARGET, "as_of": AS_OF}))
    assert stages == ["technical", "sentiment_flow", "fundamental", "event"]


def test_prompts_define_schema_few_shot_and_event_safety_boundaries() -> None:
    assert "source_record_keys" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "Few-shot" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "宇树科技机器人撞墙损坏" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "citable=false" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "不是公司未来业绩" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "resolve_stock_identity" in TARGETED_PLANNING_SYSTEM_PROMPT
    assert "逐行 source_record_keys" in VERIFICATION_REVIEW_SYSTEM_PROMPT
    assert "unresolved_questions" in VERIFICATION_REVIEW_SYSTEM_PROMPT


def test_news_disclosure_request_rejects_windows_over_31_days() -> None:
    with pytest.raises(ValidationError, match="NEWS_DISCLOSURES 单次查证窗口不能超过 31 天"):
        EventVerificationRequestDraft(
            target=STOCK_TARGET,
            question="过去一年是否有相关新闻或公告？",
            requested_evidence="读取个股新闻与公告索引。",
            checks=(EventCheck.NEWS_DISCLOSURES,),
            lookback_days=365,
            reason="验证 Tool 窗口约束在 Schema 层提前生效。",
        )


def test_event_request_rejects_announcement_categories_the_tool_cannot_accept() -> None:
    with pytest.raises(ValidationError):
        EventVerificationRequestDraft(
            target=STOCK_TARGET,
            question="是否有相关公告？",
            requested_evidence="读取指定类别的公告索引。",
            checks=(EventCheck.NEWS_DISCLOSURES,),
            announcement_category="模型自行发明的类别",
            reason="验证 Agent 输出 Schema 与 Tool 输入枚举一致。",
        )


def _research_request() -> ResearchRequest:
    return ResearchRequest(
        request_id="rq_event_001",
        run_id=RUN_ID,
        thesis_id="th_event_candidate_001",
        target=STOCK_TARGET,
        assigned_domain=EvidenceDomain.EVENT,
        question="该股票近期事件是否有可靠来源支持？",
        requested_evidence="核对新闻、公告和卖方研究元数据。",
        time_range=TimeRange(start=date(2026, 8, 1), end=AS_OF.date()),
        priority=ResearchPriority.HIGH,
        status=ResearchRequestStatus.PENDING,
        requested_by="ThesisValidationAnalyst",
        created_at=AS_OF,
    )


async def _fake_runtime(
    *,
    identity_code: str = STOCK_TARGET.code,
    news_failure: bool = False,
    news_partial: bool = False,
) -> tuple[ResearchToolContext, tuple[Any, ...], list[str]]:
    data_store = InMemoryResearchDataStore()
    context = ResearchToolContext(
        services=build_data_services(NoopProvider()),
        as_of=AS_OF,
        run_id=RUN_ID,
        data_store=data_store,
    )
    calls: list[str] = []

    async def daily_snapshot(
        candidate_count: int = 10,
        news_lookback_hours: int = 24,
        announcement_lookback_days: int = 3,
        research_lookback_days: int = 7,
    ) -> dict[str, object]:
        calls.append("get_daily_event_snapshot")
        dataset = _news_dataset()
        report_dataset = _sell_side_report_dataset()
        broker_dataset = _broker_recommendation_dataset()
        context_ref = await data_store.put(
            RUN_ID,
            ResearchDataBundle(
                kind="daily_event_snapshot",
                tool_name="get_daily_event_snapshot",
                as_of=AS_OF,
                datasets={
                    "market_news_eastmoney": dataset,
                    "sell_side_reports_20260820": report_dataset,
                    "broker_recommendations_202608": broker_dataset,
                },
                metadata={"candidate_count": candidate_count},
            ),
        )
        return {
            "tool_name": "get_daily_event_snapshot",
            "status": "ok",
            "as_of": AS_OF.isoformat(),
            "context_ref": context_ref,
            "snapshot": {
                "as_of": AS_OF.isoformat(),
                "market_news": [
                    {
                        "title": "平安银行发生示例事件",
                        "summary": "一条明确点名上市公司的新闻。",
                        "published_at": "2026-08-20T15:30:00+08:00",
                        "source_names": ["东方财富"],
                        "source_urls": ["https://example.test/news/1"],
                        "source_dataset_labels": ["market_news_eastmoney"],
                        "record_keys": ["news:stock:1", "news:group:other"],
                        "citable": True,
                        "related_stocks": [
                            {
                                "ts_code": STOCK_TARGET.code,
                                "stock_name": STOCK_TARGET.name,
                                "supporting_record_keys": ["news:stock:1"],
                            }
                        ],
                    },
                    {
                        "title": "宇树科技机器人撞墙损坏",
                        "summary": "未上市公司事件。",
                        "published_at": "2026-08-20T15:20:00+08:00",
                        "source_names": ["东方财富"],
                        "source_urls": ["https://example.test/news/2"],
                        "source_dataset_labels": ["market_news_eastmoney"],
                        "record_keys": ["news:unlisted:1"],
                        "citable": True,
                        "related_stocks": [],
                    },
                    {
                        "title": "不可引用测试行",
                        "summary": "没有原文链接。",
                        "published_at": "2026-08-20T15:10:00+08:00",
                        "source_names": ["财联社"],
                        "source_urls": ["https://example.test/group-url"],
                        "source_dataset_labels": ["market_news_eastmoney"],
                        "record_keys": ["news:false:1"],
                        "citable": True,
                        "related_stocks": [
                            {
                                "ts_code": STOCK_TARGET.code,
                                "stock_name": STOCK_TARGET.name,
                                "supporting_record_keys": ["news:false:1"],
                            }
                        ],
                    },
                ],
                "announcements": [],
                "sell_side_reports": [_sell_side_report_candidate()],
                "broker_recommendations": [_broker_recommendation_candidate()],
                "coverage": {"recent_feed_is_complete_history": False},
            },
            "issues": [],
            "source_dataset_count": 1,
            "total_stored_items": 4,
            "complete": True,
        }

    async def identity(ts_code: str, list_status: str = "L") -> dict[str, object]:
        calls.append("resolve_stock_identity")
        return _tool_result(
            "resolve_stock_identity",
            "stock_basic",
            identity_code,
            record_key="identity:1",
            name=(STOCK_TARGET.name if identity_code == STOCK_TARGET.code else OTHER_STOCK.name),
        )

    async def news(
        ts_code: str,
        start_date: date,
        end_date: date,
        announcement_category: str = "全部",
    ) -> dict[str, object]:
        calls.append("get_targeted_news_and_disclosures")
        if news_failure:
            return {
                "tool_name": "get_targeted_news_and_disclosures",
                "status": "error",
                "issues": [{"code": "UPSTREAM_UNAVAILABLE", "message": "模拟失败"}],
                "complete": False,
            }
        result = _tool_result(
            "get_targeted_news_and_disclosures",
            "stock_announcements",
            ts_code,
            record_key="notice:000001:1",
            title="风险提示公告",
            announcement_date=end_date.isoformat(),
            source_url="https://example.test/notice/1",
            citable=True,
        )
        if news_partial:
            result["status"] = "partial"
            result["complete"] = False
            result["issues"] = [
                {
                    "code": "UPSTREAM_UNAVAILABLE",
                    "dataset_label": "stock_news",
                    "message": "模拟新闻源失败",
                }
            ]
        return result

    async def sell_side(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        calls.append("get_sell_side_research_context")
        return _tool_result(
            "get_sell_side_research_context",
            "sell_side_reports",
            ts_code,
            record_key="report:000001:1",
            report_title="示例研报",
            report_date=end_date.isoformat(),
            org_name="示例券商",
        )

    async def corporate(ts_code: str, start_date: date, end_date: date):
        calls.append("get_corporate_action_events")
        return _tool_result(
            "get_corporate_action_events",
            "repurchase_events",
            ts_code,
            record_key="action:1",
        )

    async def earnings(ts_code: str, period: str):
        calls.append("get_earnings_and_disclosure")
        return _tool_result(
            "get_earnings_and_disclosure",
            "earnings_forecast",
            ts_code,
            record_key="earnings:1",
            end_date=period,
        )

    tools = (
        create_structured_tool(
            name="get_daily_event_snapshot",
            description="fake daily event snapshot",
            args_schema=DailyEventSnapshotInput,
            coroutine=daily_snapshot,
        ),
        create_structured_tool(
            name="resolve_stock_identity",
            description="fake stock identity",
            args_schema=StockIdentityInput,
            coroutine=identity,
        ),
        create_structured_tool(
            name="get_targeted_news_and_disclosures",
            description="fake targeted news",
            args_schema=TargetedNewsDisclosureInput,
            coroutine=news,
        ),
        create_structured_tool(
            name="get_sell_side_research_context",
            description="fake sell-side research",
            args_schema=SellSideInput,
            coroutine=sell_side,
        ),
        create_structured_tool(
            name="get_corporate_action_events",
            description="fake corporate actions",
            args_schema=CorporateActionInput,
            coroutine=corporate,
        ),
        create_structured_tool(
            name="get_earnings_and_disclosure",
            description="fake earnings disclosure",
            args_schema=StockPeriodInput,
            coroutine=earnings,
        ),
    )
    return context, tools, calls


def _news_dataset() -> ServiceDataset:
    rows = [
        {
            "record_key": "news:stock:1",
            "title": "平安银行发生示例事件",
            "content": "一条明确点名上市公司的新闻。",
            "published_at": "2026-08-20T15:30:00+08:00",
            "source_url": "https://example.test/news/1",
            "citable": True,
        },
        {
            "record_key": "news:group:other",
            "title": "同一事件的另一条聚合快讯",
            "content": "这条原始行没有出现上市公司全名。",
            "published_at": "2026-08-20T15:31:00+08:00",
            "source_url": "https://example.test/news/group-other",
            "citable": True,
        },
        {
            "record_key": "news:unlisted:1",
            "title": "宇树科技机器人撞墙损坏",
            "content": "未上市公司事件。",
            "published_at": "2026-08-20T15:20:00+08:00",
            "source_url": "https://example.test/news/2",
            "citable": True,
        },
        {
            "record_key": "news:false:1",
            "title": "不可引用测试行",
            "content": "没有原文链接。",
            "published_at": "2026-08-20T15:10:00+08:00",
            "source_url": None,
            "citable": False,
        },
    ]
    fields = tuple(rows[0])
    traces = tuple(
        ServiceItemTrace(
            page_index=0,
            source_offset=index,
            provider=ProviderSource.AKSHARE_EASTMONEY,
            from_cache=False,
            fetched_at=FETCHED_AT,
        )
        for index in range(len(rows))
    )
    return ServiceDataset(
        api_name="stock_info_global_em",
        query_params={},
        requested_fields=fields,
        items=rows,
        item_traces=traces,
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.AKSHARE_EASTMONEY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=len(rows),
                returned_fields=fields,
                response_bytes=512,
            ),
        ),
        as_of=AS_OF.date(),
        data_as_of=AS_OF.date(),
        received_item_count=len(rows),
        discarded_item_count=0,
        complete=True,
    )


def _sell_side_report_rows() -> list[dict[str, Any]]:
    common = {
        "ts_code": STOCK_TARGET.code,
        "name": STOCK_TARGET.name,
        "report_date": AS_OF.date().strftime("%Y%m%d"),
        "report_title": "净息差与资产质量跟踪",
        "report_type": "公司",
        "classify": "点评",
        "org_name": "示例证券",
        "author_name": "示例研究员",
        "rating": "增持",
        "citable": True,
    }
    return [
        {**common, "quarter": "2026Q4", "eps": 2.0, "pe": 6.0},
        {**common, "quarter": "2027Q4", "eps": 2.2, "pe": 5.5},
    ]


def _report_record_keys() -> tuple[str, str]:
    keys = tuple(event_report_record_key(row) for row in _sell_side_report_rows())
    assert all(key is not None for key in keys)
    return keys  # type: ignore[return-value]


def _sell_side_report_candidate() -> dict[str, Any]:
    rows = _sell_side_report_rows()
    keys = _report_record_keys()
    return {
        "ts_code": STOCK_TARGET.code,
        "stock_name": STOCK_TARGET.name,
        "report_date": AS_OF.date().isoformat(),
        "report_title": "净息差与资产质量跟踪",
        "report_type": "公司",
        "classify": "点评",
        "org_name": "示例证券",
        "author_name": "示例研究员",
        "rating": "增持",
        "forecast_points": [
            {
                "quarter": row["quarter"],
                "eps": row["eps"],
                "pe": row["pe"],
                "source_record_key": key,
            }
            for row, key in zip(rows, keys, strict=True)
        ],
        "source_dataset_labels": ["sell_side_reports_20260820"],
        "supporting_record_keys": list(keys),
        "citable": True,
    }


def _sell_side_report_dataset() -> ServiceDataset:
    rows = _sell_side_report_rows()
    fields = tuple(rows[0])
    providers = (ProviderSource.PRIMARY, ProviderSource.BACKUP)
    traces = tuple(
        ServiceItemTrace(
            page_index=0,
            source_offset=index,
            provider=provider,
            from_cache=False,
            fetched_at=FETCHED_AT,
        )
        for index, provider in enumerate(providers)
    )
    return ServiceDataset(
        api_name="report_rc",
        query_params={"report_date": AS_OF.date().strftime("%Y%m%d")},
        requested_fields=fields,
        items=rows,
        item_traces=traces,
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=len(rows),
                returned_fields=fields,
                response_bytes=512,
            ),
        ),
        as_of=AS_OF.date(),
        data_as_of=AS_OF.date(),
        received_item_count=len(rows),
        discarded_item_count=0,
        complete=True,
    )


def _broker_recommendation_rows() -> list[dict[str, Any]]:
    common = {
        "month": AS_OF.strftime("%Y%m"),
        "ts_code": STOCK_TARGET.code,
        "name": STOCK_TARGET.name,
        "citable": True,
    }
    return [{**common, "broker": "甲证券"}, {**common, "broker": "乙证券"}]


def _broker_record_keys() -> tuple[str, str]:
    first, second = (
        event_broker_recommendation_record_key(row)
        for row in _broker_recommendation_rows()
    )
    assert first is not None and second is not None
    return first, second


def _broker_recommendation_candidate() -> dict[str, Any]:
    keys = _broker_record_keys()
    return {
        "ts_code": STOCK_TARGET.code,
        "stock_name": STOCK_TARGET.name,
        "month": AS_OF.strftime("%Y%m"),
        "brokers": ["甲证券", "乙证券"],
        "broker_count": 2,
        "source_dataset_labels": ["broker_recommendations_202608"],
        "supporting_record_keys": list(keys),
        "citable": True,
    }


def _broker_recommendation_dataset() -> ServiceDataset:
    rows = _broker_recommendation_rows()
    fields = tuple(rows[0])
    providers = (ProviderSource.PRIMARY, ProviderSource.BACKUP)
    traces = tuple(
        ServiceItemTrace(
            page_index=0,
            source_offset=index,
            provider=provider,
            from_cache=False,
            fetched_at=FETCHED_AT,
        )
        for index, provider in enumerate(providers)
    )
    return ServiceDataset(
        api_name="broker_recommend",
        query_params={"month": AS_OF.strftime("%Y%m")},
        requested_fields=fields,
        items=rows,
        item_traces=traces,
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=len(rows),
                returned_fields=fields,
                response_bytes=256,
            ),
        ),
        as_of=AS_OF.date(),
        data_as_of=AS_OF.date(),
        received_item_count=len(rows),
        discarded_item_count=0,
        complete=True,
    )


def _tool_result(
    tool_name: str,
    label: str,
    ts_code: str,
    **values: object,
) -> dict[str, object]:
    data = {"ts_code": ts_code, **values}
    return {
        "tool_name": tool_name,
        "status": "ok",
        "as_of": AS_OF.isoformat(),
        "datasets": [
            {
                "label": label,
                "api_name": label,
                "query_params": {"ts_code": ts_code},
                "requested_fields": list(data),
                "rows": [
                    {
                        "data": data,
                        "source": {
                            "provider": "PRIMARY",
                            "from_cache": False,
                            "fetched_at": FETCHED_AT.isoformat(),
                            "page_index": 0,
                            "source_offset": 0,
                        },
                    }
                ],
                "received_item_count": 1,
                "returned_item_count": 1,
                "discarded_item_count": 0,
                "data_as_of": AS_OF.date().isoformat(),
                "complete": True,
                "source_summary": [],
            }
        ],
        "issues": [],
        "total_returned_items": 1,
        "complete": True,
    }
