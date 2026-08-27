"""确定性最终报告组装与 Markdown 渲染测试。"""

from datetime import datetime

from pydantic import ValidationError

from stock_research_agent.agents.consensus_assembly import ConsensusAssemblyRunSummary
from stock_research_agent.agents.negotiation import NegotiationProposalPool
from stock_research_agent.domain import (
    EvidenceRecord,
    RecommendationRecord,
    ResearchTarget,
    SourceReference,
    ThesisRecord,
)
from stock_research_agent.domain.enums import (
    DebateStatus,
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
from stock_research_agent.domain.recommendation import (
    DebateSummary,
    ProposalEvaluation,
    ProposalItem,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisValidation
from stock_research_agent.graph import build_research_graph
from stock_research_agent.graph.nodes.report_composer import report_composer_node
from stock_research_agent.reporting import ReportHealth, ReportOutcome, ResearchReport

RUN_ID = "run_20260826_report"
AS_OF = datetime.fromisoformat("2026-08-26T15:30:00+08:00")
TARGET = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")


def test_complete_state_generates_structured_report_and_markdown() -> None:
    state = _complete_state()

    result = report_composer_node(state)

    report = result["research_report"]
    markdown = result["research_report_markdown"]
    assert report.outcome is ReportOutcome.CONSENSUS_READY
    assert report.health is ReportHealth.CLEAN
    assert report.evidence.total_count == 1
    assert report.evidence.counts_by_domain["TECHNICAL"] == 1
    assert report.theses.total_count == 1
    assert report.theses.counts_by_validation_status["SUPPORTED"] == 1
    assert report.recommendations.aggressive.recommendation_id == "rec_aggressive"
    assert report.recommendations.conservative.recommendation_id == "rec_conservative"
    assert report.recommendations.consensus.recommendation_id == "rec_consensus"
    assert report.report_id == f"report_{report.source_fingerprint[:24]}"
    assert "# 平安银行（000001.SZ）投研报告" in markdown
    assert "## 证据" in markdown
    assert "## 观点" in markdown
    assert "## 投资建议" in markdown
    assert "## 未决分歧" in markdown
    assert "## 运行诊断" in markdown
    assert "仅供研究，不构成投资建议" in markdown


def test_only_initialized_state_still_generates_honest_incomplete_report() -> None:
    state = {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": TARGET,
        "errors": ["primary provider unavailable"],
    }

    result = report_composer_node(state)

    report = result["research_report"]
    assert report.outcome is ReportOutcome.INCOMPLETE
    assert report.health is ReportHealth.WITH_ERRORS
    assert report.evidence.records == ()
    assert report.theses.records == ()
    assert report.recommendations.aggressive is None
    assert report.recommendations.conservative is None
    assert report.recommendations.consensus is None
    assert report.diagnostics.upstream_errors == ("primary provider unavailable",)
    markdown = result["research_report_markdown"]
    assert "当前运行未产出证据" in markdown
    assert "当前运行未形成观点" in markdown
    assert markdown.count("未生成。") == 3
    assert "primary provider unavailable" in markdown
    assert "协商后的最终建议未生成，因此不能认定当前不存在未决分歧。" in markdown


def test_no_actionable_consensus_preserves_original_recommendations_without_fabrication() -> None:
    evidence, thesis = _evidence_and_thesis()
    state = {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": TARGET,
        "evidence_pool": [evidence],
        "thesis_pool": [thesis],
        "aggressive_recommendation": _independent_recommendation(
            RecommendationProfile.AGGRESSIVE
        ),
        "conservative_recommendation": _independent_recommendation(
            RecommendationProfile.CONSERVATIVE
        ),
        "consensus_recommendation": None,
        "consensus_assembly_run_summary": ConsensusAssemblyRunSummary(
            run_id=RUN_ID,
            as_of=AS_OF,
            source_fingerprint="a" * 64,
            debate_round=3,
            excluded_item_ids=("item_unresolved_action",),
            missing_required_dimensions=(DecisionDimension.ACTION,),
            stop_reason="no_actionable_consensus",
        ),
        "errors": [],
        "debate_round": 3,
    }

    result = report_composer_node(state)

    report = result["research_report"]
    assert report.outcome is ReportOutcome.NO_ACTIONABLE_CONSENSUS
    assert report.health is ReportHealth.CLEAN
    assert report.recommendations.aggressive is not None
    assert report.recommendations.conservative is not None
    assert report.recommendations.consensus is None
    disclosure = report.recommendations.disagreement
    assert disclosure.excluded_item_ids == ("item_unresolved_action",)
    assert disclosure.missing_required_dimensions == (DecisionDimension.ACTION,)
    assert disclosure.derived_from == ("CONSENSUS_ASSEMBLY",)
    markdown = result["research_report_markdown"]
    assert "`NO_ACTIONABLE_CONSENSUS`" in markdown
    assert "### 协商后的最终建议\n\n未生成。" in markdown
    consensus_section = markdown.split("### 协商后的最终建议", maxsplit=1)[1].split(
        "## 未决分歧", maxsplit=1
    )[0]
    assert "- 动作：" not in consensus_section


def test_integrity_problem_becomes_warning_instead_of_blocking_report() -> None:
    _, thesis = _evidence_and_thesis()
    state = {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": TARGET,
        "evidence_pool": [],
        "thesis_pool": [thesis],
        "errors": [],
    }

    result = report_composer_node(state)

    report = result["research_report"]
    assert report.outcome is ReportOutcome.INCOMPLETE
    assert report.health is ReportHealth.WITH_WARNINGS
    assert report.diagnostics.integrity_warnings == (
        "thesis th_supported references missing evidence: ev_technical_001",
    )
    assert "完整性警告" in result["research_report_markdown"]


def test_excluded_negotiation_item_is_disclosed_but_not_added_to_consensus() -> None:
    state = _complete_state()
    consensus = state["consensus_recommendation"]
    debate = DebateSummary(
        rounds=3,
        status=DebateStatus.PARTIAL_CONSENSUS,
        aggressive_original_recommendation_id="rec_aggressive",
        conservative_original_recommendation_id="rec_conservative",
        excluded_item_ids=["item_excluded_valuation"],
        remaining_disagreements=["双方没有就估值上限达成一致。"],
    )
    state["consensus_recommendation"] = RecommendationRecord.model_validate(
        {**consensus.model_dump(), "debate": debate.model_dump()}
    )
    accepted = consensus.proposal_items[0]
    excluded = ProposalItem(
        item_id="item_excluded_valuation",
        target=TARGET,
        decision_dimension=DecisionDimension.VALUATION,
        conflict_group="STOCK:000001.SZ:VALUATION",
        proposer=PortfolioManager.CONSERVATIVE,
        proposal="仅在市净率低于特定水平时买入",
        supporting_thesis_ids=["th_supported"],
        evaluations=[
            ProposalEvaluation(
                manager=PortfolioManager.CONSERVATIVE,
                support_score=0.75,
                reason="需要估值安全边际",
            ),
            ProposalEvaluation(
                manager=PortfolioManager.AGGRESSIVE,
                support_score=-0.5,
                reason="阈值过于保守",
            ),
        ],
        status=ProposalStatus.EXCLUDED,
    )
    state["negotiation_proposal_pool"] = NegotiationProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=TARGET,
        aggressive_recommendation_id="rec_aggressive",
        conservative_recommendation_id="rec_conservative",
        proposal_items=(accepted, excluded),
    )
    state["debate_round"] = 3

    result = report_composer_node(state)

    report = result["research_report"]
    disclosure = report.recommendations.disagreement
    assert disclosure.excluded_item_ids == ("item_excluded_valuation",)
    assert disclosure.excluded_items == (excluded,)
    assert disclosure.remaining_disagreements == ("双方没有就估值上限达成一致。",)
    assert "NEGOTIATION_POOL" in disclosure.derived_from
    assert {
        item.item_id for item in report.recommendations.consensus.proposal_items
    } == {"item_consensus_action"}
    markdown = result["research_report_markdown"]
    assert "被排除条目详情" in markdown
    assert "仅在市净率低于特定水平时买入" in markdown


def test_report_composition_is_idempotent_and_changes_with_source_state() -> None:
    state = _complete_state()
    first = report_composer_node(state)

    replay = {**state, **first}
    assert report_composer_node(replay) == {}

    changed = {**replay, "errors": ["late diagnostic"]}
    second = report_composer_node(changed)
    assert second["research_report"].report_id != first["research_report"].report_id
    assert second["research_report"].health is ReportHealth.WITH_ERRORS


def test_missing_initialization_scope_cannot_generate_report() -> None:
    result = report_composer_node({"errors": []})

    assert "research_report" not in result
    assert result["errors"] == [
        "ReportComposerNode skipped: run_id, as_of and target are required"
    ]


def test_report_schema_forbids_unknown_fields() -> None:
    report = report_composer_node(_complete_state())["research_report"]

    payload = report.model_dump()
    payload["unexpected"] = True
    try:
        ResearchReport.model_validate(payload)
    except ValidationError as exc:
        assert "unexpected" in str(exc)
    else:  # pragma: no cover - protects the strict schema contract
        raise AssertionError("ResearchReport should reject unknown fields")


def test_report_composer_is_the_only_main_graph_end_predecessor() -> None:
    """成功、正常无共识和失败关闭分支都不能绕过报告终点。"""

    dependency = object()
    graph = build_research_graph(
        lead_research_strategist_model=dependency,  # type: ignore[arg-type]
        thesis_validation_model=dependency,  # type: ignore[arg-type]
        aggressive_portfolio_manager_model=dependency,  # type: ignore[arg-type]
        conservative_portfolio_manager_model=dependency,  # type: ignore[arg-type]
        aggressive_cross_review_model=dependency,  # type: ignore[arg-type]
        conservative_cross_review_model=dependency,  # type: ignore[arg-type]
        aggressive_negotiation_model=dependency,  # type: ignore[arg-type]
        conservative_negotiation_model=dependency,  # type: ignore[arg-type]
        consensus_assembly_model=dependency,  # type: ignore[arg-type]
    )

    end_sources = {
        edge.source for edge in graph.get_graph().edges if edge.target == "__end__"
    }
    assert end_sources == {"compose_report"}


def _complete_state() -> dict:
    evidence, thesis = _evidence_and_thesis()
    aggressive = _independent_recommendation(RecommendationProfile.AGGRESSIVE)
    conservative = _independent_recommendation(RecommendationProfile.CONSERVATIVE)
    consensus_item = ProposalItem(
        item_id="item_consensus_action",
        target=TARGET,
        decision_dimension=DecisionDimension.ACTION,
        conflict_group="STOCK:000001.SZ:ACTION",
        proposer=PortfolioManager.AGGRESSIVE,
        proposal="维持小幅超配",
        supporting_thesis_ids=[thesis.thesis_id],
        evaluations=[
            ProposalEvaluation(
                manager=PortfolioManager.AGGRESSIVE,
                support_score=0.75,
                reason="趋势和基本面共同支持",
            ),
            ProposalEvaluation(
                manager=PortfolioManager.CONSERVATIVE,
                support_score=0.25,
                reason="在控制仓位的前提下接受",
            ),
        ],
        status=ProposalStatus.AGREED,
    )
    consensus = RecommendationRecord(
        recommendation_id="rec_consensus",
        run_id=RUN_ID,
        as_of=AS_OF,
        profile=RecommendationProfile.CONSENSUS,
        target=TARGET,
        action=RecommendationAction.OVERWEIGHT,
        horizon="1至3个月",
        confidence=0.8,
        supporting_thesis_ids=[thesis.thesis_id],
        summary="双方同意在控制仓位的前提下小幅超配。",
        risk_summary="若趋势反转则降低仓位。",
        proposal_items=[consensus_item],
        debate=DebateSummary(
            rounds=1,
            status=DebateStatus.AGREED,
            aggressive_original_recommendation_id=aggressive.recommendation_id,
            conservative_original_recommendation_id=conservative.recommendation_id,
        ),
        generated_by="ConsensusRecommendationAssemblerNode",
        created_at=AS_OF,
    )
    return {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": TARGET,
        "evidence_pool": [evidence],
        "thesis_pool": [thesis],
        "aggressive_recommendation": aggressive,
        "conservative_recommendation": conservative,
        "consensus_recommendation": consensus,
        "errors": [],
    }


def _evidence_and_thesis() -> tuple[EvidenceRecord, ThesisRecord]:
    evidence = EvidenceRecord(
        evidence_id="ev_technical_001",
        run_id=RUN_ID,
        target=TARGET,
        domain=EvidenceDomain.TECHNICAL,
        as_of=AS_OF,
        title="价格站上中期均线",
        description="收盘价连续三个交易日位于二十日均线上方。",
        source_refs=[
            SourceReference(
                provider="primary",
                interface="daily",
                record_key="000001.SZ:20260826",
                published_at=AS_OF,
                url="https://example.test/daily/000001.SZ",
            )
        ],
        verification_status=VerificationStatus.VERIFIED,
        tags=["趋势"],
        collected_by="TechnicalResearchAnalyst",
        created_at=AS_OF,
    )
    thesis = ThesisRecord(
        thesis_id="th_supported",
        run_id=RUN_ID,
        target=TARGET,
        as_of=AS_OF,
        title="中期趋势偏强",
        description="短期价格结构对中期趋势构成支持。",
        direction=ThesisDirection.BULLISH,
        horizon="1至3个月",
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent="LeadResearchStrategist",
        ),
        validation=ThesisValidation(
            status=ThesisValidationStatus.SUPPORTED,
            confidence=0.8,
            round=1,
        ),
        supporting_evidence_ids=[evidence.evidence_id],
        reasoning_summary="证据对观点构成直接支持。",
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )
    return evidence, thesis


def _independent_recommendation(profile: RecommendationProfile) -> RecommendationRecord:
    if profile is RecommendationProfile.AGGRESSIVE:
        manager = PortfolioManager.AGGRESSIVE
        recommendation_id = "rec_aggressive"
        item_id = "item_aggressive_action"
        action = RecommendationAction.BUY
    else:
        manager = PortfolioManager.CONSERVATIVE
        recommendation_id = "rec_conservative"
        item_id = "item_conservative_action"
        action = RecommendationAction.HOLD
    item = ProposalItem(
        item_id=item_id,
        target=TARGET,
        decision_dimension=DecisionDimension.ACTION,
        conflict_group="STOCK:000001.SZ:ACTION",
        proposer=manager,
        proposal=f"{manager.value} 的独立动作建议",
        supporting_thesis_ids=["th_supported"],
        evaluations=[
            ProposalEvaluation(
                manager=manager,
                support_score=0.75,
                reason="基于已查证观点形成",
            )
        ],
    )
    return RecommendationRecord(
        recommendation_id=recommendation_id,
        run_id=RUN_ID,
        as_of=AS_OF,
        profile=profile,
        target=TARGET,
        action=action,
        horizon="1至3个月",
        confidence=0.8,
        supporting_thesis_ids=["th_supported"],
        summary=f"{manager.value} 的原始摘要",
        risk_summary="控制单一标的仓位。",
        proposal_items=[item],
        generated_by=manager.value,
        created_at=AS_OF,
    )
