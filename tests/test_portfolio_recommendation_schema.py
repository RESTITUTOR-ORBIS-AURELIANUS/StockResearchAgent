"""投资组合经理共同输出 Schema 的确定性约束测试。"""

import pytest
from pydantic import ValidationError

from stock_research_agent.agents.portfolio import (
    PortfolioRecommendationDraft,
    RecommendationProposalDraft,
)
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import DecisionDimension, RecommendationAction

MARKET = ResearchTarget(type="MARKET", code="A_SHARE", name="A股市场")


def _proposal(
    dimension: DecisionDimension,
    *,
    score: float = 0.75,
    target: ResearchTarget = MARKET,
) -> RecommendationProposalDraft:
    return RecommendationProposalDraft(
        target=target,
        decision_dimension=dimension,
        proposal=f"针对 {dimension.value} 的可执行建议。",
        supporting_thesis_ids=("th_20260825_000001_001",),
        insistence_score=score,
        score_reason="已完成查证的观点能够直接支持该决策，同时保留风险边界。",
    )


def _valid_payload() -> dict:
    return {
        "action": RecommendationAction.OVERWEIGHT,
        "horizon": "未来一个至三个月",
        "confidence": 0.68,
        "summary": "已验证的盈利改善支持适度配置，但短期估值约束要求分批执行。",
        "valuation_guidance": "估值回到历史中位附近且盈利预期未下修时再增加配置。",
        "risk_summary": "盈利修复中断或资金面持续转弱时应降低风险暴露。",
        "proposal_items": (
            _proposal(DecisionDimension.ACTION),
            _proposal(DecisionDimension.HORIZON),
            _proposal(DecisionDimension.RISK_CONTROL),
        ),
    }


def test_valid_portfolio_recommendation_draft_round_trips_json() -> None:
    draft = PortfolioRecommendationDraft.model_validate(_valid_payload())

    restored = PortfolioRecommendationDraft.model_validate_json(draft.model_dump_json())

    assert restored == draft
    assert restored.proposal_items[0].insistence_score == 0.75


@pytest.mark.parametrize("score", [-1.0, -0.25, 0.0, 0.3, 1.25])
def test_initial_proposal_rejects_nonpositive_or_non_discrete_score(score: float) -> None:
    with pytest.raises(ValidationError):
        _proposal(DecisionDimension.ACTION, score=score)


def test_proposal_rejects_duplicate_supporting_thesis_ids() -> None:
    with pytest.raises(ValidationError, match="supporting_thesis_ids 不能重复"):
        RecommendationProposalDraft(
            target=MARKET,
            decision_dimension=DecisionDimension.ACTION,
            proposal="维持适度增配。",
            supporting_thesis_ids=(
                "th_20260825_000001_001",
                "th_20260825_000001_001",
            ),
            insistence_score=0.5,
            score_reason="同一观点不应重复计入。",
        )


def test_recommendation_requires_action_horizon_and_risk_control_items() -> None:
    payload = _valid_payload()
    payload["proposal_items"] = (
        _proposal(DecisionDimension.ACTION),
        _proposal(DecisionDimension.HORIZON),
        _proposal(DecisionDimension.ENTRY_STRATEGY),
    )

    with pytest.raises(ValidationError, match="RISK_CONTROL"):
        PortfolioRecommendationDraft.model_validate(payload)


def test_recommendation_rejects_duplicate_target_dimension() -> None:
    payload = _valid_payload()
    payload["proposal_items"] = (
        _proposal(DecisionDimension.ACTION),
        _proposal(DecisionDimension.HORIZON),
        _proposal(DecisionDimension.HORIZON),
    )

    with pytest.raises(ValidationError, match="同一目标和决策维度"):
        PortfolioRecommendationDraft.model_validate(payload)


def test_generated_json_schema_is_closed_and_exposes_score_enum() -> None:
    schema = PortfolioRecommendationDraft.model_json_schema()
    proposal_schema = schema["$defs"]["RecommendationProposalDraft"]

    assert schema["additionalProperties"] is False
    assert proposal_schema["additionalProperties"] is False
    assert proposal_schema["properties"]["insistence_score"] == {
        "description": (
            "提议方对本条建议进入最终方案的坚持程度；独立方案只允许正分："
            "0.25/0.5/0.75/1.0"
        ),
        "enum": [0.25, 0.5, 0.75, 1.0],
        "title": "Insistence Score",
        "type": "number",
    }


def test_schema_rejects_unknown_llm_fields() -> None:
    payload = _valid_payload()
    payload["expected_return"] = "保证上涨 20%"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PortfolioRecommendationDraft.model_validate(payload)
