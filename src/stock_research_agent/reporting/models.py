"""最终研究报告的严格、可序列化数据契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.agents.consensus_assembly import ConsensusAssemblyRunSummary
from stock_research_agent.agents.negotiation import ConsensusGateReport
from stock_research_agent.domain import (
    EvidenceCollection,
    EvidenceRecord,
    RecommendationRecord,
    ResearchTarget,
    ThesisRecord,
)
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import (
    DecisionDimension,
    EvidenceDomain,
    ProposalStatus,
    ThesisDirection,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.recommendation import ProposalItem

_NonEmptyText = Annotated[str, Field(min_length=1)]


class ReportOutcome(StrEnum):
    """报告在投资决策链上的完成程度。"""

    CONSENSUS_READY = "CONSENSUS_READY"
    NO_ACTIONABLE_CONSENSUS = "NO_ACTIONABLE_CONSENSUS"
    INCOMPLETE = "INCOMPLETE"


class ReportHealth(StrEnum):
    """报告所覆盖运行的健康程度，与投资结论是否达成共识相互独立。"""

    CLEAN = "CLEAN"
    WITH_WARNINGS = "WITH_WARNINGS"
    WITH_ERRORS = "WITH_ERRORS"


class EvidenceReportSection(DomainModel):
    """保留完整证据，并同时提供便于前端展示的确定性计数。"""

    total_count: int = Field(ge=0)
    counts_by_domain: dict[str, int]
    counts_by_verification_status: dict[str, int]
    records: tuple[EvidenceRecord, ...] = ()
    collection: EvidenceCollection | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> EvidenceReportSection:
        if self.total_count != len(self.records):
            raise ValueError("EvidenceReportSection.total_count 必须等于 records 条数")
        for name, counts in (
            ("counts_by_domain", self.counts_by_domain),
            ("counts_by_verification_status", self.counts_by_verification_status),
        ):
            if any(value < 0 for value in counts.values()):
                raise ValueError(f"{name} 不能包含负数")
            if sum(counts.values()) != self.total_count:
                raise ValueError(f"{name} 合计必须等于 total_count")
        if set(self.counts_by_domain) != {item.value for item in EvidenceDomain}:
            raise ValueError("counts_by_domain 必须完整覆盖 EvidenceDomain")
        if set(self.counts_by_verification_status) != {
            item.value for item in VerificationStatus
        }:
            raise ValueError(
                "counts_by_verification_status 必须完整覆盖 VerificationStatus"
            )
        return self


class ThesisReportSection(DomainModel):
    """保留完整观点及其查证状态。"""

    total_count: int = Field(ge=0)
    counts_by_direction: dict[str, int]
    counts_by_validation_status: dict[str, int]
    records: tuple[ThesisRecord, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> ThesisReportSection:
        if self.total_count != len(self.records):
            raise ValueError("ThesisReportSection.total_count 必须等于 records 条数")
        for name, counts in (
            ("counts_by_direction", self.counts_by_direction),
            ("counts_by_validation_status", self.counts_by_validation_status),
        ):
            if any(value < 0 for value in counts.values()):
                raise ValueError(f"{name} 不能包含负数")
            if sum(counts.values()) != self.total_count:
                raise ValueError(f"{name} 合计必须等于 total_count")
        if set(self.counts_by_direction) != {item.value for item in ThesisDirection}:
            raise ValueError("counts_by_direction 必须完整覆盖 ThesisDirection")
        if set(self.counts_by_validation_status) != {
            item.value for item in ThesisValidationStatus
        }:
            raise ValueError(
                "counts_by_validation_status 必须完整覆盖 ThesisValidationStatus"
            )
        return self


class DisagreementDisclosure(DomainModel):
    """披露未进入最终建议的分歧，不把它们伪装成委员会结论。"""

    debate_round: int = Field(default=0, ge=0, le=3)
    excluded_item_ids: tuple[str, ...] = ()
    excluded_items: tuple[ProposalItem, ...] = ()
    remaining_disagreements: tuple[str, ...] = ()
    missing_required_dimensions: tuple[DecisionDimension, ...] = ()
    derived_from: tuple[
        Literal[
            "CONSENSUS_RECOMMENDATION",
            "CONSENSUS_GATE",
            "NEGOTIATION_POOL",
            "CONSENSUS_ASSEMBLY",
        ],
        ...,
    ] = ()

    @model_validator(mode="after")
    def validate_disclosure(self) -> DisagreementDisclosure:
        if len(set(self.excluded_item_ids)) != len(self.excluded_item_ids):
            raise ValueError("excluded_item_ids 不能重复")
        detail_ids = [item.item_id for item in self.excluded_items]
        if len(set(detail_ids)) != len(detail_ids):
            raise ValueError("excluded_items 不能重复")
        if not set(detail_ids).issubset(self.excluded_item_ids):
            raise ValueError("excluded_items 必须属于 excluded_item_ids")
        if any(item.status is not ProposalStatus.EXCLUDED for item in self.excluded_items):
            raise ValueError("excluded_items 只能包含 EXCLUDED 条目")
        if len(set(self.missing_required_dimensions)) != len(
            self.missing_required_dimensions
        ):
            raise ValueError("missing_required_dimensions 不能重复")
        if len(set(self.derived_from)) != len(self.derived_from):
            raise ValueError("derived_from 不能重复")
        return self


class RecommendationReportSection(DomainModel):
    """三套建议及共识形成过程的最终可审计快照。"""

    aggressive: RecommendationRecord | None = None
    conservative: RecommendationRecord | None = None
    consensus: RecommendationRecord | None = None
    consensus_gate: ConsensusGateReport | None = None
    consensus_assembly: ConsensusAssemblyRunSummary | None = None
    disagreement: DisagreementDisclosure


class ReportDiagnostics(DomainModel):
    """区分上游显式错误和报告组装时发现的完整性问题。"""

    upstream_errors: tuple[_NonEmptyText, ...] = ()
    integrity_warnings: tuple[_NonEmptyText, ...] = ()


class ResearchReport(DomainModel):
    """可供 API、持久化层和 Markdown 渲染器共同消费的最终报告。"""

    schema_version: Literal["1.0"] = "1.0"
    report_id: str = Field(pattern=r"^report_[a-f0-9]{24}$")
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    target: ResearchTarget
    outcome: ReportOutcome
    health: ReportHealth
    evidence: EvidenceReportSection
    theses: ThesisReportSection
    recommendations: RecommendationReportSection
    diagnostics: ReportDiagnostics
    disclaimer: str = "本报告由自动化研究系统生成，仅供研究，不构成投资建议。"

    @model_validator(mode="after")
    def validate_identity_and_outcome(self) -> ResearchReport:
        if self.report_id != f"report_{self.source_fingerprint[:24]}":
            raise ValueError("report_id 必须由 source_fingerprint 稳定生成")

        recommendations = self.recommendations
        originals_ready = (
            recommendations.aggressive is not None
            and recommendations.conservative is not None
        )
        assembly = recommendations.consensus_assembly
        if originals_ready and recommendations.consensus is not None:
            expected_outcome = ReportOutcome.CONSENSUS_READY
        elif (
            originals_ready
            and recommendations.consensus is None
            and assembly is not None
            and assembly.stop_reason == "no_actionable_consensus"
        ):
            expected_outcome = ReportOutcome.NO_ACTIONABLE_CONSENSUS
        else:
            expected_outcome = ReportOutcome.INCOMPLETE
        if self.outcome is not expected_outcome:
            raise ValueError("outcome 必须精确反映三套建议和共识组装状态")

        if self.diagnostics.upstream_errors:
            expected_health = ReportHealth.WITH_ERRORS
        elif self.diagnostics.integrity_warnings:
            expected_health = ReportHealth.WITH_WARNINGS
        else:
            expected_health = ReportHealth.CLEAN
        if self.health is not expected_health:
            raise ValueError("health 必须精确反映错误和完整性警告")
        return self
