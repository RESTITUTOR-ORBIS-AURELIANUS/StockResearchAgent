"""情绪资金 Agent 每日模式、查证模式和主图接入的离线测试。"""

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.agents.sentiment_flow import (
    SentimentFlowAgentLimits,
    SentimentFlowResearchMode,
    build_sentiment_flow_agent_graph,
)
from stock_research_agent.agents.sentiment_flow.models import (
    DailySentimentFlowAnalysis,
    SentimentFlowAgentRunSummary,
    SentimentFlowCheck,
    SentimentFlowEvidenceDraft,
    SentimentFlowReviewDecision,
    SentimentFlowToolObservation,
    SentimentFlowVerificationRequestDraft,
    TargetedSentimentFlowPlan,
)
from stock_research_agent.agents.sentiment_flow.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.agents.sentiment_flow.subgraph import (
    _uncitable_observation_error as _sentiment_observation_error,
)
from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
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
from stock_research_agent.services.models import (
    ServiceDataset,
    ServiceItemTrace,
    ServicePageTrace,
)
from stock_research_agent.tools import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool
from stock_research_agent.tools.models import (
    CapitalFlowContextInput,
    DailySentimentFlowSnapshotInput,
    StockDateRangeToolInput,
    UnusualTradingInput,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 10, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_A_SHARE_bbbbbbbb"
MARKET_TARGET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
OTHER_STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


def test_default_sentiment_flow_limits_allow_deeper_tool_research() -> None:
    limits = SentimentFlowAgentLimits()

    assert limits.max_verification_rounds == 2
    assert limits.max_total_tool_calls == 24


class NoopProvider:
    async def query(self, request):  # pragma: no cover - fake Tools bypass provider
        raise AssertionError(f"unexpected provider call: {request.api_name}")


class ScriptedSentimentFlowModel:
    def __init__(
        self,
        *,
        daily: DailySentimentFlowAnalysis | None = None,
        targeted: TargetedSentimentFlowPlan | None = None,
        reviews: tuple[SentimentFlowReviewDecision, ...] = (),
    ) -> None:
        self.daily = daily
        self.targeted = targeted
        self.reviews = list(reviews)
        self.daily_calls = 0
        self.targeted_calls = 0
        self.review_calls = 0

    async def analyze_daily(self, request):
        self.daily_calls += 1
        assert request.snapshot_result["snapshot"]["trade_date"] == "2026-08-20"
        assert self.daily is not None
        return self.daily

    async def plan_targeted(self, request):
        self.targeted_calls += 1
        assert request.research_request.question
        assert self.targeted is not None
        return self.targeted

    async def review_verification(self, request):
        self.review_calls += 1
        assert request.observations
        return self.reviews.pop(0)


def test_sentiment_observation_semantics_distinguish_empty_partial_and_error() -> None:
    for status in ("ok", "partial", "empty"):
        observation = SentimentFlowToolObservation(
            call_id="sfc_r1_1_test",
            tool_name="get_stock_active_money_flow_context",
            arguments={"ts_code": STOCK_TARGET.code},
            result={"status": status},
        )
        assert _sentiment_observation_error(observation, target_code=STOCK_TARGET.code) is None

    failed = SentimentFlowToolObservation(
        call_id="sfc_r1_1_test_error",
        tool_name="get_stock_active_money_flow_context",
        arguments={"ts_code": STOCK_TARGET.code},
        result={"status": "too_large"},
    )
    message = _sentiment_observation_error(failed, target_code=STOCK_TARGET.code)
    assert message is not None
    assert "status=too_large" in message


def test_daily_mode_builds_snapshot_evidence_and_targeted_money_flow_evidence() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        model = ScriptedSentimentFlowModel(
            daily=DailySentimentFlowAnalysis(
                market_summary="北向资金当日净流入，平安银行进入个股资金候选。",
                snapshot_evidence=(
                    SentimentFlowEvidenceDraft(
                        target=MARKET_TARGET,
                        title="北向资金当日净流入",
                        description="2026-08-20，north_money 为 42.6，方向为净流入。",
                        source_call_ids=("sfc_daily_snapshot_1",),
                        tags=("北向资金",),
                    ),
                ),
                verification_requests=(_active_flow_request(STOCK_TARGET),),
            ),
            reviews=(
                SentimentFlowReviewDecision(
                    review_summary="THS 和 DC 两种口径的区间资金流已取得。",
                    evidence=(
                        SentimentFlowEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行两种口径的区间资金方向一致",
                            description=(
                                "2026-08-01 至 2026-08-20，THS 与 DC 返回的区间记录"
                                "均呈净流入方向，两套口径分别保留。"
                            ),
                            source_call_ids=("sfc_r1_1_active_money_flow",),
                            tags=("个股资金流", "跨口径一致"),
                            limitations=("THS 与 DC 不可相加或平均",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": SentimentFlowResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_sentiment_flow_snapshot",
            "get_stock_active_money_flow_context",
        ]
        assert [record.title for record in result["evidence_records"]] == [
            "北向资金当日净流入",
            "平安银行两种口径的区间资金方向一致",
        ]
        assert all(
            record.domain is EvidenceDomain.SENTIMENT_FLOW
            and record.verification_status is VerificationStatus.VERIFIED
            for record in result["evidence_records"]
        )
        assert result["evidence_records"][0].raw_payload_ref is not None
        assert result["evidence_records"][1].raw_payload_ref is None
        assert {source.interface for source in result["evidence_records"][1].source_refs} == {
            "get_stock_active_money_flow_context:moneyflow_ths",
            "get_stock_active_money_flow_context:moneyflow_dc",
        }
        assert result["run_summary"].verification_rounds == 1
        assert result["run_summary"].tool_call_count == 2
        assert model.daily_calls == 1
        assert model.targeted_calls == 0
        assert model.review_calls == 1

    asyncio.run(scenario())


def test_verification_mode_skips_daily_snapshot_and_completes_request() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        request = _research_request("rq_sentiment_001")
        model = ScriptedSentimentFlowModel(
            targeted=TargetedSentimentFlowPlan(
                planning_summary="查看两种口径的区间资金流即可。",
                verification_requests=(_active_flow_request(STOCK_TARGET),),
            ),
            reviews=(
                SentimentFlowReviewDecision(
                    review_summary="区间资金流已取得。",
                    evidence=(
                        SentimentFlowEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行区间资金流记录已取得",
                            description="THS 和 DC 口径的记录分别可追溯。",
                            source_call_ids=("sfc_r1_1_active_money_flow",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": SentimentFlowResearchMode.VERIFICATION,
                "research_request": request,
            }
        )

        assert calls == ["get_stock_active_money_flow_context"]
        assert "get_daily_sentiment_flow_snapshot" not in calls
        assert result["completed_research_request"].status is ResearchRequestStatus.COMPLETED
        assert result["completed_research_request"].result_evidence_ids == [
            result["evidence_records"][0].evidence_id
        ]
        assert model.daily_calls == 0
        assert model.targeted_calls == 1

    asyncio.run(scenario())


def test_targeted_result_cannot_be_written_to_another_stock() -> None:
    async def scenario() -> None:
        context, tools, _ = await fake_tool_runtime()
        model = ScriptedSentimentFlowModel(
            daily=DailySentimentFlowAnalysis(
                market_summary="平安银行进入异常候选。",
                verification_requests=(_active_flow_request(STOCK_TARGET),),
            ),
            reviews=(
                SentimentFlowReviewDecision(
                    review_summary="模型错误地把结果写给了另一只股票。",
                    evidence=(
                        SentimentFlowEvidenceDraft(
                            target=OTHER_STOCK_TARGET,
                            title="错误标的的资金证据",
                            description="这条证据应被程序硬校验拒绝。",
                            source_call_ids=("sfc_r1_1_active_money_flow",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": SentimentFlowResearchMode.DAILY,
            }
        )

        assert result["evidence_records"] == []
        assert any("错误标的" in message for message in result["errors"])

    asyncio.run(scenario())


def test_upstream_rows_for_another_stock_cannot_support_target_evidence() -> None:
    async def scenario() -> None:
        context, tools, _ = await fake_tool_runtime(
            active_money_flow_returned_ts_code=OTHER_STOCK_TARGET.code
        )
        model = ScriptedSentimentFlowModel(
            daily=DailySentimentFlowAnalysis(
                market_summary="平安银行进入资金异常候选。",
                verification_requests=(_active_flow_request(STOCK_TARGET),),
            ),
            reviews=(
                SentimentFlowReviewDecision(
                    review_summary="上游实际返回了另一只股票，证据必须被程序拒绝。",
                    evidence=(
                        SentimentFlowEvidenceDraft(
                            target=STOCK_TARGET,
                            title="错误上游行被写入平安银行",
                            description="请求参数正确，但返回行的 ts_code 不正确。",
                            source_call_ids=("sfc_r1_1_active_money_flow",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": SentimentFlowResearchMode.DAILY,
            }
        )

        assert result["evidence_records"] == []
        assert any("错误标的" in message for message in result["errors"])

    asyncio.run(scenario())


def test_daily_verification_uses_snapshot_trade_date_on_weekend() -> None:
    async def scenario() -> None:
        weekend_as_of = datetime(2026, 8, 22, 16, 0, tzinfo=SHANGHAI)
        argument_calls: list[tuple[str, dict[str, object]]] = []
        context, tools, calls = await fake_tool_runtime(
            as_of=weekend_as_of,
            argument_calls=argument_calls,
        )
        bad_event_request = SentimentFlowVerificationRequestDraft(
            target=STOCK_TARGET,
            question="周末日期是否存在龙虎榜记录？",
            requested_evidence="查询每日快照交易日的龙虎榜记录。",
            checks=(SentimentFlowCheck.UNUSUAL_TRADING,),
            lookback_days=30,
            event_trade_date=weekend_as_of.date(),
            priority=ResearchPriority.MEDIUM,
            reason="用于验证每日模式的交易日硬边界。",
        )
        model = ScriptedSentimentFlowModel(
            daily=DailySentimentFlowAnalysis(
                market_summary="周末运行仍应锚定 2026-08-20。",
                verification_requests=(
                    _active_flow_request(STOCK_TARGET),
                    bad_event_request,
                ),
            ),
            reviews=(
                SentimentFlowReviewDecision(
                    review_summary="仅使用真实交易日窗口取得的资金流。",
                ),
            ),
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": weekend_as_of,
                "mode": SentimentFlowResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_sentiment_flow_snapshot",
            "get_stock_active_money_flow_context",
        ]
        active_arguments = next(
            arguments
            for tool_name, arguments in argument_calls
            if tool_name == "get_stock_active_money_flow_context"
        )
        assert active_arguments["end_date"] == date(2026, 8, 20)
        assert any("必须等于快照交易日" in message for message in result["errors"])

    asyncio.run(scenario())


def test_program_budget_stops_verification_before_extra_tool_calls() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        model = ScriptedSentimentFlowModel(
            daily=DailySentimentFlowAnalysis(
                market_summary="平安银行需要查证。",
                verification_requests=(_active_flow_request(STOCK_TARGET),),
            )
        )
        graph = build_sentiment_flow_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
            limits=SentimentFlowAgentLimits(max_total_tool_calls=1),
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": SentimentFlowResearchMode.DAILY,
            }
        )

        assert calls == ["get_daily_sentiment_flow_snapshot"]
        assert result["run_summary"].budget_exhausted is True
        assert result["run_summary"].stop_reason == "verification_budget_reached"
        assert model.review_calls == 0

    asyncio.run(scenario())


def test_main_graph_runs_technical_then_sentiment_flow_stage() -> None:
    stages: list[str] = []

    class StubTechnicalGraph:
        async def ainvoke(self, state):
            stages.append("technical")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": TechnicalAgentRunSummary(
                    mode=TechnicalResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    class StubSentimentFlowGraph:
        async def ainvoke(self, state):
            stages.append("sentiment_flow")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": SentimentFlowAgentRunSummary(
                    mode=SentimentFlowResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: StubTechnicalGraph(),
        sentiment_flow_agent_graph_factory=lambda **_: StubSentimentFlowGraph(),
    )
    result = asyncio.run(graph.ainvoke({"target": MARKET_TARGET, "as_of": AS_OF}))

    assert stages == ["technical", "sentiment_flow"]
    assert result["technical_run_summary"].mode is TechnicalResearchMode.DAILY
    assert result["sentiment_flow_run_summary"].mode is SentimentFlowResearchMode.DAILY
    assert result["technical_request_count"] == 0
    assert result["sentiment_flow_request_count"] == 0


def test_prompts_make_schema_and_market_data_boundaries_explicit() -> None:
    assert "snapshot_evidence" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "verification_requests" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "market_summary" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "THS 与 DC" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "绝不能相加、平均" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "不得输出 Markdown" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "最小充分" in TARGETED_PLANNING_SYSTEM_PROMPT
    assert "status=partial" in VERIFICATION_REVIEW_SYSTEM_PROMPT
    assert "unresolved_questions" in VERIFICATION_REVIEW_SYSTEM_PROMPT


def _active_flow_request(target: ResearchTarget) -> SentimentFlowVerificationRequestDraft:
    return SentimentFlowVerificationRequestDraft(
        target=target,
        question="该股的异常资金方向是否具有多日持续性？",
        requested_evidence="分别列出 THS 和 DC 区间资金方向，并保留口径差异。",
        checks=(SentimentFlowCheck.ACTIVE_MONEY_FLOW,),
        lookback_days=30,
        priority=ResearchPriority.HIGH,
        reason="该股进入当日资金异常候选。",
    )


def _research_request(request_id: str) -> ResearchRequest:
    return ResearchRequest(
        request_id=request_id,
        run_id=RUN_ID,
        thesis_id="th_candidate_001",
        target=STOCK_TARGET,
        assigned_domain=EvidenceDomain.SENTIMENT_FLOW,
        question="当日资金净流入是否具有多日持续性？",
        requested_evidence="分别查询 THS 和 DC 个股资金流。",
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 20)),
        priority=ResearchPriority.HIGH,
        status=ResearchRequestStatus.PENDING,
        requested_by="ThesisValidationAnalyst",
        created_at=AS_OF,
    )


async def fake_tool_runtime(
    *,
    run_id: str = RUN_ID,
    as_of: datetime = AS_OF,
    active_money_flow_returned_ts_code: str | None = None,
    argument_calls: list[tuple[str, dict[str, object]]] | None = None,
) -> tuple[ResearchToolContext, tuple[Any, ...], list[str]]:
    data_store = InMemoryResearchDataStore()
    context = ResearchToolContext(
        services=build_data_services(NoopProvider()),
        as_of=as_of,
        run_id=run_id,
        data_store=data_store,
    )
    calls: list[str] = []

    async def daily_snapshot(candidate_count: int = 10) -> dict[str, object]:
        calls.append("get_daily_sentiment_flow_snapshot")
        context_ref = await data_store.put(
            run_id,
            ResearchDataBundle(
                kind="daily_sentiment_flow_snapshot",
                tool_name="get_daily_sentiment_flow_snapshot",
                as_of=as_of,
                datasets={"sentiment_hsgt_flow": _service_dataset("moneyflow_hsgt")},
                metadata={"candidate_count": candidate_count},
            ),
        )
        return {
            "tool_name": "get_daily_sentiment_flow_snapshot",
            "status": "ok",
            "as_of": as_of.isoformat(),
            "context_ref": context_ref,
            "snapshot": {
                "trade_date": "2026-08-20",
                "technical_context": {"market_breadth": {"advancing_count": 3200}},
                "market_flow": {
                    "hsgt_history": [{"trade_date": "2026-08-20", "north_money": 42.6}],
                    "market_moneyflow_dc": {"net_amount": 80.0},
                    "margin_markets": [],
                },
                "industry_top_inflows": [],
                "industry_top_outflows": [],
                "stock_candidates": {
                    "ths_top_inflows": [
                        {
                            "source_api": "moneyflow_ths",
                            "ts_code": "000001.SZ",
                            "name": "平安银行",
                            "net_amount": 120.0,
                        }
                    ],
                    "ths_top_outflows": [],
                    "dc_top_inflows": [],
                    "dc_top_outflows": [],
                    "strongest_limit_events": [],
                    "most_opened_limit_events": [],
                },
                "authorized_targets": [
                    {"type": "MARKET", "code": "A_SHARE", "name": "A股市场"},
                    {"type": "STOCK", "code": "000001.SZ", "name": "平安银行"},
                ],
                "coverage": {
                    "source_dataset_count": 1,
                    "optional_failure_count": 0,
                },
            },
            "issues": [],
            "source_dataset_count": 1,
            "total_stored_items": 1,
            "complete": True,
        }

    async def active_money_flow(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        calls.append("get_stock_active_money_flow_context")
        if argument_calls is not None:
            argument_calls.append(
                (
                    "get_stock_active_money_flow_context",
                    {
                        "ts_code": ts_code,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
            )
        return _targeted_result(
            "get_stock_active_money_flow_context",
            ("moneyflow_ths", "moneyflow_dc"),
            active_money_flow_returned_ts_code or ts_code,
            start_date,
            end_date,
        )

    async def capital_positioning(
        ts_code: str,
        start_date: date,
        end_date: date,
        exchange_id: str,
    ) -> dict[str, object]:
        calls.append("get_capital_flow_context")
        return _targeted_result(
            "get_capital_flow_context",
            ("margin_detail",),
            ts_code,
            start_date,
            end_date,
            extra={"exchange_id": exchange_id},
        )

    async def unusual_trading(
        ts_code: str,
        trade_date: date,
    ) -> dict[str, object]:
        calls.append("get_unusual_trading_activity")
        return _targeted_result(
            "get_unusual_trading_activity",
            ("top_list",),
            ts_code,
            trade_date,
            trade_date,
        )

    tools = (
        create_structured_tool(
            name="get_daily_sentiment_flow_snapshot",
            description="fake daily sentiment snapshot",
            args_schema=DailySentimentFlowSnapshotInput,
            coroutine=daily_snapshot,
        ),
        create_structured_tool(
            name="get_stock_active_money_flow_context",
            description="fake active money flow",
            args_schema=StockDateRangeToolInput,
            coroutine=active_money_flow,
        ),
        create_structured_tool(
            name="get_capital_flow_context",
            description="fake capital positioning",
            args_schema=CapitalFlowContextInput,
            coroutine=capital_positioning,
        ),
        create_structured_tool(
            name="get_unusual_trading_activity",
            description="fake unusual trading",
            args_schema=UnusualTradingInput,
            coroutine=unusual_trading,
        ),
    )
    return context, tools, calls


def _service_dataset(api_name: str) -> ServiceDataset:
    fields = ("trade_date", "north_money")
    return ServiceDataset(
        api_name=api_name,
        query_params={"start_date": "20260820", "end_date": "20260820"},
        requested_fields=fields,
        items=[{"trade_date": "20260820", "north_money": 42.6}],
        item_traces=(
            ServiceItemTrace(
                page_index=0,
                source_offset=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
            ),
        ),
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=1,
                returned_fields=fields,
                response_bytes=128,
            ),
        ),
        as_of=date(2026, 8, 20),
        data_as_of=date(2026, 8, 20),
        received_item_count=1,
        discarded_item_count=0,
        complete=True,
    )


def _targeted_result(
    tool_name: str,
    labels: tuple[str, ...],
    ts_code: str,
    start_date: date,
    end_date: date,
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    datasets = []
    for index, label in enumerate(labels):
        datasets.append(
            {
                "label": label,
                "api_name": label,
                "query_params": {
                    "ts_code": ts_code,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    **(extra or {}),
                },
                "requested_fields": ["ts_code", "trade_date", "net_amount"],
                "rows": [
                    {
                        "data": {
                            "ts_code": ts_code,
                            "trade_date": end_date.isoformat(),
                            "net_amount": 100.0 + index,
                        },
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
                "data_as_of": end_date.isoformat(),
                "complete": True,
                "source_summary": [
                    {
                        "provider": "PRIMARY",
                        "from_cache": False,
                        "page_count": 1,
                        "item_count": 1,
                        "response_bytes": 128,
                        "latest_fetched_at": FETCHED_AT.isoformat(),
                    }
                ],
            }
        )
    return {
        "tool_name": tool_name,
        "status": "ok",
        "as_of": AS_OF.isoformat(),
        "datasets": datasets,
        "issues": [],
        "total_returned_items": len(datasets),
        "complete": True,
    }
