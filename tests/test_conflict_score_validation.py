"""竞争决策槽评分规则的确定性校验测试。"""

from datetime import datetime

import pytest

from stock_research_agent.agents.debate import (
    ConflictScoreViolation,
    CrossReviewApplicationRunSummary,
    CrossReviewedProposalPool,
    CrossReviewEvaluationDraft,
    NormalizedProposalPool,
    PortfolioCrossReviewRecord,
)
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    TargetType,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.graph.nodes.conflict_score_validation import (
    build_conflict_score_validator_node,
    route_after_conflict_score_validation,
)

RUN_ID = "run_20260825_conflict_score_001"
AS_OF = datetime.fromisoformat("2026-08-25T19:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")


@pytest.mark.parametrize(
    ("own_score", "counterpart_score"),
    [
        (0.25, -0.25),
        (0.25, -0.5),
        (0.5, -0.5),
        (0.5, -0.75),
        (0.75, -0.75),
        (0.75, -1.0),
        (1.0, -1.0),
    ],
)
def test_validator_accepts_every_documented_conflict_mapping(
    own_score: float,
    counterpart_score: float,
) -> None:
    state = _reviewed_state(
        aggressive_own_score=own_score,
        aggressive_cross_score=counterpart_score,
    )

    result = build_conflict_score_validator_node()(state)

    report = result["conflict_score_validation_report"]
    assert report.valid is True
    assert report.stop_reason == "valid"
    assert report.violations == ()


@pytest.mark.parametrize(
    ("own_score", "counterpart_score", "rule_code"),
    [
        (0.25, 0.25, "CONFLICT_GROUP_SUM_POSITIVE"),
        (0.25, 0.0, "CONFLICT_GROUP_SUM_POSITIVE"),
        (0.5, -0.25, "CONFLICT_GROUP_SUM_POSITIVE"),
        (0.5, 0.0, "CONFLICT_GROUP_SUM_POSITIVE"),
        (0.75, -0.5, "CONFLICT_GROUP_SUM_POSITIVE"),
        (1.0, 0.0, "CONFLICT_GROUP_SUM_POSITIVE"),
        (1.0, -0.75, "CONFLICT_GROUP_SUM_POSITIVE"),
    ],
)
def test_validator_reports_one_primary_violation_per_manager_and_pair(
    own_score: float,
    counterpart_score: float,
    rule_code: str,
) -> None:
    state = _reviewed_state(
        aggressive_own_score=own_score,
        aggressive_cross_score=counterpart_score,
    )

    result = build_conflict_score_validator_node()(state)

    report = result["conflict_score_validation_report"]
    assert report.valid is False
    assert report.stop_reason == "retry_required"
    assert report.invalid_managers == (PortfolioManager.AGGRESSIVE,)
    assert len(report.violations) == 1
    assert report.violations[0].rule_code == rule_code


def test_validator_allows_negative_one_with_or_without_hard_veto() -> None:
    for hard_veto in (False, True):
        state = _reviewed_state(
            aggressive_own_score=1.0,
            aggressive_cross_score=-1.0,
            aggressive_hard_veto=hard_veto,
        )

        report = build_conflict_score_validator_node()(state)["conflict_score_validation_report"]

        assert report.valid is True


def test_positive_cross_scores_are_allowed_when_items_do_not_conflict() -> None:
    state = _reviewed_state(
        aggressive_cross_score=0.75,
        conservative_cross_score=0.75,
        conflicting=False,
    )

    report = build_conflict_score_validator_node()(state)["conflict_score_validation_report"]

    assert report.valid is True


def test_validator_prevents_two_mutually_exclusive_items_from_both_passing() -> None:
    state = _reviewed_state(
        aggressive_cross_score=0.25,
        conservative_cross_score=0.25,
    )

    aggressive_item, conservative_item = state["cross_reviewed_proposal_pool"].proposal_items
    assert sum(e.support_score for e in aggressive_item.evaluations) > 0
    assert sum(e.support_score for e in conservative_item.evaluations) > 0

    report = build_conflict_score_validator_node()(state)["conflict_score_validation_report"]

    assert report.stop_reason == "retry_required"
    assert report.invalid_managers == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    assert tuple(violation.manager for violation in report.violations) == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    assert all(
        violation.rule_code == "CONFLICT_GROUP_SUM_POSITIVE"
        for violation in report.violations
    )


def test_validator_rejects_review_record_that_differs_from_applied_pool() -> None:
    state = _reviewed_state()
    aggressive = state["aggressive_cross_review"]
    changed_evaluation = aggressive.evaluations[0].model_copy(update={"support_score": 0.0})
    state["aggressive_cross_review"] = aggressive.model_copy(
        update={"evaluations": (changed_evaluation,)}
    )

    result = build_conflict_score_validator_node()(state)
    report = result["conflict_score_validation_report"]

    assert report.valid is False
    assert report.stop_reason == "invalid_state"
    assert report.invalid_managers == ()
    assert report.violations == ()
    assert "applied evaluation does not match current review" in result["errors"][0]


def test_validation_routes_valid_retry_and_exhausted_results() -> None:
    valid_state = _reviewed_state()
    valid_state.update(build_conflict_score_validator_node()(valid_state))
    assert route_after_conflict_score_validation(valid_state) == "valid"

    retry_state = _reviewed_state(aggressive_cross_score=0.25)
    retry_state.update(build_conflict_score_validator_node()(retry_state))
    assert route_after_conflict_score_validation(retry_state) == "retry"

    exhausted_state = _reviewed_state(
        aggressive_cross_score=0.25,
        aggressive_attempt=3,
    )
    exhausted_state.update(build_conflict_score_validator_node()(exhausted_state))
    assert route_after_conflict_score_validation(exhausted_state) == "failed"


def test_tightened_attempt_limit_revalidates_an_existing_retry_report() -> None:
    state = _reviewed_state(
        aggressive_cross_score=0.25,
        aggressive_attempt=2,
    )
    first = build_conflict_score_validator_node(max_attempts=3)(state)
    assert first["conflict_score_validation_report"].stop_reason == "retry_required"

    tightened = build_conflict_score_validator_node(max_attempts=2)({**state, **first})

    assert tightened["conflict_score_validation_report"].stop_reason == ("retry_exhausted")


def test_third_invalid_attempt_is_exhausted_and_idempotent() -> None:
    state = _reviewed_state(
        aggressive_cross_score=0.25,
        aggressive_attempt=3,
    )
    node = build_conflict_score_validator_node(max_attempts=3)

    result = node(state)
    report = result["conflict_score_validation_report"]

    assert report.stop_reason == "retry_exhausted"
    assert report.aggressive_review_attempt == 3
    assert result["errors"] == [
        "ConflictScoreValidatorNode exhausted cross-review correction attempts: "
        "AggressivePortfolioManager"
    ]
    assert node({**state, **result}) == {}


def _reviewed_state(
    *,
    aggressive_own_score: float = 0.5,
    conservative_own_score: float = 0.5,
    aggressive_cross_score: float = -0.75,
    conservative_cross_score: float = -0.75,
    aggressive_hard_veto: bool = False,
    aggressive_attempt: int = 1,
    conservative_attempt: int = 1,
    conflicting: bool = True,
):
    aggressive_item_id = "item_aggressive_action"
    conservative_item_id = "item_conservative_action"
    aggressive_dimension = DecisionDimension.ACTION
    conservative_dimension = DecisionDimension.ACTION if conflicting else DecisionDimension.HORIZON
    aggressive_conflicts = [conservative_item_id] if conflicting else []
    conservative_conflicts = [aggressive_item_id] if conflicting else []

    aggressive_item = ProposalItem(
        item_id=aggressive_item_id,
        target=MARKET,
        decision_dimension=aggressive_dimension,
        conflict_group=f"MARKET:A_SHARE:{aggressive_dimension.value}",
        conflicts_with=aggressive_conflicts,
        proposer=PortfolioManager.AGGRESSIVE,
        proposal="进取经理的原始建议。",
        supporting_thesis_ids=["th_conflict_score_001"],
        evaluations=[
            ProposalEvaluation(
                manager=PortfolioManager.AGGRESSIVE,
                support_score=aggressive_own_score,
                reason="进取经理初始坚持分。",
            ),
            ProposalEvaluation(
                manager=PortfolioManager.CONSERVATIVE,
                support_score=conservative_cross_score,
                reason="防御经理对进取建议的评分。",
            ),
        ],
        status=ProposalStatus.PROPOSED,
    )
    conservative_item = ProposalItem(
        item_id=conservative_item_id,
        target=MARKET,
        decision_dimension=conservative_dimension,
        conflict_group=f"MARKET:A_SHARE:{conservative_dimension.value}",
        conflicts_with=conservative_conflicts,
        proposer=PortfolioManager.CONSERVATIVE,
        proposal="防御经理的原始建议。",
        supporting_thesis_ids=["th_conflict_score_001"],
        evaluations=[
            ProposalEvaluation(
                manager=PortfolioManager.CONSERVATIVE,
                support_score=conservative_own_score,
                reason="防御经理初始坚持分。",
            ),
            ProposalEvaluation(
                manager=PortfolioManager.AGGRESSIVE,
                support_score=aggressive_cross_score,
                hard_veto=aggressive_hard_veto,
                reason="进取经理对防御建议的评分。",
            ),
        ],
        status=ProposalStatus.PROPOSED,
    )
    aggressive_review = _review_record(
        manager=PortfolioManager.AGGRESSIVE,
        item_id=conservative_item_id,
        score=aggressive_cross_score,
        hard_veto=aggressive_hard_veto,
        attempt=aggressive_attempt,
        own_item_id=aggressive_item_id,
    )
    conservative_review = _review_record(
        manager=PortfolioManager.CONSERVATIVE,
        item_id=aggressive_item_id,
        score=conservative_cross_score,
        hard_veto=False,
        attempt=conservative_attempt,
        own_item_id=conservative_item_id,
    )
    pool = CrossReviewedProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_conflict_score",
        conservative_recommendation_id="rec_conservative_conflict_score",
        proposal_items=(aggressive_item, conservative_item),
    )
    normalized_pool = NormalizedProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_conflict_score",
        conservative_recommendation_id="rec_conservative_conflict_score",
        proposal_items=(
            aggressive_item.model_copy(
                deep=True,
                update={"evaluations": [aggressive_item.evaluations[0]]},
            ),
            conservative_item.model_copy(
                deep=True,
                update={"evaluations": [conservative_item.evaluations[0]]},
            ),
        ),
    )
    return {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": MARKET,
        "aggressive_cross_review": aggressive_review,
        "conservative_cross_review": conservative_review,
        "normalized_proposal_pool": normalized_pool,
        "cross_reviewed_proposal_pool": pool,
        "cross_review_application_run_summary": CrossReviewApplicationRunSummary(
            input_proposal_count=2,
            output_proposal_count=2,
            applied_evaluation_count=2,
            aggressive_review_attempt=aggressive_attempt,
            conservative_review_attempt=conservative_attempt,
            stop_reason="complete",
        ),
        "errors": [],
    }


def _review_record(
    *,
    manager: PortfolioManager,
    item_id: str,
    score: float,
    hard_veto: bool,
    attempt: int,
    own_item_id: str,
) -> PortfolioCrossReviewRecord:
    current = CrossReviewEvaluationDraft(
        item_id=item_id,
        support_score=score,
        hard_veto=hard_veto,
        reason=(
            "进取经理对防御建议的评分。"
            if manager is PortfolioManager.AGGRESSIVE
            else "防御经理对进取建议的评分。"
        ),
        modification_suggestion=None,
    )
    previous = ()
    feedback = ()
    if attempt > 1:
        previous = (current.model_copy(update={"support_score": 0.25, "hard_veto": False}),)
        feedback = (
            ConflictScoreViolation(
                rule_code="CONFLICT_GROUP_SUM_POSITIVE",
                manager=manager,
                own_item_id=own_item_id,
                counterpart_item_id=item_id,
                own_support_score=0.5,
                counterpart_support_score=0.25,
                message="互斥版本评分和必须小于或等于零。",
            ),
        )
    own_recommendation_id = (
        "rec_aggressive_conflict_score"
        if manager is PortfolioManager.AGGRESSIVE
        else "rec_conservative_conflict_score"
    )
    counterpart_recommendation_id = (
        "rec_conservative_conflict_score"
        if manager is PortfolioManager.AGGRESSIVE
        else "rec_aggressive_conflict_score"
    )
    return PortfolioCrossReviewRecord(
        run_id=RUN_ID,
        as_of=AS_OF,
        reviewer=manager,
        attempt=attempt,
        own_recommendation_id=own_recommendation_id,
        counterpart_recommendation_id=counterpart_recommendation_id,
        evaluations=(current,),
        previous_evaluations=previous,
        correction_feedback=feedback,
        created_at=AS_OF,
    )
