"""首席研究策略师的输入、结构化输出和运行摘要。"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain import CollectedEvidenceSummary, ResearchTarget
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import ThesisDirection

_EvidenceId = Annotated[str, Field(pattern=r"^ev_[A-Za-z0-9_]+$")]
_Question = Annotated[str, Field(min_length=1, max_length=500)]
_Condition = Annotated[str, Field(min_length=1, max_length=500)]
CandidateThesisStopReason = Literal[
    "complete",
    "no_evidence",
    "evidence_limit_exceeded",
    "context_limit_exceeded",
    "invalid_collection",
    "model_error",
]


class CandidateThesisLimits(DomainModel):
    max_candidates: int = Field(default=8, ge=1, le=8)
    max_evidence_count: int = Field(default=128, ge=1, le=512)
    max_context_characters: int = Field(default=120_000, ge=1_000, le=1_000_000)


class LeadStrategistInput(DomainModel):
    """只包含 Collector 接受的全部证据摘要，不包含原始行情行或新闻全文。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    research_target: ResearchTarget
    as_of: AwareDatetime
    counts_by_domain: dict[str, int]
    counts_by_verification_status: dict[str, int]
    counts_by_target_type: dict[str, int]
    evidence: tuple[CollectedEvidenceSummary, ...]
    policy_notes: tuple[str, ...] = ()
    max_candidates: int = Field(ge=1, le=8)


class CandidateThesisDraft(DomainModel):
    """LLM 提出的可查证猜想；不携带 ID、验证状态或置信度。"""

    target: ResearchTarget
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(
        min_length=1,
        max_length=2_000,
        description="基于证据提出的机制或趋势猜想，必须与已知事实措辞分离",
    )
    direction: ThesisDirection
    horizon: str = Field(min_length=1, max_length=100)
    supporting_evidence_ids: tuple[_EvidenceId, ...] = Field(min_length=1, max_length=24)
    contradicting_evidence_ids: tuple[_EvidenceId, ...] = Field(default=(), max_length=24)
    reasoning_summary: str = Field(min_length=1, max_length=2_000)
    missing_questions: tuple[_Question, ...] = Field(min_length=1, max_length=8)
    catalysts: tuple[_Condition, ...] = Field(default=(), max_length=8)
    invalidation_conditions: tuple[_Condition, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> "CandidateThesisDraft":
        supporting = set(self.supporting_evidence_ids)
        contradicting = set(self.contradicting_evidence_ids)
        if len(supporting) != len(self.supporting_evidence_ids):
            raise ValueError("supporting_evidence_ids 不能重复")
        if len(contradicting) != len(self.contradicting_evidence_ids):
            raise ValueError("contradicting_evidence_ids 不能重复")
        if supporting & contradicting:
            raise ValueError("同一 evidence_id 不能同时支持和反驳同一候选观点")
        return self


class CandidateThesisGeneration(DomainModel):
    candidates: tuple[CandidateThesisDraft, ...] = Field(default=(), max_length=8)
    generation_summary: str = Field(
        min_length=1,
        max_length=1_200,
        description="只总结生成了哪些候选方向及主要证据缺口，不宣布观点成立",
    )


class CandidateThesisRunSummary(DomainModel):
    input_evidence_count: int = Field(ge=0)
    context_character_count: int = Field(ge=0)
    model_called: bool
    generated_candidate_count: int = Field(ge=0)
    accepted_candidate_count: int = Field(ge=0)
    rejected_candidate_count: int = Field(ge=0)
    stop_reason: CandidateThesisStopReason
    generation_summary: str | None = Field(default=None, max_length=1_200)

    @model_validator(mode="after")
    def validate_candidate_counts(self) -> "CandidateThesisRunSummary":
        if self.generated_candidate_count != (
            self.accepted_candidate_count + self.rejected_candidate_count
        ):
            raise ValueError("generated_candidate_count 必须等于 accepted + rejected")
        if not self.model_called and self.generated_candidate_count != 0:
            raise ValueError("未调用模型时不能出现生成候选")
        return self
