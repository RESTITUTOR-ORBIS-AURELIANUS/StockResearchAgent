"""最终委员会建议组装阶段使用的严格数据契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    ProposalStatus,
    RecommendationAction,
)
from stock_research_agent.domain.recommendation import ProposalItem

_ItemId = Annotated[str, Field(pattern=r"^item_[A-Za-z0-9_]+$")]
_RecommendationId = Annotated[str, Field(pattern=r"^rec_[A-Za-z0-9_]+$")]
_Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

ConsensusAssemblyStopReason = Literal[
    "complete",
    "no_actionable_consensus",
    "missing_input",
    "stale_input",
    "invalid_state",
    "context_limit_exceeded",
    "model_error",
    "rejected_output",
]


class ConsensusRecommendationSynthesisInput(DomainModel):
    """只把已经通过共识门的条目交给文字合成模型。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    debate_round: int = Field(ge=0, le=3)
    source_fingerprint: _Fingerprint
    accepted_items: tuple[ProposalItem, ...] = Field(min_length=1, max_length=32)
    supporting_theses: tuple[DecisionThesisSummary, ...] = Field(min_length=1, max_length=64)
    policy_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_accepted_catalog(self) -> ConsensusRecommendationSynthesisInput:
        item_ids = [item.item_id for item in self.accepted_items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("accepted_items 不能包含重复 item_id")
        if any(item.status is not ProposalStatus.AGREED for item in self.accepted_items):
            raise ValueError("合成模型只能看到 AGREED 条目")

        thesis_ids = [thesis.thesis_id for thesis in self.supporting_theses]
        if len(set(thesis_ids)) != len(thesis_ids):
            raise ValueError("supporting_theses 不能包含重复 thesis_id")
        expected_thesis_ids = {
            thesis_id
            for item in self.accepted_items
            for thesis_id in item.supporting_thesis_ids
        }
        if set(thesis_ids) != expected_thesis_ids:
            raise ValueError("supporting_theses 必须精确覆盖 AGREED 条目引用的观点")
        return self


class ConsensusRecommendationSynthesisDraft(DomainModel):
    """模型只能压缩顶层表达，不能决定最终纳入哪些原子建议。"""

    action: RecommendationAction = Field(
        description="仅依据已通过的总体目标 ACTION 条目归纳出的动作",
    )
    horizon: str = Field(
        min_length=1,
        max_length=100,
        description="仅依据已通过的总体目标 HORIZON 条目归纳出的期限",
    )
    summary: str = Field(
        min_length=1,
        max_length=4_000,
        description="对全部已通过条目的忠实综合，不得新增建议",
    )
    valuation_guidance: str | None = Field(default=None, max_length=2_000)
    risk_summary: str = Field(
        min_length=1,
        max_length=2_000,
        description="仅依据已通过的总体目标 RISK_CONTROL 条目归纳",
    )
    action_source_item_id: _ItemId
    horizon_source_item_id: _ItemId
    risk_source_item_ids: tuple[_ItemId, ...] = Field(min_length=1, max_length=8)
    summary_source_item_ids: tuple[_ItemId, ...] = Field(min_length=1, max_length=32)
    valuation_source_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_source_catalogs(self) -> ConsensusRecommendationSynthesisDraft:
        catalogs = {
            "risk_source_item_ids": self.risk_source_item_ids,
            "summary_source_item_ids": self.summary_source_item_ids,
            "valuation_source_item_ids": self.valuation_source_item_ids,
        }
        for name, values in catalogs.items():
            if len(set(values)) != len(values):
                raise ValueError(f"{name} 不能包含重复 item_id")
        if (self.valuation_guidance is None) != (not self.valuation_source_item_ids):
            raise ValueError(
                "valuation_guidance 与 valuation_source_item_ids 必须同时存在或同时为空"
            )
        return self


class ConsensusAssemblyRunSummary(DomainModel):
    """最终建议组装的一次不可混淆、可审计结果。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    source_fingerprint: _Fingerprint | None = None
    debate_round: int = Field(default=0, ge=0, le=3)
    agreed_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    excluded_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    rejected_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    withdrawn_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    missing_required_dimensions: tuple[DecisionDimension, ...] = Field(default=(), max_length=3)
    input_thesis_count: int = Field(default=0, ge=0, le=64)
    context_character_count: int = Field(default=0, ge=0)
    model_called: bool = False
    recommendation_id: _RecommendationId | None = None
    stop_reason: ConsensusAssemblyStopReason

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ConsensusAssemblyRunSummary:
        if self.stop_reason == "complete":
            if (
                not self.model_called
                or self.recommendation_id is None
                or not self.agreed_item_ids
                or self.missing_required_dimensions
            ):
                raise ValueError("complete 必须来自可执行共识和成功的模型调用")
        elif self.recommendation_id is not None:
            raise ValueError("只有 complete 可以生成 recommendation_id")

        if self.stop_reason == "no_actionable_consensus":
            if self.model_called:
                raise ValueError("no_actionable_consensus 不得调用模型")
            if self.agreed_item_ids and not self.missing_required_dimensions:
                raise ValueError("存在完整可执行共识时不能标记 no_actionable_consensus")
        if self.stop_reason in {"model_error", "rejected_output"} and not self.model_called:
            raise ValueError(f"{self.stop_reason} 必须发生在模型调用之后")
        return self
