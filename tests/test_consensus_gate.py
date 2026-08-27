"""确定性共识门和正式协商评分不变量测试。"""

from datetime import datetime

from stock_research_agent.agents.negotiation import NegotiationProposalPool
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    ConsensusItemOutcome,
    ConsensusRoute,
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    TargetType,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.graph.nodes.consensus_gate import (
    build_consensus_gate_node,
    route_after_consensus_gate,
)
from stock_research_agent.graph.nodes.negotiation_score_validation import (
    collect_negotiation_score_violations,
    route_after_negotiation_score_validation,
    validate_negotiation_scores_node,
)

RUN_ID = "run_20260826_consensus_gate"
AS_OF = datetime.fromisoformat("2026-08-26T17:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")


def test_gate_assembles_when_each_required_dimension_has_one_agreed_winner() -> None:
    state = _state(_pool())

    result = build_consensus_gate_node()(state)

    report = result["consensus_gate_report"]
    assert report.route is ConsensusRoute.ASSEMBLE
    assert route_after_consensus_gate({**state, **result}) == "assemble"
    assert report.all_required_dimensions_resolved is True
    assert report.missing_required_dimensions == ()
    assert report.agreed_item_ids == (
        "item_aggressive_action",
        "item_aggressive_horizon",
        "item_aggressive_risk_control",
    )
    assert report.rejected_item_ids == (
        "item_conservative_action",
        "item_conservative_horizon",
        "item_conservative_risk_control",
    )
    statuses = {
        item.item_id: item.status
        for item in result["negotiation_proposal_pool"].proposal_items
    }
    assert all(
        statuses[item_id] is ProposalStatus.AGREED for item_id in report.agreed_item_ids
    )
    assert all(
        statuses[item_id] is ProposalStatus.REJECTED
        for item_id in report.rejected_item_ids
    )


def test_unresolved_required_dimension_routes_to_negotiation_before_round_limit() -> None:
    state = _state(_pool(action_mode="minimum_blocked"), debate_round=1)

    result = build_consensus_gate_node()(state)

    report = result["consensus_gate_report"]
    assert report.route is ConsensusRoute.NEGOTIATE
    assert route_after_consensus_gate({**state, **result}) == "negotiate"
    assert report.negotiating_item_ids == (
        "item_aggressive_action",
        "item_conservative_action",
    )
    assert report.missing_required_dimensions == (DecisionDimension.ACTION,)
    decisions = {decision.item_id: decision for decision in report.item_decisions}
    aggressive = decisions["item_aggressive_action"]
    assert aggressive.outcome is ConsensusItemOutcome.NEGOTIATING
    assert aggressive.combined_score == 0.5
    assert aggressive.minimum_score == -0.5
    assert "MINIMUM_SCORE_BELOW_NEGATIVE_QUARTER" in aggressive.reason_codes


def test_final_round_excludes_every_unresolved_item_and_routes_to_assembly() -> None:
    state = _state(_pool(action_mode="unresolved"), debate_round=3)

    result = build_consensus_gate_node()(state)

    report = result["consensus_gate_report"]
    assert report.route is ConsensusRoute.ASSEMBLE
    assert route_after_consensus_gate({**state, **result}) == "assemble"
    assert report.negotiating_item_ids == ()
    assert report.excluded_item_ids == (
        "item_aggressive_action",
        "item_conservative_action",
    )
    assert all(
        decision.outcome is ConsensusItemOutcome.EXCLUDED
        for decision in report.item_decisions
        if decision.item_id in report.excluded_item_ids
    )
    assert all(
        item.status is ProposalStatus.EXCLUDED
        for item in result["negotiation_proposal_pool"].proposal_items
        if item.item_id in report.excluded_item_ids
    )
    assert report.missing_required_dimensions == (DecisionDimension.ACTION,)


def test_frozen_agreed_item_rejects_its_conflicting_sibling_and_is_idempotent() -> None:
    pool = _pool(action_mode="unresolved")
    items = list(pool.proposal_items)
    action_winner = next(item for item in items if item.item_id == "item_aggressive_action")
    winner_index = items.index(action_winner)
    items[winner_index] = action_winner.model_copy(
        deep=True,
        update={"status": ProposalStatus.AGREED},
    )
    frozen_pool = pool.model_copy(deep=True, update={"proposal_items": tuple(items)})
    state = _state(frozen_pool, debate_round=1)

    first = build_consensus_gate_node()(state)

    report = first["consensus_gate_report"]
    decisions = {decision.item_id: decision for decision in report.item_decisions}
    assert decisions["item_aggressive_action"].outcome is ConsensusItemOutcome.AGREED
    assert decisions["item_aggressive_action"].reason_codes == ("FROZEN_AGREED",)
    assert decisions["item_conservative_action"].outcome is ConsensusItemOutcome.REJECTED
    assert decisions["item_conservative_action"].reason_codes == (
        "CONFLICTING_ITEM_AGREED",
    )
    assert "item_conservative_action" not in report.negotiating_item_ids

    replay_state = {**state, **first}
    assert build_consensus_gate_node()(replay_state) == {}


def test_gate_fails_closed_when_formal_scores_break_conflict_sum_rule() -> None:
    state = _state(_pool(action_mode="invalid_sum"), debate_round=1)

    result = build_consensus_gate_node()(state)

    assert result["consensus_gate_report"] is None
    assert "negotiation_proposal_pool" not in result
    assert result["errors"] == [
        "ConsensusGateNode skipped: current pool violates the mutual-exclusion score rule"
    ]
    assert route_after_consensus_gate({**state, **result}) == "failed"


def test_formal_score_validator_accepts_zero_sum_boundary_and_is_idempotent() -> None:
    pool = _pool(action_mode="unresolved")
    state = _state(pool, debate_round=1)

    assert collect_negotiation_score_violations(pool.proposal_items) == ()
    first = validate_negotiation_scores_node(state)
    report = first["negotiation_score_validation_report"]
    assert report.valid is True
    assert report.stop_reason == "valid"
    assert route_after_negotiation_score_validation({**state, **first}) == "valid"
    assert validate_negotiation_scores_node({**state, **first}) == {}


def test_formal_score_validator_rejects_positive_sum_for_the_exact_manager() -> None:
    pool = _pool(action_mode="invalid_sum")
    state = _state(pool, debate_round=2)

    result = validate_negotiation_scores_node(state)

    report = result["negotiation_score_validation_report"]
    assert report.valid is False
    assert report.stop_reason == "invalid_scores"
    assert route_after_negotiation_score_validation({**state, **result}) == "failed"
    assert len(report.violations) == 1
    violation = report.violations[0]
    assert violation.manager is PortfolioManager.AGGRESSIVE
    assert {violation.left_item_id, violation.right_item_id} == {
        "item_aggressive_action",
        "item_conservative_action",
    }
    assert violation.left_score + violation.right_score > 0
    assert result["errors"] == [
        "NegotiationScoreValidatorNode rejected a formal score batch: "
        "1 conflict invariant violation(s)"
    ]


def _state(
    pool: NegotiationProposalPool,
    *,
    debate_round: int = 0,
) -> dict:
    return {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": MARKET,
        "debate_round": debate_round,
        "negotiation_proposal_pool": pool,
        "errors": [],
    }


def _pool(*, action_mode: str = "aggressive_pass") -> NegotiationProposalPool:
    items: list[ProposalItem] = []
    for dimension in (
        DecisionDimension.ACTION,
        DecisionDimension.HORIZON,
        DecisionDimension.RISK_CONTROL,
    ):
        mode = action_mode if dimension is DecisionDimension.ACTION else "aggressive_pass"
        items.extend(_pair(dimension, mode=mode))
    return NegotiationProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_gate",
        conservative_recommendation_id="rec_conservative_gate",
        proposal_items=tuple(items),
    )


def _pair(
    dimension: DecisionDimension,
    *,
    mode: str,
) -> tuple[ProposalItem, ProposalItem]:
    suffix = dimension.value.lower()
    aggressive_id = f"item_aggressive_{suffix}"
    conservative_id = f"item_conservative_{suffix}"
    if mode == "aggressive_pass":
        aggressive_scores = (0.5, -0.25)
        conservative_scores = (0.25, -0.5)
    elif mode == "unresolved":
        aggressive_scores = (0.5, -0.5)
        conservative_scores = (0.5, -0.5)
    elif mode == "minimum_blocked":
        aggressive_scores = (1.0, -0.5)
        conservative_scores = (0.5, -1.0)
    elif mode == "invalid_sum":
        aggressive_scores = (0.5, -0.25)
        conservative_scores = (0.25, 0.0)
    else:
        raise ValueError(f"unknown pair mode: {mode}")
    return (
        _item(
            item_id=aggressive_id,
            proposer=PortfolioManager.AGGRESSIVE,
            dimension=dimension,
            conflicts_with=(conservative_id,),
            proposer_score=aggressive_scores[0],
            counterpart_score=aggressive_scores[1],
        ),
        _item(
            item_id=conservative_id,
            proposer=PortfolioManager.CONSERVATIVE,
            dimension=dimension,
            conflicts_with=(aggressive_id,),
            proposer_score=conservative_scores[0],
            counterpart_score=conservative_scores[1],
        ),
    )


def _item(
    *,
    item_id: str,
    proposer: PortfolioManager,
    dimension: DecisionDimension,
    conflicts_with: tuple[str, ...],
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
        conflicts_with=list(conflicts_with),
        proposer=proposer,
        revision=1,
        proposal=f"{proposer.value} 针对 {dimension.value} 的原子建议。",
        supporting_thesis_ids=[f"th_{item_id.removeprefix('item_')}_001"],
        evaluations=[
            ProposalEvaluation(
                manager=proposer,
                support_score=proposer_score,
                reason="原提议方当前坚持分。",
            ),
            ProposalEvaluation(
                manager=counterpart,
                support_score=counterpart_score,
                reason="对方经理当前评分。",
            ),
        ],
        status=ProposalStatus.PROPOSED,
    )
