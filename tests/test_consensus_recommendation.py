"""最终共识建议只消费 AGREED 条目，并诚实披露被排除的分歧。"""

import asyncio
from collections.abc import Callable
from datetime import datetime

import pytest
from pydantic import ValidationError

from stock_research_agent.agents.consensus_assembly import (
    ConsensusRecommendationSynthesisDraft,
)
from stock_research_agent.agents.negotiation import NegotiationProposalPool
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    DebateStatus,
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    RecommendationAction,
    RecommendationProfile,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import (
    ProposalEvaluation,
    ProposalItem,
    RecommendationRecord,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph.nodes.consensus_gate import build_consensus_gate_node
from stock_research_agent.graph.nodes.consensus_recommendation import (
    build_consensus_recommendation_assembler_node,
)

RUN_ID = "run_20260826_consensus_assembly"
AS_OF = datetime.fromisoformat("2026-08-26T19:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
REQUIRED_DIMENSIONS = (
    DecisionDimension.ACTION,
    DecisionDimension.HORIZON,
    DecisionDimension.RISK_CONTROL,
)


class ScriptedSynthesisModel:
    def __init__(self, script: Callable | Exception | None = None) -> None:
        self.script = script or _valid_draft
        self.calls = []

    async def synthesize(self, request):
        self.calls.append(request)
        if isinstance(self.script, Exception):
            raise self.script
        return self.script(request)


def test_assembler_outputs_three_recommendations_and_exactly_agreed_items() -> None:
    state = _assembly_state()
    aggressive_before = state["aggressive_recommendation"].model_copy(deep=True)
    conservative_before = state["conservative_recommendation"].model_copy(deep=True)
    model = ScriptedSynthesisModel()

    result = asyncio.run(build_consensus_recommendation_assembler_node(model)(state))
    merged = {**state, **result}

    recommendation = result["consensus_recommendation"]
    gate = state["consensus_gate_report"]
    assert merged["aggressive_recommendation"] == aggressive_before
    assert merged["conservative_recommendation"] == conservative_before
    assert recommendation.profile is RecommendationProfile.CONSENSUS
    assert recommendation.generated_by == "ConsensusRecommendationAssemblerNode"
    assert tuple(item.item_id for item in recommendation.proposal_items) == (
        gate.agreed_item_ids
    )
    assert all(item.status is ProposalStatus.AGREED for item in recommendation.proposal_items)
    assert set(recommendation.supporting_thesis_ids) == {
        thesis_id
        for item in recommendation.proposal_items
        for thesis_id in item.supporting_thesis_ids
    }
    assert recommendation.debate.status is DebateStatus.AGREED
    assert recommendation.debate.excluded_item_ids == []
    assert recommendation.debate.remaining_disagreements == []
    assert result["consensus_assembly_run_summary"].stop_reason == "complete"

    assert len(model.calls) == 1
    request = model.calls[0]
    assert tuple(item.item_id for item in request.accepted_items) == gate.agreed_item_ids
    assert all(item.status is ProposalStatus.AGREED for item in request.accepted_items)
    assert {thesis.thesis_id for thesis in request.supporting_theses} == {
        thesis_id
        for item in request.accepted_items
        for thesis_id in item.supporting_thesis_ids
    }


def test_optional_unresolved_items_are_excluded_and_disclosed_without_arbitration() -> None:
    state = _assembly_state(unresolved_dimension=DecisionDimension.VALUATION)
    model = ScriptedSynthesisModel()

    result = asyncio.run(build_consensus_recommendation_assembler_node(model)(state))

    gate = state["consensus_gate_report"]
    recommendation = result["consensus_recommendation"]
    included_ids = {item.item_id for item in recommendation.proposal_items}
    assert included_ids == set(gate.agreed_item_ids)
    assert included_ids.isdisjoint(gate.excluded_item_ids)
    assert all(item.status is ProposalStatus.AGREED for item in recommendation.proposal_items)
    assert recommendation.debate.status is DebateStatus.PARTIAL_CONSENSUS
    assert recommendation.debate.rounds == 3
    assert tuple(recommendation.debate.excluded_item_ids) == gate.excluded_item_ids
    excluded_items = [
        item
        for item in state["negotiation_proposal_pool"].proposal_items
        if item.item_id in set(gate.excluded_item_ids)
    ]
    excluded_groups = {item.conflict_group for item in excluded_items}
    assert len(recommendation.debate.remaining_disagreements) == len(excluded_groups)
    for item_id in gate.excluded_item_ids:
        matching = [
            disagreement
            for disagreement in recommendation.debate.remaining_disagreements
            if item_id in disagreement
        ]
        assert len(matching) == 1
    disclosure = " ".join(recommendation.debate.remaining_disagreements)
    assert "仲裁" not in disclosure
    assert "ARBITRAT" not in disclosure.upper()
    assert all(item.arbitration is None for item in excluded_items)

    request = model.calls[0]
    assert {item.item_id for item in request.accepted_items}.isdisjoint(
        gate.excluded_item_ids
    )


@pytest.mark.parametrize("missing_dimension", REQUIRED_DIMENSIONS)
def test_missing_required_dimension_is_normal_no_actionable_consensus(
    missing_dimension: DecisionDimension,
) -> None:
    state = _assembly_state(unresolved_dimension=missing_dimension)
    model = ScriptedSynthesisModel()

    result = asyncio.run(build_consensus_recommendation_assembler_node(model)(state))
    merged = {**state, **result}

    assert "consensus_recommendation" not in result
    assert merged["consensus_recommendation"] is None
    assert merged["aggressive_recommendation"] is not None
    assert merged["conservative_recommendation"] is not None
    summary = result["consensus_assembly_run_summary"]
    assert summary.stop_reason == "no_actionable_consensus"
    assert summary.missing_required_dimensions == (missing_dimension,)
    assert summary.model_called is False
    assert summary.recommendation_id is None
    assert "errors" not in result
    assert model.calls == []


def test_assembler_rejects_model_attempt_to_smuggle_an_excluded_item() -> None:
    state = _assembly_state(unresolved_dimension=DecisionDimension.VALUATION)
    excluded_id = state["consensus_gate_report"].excluded_item_ids[0]

    def bad_draft(request):
        draft = _valid_draft(request)
        return draft.model_copy(
            update={
                "summary_source_item_ids": (
                    *draft.summary_source_item_ids,
                    excluded_id,
                )
            }
        )

    result = asyncio.run(
        build_consensus_recommendation_assembler_node(
            ScriptedSynthesisModel(bad_draft)
        )(state)
    )

    assert "consensus_recommendation" not in result
    assert result["consensus_assembly_run_summary"].stop_reason == "rejected_output"
    assert result["errors"] == [
        "Consensus recommendation synthesis rejected: "
        "summary sources must equal all AGREED items"
    ]


def test_assembler_rejects_stale_gate_without_calling_model() -> None:
    state = _assembly_state()
    gate = state["consensus_gate_report"]
    state["consensus_gate_report"] = gate.model_copy(
        update={"source_fingerprint": "f" * 64}
    )
    model = ScriptedSynthesisModel()

    result = asyncio.run(build_consensus_recommendation_assembler_node(model)(state))

    assert "consensus_recommendation" not in result
    assert result["consensus_assembly_run_summary"].stop_reason == "stale_input"
    assert result["errors"] == [
        "ConsensusRecommendationAssemblerNode refused a stale gate report"
    ]
    assert model.calls == []


def test_successful_assembly_is_idempotent_and_does_not_call_model_twice() -> None:
    state = _assembly_state()
    model = ScriptedSynthesisModel()
    node = build_consensus_recommendation_assembler_node(model)

    first = asyncio.run(node(state))
    replay_state = {**state, **first}
    second = asyncio.run(node(replay_state))

    assert second == {}
    assert len(model.calls) == 1


def test_consensus_record_schema_rejects_an_excluded_proposal_item() -> None:
    state = _assembly_state()
    result = asyncio.run(
        build_consensus_recommendation_assembler_node(ScriptedSynthesisModel())(state)
    )
    payload = result["consensus_recommendation"].model_dump()
    payload["proposal_items"][0]["status"] = ProposalStatus.EXCLUDED

    with pytest.raises(ValidationError, match="只能包含.*AGREED"):
        RecommendationRecord.model_validate(payload)


def _valid_draft(request) -> ConsensusRecommendationSynthesisDraft:
    by_dimension = {
        item.decision_dimension: item for item in request.accepted_items
    }
    valuation = by_dimension.get(DecisionDimension.VALUATION)
    return ConsensusRecommendationSynthesisDraft(
        action=RecommendationAction.OVERWEIGHT,
        horizon="未来一个至三个月",
        summary="双方通过的动作、期限和风险控制条目共同支持适度增配。",
        valuation_guidance=(valuation.proposal if valuation is not None else None),
        risk_summary=by_dimension[DecisionDimension.RISK_CONTROL].proposal,
        action_source_item_id=by_dimension[DecisionDimension.ACTION].item_id,
        horizon_source_item_id=by_dimension[DecisionDimension.HORIZON].item_id,
        risk_source_item_ids=(by_dimension[DecisionDimension.RISK_CONTROL].item_id,),
        summary_source_item_ids=tuple(item.item_id for item in request.accepted_items),
        valuation_source_item_ids=(
            (valuation.item_id,) if valuation is not None else ()
        ),
    )


def _assembly_state(
    *,
    unresolved_dimension: DecisionDimension | None = None,
) -> dict:
    dimensions = [*REQUIRED_DIMENSIONS]
    if unresolved_dimension is DecisionDimension.VALUATION:
        dimensions.append(DecisionDimension.VALUATION)
    debate_round = 3 if unresolved_dimension is not None else 0
    proposal_items: list[ProposalItem] = []
    for dimension in dimensions:
        proposal_items.extend(
            _negotiation_pair(
                dimension,
                unresolved=dimension is unresolved_dimension,
            )
        )
    pool = NegotiationProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_assembly",
        conservative_recommendation_id="rec_conservative_assembly",
        proposal_items=tuple(proposal_items),
    )
    base = {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": MARKET,
        "debate_round": debate_round,
        "negotiation_proposal_pool": pool,
        "aggressive_recommendation": _original_recommendation(
            PortfolioManager.AGGRESSIVE,
            dimensions,
        ),
        "conservative_recommendation": _original_recommendation(
            PortfolioManager.CONSERVATIVE,
            dimensions,
        ),
        "consensus_recommendation": None,
        "consensus_assembly_run_summary": None,
        "thesis_pool": [
            _thesis(item.supporting_thesis_ids[0]) for item in proposal_items
        ],
        "errors": [],
    }
    gate_updates = build_consensus_gate_node()(base)
    assert "errors" not in gate_updates
    return {**base, **gate_updates}


def _negotiation_pair(
    dimension: DecisionDimension,
    *,
    unresolved: bool,
) -> tuple[ProposalItem, ProposalItem]:
    suffix = dimension.value.lower()
    aggressive_id = f"item_aggressive_{suffix}"
    conservative_id = f"item_conservative_{suffix}"
    if unresolved:
        aggressive_scores = (0.5, -0.5)
        conservative_scores = (0.5, -0.5)
    else:
        aggressive_scores = (0.5, -0.25)
        conservative_scores = (0.25, -0.5)
    return (
        _negotiation_item(
            item_id=aggressive_id,
            proposer=PortfolioManager.AGGRESSIVE,
            dimension=dimension,
            conflict_id=conservative_id,
            proposer_score=aggressive_scores[0],
            counterpart_score=aggressive_scores[1],
        ),
        _negotiation_item(
            item_id=conservative_id,
            proposer=PortfolioManager.CONSERVATIVE,
            dimension=dimension,
            conflict_id=aggressive_id,
            proposer_score=conservative_scores[0],
            counterpart_score=conservative_scores[1],
        ),
    )


def _negotiation_item(
    *,
    item_id: str,
    proposer: PortfolioManager,
    dimension: DecisionDimension,
    conflict_id: str,
    proposer_score: float,
    counterpart_score: float,
) -> ProposalItem:
    counterpart = (
        PortfolioManager.CONSERVATIVE
        if proposer is PortfolioManager.AGGRESSIVE
        else PortfolioManager.AGGRESSIVE
    )
    return ProposalItem(
        item_id=item_id,
        target=MARKET,
        decision_dimension=dimension,
        conflict_group=f"MARKET:A_SHARE:{dimension.value}",
        conflicts_with=[conflict_id],
        proposer=proposer,
        proposal=f"{proposer.value} 对 {dimension.value} 的原子建议。",
        supporting_thesis_ids=[f"th_{item_id.removeprefix('item_')}_001"],
        evaluations=[
            ProposalEvaluation(
                manager=proposer,
                support_score=proposer_score,
                reason="提议方当前坚持分。",
            ),
            ProposalEvaluation(
                manager=counterpart,
                support_score=counterpart_score,
                reason="对方经理当前评分。",
            ),
        ],
    )


def _original_recommendation(
    manager: PortfolioManager,
    dimensions: list[DecisionDimension],
) -> RecommendationRecord:
    profile = {
        PortfolioManager.AGGRESSIVE: RecommendationProfile.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE: RecommendationProfile.CONSERVATIVE,
    }[manager]
    prefix = "aggressive" if manager is PortfolioManager.AGGRESSIVE else "conservative"
    items = [
        ProposalItem(
            item_id=f"item_{prefix}_{dimension.value.lower()}",
            target=MARKET,
            decision_dimension=dimension,
            conflict_group=f"MARKET:A_SHARE:{dimension.value}",
            conflicts_with=[
                f"item_{'conservative' if prefix == 'aggressive' else 'aggressive'}_"
                f"{dimension.value.lower()}"
            ],
            proposer=manager,
            proposal=f"{manager.value} 对 {dimension.value} 的原始建议。",
            supporting_thesis_ids=[
                f"th_{prefix}_{dimension.value.lower()}_001"
            ],
            evaluations=[
                ProposalEvaluation(
                    manager=manager,
                    support_score=0.5,
                    reason="原始独立建议的坚持分。",
                )
            ],
        )
        for dimension in dimensions
    ]
    return RecommendationRecord(
        recommendation_id=f"rec_{prefix}_assembly",
        run_id=RUN_ID,
        as_of=AS_OF,
        profile=profile,
        target=MARKET,
        action=(
            RecommendationAction.OVERWEIGHT
            if manager is PortfolioManager.AGGRESSIVE
            else RecommendationAction.HOLD
        ),
        horizon="未来一个至三个月",
        confidence=0.7,
        supporting_thesis_ids=[
            thesis_id for item in items for thesis_id in item.supporting_thesis_ids
        ],
        summary=f"{manager.value} 的原始独立建议。",
        risk_summary="控制总体风险暴露并设置失效条件。",
        proposal_items=items,
        generated_by=manager.value,
        created_at=AS_OF,
    )


def _thesis(thesis_id: str) -> ThesisRecord:
    return ThesisRecord(
        thesis_id=thesis_id,
        run_id=RUN_ID,
        target=MARKET,
        as_of=AS_OF,
        title=f"支持 {thesis_id} 的已验证观点",
        description="该观点已经完成查证，可作为最终建议的直接依据。",
        direction=ThesisDirection.MIXED,
        horizon="未来一个季度",
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent="LeadResearchStrategist",
        ),
        validation=ThesisValidation(
            status=ThesisValidationStatus.SUPPORTED,
            confidence=0.75,
            round=1,
        ),
        supporting_evidence_ids=[f"ev_{thesis_id.removeprefix('th_')}_001"],
        reasoning_summary="支持事实和主要风险已经完成核验。",
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )
