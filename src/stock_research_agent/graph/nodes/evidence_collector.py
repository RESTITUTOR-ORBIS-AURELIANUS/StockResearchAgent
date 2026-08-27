"""把四位证据研究员的输出整理为观点生成上下文。"""

from collections.abc import Callable, Iterable
from datetime import datetime
from enum import StrEnum

from stock_research_agent.domain import (
    CollectedEvidenceSummary,
    EvidenceCollection,
    EvidenceRecord,
    EvidenceRejectionReason,
    RejectedEvidenceSummary,
)
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph.state import ResearchGraphState

_TARGET_ORDER = {
    TargetType.MARKET: 0,
    TargetType.SECTOR: 1,
    TargetType.STOCK: 2,
}
_DOMAIN_ORDER = {
    EvidenceDomain.TECHNICAL: 0,
    EvidenceDomain.FUNDAMENTAL: 1,
    EvidenceDomain.EVENT: 2,
    EvidenceDomain.SENTIMENT_FLOW: 3,
    EvidenceDomain.MACRO: 4,
}


def evidence_collector_node(state: ResearchGraphState) -> ResearchGraphState:
    """检查运行边界和来源时点，并生成不做语义去重的证据目录。"""

    run_id = state.get("run_id")
    as_of = state.get("as_of")
    if not run_id or as_of is None:
        raise ValueError("EvidenceCollectorNode 运行前必须已经初始化 run_id 和 as_of")

    records = list(state.get("evidence_pool", []))
    accepted: list[EvidenceRecord] = []
    rejected: list[RejectedEvidenceSummary] = []
    for record in records:
        reasons = _rejection_reasons(record, run_id=run_id, as_of=as_of)
        if reasons:
            rejected.append(
                RejectedEvidenceSummary(
                    evidence_id=record.evidence_id,
                    reasons=tuple(reasons),
                )
            )
        else:
            accepted.append(record)

    accepted.sort(key=_evidence_sort_key)
    rejected.sort(key=lambda item: item.evidence_id)
    summaries = tuple(_summarize(record) for record in accepted)
    collection = EvidenceCollection(
        run_id=run_id,
        as_of=as_of,
        total_input_count=len(records),
        accepted_count=len(accepted),
        rejected_count=len(rejected),
        counts_by_domain=_enum_counts(accepted, EvidenceDomain, lambda item: item.domain),
        counts_by_verification_status=_enum_counts(
            accepted,
            VerificationStatus,
            lambda item: item.verification_status,
        ),
        counts_by_target_type=_enum_counts(
            accepted,
            TargetType,
            lambda item: item.target.type,
        ),
        evidence=summaries,
        rejected=tuple(rejected),
        policy_notes=(
            "未执行语义重复检查；不同 evidence_id 的相似证据全部保留。",
            "evidence_pool 保留完整 SourceReference；本目录通过 evidence_id 回溯原始证据。",
        ),
    )
    updates: ResearchGraphState = {"evidence_collection": collection}
    if rejected:
        updates["errors"] = [
            f"evidence collector rejected {len(rejected)} record(s); "
            "inspect evidence_collection.rejected"
        ]
    return updates


def _rejection_reasons(
    record: EvidenceRecord,
    *,
    run_id: str,
    as_of: datetime,
) -> list[EvidenceRejectionReason]:
    reasons: list[EvidenceRejectionReason] = []
    if record.run_id != run_id:
        reasons.append(EvidenceRejectionReason.RUN_ID_MISMATCH)
    if record.as_of != as_of:
        reasons.append(EvidenceRejectionReason.AS_OF_MISMATCH)
    if record.verification_status is VerificationStatus.RETRACTED:
        reasons.append(EvidenceRejectionReason.RETRACTED)
    if any(
        source.published_at is not None and source.published_at > as_of
        for source in record.source_refs
    ):
        reasons.append(EvidenceRejectionReason.SOURCE_AFTER_AS_OF)
    if any(
        source.data_as_of is not None and source.data_as_of > as_of.date()
        for source in record.source_refs
    ):
        reasons.append(EvidenceRejectionReason.DATA_AFTER_AS_OF)
    return reasons


def _summarize(record: EvidenceRecord) -> CollectedEvidenceSummary:
    return CollectedEvidenceSummary(
        evidence_id=record.evidence_id,
        target=record.target,
        domain=record.domain,
        title=record.title,
        description=record.description,
        verification_status=record.verification_status,
        tags=tuple(record.tags),
        source_count=len(record.source_refs),
        source_providers=tuple(sorted({source.provider for source in record.source_refs})),
        source_interfaces=tuple(sorted({source.interface for source in record.source_refs})),
        collected_by=record.collected_by,
    )


def _evidence_sort_key(record: EvidenceRecord) -> tuple[int, str, int, str, str]:
    return (
        _TARGET_ORDER[record.target.type],
        record.target.code,
        _DOMAIN_ORDER[record.domain],
        record.title,
        record.evidence_id,
    )


def _enum_counts[RecordT, EnumT: StrEnum](
    records: Iterable[RecordT],
    enum_type: type[EnumT],
    get_value: Callable[[RecordT], EnumT],
) -> dict[str, int]:
    counts = {member.value: 0 for member in enum_type}
    for record in records:
        counts[get_value(record).value] += 1
    return counts
