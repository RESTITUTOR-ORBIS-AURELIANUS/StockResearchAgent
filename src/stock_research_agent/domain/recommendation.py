"""投资建议、条目互评和辩论结果。"""

from typing import Literal

from pydantic import AwareDatetime, Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    DebateStatus,
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    RecommendationAction,
    RecommendationProfile,
)

SupportScore = Literal[-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]


class ProposalEvaluation(DomainModel):
    manager: PortfolioManager
    previous_score: SupportScore | None = None
    support_score: SupportScore
    hard_veto: bool = False
    reason: str = Field(min_length=1)
    modification_suggestion: str | None = None
    score_change_reason: str | None = None


class ArbitrationDecision(DomainModel):
    decided_by: str = "InvestmentCommitteeChair"
    decision: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    remaining_disagreement: str | None = None


class ProposalItem(DomainModel):
    item_id: str = Field(pattern=r"^item_[A-Za-z0-9_]+$")
    decision_dimension: DecisionDimension
    conflict_group: str = Field(min_length=1, max_length=100)
    conflicts_with: list[str] = Field(default_factory=list)
    proposer: PortfolioManager
    revision: int = Field(default=1, ge=1)
    proposal: str = Field(min_length=1)
    supporting_thesis_ids: list[str] = Field(min_length=1)
    evaluations: list[ProposalEvaluation] = Field(default_factory=list)
    status: ProposalStatus = ProposalStatus.PROPOSED
    arbitration: ArbitrationDecision | None = None


class DebateSummary(DomainModel):
    rounds: int = Field(default=0, ge=0, le=3)
    status: DebateStatus
    aggressive_original_recommendation_id: str
    conservative_original_recommendation_id: str
    remaining_disagreements: list[str] = Field(default_factory=list)


class RecommendationRecord(DomainModel):
    """进取、保守或委员会最终形成的一套投资建议。"""

    recommendation_id: str = Field(pattern=r"^rec_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    profile: RecommendationProfile
    target: ResearchTarget
    action: RecommendationAction
    horizon: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
    supporting_thesis_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    valuation_guidance: str | None = None
    risk_summary: str = Field(min_length=1)
    proposal_items: list[ProposalItem] = Field(default_factory=list)
    debate: DebateSummary | None = None
    generated_by: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime
    disclaimer: str = "仅供研究，不构成投资建议。"
