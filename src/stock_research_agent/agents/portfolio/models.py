"""进取型与防御型投资组合经理共同使用的输入、输出和运行摘要。"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain import ResearchTarget, ThesisRecord
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    RecommendationAction,
    RecommendationProfile,
    ThesisDirection,
    ThesisValidationStatus,
)

_ThesisId = Annotated[str, Field(pattern=r"^th_[A-Za-z0-9_]+$")]

# 独立方案只应包含经理自己愿意主张的条目。完整的 -1 到 1 评分范围留给交叉评审阶段。
InitialInsistenceScore = Literal[0.25, 0.5, 0.75, 1.0]
PortfolioRecommendationStopReason = Literal[
    "complete",
    "no_decision_theses",
    "thesis_limit_exceeded",
    "context_limit_exceeded",
    "invalid_state",
    "model_error",
    "rejected_output",
]


class PortfolioRecommendationLimits(DomainModel):
    """投资建议阶段的确定性输入与上下文硬限制。"""

    max_input_theses: int = Field(default=32, ge=1, le=64)
    max_context_characters: int = Field(default=120_000, ge=1_000, le=1_000_000)


class DecisionThesisSummary(DomainModel):
    """供经理决策使用的已完成查证观点摘要。"""

    thesis_id: _ThesisId
    target: ResearchTarget
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2_000)
    direction: ThesisDirection
    horizon: str = Field(min_length=1, max_length=100)
    validation_status: ThesisValidationStatus
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)
    contradicting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)
    reasoning_summary: str = Field(min_length=1, max_length=4_000)
    remaining_questions: tuple[str, ...] = Field(default=(), max_length=12)
    catalysts: tuple[str, ...] = Field(default=(), max_length=12)
    invalidation_conditions: tuple[str, ...] = Field(default=(), max_length=12)

    @classmethod
    def from_record(cls, thesis: ThesisRecord) -> "DecisionThesisSummary":
        if thesis.validation.confidence is None or thesis.reasoning_summary is None:
            raise ValueError("只有完成查证且具有 reasoning_summary 的观点才能进入投资建议")
        return cls(
            thesis_id=thesis.thesis_id,
            target=thesis.target,
            title=thesis.title,
            description=thesis.description,
            direction=thesis.direction,
            horizon=thesis.horizon,
            validation_status=thesis.validation.status,
            confidence=thesis.validation.confidence,
            supporting_evidence_ids=tuple(thesis.supporting_evidence_ids),
            contradicting_evidence_ids=tuple(thesis.contradicting_evidence_ids),
            reasoning_summary=thesis.reasoning_summary,
            remaining_questions=tuple(thesis.missing_questions),
            catalysts=tuple(thesis.catalysts),
            invalidation_conditions=tuple(thesis.invalidation_conditions),
        )


class PortfolioRecommendationInput(DomainModel):
    """一位经理单次生成独立方案时得到的完整、冻结输入。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    manager: PortfolioManager
    profile: RecommendationProfile
    theses: tuple[DecisionThesisSummary, ...] = Field(min_length=1, max_length=64)
    eligible_supporting_thesis_ids: tuple[_ThesisId, ...] = Field(
        min_length=1,
        max_length=64,
        description="唯一允许 proposal_items 引用为直接支持依据的观点 ID",
    )
    policy_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_manager_and_thesis_catalog(self) -> "PortfolioRecommendationInput":
        expected_profile = {
            PortfolioManager.AGGRESSIVE: RecommendationProfile.AGGRESSIVE,
            PortfolioManager.CONSERVATIVE: RecommendationProfile.CONSERVATIVE,
        }[self.manager]
        if self.profile is not expected_profile:
            raise ValueError("manager 与 profile 不匹配")

        thesis_ids = [thesis.thesis_id for thesis in self.theses]
        if len(set(thesis_ids)) != len(thesis_ids):
            raise ValueError("theses 中不能出现重复 thesis_id")
        completed_statuses = {
            ThesisValidationStatus.SUPPORTED,
            ThesisValidationStatus.REFUTED,
            ThesisValidationStatus.MIXED,
            ThesisValidationStatus.INCONCLUSIVE,
        }
        if any(thesis.validation_status not in completed_statuses for thesis in self.theses):
            raise ValueError("投资建议输入只能包含已经完成查证的观点")

        expected_eligible = {
            thesis.thesis_id
            for thesis in self.theses
            if thesis.validation_status
            in {ThesisValidationStatus.SUPPORTED, ThesisValidationStatus.MIXED}
        }
        actual_eligible = set(self.eligible_supporting_thesis_ids)
        if len(actual_eligible) != len(self.eligible_supporting_thesis_ids):
            raise ValueError("eligible_supporting_thesis_ids 不能重复")
        if actual_eligible != expected_eligible:
            raise ValueError("eligible_supporting_thesis_ids 必须精确对应 SUPPORTED/MIXED 观点")
        return self


class RecommendationProposalDraft(DomainModel):
    """一条可以独立评分、修改、接受或从委员会建议中排除的原子建议。"""

    target: ResearchTarget = Field(
        description="该条原子建议直接针对的市场、板块或 A 股股票",
    )
    decision_dimension: DecisionDimension = Field(
        description="该条建议所回答的决策维度",
    )
    proposal: str = Field(
        min_length=1,
        max_length=2_000,
        description="完整的自然语言建议；应写明动作、条件或约束，不把观点当成事实",
    )
    supporting_thesis_ids: tuple[_ThesisId, ...] = Field(
        min_length=1,
        max_length=16,
        description="直接支撑本条建议的已完成查证观点 ID",
    )
    insistence_score: InitialInsistenceScore = Field(
        description=(
            "提议方对本条建议进入最终方案的坚持程度；独立方案只允许正分：0.25/0.5/0.75/1.0"
        ),
    )
    score_reason: str = Field(
        min_length=1,
        max_length=1_200,
        description="解释坚持分，必须联系收益、风险或证据强弱",
    )

    @model_validator(mode="after")
    def validate_thesis_ids(self) -> "RecommendationProposalDraft":
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("supporting_thesis_ids 不能重复")
        return self


class PortfolioRecommendationDraft(DomainModel):
    """一位投资组合经理的一套独立建议；不包含系统生成字段或对手评分。"""

    action: RecommendationAction = Field(
        description="整套方案对当前研究目标的总体动作",
    )
    horizon: str = Field(
        min_length=1,
        max_length=100,
        description="建议成立和执行所对应的投资期限",
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="对整套判断可靠性的确信程度，不是上涨概率或预期收益率",
    )
    summary: str = Field(
        min_length=1,
        max_length=4_000,
        description="整套建议的自然语言摘要，区分已验证观点、推断和执行条件",
    )
    valuation_guidance: str | None = Field(
        default=None,
        max_length=2_000,
        description="可选估值或价格条件；证据不足时必须为空，不得捏造目标价",
    )
    risk_summary: str = Field(
        min_length=1,
        max_length=2_000,
        description="主要风险、失效条件及风险控制重点的文字说明",
    )
    proposal_items: tuple[RecommendationProposalDraft, ...] = Field(
        min_length=3,
        max_length=16,
        description="供后续逐条交叉评分与协商的原子建议",
    )

    @model_validator(mode="after")
    def validate_atomic_proposals(self) -> "PortfolioRecommendationDraft":
        decision_keys = [
            (item.target.type, item.target.code, item.decision_dimension)
            for item in self.proposal_items
        ]
        if len(set(decision_keys)) != len(decision_keys):
            raise ValueError("同一目标和决策维度只能提出一条独立建议")

        required_dimensions = {
            DecisionDimension.ACTION,
            DecisionDimension.HORIZON,
            DecisionDimension.RISK_CONTROL,
        }
        actual_dimensions = {item.decision_dimension for item in self.proposal_items}
        missing = required_dimensions - actual_dimensions
        if missing:
            names = ", ".join(sorted(dimension.value for dimension in missing))
            raise ValueError(f"独立方案缺少必要的原子决策维度: {names}")
        return self


class PortfolioRecommendationRunSummary(DomainModel):
    """单个投资组合经理节点的可审计运行摘要。"""

    manager: PortfolioManager
    input_thesis_count: int = Field(ge=0)
    eligible_thesis_count: int = Field(ge=0)
    context_character_count: int = Field(ge=0)
    model_called: bool
    proposal_count: int = Field(ge=0, le=16)
    stop_reason: PortfolioRecommendationStopReason

    @model_validator(mode="after")
    def validate_call_result(self) -> "PortfolioRecommendationRunSummary":
        if not self.model_called and self.proposal_count:
            raise ValueError("未调用模型时不能产生 proposal")
        if self.stop_reason == "complete" and not self.model_called:
            raise ValueError("complete 必须来自一次模型调用")
        return self
