"""技术分析 Agent 每日模式、查证模式和 LLM 配置的离线测试。"""

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_research_agent.agents.technical import (
    OpenAITechnicalReasoningModel,
    TechnicalResearchMode,
    build_technical_agent_graph,
)
from stock_research_agent.agents.technical.models import (
    DailyTechnicalAnalysis,
    TargetedTechnicalPlan,
    TechnicalAgentLimits,
    TechnicalBenchmark,
    TechnicalEvidenceDraft,
    TechnicalInstrumentKind,
    TechnicalMeasurement,
    TechnicalVerificationRequestDraft,
    VerificationReviewDecision,
)
from stock_research_agent.agents.technical.prompts import DAILY_ANALYSIS_SYSTEM_PROMPT
from stock_research_agent.config import LLMSettings
from stock_research_agent.domain import ResearchRequest, ResearchTarget, TimeRange
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph import build_research_graph
from stock_research_agent.llm import build_chat_model
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
    DailyTechnicalSnapshotInput,
    FundMarketContextInput,
    IndexMarketContextInput,
    MomentumCalculatorInput,
    RelativeStrengthCalculatorInput,
    ReturnAndTrendCalculatorInput,
    RiskAndTradabilityCalculatorInput,
    StockPriceContextInput,
    VolumeAndLiquidityCalculatorInput,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 5, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_A_SHARE_aaaaaaaa"
MARKET_TARGET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
OTHER_STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


def test_default_technical_limits_cap_each_verification_round_at_three_targets() -> None:
    limits = TechnicalAgentLimits()

    assert limits.daily_candidate_count == 6
    assert limits.max_requests_per_round == 3
    assert limits.max_total_tool_calls == 24


class NoopProvider:
    async def query(self, request):  # pragma: no cover - fake Tools bypass provider
        raise AssertionError(f"unexpected provider call: {request.api_name}")


class ScriptedTechnicalModel:
    def __init__(
        self,
        *,
        daily: DailyTechnicalAnalysis | None = None,
        targeted: TargetedTechnicalPlan | None = None,
        reviews: tuple[VerificationReviewDecision, ...] = (),
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


def test_daily_mode_generates_snapshot_evidence_then_verifies_selected_stock() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="市场宽度分化，平安银行进入涨幅候选。",
                snapshot_evidence=(
                    TechnicalEvidenceDraft(
                        target=MARKET_TARGET,
                        title="市场上涨家数多于下跌家数",
                        description="2026-08-20上涨3200家、下跌1800家。",
                        source_call_ids=("tc_daily_snapshot_1",),
                        tags=("市场宽度",),
                    ),
                ),
                verification_requests=(
                    TechnicalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        instrument_kind=TechnicalInstrumentKind.STOCK,
                        question="平安银行的强势是否形成多日趋势？",
                        requested_evidence="计算多周期收益和均线结构。",
                        measurements=(TechnicalMeasurement.RETURN_TREND,),
                        lookback_days=180,
                        reason="它进入当日涨幅候选，需要区分单日异动和持续趋势。",
                    ),
                ),
            ),
            reviews=(
                VerificationReviewDecision(
                    review_summary="多周期收益和均线结果支持趋势事实。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行收盘价位于二十日均线上方",
                            description="确定性计算显示最新收盘价高于二十日均线。",
                            source_call_ids=("tc_r1_1_return_trend",),
                            tags=("趋势", "均线"),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.DAILY,
            }
        )

        assert [record.title for record in result["evidence_records"]] == [
            "市场上涨家数多于下跌家数",
            "平安银行收盘价位于二十日均线上方",
        ]
        assert all(
            record.verification_status is VerificationStatus.VERIFIED
            for record in result["evidence_records"]
        )
        assert all(
            record.source_refs[0].fetched_at == FETCHED_AT for record in result["evidence_records"]
        )
        assert result["run_summary"].verification_rounds == 1
        assert result["run_summary"].tool_call_count == 3
        assert calls == [
            "get_daily_technical_market_snapshot",
            "get_stock_price_context",
            "calculate_return_and_trend",
        ]
        assert model.daily_calls == 1
        assert model.targeted_calls == 0
        assert model.review_calls == 1

    asyncio.run(scenario())


def test_main_graph_factory_builds_a_real_technical_graph_for_generated_run_id() -> None:
    async def scenario() -> None:
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="市场上涨家数多于下跌家数。",
                snapshot_evidence=(
                    TechnicalEvidenceDraft(
                        target=MARKET_TARGET,
                        title="A股市场上涨家数占优",
                        description="2026-08-20上涨3200家、下跌1800家。",
                        source_call_ids=("tc_daily_snapshot_1",),
                        tags=("市场宽度",),
                    ),
                ),
            )
        )
        factory_arguments: list[tuple[str, datetime]] = []
        runtime_calls: list[list[str]] = []

        async def factory(*, run_id: str, as_of: datetime):
            factory_arguments.append((run_id, as_of))
            context, tools, calls = await fake_tool_runtime(run_id=run_id, as_of=as_of)
            runtime_calls.append(calls)
            return build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        graph = build_research_graph(technical_agent_graph_factory=factory)
        result = await graph.ainvoke({"target": MARKET_TARGET, "as_of": AS_OF})

        assert factory_arguments == [(result["run_id"], AS_OF)]
        assert runtime_calls == [["get_daily_technical_market_snapshot"]]
        assert result["evidence_pool"][0].run_id == result["run_id"]
        assert result["evidence_pool"][0].title == "A股市场上涨家数占优"
        assert result["technical_run_summary"].mode is TechnicalResearchMode.DAILY

    asyncio.run(scenario())


def test_verification_mode_skips_daily_snapshot_and_completes_research_request() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        request = ResearchRequest(
            request_id="rq_technical_001",
            run_id=RUN_ID,
            thesis_id="th_candidate_001",
            target=STOCK_TARGET,
            assigned_domain=EvidenceDomain.TECHNICAL,
            question="短期上涨是否得到动能支持？",
            requested_evidence="检查 RSI、MACD 和 ROC。",
            time_range=TimeRange(start=date(2026, 5, 1), end=date(2026, 8, 20)),
            priority=ResearchPriority.HIGH,
            status=ResearchRequestStatus.PENDING,
            requested_by="ThesisValidationAnalyst",
            created_at=AS_OF,
        )
        model = ScriptedTechnicalModel(
            targeted=TargetedTechnicalPlan(
                planning_summary="只需要动能测量。",
                verification_requests=(
                    TechnicalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        instrument_kind=TechnicalInstrumentKind.STOCK,
                        question=request.question,
                        requested_evidence=request.requested_evidence,
                        measurements=(TechnicalMeasurement.MOMENTUM,),
                        lookback_days=180,
                        reason="问题明确要求动能指标。",
                    ),
                ),
            ),
            reviews=(
                VerificationReviewDecision(
                    review_summary="动能指标已计算。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行动能指标处于正区间",
                            description="确定性计算返回的 MACD 柱为正。",
                            source_call_ids=("tc_r1_1_momentum",),
                            tags=("MACD",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.VERIFICATION,
                "research_request": request,
            }
        )

        assert calls == ["get_stock_price_context", "calculate_momentum"]
        assert "get_daily_technical_market_snapshot" not in calls
        assert result["completed_research_request"].status is ResearchRequestStatus.COMPLETED
        assert result["completed_research_request"].result_evidence_ids == [
            result["evidence_records"][0].evidence_id
        ]
        assert model.daily_calls == 0
        assert model.targeted_calls == 1

    asyncio.run(scenario())


def test_daily_mode_can_add_one_follow_up_round_without_refetching_context() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        first_request = TechnicalVerificationRequestDraft(
            target=STOCK_TARGET,
            instrument_kind=TechnicalInstrumentKind.STOCK,
            question="强势是否形成多日趋势？",
            requested_evidence="计算均线和区间收益。",
            measurements=(TechnicalMeasurement.RETURN_TREND,),
            reason="先确认趋势。",
        )
        follow_up = TechnicalVerificationRequestDraft(
            target=STOCK_TARGET,
            instrument_kind=TechnicalInstrumentKind.STOCK,
            question="趋势是否得到动能支持？",
            requested_evidence="补充 RSI、MACD 和 ROC。",
            measurements=(TechnicalMeasurement.MOMENTUM,),
            reason="趋势计算不能代替动能确认。",
        )
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="平安银行进入异常候选。",
                verification_requests=(first_request,),
            ),
            reviews=(
                VerificationReviewDecision(
                    review_summary="趋势存在，但还需动能确认。",
                    follow_up_requests=(follow_up,),
                ),
                VerificationReviewDecision(
                    review_summary="动能结果已取得。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="趋势与动能计算均已完成",
                            description="均线结构与动能结果可以分别追溯。",
                            source_call_ids=(
                                "tc_r1_1_return_trend",
                                "tc_r2_1_momentum",
                            ),
                            tags=("趋势", "动能"),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.DAILY,
            }
        )

        assert result["run_summary"].verification_rounds == 2
        assert result["run_summary"].tool_call_count == 4
        assert calls.count("get_stock_price_context") == 1
        assert calls == [
            "get_daily_technical_market_snapshot",
            "get_stock_price_context",
            "calculate_return_and_trend",
            "calculate_momentum",
        ]
        assert result["evidence_records"][0].title == "趋势与动能计算均已完成"

    asyncio.run(scenario())


def test_llm_settings_are_separate_and_build_openai_compatible_model() -> None:
    settings = LLMSettings(
        base_url="https://llm.example.test/v1",
        api_key="secret-for-test",
        model="example-chat-model",
        request_timeout_seconds=45,
        max_retries=1,
        temperature=0,
        structured_output_method="json_schema",
    )
    model = build_chat_model(settings)

    assert model.model_name == "example-chat-model"
    assert model.openai_api_base == "https://llm.example.test/v1"
    assert model.request_timeout == 45
    assert model.max_retries == 1
    assert model.openai_api_key.get_secret_value() == "secret-for-test"
    assert settings.structured_output_method == "json_schema"


def test_technical_request_enforces_instrument_kind_including_sw_index() -> None:
    sector = ResearchTarget(type=TargetType.SECTOR, code="801780.SI", name="银行")
    request = TechnicalVerificationRequestDraft(
        target=sector,
        instrument_kind=TechnicalInstrumentKind.INDEX,
        question="行业趋势是否持续？",
        requested_evidence="计算行业指数区间收益。",
        measurements=(TechnicalMeasurement.RETURN_TREND,),
        reason="申万行业必须通过指数行情查询。",
    )
    assert request.instrument_kind is TechnicalInstrumentKind.INDEX

    with pytest.raises(ValueError, match="交易所证券代码"):
        TechnicalVerificationRequestDraft(
            target=sector,
            instrument_kind=TechnicalInstrumentKind.FUND,
            question="错误地把申万行业当成基金。",
            requested_evidence="不应生成可执行计划。",
            measurements=(TechnicalMeasurement.RETURN_TREND,),
            reason="验证程序硬约束。",
        )

    fund_proxy = ResearchTarget(type=TargetType.SECTOR, code="512800.SH", name="银行ETF")
    fund_request = TechnicalVerificationRequestDraft(
        target=fund_proxy,
        instrument_kind=TechnicalInstrumentKind.FUND,
        question="行业 ETF 是否呈现同向趋势？",
        requested_evidence="计算基金行情趋势。",
        measurements=(TechnicalMeasurement.RETURN_TREND,),
        reason="ETF 作为行业代理，领域类型暂归入 SECTOR。",
    )
    assert fund_request.instrument_kind is TechnicalInstrumentKind.FUND


def test_abstract_a_share_index_request_is_accepted_for_deterministic_proxy_mapping() -> None:
    request = TechnicalVerificationRequestDraft(
        target=MARKET_TARGET,
        instrument_kind=TechnicalInstrumentKind.INDEX,
        question="全市场趋势是否由主要宽基指数共同确认？",
        requested_evidence="计算真实宽基指数的区间收益与均线结构。",
        measurements=(TechnicalMeasurement.RETURN_TREND,),
        reason="A_SHARE 只是研究范围，执行前必须映射到真实指数。",
    )

    assert request.target.code == "A_SHARE"


def test_targeted_a_share_request_executes_real_market_index_proxy() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        request = ResearchRequest(
            request_id="rq_technical_a_share_proxy",
            run_id=RUN_ID,
            thesis_id="th_a_share_proxy_001",
            requested_by="ThesisValidationAnalyst",
            assigned_domain=EvidenceDomain.TECHNICAL,
            target=MARKET_TARGET,
            question="A股主要宽基指数是否同步走强？",
            requested_evidence="比较宽基指数区间收益与均线。",
            time_range=TimeRange(start=date(2026, 6, 1), end=AS_OF.date()),
            priority=ResearchPriority.HIGH,
            created_at=AS_OF,
        )
        model = ScriptedTechnicalModel(
            targeted=TargetedTechnicalPlan(
                planning_summary="把抽象市场目标交给程序映射。",
                verification_requests=(
                    TechnicalVerificationRequestDraft(
                        target=MARKET_TARGET,
                        instrument_kind=TechnicalInstrumentKind.INDEX,
                        question=request.question,
                        requested_evidence=request.requested_evidence,
                        measurements=(TechnicalMeasurement.RETURN_TREND,),
                        reason="需要真实宽基指数代理。",
                    ),
                ),
            ),
            reviews=(
                VerificationReviewDecision(
                    review_summary="宽基指数结果已经返回。",
                    unresolved_questions=("市场宽度仍需全市场快照补充。",),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(verification_input(request))

        assert calls == ["get_index_market_context", "calculate_return_and_trend"]
        target_observation = next(
            item
            for item in result["observations"]
            if item.tool_name == "get_index_market_context"
        )
        assert target_observation.arguments["ts_code"] == "000300.SH"
        assert (
            result["completed_research_request"].status
            is ResearchRequestStatus.NO_NEW_EVIDENCE
        )

    asyncio.run(scenario())


def test_daily_snapshot_permissions_reject_a_stock_recast_as_an_index() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        disguised_stock = ResearchTarget(
            type=TargetType.MARKET,
            code="000001.SZ",
            name="伪装成指数的平安银行",
        )
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="模型错误地改写了候选股票类型。",
                snapshot_evidence=(
                    TechnicalEvidenceDraft(
                        target=disguised_stock,
                        title="错误类型的快照证据",
                        description="候选股票不能被改写成市场指数。",
                        source_call_ids=("tc_daily_snapshot_1",),
                    ),
                ),
                verification_requests=(
                    TechnicalVerificationRequestDraft(
                        target=disguised_stock,
                        instrument_kind=TechnicalInstrumentKind.INDEX,
                        question="错误地把股票代码送入指数行情接口。",
                        requested_evidence="这条计划应被程序拒绝。",
                        measurements=(TechnicalMeasurement.RETURN_TREND,),
                        reason="验证快照授权同时绑定代码、目标类型和行情类型。",
                    ),
                ),
            )
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.DAILY,
            }
        )

        assert calls == ["get_daily_technical_market_snapshot"]
        assert result["evidence_records"] == []
        assert any("错误类型" in message for message in result["errors"])
        assert any("错误行情类型" in message for message in result["errors"])

    asyncio.run(scenario())


def test_structured_output_method_can_select_json_schema() -> None:
    class RecordingChatModel:
        def __init__(self) -> None:
            self.calls: list[tuple[type, str, bool, bool]] = []

        def with_structured_output(self, schema, *, method, include_raw, strict):
            self.calls.append((schema, method, include_raw, strict))
            return object()

    chat_model = RecordingChatModel()
    OpenAITechnicalReasoningModel(  # type: ignore[arg-type]
        chat_model,
        structured_output_method="json_schema",
    )

    assert len(chat_model.calls) == 3
    assert {method for _, method, _, _ in chat_model.calls} == {"json_schema"}
    assert all(call[2:] == (True, True) for call in chat_model.calls)


def test_daily_output_schema_and_prompt_describe_the_same_contract() -> None:
    schema = DailyTechnicalAnalysis.model_json_schema()

    assert set(schema["properties"]) == {
        "snapshot_evidence",
        "verification_requests",
        "market_summary",
    }
    assert all(
        "description" in schema["properties"][field_name] for field_name in schema["properties"]
    )
    assert "DailyTechnicalAnalysis Schema" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert '"snapshot_evidence"' in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert '"verification_requests"' in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert '"market_summary"' in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "Markdown" in DAILY_ANALYSIS_SYSTEM_PROMPT
    benchmark_target_type = schema["$defs"]["TechnicalBenchmarkTarget"]["properties"]["type"]
    assert set(benchmark_target_type["enum"]) == {"MARKET", "SECTOR"}
    assert schema["properties"]["verification_requests"]["maxItems"] == 3
    assert "verification_requests 最多提出 3 个目标" in DAILY_ANALYSIS_SYSTEM_PROMPT


def test_technical_measurements_are_deduplicated_in_input_order() -> None:
    request = TechnicalVerificationRequestDraft(
        target=STOCK_TARGET,
        instrument_kind=TechnicalInstrumentKind.STOCK,
        question="异常放量是否同时伴随趋势和动量变化？",
        requested_evidence="计算量能、趋势和动量指标。",
        measurements=(
            TechnicalMeasurement.VOLUME_LIQUIDITY,
            TechnicalMeasurement.VOLUME_LIQUIDITY,
            TechnicalMeasurement.RETURN_TREND,
            TechnicalMeasurement.RETURN_TREND,
            TechnicalMeasurement.MOMENTUM,
        ),
        lookback_days=120,
        reason="验证模型重复枚举时程序会做幂等归一化。",
    )

    assert request.measurements == (
        TechnicalMeasurement.VOLUME_LIQUIDITY,
        TechnicalMeasurement.RETURN_TREND,
        TechnicalMeasurement.MOMENTUM,
    )


def test_evidence_target_must_match_referenced_source_subject() -> None:
    async def scenario() -> None:
        context, tools, _ = await fake_tool_runtime()
        request = make_research_request("rq_technical_target_guard")
        model = ScriptedTechnicalModel(
            targeted=single_measurement_plan(request, TechnicalMeasurement.MOMENTUM),
            reviews=(
                VerificationReviewDecision(
                    review_summary="模型错误地把平安银行计算结果写给了万科。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=OTHER_STOCK_TARGET,
                            title="错误标的证据",
                            description="这条描述引用了另一只股票的计算结果。",
                            source_call_ids=("tc_r1_1_momentum",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(verification_input(request))

        assert result["evidence_records"] == []
        assert result["run_summary"].rejected_evidence_count == 1
        assert any("引用无法核验" in message for message in result["errors"])

    asyncio.run(scenario())


def test_same_run_daily_and_targeted_evidence_ids_do_not_collide() -> None:
    async def scenario() -> None:
        context, tools, _ = await fake_tool_runtime()
        request = make_research_request("rq_technical_second_pass")
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="记录市场宽度。",
                snapshot_evidence=(
                    TechnicalEvidenceDraft(
                        target=MARKET_TARGET,
                        title="每日市场证据",
                        description="当日上涨家数多于下跌家数。",
                        source_call_ids=("tc_daily_snapshot_1",),
                    ),
                ),
            ),
            targeted=single_measurement_plan(request, TechnicalMeasurement.MOMENTUM),
            reviews=tuple(
                VerificationReviewDecision(
                    review_summary="取得定向动能证据。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="定向动能证据",
                            description="MACD 计算结果可追溯。",
                            source_call_ids=("tc_r1_1_momentum",),
                        ),
                    ),
                )
                for _ in range(2)
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        daily = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.DAILY,
            }
        )
        targeted = await graph.ainvoke(verification_input(request))
        repeated = await graph.ainvoke(verification_input(request))

        daily_id = daily["evidence_records"][0].evidence_id
        targeted_id = targeted["evidence_records"][0].evidence_id
        assert daily_id != targeted_id
        assert "_daily_" in daily_id
        assert "_rq_technical_second_pass_" in targeted_id
        assert repeated["evidence_records"][0].evidence_id == targeted_id

    asyncio.run(scenario())


def test_too_large_context_can_be_calculated_but_not_cited_as_evidence() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()

        async def too_large_stock_context(ts_code, start_date, end_date, frequency="daily"):
            calls.append("get_stock_price_context")
            context_ref = await context.data_store.put(
                RUN_ID,
                ResearchDataBundle(
                    kind="stock_price_context",
                    tool_name="get_stock_price_context",
                    as_of=AS_OF,
                    datasets={"price_bars": source_dataset(ts_code)},
                    metadata={"ts_code": ts_code, "frequency": frequency},
                ),
            )
            return {
                **stored_result("get_stock_price_context", context_ref),
                "status": "too_large",
                "complete": False,
            }

        tools = replace_tool(
            tools,
            create_structured_tool(
                name="get_stock_price_context",
                description="fake too large context",
                args_schema=StockPriceContextInput,
                coroutine=too_large_stock_context,
            ),
        )
        request = make_research_request("rq_technical_too_large")
        model = ScriptedTechnicalModel(
            targeted=single_measurement_plan(request, TechnicalMeasurement.MOMENTUM),
            reviews=(
                VerificationReviewDecision(
                    review_summary="原始预览超限，但计算器成功。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="不应直接接受的原始行情证据",
                            description="这条证据只引用没有预览的原始调用。",
                            source_call_ids=("tc_r1_1_target",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(verification_input(request))

        assert "calculate_momentum" in calls
        assert result["evidence_records"] == []
        assert any("不存在或失败" in message for message in result["errors"])

    asyncio.run(scenario())


def test_relative_strength_benchmark_cannot_become_evidence_target() -> None:
    async def scenario() -> None:
        context, tools, _ = await fake_tool_runtime()
        request = make_research_request("rq_technical_relative_subject")
        benchmark_target = ResearchTarget(
            type=TargetType.MARKET,
            code="000300.SH",
            name="沪深300",
        )
        model = ScriptedTechnicalModel(
            targeted=TargetedTechnicalPlan(
                planning_summary="将股票与固定市场基准比较。",
                verification_requests=(
                    TechnicalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        instrument_kind=TechnicalInstrumentKind.STOCK,
                        question=request.question,
                        requested_evidence=request.requested_evidence,
                        measurements=(TechnicalMeasurement.RELATIVE_STRENGTH,),
                        benchmark=TechnicalBenchmark(
                            target=benchmark_target,
                            instrument_kind=TechnicalInstrumentKind.INDEX,
                        ),
                        reason="问题要求判断股票相对市场的强弱。",
                    ),
                ),
            ),
            reviews=(
                VerificationReviewDecision(
                    review_summary="模型错误地把基准当成了证据主体。",
                    evidence=(
                        TechnicalEvidenceDraft(
                            target=benchmark_target,
                            title="错误的基准主体证据",
                            description="相对强弱计算的主体应当是股票，不是比较基准。",
                            source_call_ids=("tc_r1_1_relative_strength",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(verification_input(request))

        assert result["evidence_records"] == []
        assert result["run_summary"].rejected_evidence_count == 1

    asyncio.run(scenario())


def test_budget_exhaustion_cancels_request_and_records_skipped_tasks() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()
        request = make_research_request("rq_technical_budget")
        model = ScriptedTechnicalModel(
            targeted=single_measurement_plan(request, TechnicalMeasurement.MOMENTUM),
        )
        graph = build_technical_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
            limits=TechnicalAgentLimits(max_total_tool_calls=1),
        )

        result = await graph.ainvoke(verification_input(request))

        assert calls == []
        assert result["tool_call_count"] == 0
        assert result["budget_exhausted"] is True
        assert len(result["skipped_task_ids"]) == 1
        assert result["run_summary"].budget_exhausted is True
        assert result["run_summary"].skipped_task_ids == tuple(result["skipped_task_ids"])
        assert (
            result["completed_research_request"].status is ResearchRequestStatus.CANCELLED_BY_BUDGET
        )
        assert result["run_summary"].stop_reason == "verification_budget_reached"
        assert model.review_calls == 0

    asyncio.run(scenario())


def test_failed_retries_count_every_real_tool_invocation() -> None:
    async def scenario() -> None:
        context, tools, calls = await fake_tool_runtime()

        async def failing_stock_context(ts_code, start_date, end_date, frequency="daily"):
            calls.append("get_stock_price_context")
            return {
                "tool_name": "get_stock_price_context",
                "status": "error",
                "issues": [],
                "complete": False,
            }

        tools = replace_tool(
            tools,
            create_structured_tool(
                name="get_stock_price_context",
                description="fake failed context",
                args_schema=StockPriceContextInput,
                coroutine=failing_stock_context,
            ),
        )
        first = verification_draft(TechnicalMeasurement.RETURN_TREND)
        second = verification_draft(TechnicalMeasurement.MOMENTUM)
        model = ScriptedTechnicalModel(
            daily=DailyTechnicalAnalysis(
                market_summary="同一标的需要两类测量。",
                verification_requests=(first, second),
            ),
            reviews=(VerificationReviewDecision(review_summary="两次原始查询均失败。"),),
        )
        graph = build_technical_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": TechnicalResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_technical_market_snapshot",
            "get_stock_price_context",
            "get_stock_price_context",
        ]
        assert result["tool_call_count"] == 3
        assert len(result["observations"]) == 3

    asyncio.run(scenario())


def make_research_request(request_id: str) -> ResearchRequest:
    return ResearchRequest(
        request_id=request_id,
        run_id=RUN_ID,
        thesis_id="th_candidate_001",
        target=STOCK_TARGET,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="短期上涨是否得到动能支持？",
        requested_evidence="检查确定性技术指标。",
        time_range=TimeRange(start=date(2026, 5, 1), end=date(2026, 8, 20)),
        priority=ResearchPriority.HIGH,
        status=ResearchRequestStatus.PENDING,
        requested_by="ThesisValidationAnalyst",
        created_at=AS_OF,
    )


def verification_draft(
    measurement: TechnicalMeasurement,
) -> TechnicalVerificationRequestDraft:
    return TechnicalVerificationRequestDraft(
        target=STOCK_TARGET,
        instrument_kind=TechnicalInstrumentKind.STOCK,
        question=f"查证 {measurement.value}",
        requested_evidence=f"计算 {measurement.value}",
        measurements=(measurement,),
        lookback_days=180,
        reason=f"需要 {measurement.value} 的确定性结果。",
    )


def single_measurement_plan(
    request: ResearchRequest,
    measurement: TechnicalMeasurement,
) -> TargetedTechnicalPlan:
    return TargetedTechnicalPlan(
        planning_summary="执行单项必要测量。",
        verification_requests=(
            TechnicalVerificationRequestDraft(
                target=request.target,
                instrument_kind=TechnicalInstrumentKind.STOCK,
                question=request.question,
                requested_evidence=request.requested_evidence,
                measurements=(measurement,),
                lookback_days=180,
                reason="问题要求该指标。",
            ),
        ),
    )


def verification_input(request: ResearchRequest) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "target": request.target,
        "as_of": AS_OF,
        "mode": TechnicalResearchMode.VERIFICATION,
        "research_request": request,
    }


def replace_tool(tools, replacement):
    return tuple(replacement if tool.name == replacement.name else tool for tool in tools)


async def fake_tool_runtime(
    *,
    run_id: str = RUN_ID,
    as_of: datetime = AS_OF,
):
    store = InMemoryResearchDataStore()
    context = ResearchToolContext(
        services=build_data_services(NoopProvider()),
        as_of=as_of,
        run_id=run_id,
        data_store=store,
    )
    calls: list[str] = []

    async def store_context(kind: str, tool_name: str, code: str) -> str:
        return await store.put(
            run_id,
            ResearchDataBundle(
                kind=kind,
                tool_name=tool_name,
                as_of=as_of,
                datasets={"price_bars": source_dataset(code)},
                metadata={"ts_code": code, "frequency": "daily"},
            ),
        )

    async def daily_snapshot(candidate_count: int = 10) -> dict[str, Any]:
        calls.append("get_daily_technical_market_snapshot")
        context_ref = await store_context(
            "daily_technical_market_snapshot",
            "get_daily_technical_market_snapshot",
            "A_SHARE",
        )
        return {
            "tool_name": "get_daily_technical_market_snapshot",
            "status": "ok",
            "as_of": as_of.isoformat(),
            "context_ref": context_ref,
            "snapshot": {
                "trade_date": "2026-08-20",
                "market_breadth": {"advancing_count": 3200, "declining_count": 1800},
                "market_indices": [{"ts_code": "000001.SH", "pct_chg": 0.5}],
                "industries": [{"index_code": "801780.SI", "industry_name": "银行"}],
                "candidates": {"top_gainers": [{"ts_code": "000001.SZ", "name": "平安银行"}]},
            },
            "issues": [],
            "source_dataset_count": 1,
            "total_stored_items": 1,
            "complete": True,
        }

    async def stock_context(ts_code, start_date, end_date, frequency="daily"):
        calls.append("get_stock_price_context")
        context_ref = await store_context("stock_price_context", "get_stock_price_context", ts_code)
        return stored_result("get_stock_price_context", context_ref, as_of=as_of)

    async def index_context(ts_code, start_date, end_date):
        calls.append("get_index_market_context")
        context_ref = await store_context(
            "index_market_context", "get_index_market_context", ts_code
        )
        return stored_result("get_index_market_context", context_ref, as_of=as_of)

    async def fund_context(
        ts_code,
        start_date,
        end_date,
        include_adjustment_factors=True,
        include_share_history=False,
    ):
        calls.append("get_fund_market_context")
        context_ref = await store_context("fund_market_context", "get_fund_market_context", ts_code)
        return stored_result("get_fund_market_context", context_ref, as_of=as_of)

    def single_calculator(name: str):
        async def calculate(context_ref: str, **kwargs):
            calls.append(name)
            bundle = await store.get(run_id, context_ref)
            return calculation_result(
                name,
                [context_ref],
                subjects=[calculation_subject(context_ref, bundle)],
                as_of=as_of,
            )

        return calculate

    async def relative_strength(target_context_ref: str, benchmark_context_ref: str, **kwargs):
        calls.append("calculate_relative_strength")
        target_bundle = await store.get(run_id, target_context_ref)
        benchmark_bundle = await store.get(run_id, benchmark_context_ref)
        return calculation_result(
            "calculate_relative_strength",
            [target_context_ref, benchmark_context_ref],
            subjects=[
                calculation_subject(target_context_ref, target_bundle),
                calculation_subject(benchmark_context_ref, benchmark_bundle),
            ],
            as_of=as_of,
        )

    tools = (
        create_structured_tool(
            name="get_daily_technical_market_snapshot",
            description="fake",
            args_schema=DailyTechnicalSnapshotInput,
            coroutine=daily_snapshot,
        ),
        create_structured_tool(
            name="get_stock_price_context",
            description="fake",
            args_schema=StockPriceContextInput,
            coroutine=stock_context,
        ),
        create_structured_tool(
            name="get_index_market_context",
            description="fake",
            args_schema=IndexMarketContextInput,
            coroutine=index_context,
        ),
        create_structured_tool(
            name="get_fund_market_context",
            description="fake",
            args_schema=FundMarketContextInput,
            coroutine=fund_context,
        ),
        create_structured_tool(
            name="calculate_return_and_trend",
            description="fake",
            args_schema=ReturnAndTrendCalculatorInput,
            coroutine=single_calculator("calculate_return_and_trend"),
        ),
        create_structured_tool(
            name="calculate_momentum",
            description="fake",
            args_schema=MomentumCalculatorInput,
            coroutine=single_calculator("calculate_momentum"),
        ),
        create_structured_tool(
            name="calculate_risk_and_tradability",
            description="fake",
            args_schema=RiskAndTradabilityCalculatorInput,
            coroutine=single_calculator("calculate_risk_and_tradability"),
        ),
        create_structured_tool(
            name="calculate_volume_and_liquidity",
            description="fake",
            args_schema=VolumeAndLiquidityCalculatorInput,
            coroutine=single_calculator("calculate_volume_and_liquidity"),
        ),
        create_structured_tool(
            name="calculate_relative_strength",
            description="fake",
            args_schema=RelativeStrengthCalculatorInput,
            coroutine=relative_strength,
        ),
    )
    return context, tools, calls


def source_dataset(code: str) -> ServiceDataset:
    fields = ("ts_code", "trade_date", "close")
    return ServiceDataset(
        api_name="daily",
        query_params={"ts_code": code},
        requested_fields=fields,
        items=[{"ts_code": code, "trade_date": "20260820", "close": 12.34}],
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
        as_of=AS_OF.date(),
        data_as_of=AS_OF.date(),
        received_item_count=1,
        discarded_item_count=0,
    )


def stored_result(
    tool_name: str,
    context_ref: str,
    *,
    as_of: datetime = AS_OF,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": "ok",
        "as_of": as_of.isoformat(),
        "context_ref": context_ref,
        "datasets": [],
        "issues": [],
        "total_stored_items": 1,
        "total_preview_items": 1,
        "complete": True,
    }


def calculation_result(
    tool_name: str,
    refs: list[str],
    *,
    subjects: list[dict[str, Any]] | None = None,
    as_of: datetime = AS_OF,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": "ok",
        "as_of": as_of.isoformat(),
        "source_context_refs": refs,
        "source_subjects": subjects or [],
        "calculation": {"status": "available", "value": 1.0},
        "issues": [],
        "complete": True,
    }


def calculation_subject(
    context_ref: str,
    bundle: ResearchDataBundle,
) -> dict[str, Any]:
    return {
        "context_ref": context_ref,
        "bundle_kind": bundle.kind,
        "ts_code": bundle.metadata["ts_code"],
        "frequency": bundle.metadata["frequency"],
    }
