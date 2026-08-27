"""观点生成前使用的确定性证据汇总结果。"""

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    VerificationStatus,
)


class EvidenceRejectionReason(StrEnum):
    """证据不能进入本轮观点生成上下文的结构化原因。"""

    RUN_ID_MISMATCH = "RUN_ID_MISMATCH"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    RETRACTED = "RETRACTED"
    SOURCE_AFTER_AS_OF = "SOURCE_AFTER_AS_OF"
    DATA_AFTER_AS_OF = "DATA_AFTER_AS_OF"


class CollectedEvidenceSummary(DomainModel):
    """保留观点生成所需语义，同时通过 evidence_id 反查完整原始证据。"""

    evidence_id: str = Field(pattern=r"^ev_[A-Za-z0-9_]+$")
    target: ResearchTarget
    domain: EvidenceDomain
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    verification_status: VerificationStatus
    tags: tuple[str, ...] = ()
    source_count: int = Field(ge=1)
    source_providers: tuple[str, ...] = ()
    source_interfaces: tuple[str, ...] = ()
    collected_by: str = Field(min_length=1, max_length=100)


class RejectedEvidenceSummary(DomainModel):
    evidence_id: str = Field(pattern=r"^ev_[A-Za-z0-9_]+$")
    reasons: tuple[EvidenceRejectionReason, ...] = Field(min_length=1)


class EvidenceCollection(DomainModel):
    """EvidenceCollectorNode 的可审计输出，不执行语义重复检查。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    total_input_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    counts_by_domain: dict[str, int]
    counts_by_verification_status: dict[str, int]
    counts_by_target_type: dict[str, int]
    evidence: tuple[CollectedEvidenceSummary, ...] = ()
    rejected: tuple[RejectedEvidenceSummary, ...] = ()
    policy_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> "EvidenceCollection":
        if self.total_input_count != self.accepted_count + self.rejected_count:
            raise ValueError("total_input_count 必须等于 accepted_count + rejected_count")
        if self.accepted_count != len(self.evidence):
            raise ValueError("accepted_count 必须等于 evidence 条数")
        if self.rejected_count != len(self.rejected):
            raise ValueError("rejected_count 必须等于 rejected 条数")
        for label, counts in (
            ("counts_by_domain", self.counts_by_domain),
            ("counts_by_verification_status", self.counts_by_verification_status),
            ("counts_by_target_type", self.counts_by_target_type),
        ):
            if any(value < 0 for value in counts.values()):
                raise ValueError(f"{label} 不能出现负数")
            if sum(counts.values()) != self.accepted_count:
                raise ValueError(f"{label} 合计必须等于 accepted_count")
        return self
