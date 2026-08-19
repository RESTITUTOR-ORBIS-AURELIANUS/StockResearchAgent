"""观点查证过程中产生的定向研究任务。"""

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget, TimeRange
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchPriority,
    ResearchRequestStatus,
)


class ResearchRequest(DomainModel):
    request_id: str = Field(pattern=r"^rq_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    thesis_id: str = Field(pattern=r"^th_[A-Za-z0-9_]+$")
    target: ResearchTarget
    assigned_domain: EvidenceDomain
    question: str = Field(min_length=1)
    requested_evidence: str = Field(min_length=1)
    time_range: TimeRange
    priority: ResearchPriority
    attempt: int = Field(default=1, ge=1)
    status: ResearchRequestStatus = ResearchRequestStatus.PENDING
    result_evidence_ids: list[str] = Field(default_factory=list)
    requested_by: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> "ResearchRequest":
        completed_statuses = {
            ResearchRequestStatus.COMPLETED,
            ResearchRequestStatus.NO_NEW_EVIDENCE,
            ResearchRequestStatus.FAILED,
            ResearchRequestStatus.CANCELLED_BY_BUDGET,
        }
        if self.status in completed_statuses and self.completed_at is None:
            raise ValueError("已结束的 ResearchRequest 必须填写 completed_at")
        if self.status is ResearchRequestStatus.COMPLETED and not self.result_evidence_ids:
            raise ValueError("COMPLETED 的 ResearchRequest 必须至少返回一个 evidence_id")
        return self
