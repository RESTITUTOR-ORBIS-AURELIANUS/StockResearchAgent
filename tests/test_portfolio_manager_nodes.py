"""进取型与防御型投资组合经理独立建议节点测试。"""

import asyncio
from datetime import datetime

import pytest

from stock_research_agent.agents.debate import (
    CrossReviewEvaluationDraft,
    PortfolioCrossReviewDraft,
)
from stock_research_agent.agents.portfolio import (
    OpenAIAggressivePortfolioManagerModel,
    OpenAIConservativePortfolioManagerModel,
    PortfolioRecommendationDraft,
    PortfolioRecommendationLimits,
    RecommendationProposalDraft,
)
from stock_research_agent.agents.portfolio.prompts import (
    AGGRESSIVE_PORTFOLIO_SYSTEM_PROMPT,
    CONSERVATIVE_PORTFOLIO_SYSTEM_PROMPT,
)
from stock_research_agent.agents.strategist import (
    CandidateThesisDraft,
    CandidateThesisGeneration,
)
from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
)
from stock_research_agent.agents.validator import (
    ThesisFinalizationDraft,
    ThesisValidationAction,
    ThesisValidationDecision,
    ThesisValidationRunSummary,
)
from stock_research_agent.domain import EvidenceRecord, ResearchTarget, SourceReference
from stock_research_agent.domain.enums import (
    DecisionDimension,
    EvidenceDomain,
    PortfolioManager,
    ProposalStatus,
    RecommendationAction,
    RecommendationProfile,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph import build_research_graph
from stock_research_agent.graph.nodes.portfolio_recommendation import (
    build_aggressive_portfolio_recommendation_node,
    build_conservative_portfolio_recommendation_node,
)

AS_OF = datetime.fromisoformat("2026-08-25T16:00:00+08:00")
RUN_ID = "run_20260825_160000_A_SHARE_portfolio"
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")


class ScriptedPortfolioManager:
    def __init__(self, draft: PortfolioRecommendationDraft | Exception) -> None:
        self.draft = draft
        self.calls = []

    async def generate_recommendation(self, request):
        self.calls.append(request)
        if isinstance(self.draft, Exception):
            raise self.draft
        return self.draft


def test_aggressive_node_receives_all_terminal_theses_and_assembles_record() -> None:
    supported = _thesis("th_supported_001", ThesisValidationStatus.SUPPORTED)
    mixed = _thesis("th_mixed_001", ThesisValidationStatus.MIXED)
    refuted = _thesis("th_refuted_001", ThesisValidationStatus.REFUTED)
    inconclusive = _thesis("th_inconclusive_001", ThesisValidationStatus.INCONCLUSIVE)
    model = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.OVERWEIGHT,
            action_thesis_id=supported.thesis_id,
            risk_thesis_id=mixed.thesis_id,
        )
    )
    state = _state([supported, mixed, refuted, inconclusive])

    result = asyncio.run(build_aggressive_portfolio_recommendation_node(model)(state))

    assert len(model.calls) == 1
    request = model.calls[0]
    assert [item.thesis_id for item in request.theses] == [
        supported.thesis_id,
        mixed.thesis_id,
        refuted.thesis_id,
        inconclusive.thesis_id,
    ]
    assert request.eligible_supporting_thesis_ids == (
        supported.thesis_id,
        mixed.thesis_id,
    )

    recommendation = result["aggressive_recommendation"]
    assert recommendation.profile is RecommendationProfile.AGGRESSIVE
    assert recommendation.generated_by == PortfolioManager.AGGRESSIVE.value
    assert recommendation.target == MARKET
    assert recommendation.supporting_thesis_ids == [
        supported.thesis_id,
        mixed.thesis_id,
    ]
    assert recommendation.recommendation_id.startswith("rec_")
    assert recommendation.debate is None
    assert [item.conflict_group for item in recommendation.proposal_items] == [
        "MARKET:A_SHARE:ACTION",
        "MARKET:A_SHARE:HORIZON",
        "MARKET:A_SHARE:RISK_CONTROL",
    ]
    assert all(item.status is ProposalStatus.PROPOSED for item in recommendation.proposal_items)
    assert all(item.target == MARKET for item in recommendation.proposal_items)
    assert all(len(item.evaluations) == 1 for item in recommendation.proposal_items)
    assert all(
        item.evaluations[0].manager is PortfolioManager.AGGRESSIVE
        for item in recommendation.proposal_items
    )
    assert result["aggressive_recommendation_run_summary"].stop_reason == "complete"


def test_two_manager_nodes_produce_distinct_independent_records() -> None:
    thesis = _thesis("th_supported_pair", ThesisValidationStatus.SUPPORTED)
    aggressive = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.OVERWEIGHT,
            action_thesis_id=thesis.thesis_id,
        )
    )
    conservative = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id=thesis.thesis_id,
            score=0.5,
        )
    )
    state = _state([thesis])

    aggressive_result = asyncio.run(
        build_aggressive_portfolio_recommendation_node(aggressive)(state)
    )
    conservative_result = asyncio.run(
        build_conservative_portfolio_recommendation_node(conservative)(state)
    )

    aggressive_record = aggressive_result["aggressive_recommendation"]
    conservative_record = conservative_result["conservative_recommendation"]
    assert aggressive_record.recommendation_id != conservative_record.recommendation_id
    assert aggressive_record.action is RecommendationAction.OVERWEIGHT
    assert conservative_record.action is RecommendationAction.HOLD
    assert conservative_record.profile is RecommendationProfile.CONSERVATIVE
    assert all(
        item.proposer is PortfolioManager.CONSERVATIVE
        for item in conservative_record.proposal_items
    )


@pytest.mark.parametrize(
    "bad_thesis_id",
    ["th_refuted_bad", "th_inconclusive_bad", "th_unknown_bad"],
)
def test_node_rejects_entire_draft_when_item_cites_ineligible_thesis(
    bad_thesis_id: str,
) -> None:
    supported = _thesis("th_supported_good", ThesisValidationStatus.SUPPORTED)
    refuted = _thesis("th_refuted_bad", ThesisValidationStatus.REFUTED)
    inconclusive = _thesis("th_inconclusive_bad", ThesisValidationStatus.INCONCLUSIVE)
    model = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id=bad_thesis_id,
        )
    )

    result = asyncio.run(
        build_conservative_portfolio_recommendation_node(model)(
            _state([supported, refuted, inconclusive])
        )
    )

    assert "conservative_recommendation" not in result
    assert result["conservative_recommendation_run_summary"].stop_reason == ("rejected_output")
    assert "thesis_id" in result["errors"][0]


def test_node_rejects_item_target_not_grounded_by_cited_thesis() -> None:
    supported = _thesis("th_market_only", ThesisValidationStatus.SUPPORTED)
    draft = _draft(
        action=RecommendationAction.HOLD,
        action_thesis_id=supported.thesis_id,
    )
    stock_item = RecommendationProposalDraft(
        target=STOCK,
        decision_dimension=DecisionDimension.ENTRY_STRATEGY,
        proposal="等待个股价格回落后再考虑配置。",
        supporting_thesis_ids=(supported.thesis_id,),
        insistence_score=0.5,
        score_reason="测试不能由市场观点凭空推出个股建议。",
    )
    invalid = PortfolioRecommendationDraft.model_validate(
        {
            **draft.model_dump(),
            "proposal_items": (*draft.proposal_items, stock_item),
        }
    )
    model = ScriptedPortfolioManager(invalid)

    result = asyncio.run(build_aggressive_portfolio_recommendation_node(model)(_state([supported])))

    assert "aggressive_recommendation" not in result
    assert "target is not grounded" in result["errors"][0]


def test_no_supported_or_mixed_thesis_skips_model() -> None:
    refuted = _thesis("th_refuted_only", ThesisValidationStatus.REFUTED)
    inconclusive = _thesis("th_unknown_only", ThesisValidationStatus.INCONCLUSIVE)
    model = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.AVOID,
            action_thesis_id=refuted.thesis_id,
        )
    )

    result = asyncio.run(
        build_conservative_portfolio_recommendation_node(model)(_state([refuted, inconclusive]))
    )

    assert model.calls == []
    assert result["conservative_recommendation_run_summary"].stop_reason == ("no_decision_theses")


def test_context_limit_fails_closed_without_truncation_or_model_call() -> None:
    thesis = _thesis(
        "th_large_context",
        ThesisValidationStatus.SUPPORTED,
        reasoning_summary="长上下文" * 1_000,
    )
    model = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id=thesis.thesis_id,
        )
    )

    result = asyncio.run(
        build_aggressive_portfolio_recommendation_node(
            model,
            limits=PortfolioRecommendationLimits(max_context_characters=1_000),
        )(_state([thesis]))
    )

    assert model.calls == []
    assert result["aggressive_recommendation_run_summary"].stop_reason == ("context_limit_exceeded")
    assert "input was not truncated" in result["errors"][0]


def test_model_error_is_sanitized_and_does_not_create_partial_record() -> None:
    thesis = _thesis("th_model_error", ThesisValidationStatus.SUPPORTED)
    model = ScriptedPortfolioManager(RuntimeError("secret provider payload"))

    result = asyncio.run(build_aggressive_portfolio_recommendation_node(model)(_state([thesis])))

    assert "aggressive_recommendation" not in result
    assert result["aggressive_recommendation_run_summary"].stop_reason == "model_error"
    assert result["errors"] == ["AggressivePortfolioManager model failed: RuntimeError"]


def test_missing_validation_summary_fails_before_model_call() -> None:
    thesis = _thesis("th_missing_summary", ThesisValidationStatus.SUPPORTED)
    model = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id=thesis.thesis_id,
        )
    )
    state = _state([thesis])
    state["thesis_validation_run_summary"] = None

    result = asyncio.run(build_conservative_portfolio_recommendation_node(model)(state))

    assert model.calls == []
    assert result["conservative_recommendation_run_summary"].stop_reason == ("invalid_state")
    assert "validation run summary is missing" in result["errors"][0]


def test_same_run_and_draft_generate_stable_ids() -> None:
    thesis = _thesis("th_stable_ids", ThesisValidationStatus.SUPPORTED)
    draft = _draft(
        action=RecommendationAction.OVERWEIGHT,
        action_thesis_id=thesis.thesis_id,
    )
    model = ScriptedPortfolioManager(draft)
    node = build_aggressive_portfolio_recommendation_node(model)
    state = _state([thesis])

    first = asyncio.run(node(state))["aggressive_recommendation"]
    second = asyncio.run(node(state))["aggressive_recommendation"]

    assert first.recommendation_id == second.recommendation_id
    assert [item.item_id for item in first.proposal_items] == [
        item.item_id for item in second.proposal_items
    ]


def test_builder_requires_both_managers_and_completed_validation_stage() -> None:
    dummy = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id="th_dummy_001",
        )
    )

    with pytest.raises(ValueError, match="必须成对配置"):
        build_research_graph(aggressive_portfolio_manager_model=dummy)
    with pytest.raises(ValueError, match="必须先配置 thesis_validation_model"):
        build_research_graph(
            aggressive_portfolio_manager_model=dummy,
            conservative_portfolio_manager_model=dummy,
        )


def test_full_graph_runs_two_managers_after_validation_and_joins_results() -> None:
    evidence = EvidenceRecord(
        evidence_id="ev_portfolio_graph_001",
        run_id=RUN_ID,
        target=MARKET,
        domain=EvidenceDomain.TECHNICAL,
        as_of=AS_OF,
        title="市场趋势得到确认",
        description="市场趋势和广度指标同步改善。",
        source_refs=[
            SourceReference(
                provider="test_provider",
                interface="daily",
                record_key="market:20260825",
                published_at=AS_OF,
            )
        ],
        verification_status=VerificationStatus.VERIFIED,
        collected_by="TechnicalResearchAnalyst",
        created_at=AS_OF,
    )

    class TechnicalGraph:
        async def ainvoke(self, _state):
            return {
                "evidence_records": [evidence],
                "errors": [],
                "run_summary": TechnicalAgentRunSummary(
                    mode=TechnicalResearchMode.DAILY,
                    snapshot_status="complete",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=1,
                    rejected_evidence_count=0,
                    stop_reason="complete",
                ),
            }

    class Strategist:
        async def generate_candidates(self, _request):
            return CandidateThesisGeneration(
                candidates=(
                    CandidateThesisDraft(
                        target=MARKET,
                        title="市场趋势改善可能延续",
                        description="当前趋势和广度共同改善，据此猜想风险偏好可能延续。",
                        direction=ThesisDirection.BULLISH,
                        horizon="未来一个季度",
                        supporting_evidence_ids=(evidence.evidence_id,),
                        reasoning_summary="趋势和广度形成当前候选依据。",
                        missing_questions=("改善能否持续？",),
                        invalidation_conditions=("市场广度重新恶化",),
                    ),
                ),
                generation_summary="生成一条市场候选观点。",
            )

    class Validator:
        async def review_thesis(self, request):
            return ThesisValidationDecision(
                action=ThesisValidationAction.FINALIZE,
                review_summary="现有直接证据足以完成当前观点的条件性判断。",
                finalization=ThesisFinalizationDraft(
                    final_status=ThesisValidationStatus.SUPPORTED,
                    confidence=0.72,
                    supporting_evidence_ids=(evidence.evidence_id,),
                    reasoning_summary="趋势改善获得直接证据，但仍保留广度反转风险。",
                    remaining_questions=("下一交易日能否继续确认？",),
                ),
            )

    aggressive = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.OVERWEIGHT,
            action_thesis_id="th_placeholder",
        )
    )
    conservative = ScriptedPortfolioManager(
        _draft(
            action=RecommendationAction.HOLD,
            action_thesis_id="th_placeholder",
            score=0.5,
        )
    )

    class CrossReviewer:
        def __init__(self) -> None:
            self.calls = []

        async def review_recommendation(self, request):
            self.calls.append(request)
            return PortfolioCrossReviewDraft(
                evaluations=tuple(
                    CrossReviewEvaluationDraft(
                        item_id=item.item_id,
                        support_score=-1.0 if item.conflicts_with else 0.0,
                        hard_veto=False,
                        reason="同一决策槽中的原始版本需要经过后续协商。",
                        modification_suggestion="保留观点方向并协调执行条件。",
                    )
                    for item in request.counterpart_proposals
                )
            )

    aggressive_reviewer = CrossReviewer()
    conservative_reviewer = CrossReviewer()

    async def scenario() -> None:
        graph = build_research_graph(
            technical_agent_graph_factory=lambda **_: TechnicalGraph(),
            lead_research_strategist_model=Strategist(),
            thesis_validation_model=Validator(),
            aggressive_portfolio_manager_model=aggressive,
            conservative_portfolio_manager_model=conservative,
            aggressive_cross_review_model=aggressive_reviewer,
            conservative_cross_review_model=conservative_reviewer,
        )

        # 观点 ID 由程序稳定生成，因此脚本模型在收到请求后再绑定真实 ID。
        original_aggressive = aggressive.generate_recommendation
        original_conservative = conservative.generate_recommendation

        async def aggressive_bound(request):
            aggressive.draft = _draft(
                action=RecommendationAction.OVERWEIGHT,
                action_thesis_id=request.eligible_supporting_thesis_ids[0],
            )
            return await original_aggressive(request)

        async def conservative_bound(request):
            conservative.draft = _draft(
                action=RecommendationAction.HOLD,
                action_thesis_id=request.eligible_supporting_thesis_ids[0],
                score=0.5,
            )
            return await original_conservative(request)

        aggressive.generate_recommendation = aggressive_bound
        conservative.generate_recommendation = conservative_bound
        result = await graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})

        assert len(aggressive.calls) == 1
        assert len(conservative.calls) == 1
        assert result["aggressive_recommendation"].profile is (RecommendationProfile.AGGRESSIVE)
        assert result["conservative_recommendation"].profile is (RecommendationProfile.CONSERVATIVE)
        assert result["aggressive_recommendation_run_summary"].stop_reason == ("complete")
        assert result["conservative_recommendation_run_summary"].stop_reason == ("complete")
        assert len(result["normalized_proposal_pool"].proposal_items) == 6
        assert result["proposal_normalization_run_summary"].stop_reason == "complete"
        assert result["proposal_normalization_run_summary"].conflict_pair_count == 3
        assert len(aggressive_reviewer.calls) == 1
        assert len(conservative_reviewer.calls) == 1
        assert result["aggressive_cross_review_run_summary"].stop_reason == "complete"
        assert result["conservative_cross_review_run_summary"].stop_reason == "complete"
        assert all(
            len(item.evaluations) == 2
            for item in result["cross_reviewed_proposal_pool"].proposal_items
        )
        assert result["cross_review_application_run_summary"].stop_reason == "complete"
        assert result["conflict_score_validation_report"].valid is True
        assert result["conflict_score_validation_report"].stop_reason == "valid"
        assert result["conflict_score_validation_report"].aggressive_review_attempt == 1
        assert result["conflict_score_validation_report"].conservative_review_attempt == 1
        assert result["debate_round"] == 0

    asyncio.run(scenario())


def test_openai_adapters_use_common_schema_but_distinct_role_prompts() -> None:
    class RecordingChatModel:
        def __init__(self) -> None:
            self.calls = []

        def with_structured_output(self, schema, *, method, include_raw, strict):
            self.calls.append((schema, method, include_raw, strict))
            return object()

    aggressive_chat = RecordingChatModel()
    conservative_chat = RecordingChatModel()
    OpenAIAggressivePortfolioManagerModel(  # type: ignore[arg-type]
        aggressive_chat,
        structured_output_method="json_schema",
    )
    OpenAIConservativePortfolioManagerModel(  # type: ignore[arg-type]
        conservative_chat,
        structured_output_method="json_schema",
    )

    assert aggressive_chat.calls[0][0]["title"] == "PortfolioRecommendationDraft"
    assert conservative_chat.calls[0][0]["title"] == "PortfolioRecommendationDraft"
    assert aggressive_chat.calls[0][1:] == ("json_schema", True, True)
    assert conservative_chat.calls[0][1:] == ("json_schema", True, True)
    assert "更早承担风险" in AGGRESSIVE_PORTFOLIO_SYSTEM_PROMPT
    assert "永久损失" in CONSERVATIVE_PORTFOLIO_SYSTEM_PROMPT
    for prompt in (
        AGGRESSIVE_PORTFOLIO_SYSTEM_PROMPT,
        CONSERVATIVE_PORTFOLIO_SYSTEM_PROMPT,
    ):
        assert "REFUTED 只表示原观点被反驳" in prompt
        assert "不得编造目标价" in prompt
        assert "Few-shot" in prompt
        assert "只输出符合 PortfolioRecommendationDraft Schema 的 JSON" in prompt


def _draft(
    *,
    action: RecommendationAction,
    action_thesis_id: str,
    risk_thesis_id: str | None = None,
    score: float = 0.75,
) -> PortfolioRecommendationDraft:
    risk_id = risk_thesis_id or action_thesis_id
    return PortfolioRecommendationDraft(
        action=action,
        horizon="未来一个至三个季度",
        confidence=0.7,
        summary="基于已经完成查证的观点形成有条件、可复核的独立方案。",
        valuation_guidance=None,
        risk_summary="核心观点失效时应降低风险暴露。",
        proposal_items=(
            _proposal(DecisionDimension.ACTION, action_thesis_id, score=score),
            _proposal(DecisionDimension.HORIZON, action_thesis_id, score=0.5),
            _proposal(DecisionDimension.RISK_CONTROL, risk_id, score=1.0),
        ),
    )


def _proposal(
    dimension: DecisionDimension,
    thesis_id: str,
    *,
    score: float,
) -> RecommendationProposalDraft:
    return RecommendationProposalDraft(
        target=MARKET,
        decision_dimension=dimension,
        proposal=f"针对 {dimension.value} 形成一条独立可评审的建议。",
        supporting_thesis_ids=(thesis_id,),
        insistence_score=score,
        score_reason="该条建议与已完成查证的观点及风险边界一致。",
    )


def _thesis(
    thesis_id: str,
    status: ThesisValidationStatus,
    *,
    reasoning_summary: str = "已区分支持事实、反向事实和剩余不确定性。",
) -> ThesisRecord:
    confidence = 0.75
    return ThesisRecord(
        thesis_id=thesis_id,
        run_id=RUN_ID,
        target=MARKET,
        as_of=AS_OF,
        title=f"{status.value} 测试观点",
        description="这是完成查证后供投资组合经理决策的观点。",
        direction=(
            ThesisDirection.BULLISH
            if status is ThesisValidationStatus.SUPPORTED
            else ThesisDirection.MIXED
        ),
        horizon="未来一个季度",
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent="LeadResearchStrategist",
        ),
        validation=ThesisValidation(status=status, confidence=confidence, round=1),
        supporting_evidence_ids=[f"ev_{thesis_id}"],
        reasoning_summary=reasoning_summary,
        missing_questions=["仍需跟踪哪些失效条件？"],
        invalidation_conditions=["核心事实发生反转"],
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )


def _state(theses: list[ThesisRecord]):
    statuses = {
        status.value: sum(thesis.validation.status is status for thesis in theses)
        for status in (
            ThesisValidationStatus.SUPPORTED,
            ThesisValidationStatus.REFUTED,
            ThesisValidationStatus.MIXED,
            ThesisValidationStatus.INCONCLUSIVE,
        )
    }
    return {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "thesis_pool": theses,
        "active_validation_session": None,
        "active_validation_request_id": None,
        "thesis_validation_run_summary": ThesisValidationRunSummary(
            input_thesis_count=len(theses),
            completed_thesis_count=len(theses),
            status_counts=statuses,
            model_call_count=len(theses),
            research_request_count=0,
            finding_count=0,
            discovered_candidate_count=0,
            stop_reason="complete" if theses else "no_theses",
        ),
        "errors": [],
    }
