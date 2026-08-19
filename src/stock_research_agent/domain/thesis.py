"""候选观点及其查证结果。"""

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
)


class ThesisOrigin(DomainModel):
    type: ThesisOriginType
    agent: str = Field(min_length=1, max_length=100)
    parent_thesis_ids: list[str] = Field(default_factory=list)


class ThesisValidation(DomainModel):
    status: ThesisValidationStatus
    confidence: float | None = Field(default=None, ge=0, le=1)
    round: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_confidence_lifecycle(self) -> "ThesisValidation":
        unfinished = {
            ThesisValidationStatus.UNVERIFIED,
            ThesisValidationStatus.UNDER_REVIEW,
        }
        if self.status in unfinished and self.confidence is not None:
            raise ValueError("未经完成查证的观点不能提前填写 confidence")
        if self.status not in unfinished and self.confidence is None:
            raise ValueError("完成查证后的观点必须填写 0 到 1 的 confidence")
        return self


class ThesisRecord(DomainModel):
    """可以被支持、反驳或保留为无法判断的投资观点。"""

    thesis_id: str = Field(pattern=r"^th_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    target: ResearchTarget
    as_of: AwareDatetime
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    direction: ThesisDirection
    horizon: str = Field(min_length=1, max_length=100)
    origin: ThesisOrigin
    validation: ThesisValidation
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str | None = None
    missing_questions: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    created_by: str = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
