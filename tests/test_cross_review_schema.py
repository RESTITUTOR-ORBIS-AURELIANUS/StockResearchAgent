"""投资组合经理首次交叉评分 Schema 的确定性约束测试。"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from stock_research_agent.agents.debate import (
    ConflictScoreViolation,
    CrossReviewEvaluationDraft,
    CrossReviewProposalContext,
    PortfolioCrossReviewDraft,
    PortfolioCrossReviewInput,
    PortfolioCrossReviewLimits,
)
from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ThesisDirection,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation

AS_OF = datetime.fromisoformat("2026-08-25T17:00:00+08:00")
MARKET = ResearchTarget(type="MARKET", code="A_SHARE", name="A股市场")


def _proposal(
    item_id: str,
    proposer: PortfolioManager,
    *,
    conflicts_with: tuple[str, ...] = (),
) -> CrossReviewProposalContext:
    return CrossReviewProposalContext(
        item_id=item_id,
        target=MARKET,
        decision_dimension=DecisionDimension.ACTION,
        conflict_group="MARKET:A_SHARE:ACTION",
        conflicts_with=conflicts_with,
        proposer=proposer,
        proposal="在核心观点没有失效时维持适度权益暴露。",
        supporting_thesis_ids=("th_20260825_000001_001",),
        proposer_insistence_score=0.75,
        proposer_score_reason="经过查证的观点支持该动作，但仍需控制回撤。",
    )


def _review_input() -> PortfolioCrossReviewInput:
    return PortfolioCrossReviewInput(
        run_id="run_20260825_000001",
        as_of=AS_OF,
        research_target=MARKET,
        reviewer=PortfolioManager.AGGRESSIVE,
        own_recommendation_id="rec_20260825_000001_aggressive",
        counterpart_recommendation_id="rec_20260825_000001_conservative",
        own_proposals=(
            _proposal(
                "item_aggressive_action",
                PortfolioManager.AGGRESSIVE,
                conflicts_with=("item_conservative_action",),
            ),
        ),
        counterpart_proposals=(
            _proposal(
                "item_conservative_action",
                PortfolioManager.CONSERVATIVE,
                conflicts_with=("item_aggressive_action",),
            ),
        ),
        theses=(
            DecisionThesisSummary(
                thesis_id="th_20260825_000001_001",
                target=MARKET,
                title="市场风险收益比改善",
                description="已完成查证的市场观点。",
                direction=ThesisDirection.BULLISH,
                horizon="未来一个季度",
                validation_status=ThesisValidationStatus.SUPPORTED,
                confidence=0.75,
                supporting_evidence_ids=("ev_market_001",),
                reasoning_summary="趋势和基本面信息共同支持当前判断。",
            ),
        ),
        eligible_supporting_thesis_ids=("th_20260825_000001_001",),
        policy_notes=("只评价对方条目，不得修改原始建议。",),
    )


def test_valid_cross_review_round_trips_and_matches_input() -> None:
    draft = PortfolioCrossReviewDraft(
        evaluations=(
            CrossReviewEvaluationDraft(
                item_id="item_conservative_action",
                support_score=-0.25,
                hard_veto=False,
                reason="方向可以接受，但仓位和确认条件应更明确。",
                modification_suggestion="补充最大仓位和观点失效后的减仓条件。",
            ),
        ),
    )

    restored = PortfolioCrossReviewDraft.model_validate_json(draft.model_dump_json())
    restored.validate_against(_review_input())

    assert restored == draft


@pytest.mark.parametrize("score", [-1.25, -0.3, 0.3, 1.25])
def test_cross_review_rejects_non_discrete_scores(score: float) -> None:
    with pytest.raises(ValidationError):
        CrossReviewEvaluationDraft(
            item_id="item_conservative_action",
            support_score=score,
            hard_veto=False,
            reason="无效离散分数。",
            modification_suggestion=None,
        )


def test_hard_veto_requires_negative_one() -> None:
    with pytest.raises(ValidationError, match="support_score 必须为 -1.0"):
        CrossReviewEvaluationDraft(
            item_id="item_conservative_action",
            support_score=-0.75,
            hard_veto=True,
            reason="尚未达到硬否决的分值约束。",
            modification_suggestion=None,
        )


def test_persisted_proposal_evaluation_preserves_hard_veto_constraint() -> None:
    with pytest.raises(ValidationError, match="support_score 必须为 -1.0"):
        ProposalEvaluation(
            manager=PortfolioManager.AGGRESSIVE,
            support_score=-0.75,
            hard_veto=True,
            reason="写入提案池后仍必须保留同一硬否决约束。",
        )


def test_cross_review_rejects_duplicate_item_ids() -> None:
    evaluation = CrossReviewEvaluationDraft(
        item_id="item_conservative_action",
        support_score=0.25,
        hard_veto=False,
        reason="可以有保留地接受。",
        modification_suggestion=None,
    )

    with pytest.raises(ValidationError, match="不能重复评价"):
        PortfolioCrossReviewDraft(evaluations=(evaluation, evaluation))


def test_cross_review_must_cover_exact_counterpart_catalog() -> None:
    draft = PortfolioCrossReviewDraft(
        evaluations=(
            CrossReviewEvaluationDraft(
                item_id="item_unknown",
                support_score=0.0,
                hard_veto=False,
                reason="不能评价目录外条目。",
                modification_suggestion=None,
            ),
        ),
    )

    with pytest.raises(ValueError, match="missing=.*item_conservative_action"):
        draft.validate_against(_review_input())


def test_review_input_rejects_wrong_proposer_and_asymmetric_conflicts() -> None:
    payload = _review_input().model_dump()
    payload["counterpart_proposals"][0]["proposer"] = PortfolioManager.AGGRESSIVE
    with pytest.raises(ValidationError, match="counterpart_proposals"):
        PortfolioCrossReviewInput.model_validate(payload)

    payload = _review_input().model_dump()
    payload["counterpart_proposals"][0]["conflicts_with"] = ()
    with pytest.raises(ValidationError, match="双向对称"):
        PortfolioCrossReviewInput.model_validate(payload)


def test_generated_schema_is_closed_and_exposes_full_score_enum() -> None:
    schema = PortfolioCrossReviewDraft.model_json_schema()
    evaluation_schema = schema["$defs"]["CrossReviewEvaluationDraft"]

    assert schema["additionalProperties"] is False
    assert evaluation_schema["additionalProperties"] is False
    assert evaluation_schema["properties"]["support_score"]["enum"] == [
        -1.0,
        -0.75,
        -0.5,
        -0.25,
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
    ]
    assert set(evaluation_schema["required"]) == {
        "item_id",
        "support_score",
        "hard_veto",
        "reason",
        "modification_suggestion",
    }


def test_schema_rejects_fields_that_would_mutate_the_original_item() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PortfolioCrossReviewDraft.model_validate(
            {
                "evaluations": [
                    {
                        "item_id": "item_conservative_action",
                        "support_score": -0.25,
                        "hard_veto": False,
                        "reason": "有保留地接受。",
                        "modification_suggestion": None,
                        "proposal": "试图擅自重写对方建议。",
                    }
                ]
            }
        )


def test_cross_review_attempt_limit_is_a_real_three_call_hard_cap() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 3"):
        PortfolioCrossReviewLimits(max_attempts=4)


def test_retry_input_requires_complete_previous_scores_and_bound_feedback() -> None:
    payload = _review_input().model_dump()
    payload["attempt"] = 2
    with pytest.raises(ValidationError, match="上一轮全部对方条目的评分"):
        PortfolioCrossReviewInput.model_validate(payload)

    previous = CrossReviewEvaluationDraft(
        item_id="item_conservative_action",
        support_score=0.25,
        hard_veto=False,
        reason="上一轮错误地同时支持了两个冲突版本。",
        modification_suggestion=None,
    )
    feedback = ConflictScoreViolation(
        rule_code="CONFLICT_GROUP_SUM_POSITIVE",
        manager=PortfolioManager.AGGRESSIVE,
        own_item_id="item_aggressive_action",
        counterpart_item_id="item_conservative_action",
        own_support_score=0.75,
        counterpart_support_score=0.25,
        message="同一经理不能同时正向支持两个冲突版本。",
    )
    payload["previous_evaluations"] = [previous.model_dump()]
    payload["validation_feedback"] = [feedback.model_dump()]

    retry_input = PortfolioCrossReviewInput.model_validate(payload)

    assert retry_input.attempt == 2
    assert retry_input.validation_feedback == (feedback,)


def test_retry_feedback_must_match_the_frozen_catalog_and_previous_score() -> None:
    payload = _review_input().model_dump()
    payload["attempt"] = 2
    payload["previous_evaluations"] = [
        CrossReviewEvaluationDraft(
            item_id="item_conservative_action",
            support_score=0.25,
            hard_veto=False,
            reason="上一轮错误评分。",
            modification_suggestion=None,
        ).model_dump()
    ]
    payload["validation_feedback"] = [
        ConflictScoreViolation(
            rule_code="CONFLICT_GROUP_SUM_POSITIVE",
            manager=PortfolioManager.AGGRESSIVE,
            own_item_id="item_aggressive_action",
            counterpart_item_id="item_conservative_action",
            own_support_score=0.75,
            counterpart_support_score=0.5,
            message="故意写入与上一轮评分不一致的反馈。",
        ).model_dump()
    ]

    with pytest.raises(ValidationError, match="必须匹配上一轮评分"):
        PortfolioCrossReviewInput.model_validate(payload)
