"""投资建议、条目互评和辩论结果。"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

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

    @model_validator(mode="after")
    def validate_hard_veto(self) -> "ProposalEvaluation":
        if self.hard_veto and self.support_score != -1.0:
            raise ValueError("hard_veto=true 时 support_score 必须为 -1.0")
        return self


class ArbitrationDecision(DomainModel):
    decided_by: str = "InvestmentCommitteeChair"
    decision: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    remaining_disagreement: str | None = None


class ProposalItem(DomainModel):
    item_id: str = Field(pattern=r"^item_[A-Za-z0-9_]+$")
    target: ResearchTarget
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

    @model_validator(mode="after")
    def validate_item_lifecycle(self) -> "ProposalItem":
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("ProposalItem.supporting_thesis_ids 不能重复")
        if self.item_id in self.conflicts_with:
            raise ValueError("ProposalItem 不能与自己冲突")
        if len(set(self.conflicts_with)) != len(self.conflicts_with):
            raise ValueError("ProposalItem.conflicts_with 不能重复")
        managers = [evaluation.manager for evaluation in self.evaluations]
        if len(set(managers)) != len(managers):
            raise ValueError("同一经理对同一条目只能保留一份当前评价")
        if self.status is ProposalStatus.ARBITRATED and self.arbitration is None:
            raise ValueError("ARBITRATED 条目必须包含 arbitration")
        if self.status is not ProposalStatus.ARBITRATED and self.arbitration is not None:
            raise ValueError("只有 ARBITRATED 条目可以包含 arbitration")
        return self


class DebateSummary(DomainModel):
    rounds: int = Field(default=0, ge=0, le=3)
    status: DebateStatus
    aggressive_original_recommendation_id: str
    conservative_original_recommendation_id: str
    excluded_item_ids: list[str] = Field(default_factory=list)
    remaining_disagreements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_consensus_status(self) -> "DebateSummary":
        if (
            self.aggressive_original_recommendation_id
            == self.conservative_original_recommendation_id
        ):
            raise ValueError("DebateSummary 必须引用两份不同的经理原始建议")
        if len(set(self.excluded_item_ids)) != len(self.excluded_item_ids):
            raise ValueError("DebateSummary.excluded_item_ids 不能重复")
        if self.status is DebateStatus.AGREED and self.excluded_item_ids:
            raise ValueError("完全共识不能包含被排除的未决条目")
        if self.status is DebateStatus.PARTIAL_CONSENSUS:
            if not self.excluded_item_ids or not self.remaining_disagreements:
                raise ValueError("部分共识必须披露被排除条目及剩余分歧")
        return self


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
    proposal_items: list[ProposalItem] = Field(min_length=1)
    debate: DebateSummary | None = None
    generated_by: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime
    disclaimer: str = "仅供研究，不构成投资建议。"

    @model_validator(mode="after")
    def validate_record_lifecycle(self) -> "RecommendationRecord":
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("RecommendationRecord.supporting_thesis_ids 不能重复")
        item_ids = [item.item_id for item in self.proposal_items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("RecommendationRecord.proposal_items 不能包含重复 item_id")
        item_support = {
            thesis_id for item in self.proposal_items for thesis_id in item.supporting_thesis_ids
        }
        if item_support != set(self.supporting_thesis_ids):
            raise ValueError("RecommendationRecord 的观点引用必须等于全部条目引用的并集")

        independent_profiles = {
            RecommendationProfile.AGGRESSIVE: PortfolioManager.AGGRESSIVE,
            RecommendationProfile.CONSERVATIVE: PortfolioManager.CONSERVATIVE,
        }
        if self.profile in independent_profiles:
            manager = independent_profiles[self.profile]
            if self.debate is not None:
                raise ValueError("经理原始独立建议不能提前包含 debate")
            if self.generated_by != manager.value:
                raise ValueError("独立建议的 generated_by 必须对应其经理 profile")
            for item in self.proposal_items:
                if item.proposer is not manager:
                    raise ValueError("独立建议中的 proposer 必须对应其经理 profile")
                if item.status is not ProposalStatus.PROPOSED:
                    raise ValueError("经理原始独立建议中的条目必须保持 PROPOSED")
                if len(item.evaluations) != 1 or item.evaluations[0].manager is not manager:
                    raise ValueError("独立建议条目必须且只能包含提议方的初始评价")
        elif self.profile is RecommendationProfile.CONSENSUS:
            if self.debate is None:
                raise ValueError("委员会建议必须包含 debate 摘要")
            if self.generated_by != "ConsensusRecommendationAssemblerNode":
                raise ValueError("委员会建议必须由共识建议组装节点生成")
            for item in self.proposal_items:
                if item.status is not ProposalStatus.AGREED:
                    raise ValueError("委员会建议只能包含已经通过共识门的 AGREED 条目")
                if item.arbitration is not None:
                    raise ValueError("v1 委员会建议不允许包含仲裁结果")
            if set(self.debate.excluded_item_ids) & set(item_ids):
                raise ValueError("被排除的未决条目不能重新进入委员会建议")
        return self
