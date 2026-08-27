"""共识门与正式协商数据契约的确定性约束测试。"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from stock_research_agent.agents.negotiation import (
    ConsensusGateItemDecision,
    ConsensusGateReport,
    DebateScoreDraft,
    DebateScoreEntryDraft,
    NegotiationArgumentDraft,
    NegotiationLimits,
    NegotiationProposalPool,
    NegotiationScoreValidationReport,
    NegotiationScoreViolation,
    NegotiationStageRunSummary,
    ProposalRevisionDecision,
    ProposalRevisionDecisionDraft,
    ProposalRevisionSnapshot,
    ReasonExchangeDraft,
    ReasonExchangeItemDraft,
)
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    ConsensusItemOutcome,
    ConsensusRoute,
    DecisionDimension,
    NegotiationArgumentType,
    NegotiationStance,
    PortfolioManager,
    ProposalRevisionAction,
    ProposalStatus,
    TargetType,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.graph.state import merge_consensus_gate_reports

RUN_ID = "run_20260826_formal_schema"
AS_OF = datetime.fromisoformat("2026-08-26T16:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
FINGERPRINT = "a" * 64


def test_consensus_item_decision_requires_exact_derived_scores() -> None:
    valid = _gate_decision()
    assert valid.combined_score == 0.25
    assert valid.minimum_score == -0.25

    payload = valid.model_dump()
    payload["combined_score"] = 0.5
    with pytest.raises(ValidationError, match="combined_score"):
        ConsensusGateItemDecision.model_validate(payload)

    payload = valid.model_dump()
    payload["minimum_score"] = 0.0
    with pytest.raises(ValidationError, match="minimum_score"):
        ConsensusGateItemDecision.model_validate(payload)


def test_consensus_report_requires_exact_outcome_catalogs_and_route() -> None:
    decision = _gate_decision(outcome=ConsensusItemOutcome.NEGOTIATING)
    report = ConsensusGateReport(
        run_id=RUN_ID,
        as_of=AS_OF,
        debate_round=1,
        source_fingerprint=FINGERPRINT,
        item_decisions=(decision,),
        negotiating_item_ids=(decision.item_id,),
        missing_required_dimensions=(DecisionDimension.ACTION,),
        all_required_dimensions_resolved=False,
        route=ConsensusRoute.NEGOTIATE,
    )
    assert report.negotiating_item_ids == (decision.item_id,)

    payload = report.model_dump()
    payload["negotiating_item_ids"] = ()
    with pytest.raises(ValidationError, match="NEGOTIATING"):
        ConsensusGateReport.model_validate(payload)

    payload = report.model_dump()
    payload["route"] = ConsensusRoute.ASSEMBLE
    with pytest.raises(ValidationError, match="ASSEMBLE"):
        ConsensusGateReport.model_validate(payload)


def test_final_gate_allows_excluded_items_but_only_after_round_limit() -> None:
    decision = _gate_decision(outcome=ConsensusItemOutcome.EXCLUDED)
    report = ConsensusGateReport(
        run_id=RUN_ID,
        as_of=AS_OF,
        debate_round=3,
        source_fingerprint=FINGERPRINT,
        item_decisions=(decision,),
        excluded_item_ids=(decision.item_id,),
        missing_required_dimensions=(DecisionDimension.ACTION,),
        all_required_dimensions_resolved=False,
        route=ConsensusRoute.ASSEMBLE,
    )
    assert report.excluded_item_ids == (decision.item_id,)

    payload = report.model_dump()
    payload["debate_round"] = 2
    with pytest.raises(ValidationError, match="耗尽正式协商轮次"):
        ConsensusGateReport.model_validate(payload)


def test_v1_consensus_route_has_no_arbitration_value() -> None:
    with pytest.raises(ValueError):
        ConsensusRoute("ARBITRATE")


def test_negotiation_pool_enforces_symmetric_conflicts_and_evaluation_order() -> None:
    pool = _pool()
    assert len(pool.proposal_items) == 2

    payload = pool.model_dump()
    payload["proposal_items"][1]["conflicts_with"] = []
    with pytest.raises(ValidationError, match="双向对称"):
        NegotiationProposalPool.model_validate(payload)

    payload = pool.model_dump()
    payload["proposal_items"][0]["evaluations"].reverse()
    with pytest.raises(ValidationError, match="提议方、对方顺序"):
        NegotiationProposalPool.model_validate(payload)


def test_negotiation_pool_requires_positive_live_proposer_support() -> None:
    payload = _pool().model_dump()
    payload["proposal_items"][0]["evaluations"][0]["support_score"] = -0.25

    with pytest.raises(ValidationError, match="原提议方保持正向支持"):
        NegotiationProposalPool.model_validate(payload)

    payload["proposal_items"][0]["status"] = ProposalStatus.WITHDRAWN
    withdrawn_pool = NegotiationProposalPool.model_validate(payload)
    assert withdrawn_pool.proposal_items[0].status is ProposalStatus.WITHDRAWN


def test_negotiation_pool_rejects_mutually_agreed_items() -> None:
    payload = _pool().model_dump()
    for item in payload["proposal_items"]:
        item["status"] = ProposalStatus.AGREED

    with pytest.raises(ValidationError, match="两个互斥条目"):
        NegotiationProposalPool.model_validate(payload)


def test_reason_exchange_schema_rejects_duplicate_items_and_thesis_references() -> None:
    argument = NegotiationArgumentDraft(
        argument_type=NegotiationArgumentType.OBJECTION,
        content="风险收益比尚不足以支持当前方案。",
        supporting_thesis_ids=("th_reason_001",),
    )
    response = ReasonExchangeItemDraft(
        counterpart_item_id="item_conservative_action",
        counterpart_revision=1,
        related_own_item_ids=("item_aggressive_action",),
        stance=NegotiationStance.OPPOSE,
        arguments=(argument,),
        modification_suggestion="降低初始风险暴露。",
    )
    with pytest.raises(ValidationError, match="重复回应"):
        ReasonExchangeDraft(responses=(response, response))

    with pytest.raises(ValidationError, match="supporting_thesis_ids 不能重复"):
        NegotiationArgumentDraft(
            argument_type=NegotiationArgumentType.RISK_WARNING,
            content="同一观点不应重复引用。",
            supporting_thesis_ids=("th_reason_001", "th_reason_001"),
        )


def test_revision_draft_enforces_action_specific_payload() -> None:
    with pytest.raises(ValidationError, match="MODIFY 必须提供"):
        ProposalRevisionDecisionDraft(
            item_id="item_aggressive_action",
            decision=ProposalRevisionAction.MODIFY,
            revision_reason="希望修改，但遗漏了结构化修订内容。",
        )

    with pytest.raises(ValidationError, match="KEEP/WITHDRAW"):
        ProposalRevisionDecisionDraft(
            item_id="item_aggressive_action",
            decision=ProposalRevisionAction.KEEP,
            revised_proposal="KEEP 不应携带新正文。",
            revision_reason="保持原建议。",
        )


def test_revision_decision_requires_exact_change_set_and_single_version_increment() -> None:
    before = _snapshot()
    after = _snapshot(revision=2, proposal="将初始仓位降低到更审慎的区间。")
    valid = ProposalRevisionDecision(
        item_id="item_aggressive_action",
        conflict_group="MARKET:A_SHARE:ACTION",
        decision=ProposalRevisionAction.MODIFY,
        responding_to_argument_ids=("arg_revision_001",),
        before=before,
        after=after,
        revision_reason="采纳对方关于回撤风险的异议。",
        changed_fields=("proposal",),
        material_change=True,
    )
    assert valid.after.revision == valid.before.revision + 1

    payload = valid.model_dump()
    payload["changed_fields"] = []
    with pytest.raises(ValidationError, match="changed_fields"):
        ProposalRevisionDecision.model_validate(payload)

    payload = valid.model_dump()
    payload["after"]["revision"] = 3
    with pytest.raises(ValidationError, match="恰好增加一个 revision"):
        ProposalRevisionDecision.model_validate(payload)


def test_debate_score_schema_rejects_duplicate_items_and_invalid_hard_veto() -> None:
    entry = DebateScoreEntryDraft(
        item_id="item_aggressive_action",
        item_revision=2,
        support_score=-0.25,
        reason="修订后可以有保留地接受。",
        score_change_reason="仓位风险已经下降。",
    )
    with pytest.raises(ValidationError, match="重复评分"):
        DebateScoreDraft(evaluations=(entry, entry))

    with pytest.raises(ValidationError, match="support_score 必须为 -1.0"):
        DebateScoreEntryDraft(
            item_id="item_aggressive_action",
            item_revision=2,
            support_score=-0.75,
            hard_veto=True,
            reason="错误的硬否决档位。",
            score_change_reason="测试结构约束。",
        )


def test_atomic_stage_summary_enforces_subset_and_no_partial_commit() -> None:
    with pytest.raises(ValidationError, match="completed .* staged"):
        NegotiationStageRunSummary(
            stage="DEBATE_SCORE",
            debate_round=1,
            source_fingerprint=FINGERPRINT,
            requested_managers=(PortfolioManager.AGGRESSIVE,),
            completed_managers=(PortfolioManager.AGGRESSIVE,),
            stop_reason="stage_failed",
        )

    failed = NegotiationStageRunSummary(
        stage="DEBATE_SCORE",
        debate_round=1,
        source_fingerprint=FINGERPRINT,
        requested_managers=(PortfolioManager.AGGRESSIVE, PortfolioManager.CONSERVATIVE),
        called_managers=(PortfolioManager.AGGRESSIVE, PortfolioManager.CONSERVATIVE),
        staged_managers=(PortfolioManager.AGGRESSIVE,),
        completed_managers=(),
        stop_reason="stage_failed",
    )
    assert failed.completed_managers == ()


def test_score_violation_and_report_must_be_self_consistent() -> None:
    violation = NegotiationScoreViolation(
        manager=PortfolioManager.AGGRESSIVE,
        left_item_id="item_aggressive_action",
        right_item_id="item_conservative_action",
        left_score=0.5,
        right_score=-0.25,
        message="两个互斥版本的评分和大于零。",
    )
    report = NegotiationScoreValidationReport(
        run_id=RUN_ID,
        as_of=AS_OF,
        debate_round=1,
        source_fingerprint=FINGERPRINT,
        valid=False,
        violations=(violation,),
        stop_reason="invalid_scores",
    )
    assert report.violations == (violation,)

    with pytest.raises(ValidationError, match="评分和大于 0"):
        NegotiationScoreViolation(
            manager=PortfolioManager.AGGRESSIVE,
            left_item_id="item_aggressive_action",
            right_item_id="item_conservative_action",
            left_score=0.5,
            right_score=-0.5,
            message="评分和为零不是违规。",
        )

    with pytest.raises(ValidationError, match="valid 报告"):
        NegotiationScoreValidationReport(
            run_id=RUN_ID,
            as_of=AS_OF,
            debate_round=1,
            source_fingerprint=FINGERPRINT,
            valid=True,
            violations=(violation,),
            stop_reason="valid",
        )


def test_negotiation_limits_preserve_three_round_hard_cap() -> None:
    assert NegotiationLimits().max_rounds == 3
    with pytest.raises(ValidationError):
        NegotiationLimits(max_rounds=4)


def test_consensus_gate_history_is_idempotent_but_cannot_rewrite_a_round() -> None:
    decision = _gate_decision(outcome=ConsensusItemOutcome.NEGOTIATING)
    report = ConsensusGateReport(
        run_id=RUN_ID,
        as_of=AS_OF,
        debate_round=1,
        source_fingerprint=FINGERPRINT,
        item_decisions=(decision,),
        negotiating_item_ids=(decision.item_id,),
        missing_required_dimensions=(DecisionDimension.ACTION,),
        all_required_dimensions_resolved=False,
        route=ConsensusRoute.NEGOTIATE,
    )

    assert merge_consensus_gate_reports([report], [report]) == [report]
    incompatible = report.model_copy(update={"source_fingerprint": "b" * 64})
    with pytest.raises(ValueError, match="immutable audit record changed"):
        merge_consensus_gate_reports([report], [incompatible])


def _gate_decision(
    *,
    outcome: ConsensusItemOutcome = ConsensusItemOutcome.AGREED,
) -> ConsensusGateItemDecision:
    return ConsensusGateItemDecision(
        item_id="item_aggressive_action",
        item_revision=1,
        aggressive_score=0.5,
        conservative_score=-0.25,
        combined_score=0.25,
        minimum_score=-0.25,
        hard_veto=False,
        outcome=outcome,
        reason_codes=("GATE_PASSED",) if outcome is ConsensusItemOutcome.AGREED else (),
    )


def _snapshot(
    *,
    revision: int = 1,
    proposal: str = "维持适度权益暴露。",
) -> ProposalRevisionSnapshot:
    return ProposalRevisionSnapshot(
        revision=revision,
        proposal=proposal,
        supporting_thesis_ids=("th_revision_001",),
        status=ProposalStatus.NEGOTIATING,
    )


def _pool() -> NegotiationProposalPool:
    aggressive_id = "item_aggressive_action"
    conservative_id = "item_conservative_action"
    return NegotiationProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_schema",
        conservative_recommendation_id="rec_conservative_schema",
        proposal_items=(
            _item(
                aggressive_id,
                PortfolioManager.AGGRESSIVE,
                conflicts_with=(conservative_id,),
            ),
            _item(
                conservative_id,
                PortfolioManager.CONSERVATIVE,
                conflicts_with=(aggressive_id,),
            ),
        ),
    )


def _item(
    item_id: str,
    proposer: PortfolioManager,
    *,
    conflicts_with: tuple[str, ...],
) -> ProposalItem:
    counterpart = (
        PortfolioManager.CONSERVATIVE
        if proposer is PortfolioManager.AGGRESSIVE
        else PortfolioManager.AGGRESSIVE
    )
    return ProposalItem(
        item_id=item_id,
        target=MARKET,
        decision_dimension=DecisionDimension.ACTION,
        conflict_group="MARKET:A_SHARE:ACTION",
        conflicts_with=list(conflicts_with),
        proposer=proposer,
        proposal="在风险约束下维持适度权益暴露。",
        supporting_thesis_ids=[f"th_{item_id.removeprefix('item_')}_001"],
        evaluations=[
            ProposalEvaluation(
                manager=proposer,
                support_score=0.5,
                reason="原提议方保留正向支持。",
            ),
            ProposalEvaluation(
                manager=counterpart,
                support_score=-0.5,
                reason="对方经理当前反对该版本。",
            ),
        ],
        status=ProposalStatus.NEGOTIATING,
    )
