"""证据记录。"""

from pydantic import AwareDatetime, Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget, SourceReference
from stock_research_agent.domain.enums import EvidenceDomain, VerificationStatus


class EvidenceRecord(DomainModel):
    """经过收集器校验、编号并可追溯来源的一条证据。"""

    evidence_id: str = Field(pattern=r"^ev_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    target: ResearchTarget
    domain: EvidenceDomain
    as_of: AwareDatetime
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    source_refs: list[SourceReference] = Field(min_length=1)
    verification_status: VerificationStatus
    tags: list[str] = Field(default_factory=list)
    raw_payload_ref: str | None = None
    collected_by: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime
