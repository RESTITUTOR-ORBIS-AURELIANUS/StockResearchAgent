"""把任意已初始化的工作流状态组装为结构化报告和 Markdown。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum

from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ProposalStatus,
    ThesisDirection,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.reporting import (
    DisagreementDisclosure,
    EvidenceReportSection,
    RecommendationOutputMode,
    RecommendationReportSection,
    ReportDiagnostics,
    ReportHealth,
    ReportOutcome,
    ResearchReport,
    ThesisReportSection,
    render_research_report_markdown,
)


def report_composer_node(state: ResearchGraphState) -> ResearchGraphState:
    """确定性报告终点；上游不完整时也保留已有产物并明确标记。"""

    run_id = state.get("run_id")
    as_of = state.get("as_of")
    target = state.get("target")
    if not run_id or as_of is None or target is None:
        return {
            "errors": [
                "ReportComposerNode skipped: run_id, as_of and target are required"
            ]
        }

    evidence_records = tuple(
        record.model_copy(deep=True) for record in state.get("evidence_pool", [])
    )
    thesis_records = tuple(
        record.model_copy(deep=True) for record in state.get("thesis_pool", [])
    )
    evidence_section = EvidenceReportSection(
        total_count=len(evidence_records),
        counts_by_domain=_enum_counts(
            evidence_records,
            EvidenceDomain,
            lambda record: record.domain,
        ),
        counts_by_verification_status=_enum_counts(
            evidence_records,
            VerificationStatus,
            lambda record: record.verification_status,
        ),
        records=evidence_records,
        collection=(
            state["evidence_collection"].model_copy(deep=True)
            if state.get("evidence_collection") is not None
            else None
        ),
    )
    thesis_section = ThesisReportSection(
        total_count=len(thesis_records),
        counts_by_direction=_enum_counts(
            thesis_records,
            ThesisDirection,
            lambda record: record.direction,
        ),
        counts_by_validation_status=_enum_counts(
            thesis_records,
            ThesisValidationStatus,
            lambda record: record.validation.status,
        ),
        records=thesis_records,
    )

    disagreement = _build_disagreement_disclosure(state)
    recommendations = RecommendationReportSection(
        output_mode=_recommendation_output_mode(state),
        aggressive=_copy_optional(state.get("aggressive_recommendation")),
        conservative=_copy_optional(state.get("conservative_recommendation")),
        consensus=_copy_optional(state.get("consensus_recommendation")),
        consensus_gate=_copy_optional(state.get("consensus_gate_report")),
        consensus_assembly=_copy_optional(state.get("consensus_assembly_run_summary")),
        disagreement=disagreement,
    )
    upstream_errors = tuple(
        text
        for error in state.get("errors", [])
        if (text := str(error).strip())
    )
    integrity_warnings = tuple(_integrity_warnings(state, disagreement))
    diagnostics = ReportDiagnostics(
        upstream_errors=upstream_errors,
        integrity_warnings=integrity_warnings,
    )
    outcome = _report_outcome(recommendations)
    health = _report_health(diagnostics)
    source_fingerprint = _source_fingerprint(
        run_id=run_id,
        as_of=as_of,
        target=target,
        evidence=evidence_section,
        theses=thesis_section,
        recommendations=recommendations,
        diagnostics=diagnostics,
        outcome=outcome,
        health=health,
    )
    report = ResearchReport(
        report_id=f"report_{source_fingerprint[:24]}",
        source_fingerprint=source_fingerprint,
        run_id=run_id,
        as_of=as_of,
        target=target.model_copy(deep=True),
        outcome=outcome,
        health=health,
        evidence=evidence_section,
        theses=thesis_section,
        recommendations=recommendations,
        diagnostics=diagnostics,
    )
    markdown = render_research_report_markdown(report)
    existing = state.get("research_report")
    if (
        existing is not None
        and existing == report
        and state.get("research_report_markdown") == markdown
    ):
        return {}
    return {
        "research_report": report,
        "research_report_markdown": markdown,
    }


def _build_disagreement_disclosure(
    state: ResearchGraphState,
) -> DisagreementDisclosure:
    consensus = state.get("consensus_recommendation")
    gate = state.get("consensus_gate_report")
    pool = state.get("negotiation_proposal_pool")
    assembly = state.get("consensus_assembly_run_summary")

    excluded_ids: list[str] = []
    remaining_disagreements: list[str] = []
    missing_dimensions = []
    derived_from: list[str] = []
    debate_round = int(state.get("debate_round", 0))

    if consensus is not None and consensus.debate is not None:
        derived_from.append("CONSENSUS_RECOMMENDATION")
        debate_round = max(debate_round, consensus.debate.rounds)
        excluded_ids.extend(consensus.debate.excluded_item_ids)
        remaining_disagreements.extend(consensus.debate.remaining_disagreements)
    if gate is not None:
        derived_from.append("CONSENSUS_GATE")
        debate_round = max(debate_round, gate.debate_round)
        excluded_ids.extend(gate.excluded_item_ids)
        missing_dimensions.extend(gate.missing_required_dimensions)
    if pool is not None:
        pool_excluded = [
            item for item in pool.proposal_items if item.status is ProposalStatus.EXCLUDED
        ]
        if pool_excluded:
            derived_from.append("NEGOTIATION_POOL")
            excluded_ids.extend(item.item_id for item in pool_excluded)
    else:
        pool_excluded = []
    if assembly is not None:
        derived_from.append("CONSENSUS_ASSEMBLY")
        debate_round = max(debate_round, assembly.debate_round)
        excluded_ids.extend(assembly.excluded_item_ids)
        missing_dimensions.extend(assembly.missing_required_dimensions)

    excluded_ids = _unique(excluded_ids)
    missing_dimensions = _unique(missing_dimensions)
    excluded_items = tuple(
        item.model_copy(deep=True)
        for item in pool_excluded
        if item.item_id in set(excluded_ids)
    )
    if not remaining_disagreements and excluded_items:
        grouped: dict[str, list[str]] = {}
        for item in excluded_items:
            grouped.setdefault(item.conflict_group, []).append(item.item_id)
        remaining_disagreements = [
            (
                f"冲突组 {group} 在 {debate_round} 轮协商后仍未达成共识；"
                f"条目 {', '.join(item_ids)} 未进入最终建议。"
            )
            for group, item_ids in grouped.items()
        ]

    return DisagreementDisclosure(
        debate_round=min(debate_round, 3),
        excluded_item_ids=tuple(excluded_ids),
        excluded_items=excluded_items,
        remaining_disagreements=tuple(_unique(remaining_disagreements)),
        missing_required_dimensions=tuple(missing_dimensions),
        derived_from=tuple(_unique(derived_from)),
    )


def _integrity_warnings(
    state: ResearchGraphState,
    disagreement: DisagreementDisclosure,
) -> list[str]:
    run_id = state["run_id"]
    as_of = state["as_of"]
    target = state["target"]
    warnings: list[str] = []
    evidence = state.get("evidence_pool", [])
    theses = state.get("thesis_pool", [])
    evidence_ids = {record.evidence_id for record in evidence}
    thesis_ids = {record.thesis_id for record in theses}

    if len(evidence_ids) != len(evidence):
        warnings.append("evidence_pool contains duplicate evidence_id values")
    if len(thesis_ids) != len(theses):
        warnings.append("thesis_pool contains duplicate thesis_id values")
    for record in evidence:
        if record.run_id != run_id or record.as_of != as_of:
            warnings.append(f"evidence scope mismatch: {record.evidence_id}")
    for thesis in theses:
        if thesis.run_id != run_id or thesis.as_of != as_of:
            warnings.append(f"thesis scope mismatch: {thesis.thesis_id}")
        referenced_evidence = {
            *thesis.supporting_evidence_ids,
            *thesis.contradicting_evidence_ids,
        }
        for evidence_id in sorted(referenced_evidence - evidence_ids):
            warnings.append(
                f"thesis {thesis.thesis_id} references missing evidence: {evidence_id}"
            )

    for label, recommendation in (
        ("aggressive recommendation", state.get("aggressive_recommendation")),
        ("conservative recommendation", state.get("conservative_recommendation")),
        ("consensus recommendation", state.get("consensus_recommendation")),
    ):
        if recommendation is None:
            continue
        if (
            recommendation.run_id != run_id
            or recommendation.as_of != as_of
            or recommendation.target != target
        ):
            warnings.append(f"{label} scope mismatch")
        for thesis_id in sorted(set(recommendation.supporting_thesis_ids) - thesis_ids):
            warnings.append(f"{label} references missing thesis: {thesis_id}")

    collection = state.get("evidence_collection")
    if collection is not None and (
        collection.run_id != run_id or collection.as_of != as_of
    ):
        warnings.append("evidence_collection scope mismatch")
    gate = state.get("consensus_gate_report")
    if gate is not None and (gate.run_id != run_id or gate.as_of != as_of):
        warnings.append("consensus_gate_report scope mismatch")
    assembly = state.get("consensus_assembly_run_summary")
    if assembly is not None and (assembly.run_id != run_id or assembly.as_of != as_of):
        warnings.append("consensus_assembly_run_summary scope mismatch")

    consensus = state.get("consensus_recommendation")
    if consensus is not None and consensus.debate is not None:
        aggressive = state.get("aggressive_recommendation")
        conservative = state.get("conservative_recommendation")
        if (
            aggressive is None
            or consensus.debate.aggressive_original_recommendation_id
            != aggressive.recommendation_id
        ):
            warnings.append("consensus debate does not match aggressive original recommendation")
        if (
            conservative is None
            or consensus.debate.conservative_original_recommendation_id
            != conservative.recommendation_id
        ):
            warnings.append("consensus debate does not match conservative original recommendation")

    pool = state.get("negotiation_proposal_pool")
    if pool is not None:
        pool_excluded_ids = {
            item.item_id
            for item in pool.proposal_items
            if item.status is ProposalStatus.EXCLUDED
        }
        if not pool_excluded_ids.issubset(disagreement.excluded_item_ids):
            warnings.append("report disagreement catalog omits excluded negotiation items")
    return _unique(warnings)


def _report_outcome(
    recommendations: RecommendationReportSection,
) -> ReportOutcome:
    originals_ready = (
        recommendations.aggressive is not None
        and recommendations.conservative is not None
    )
    if (
        originals_ready
        and recommendations.output_mode is RecommendationOutputMode.DUAL_INDEPENDENT
    ):
        return ReportOutcome.DUAL_RECOMMENDATIONS_READY
    if originals_ready and recommendations.consensus is not None:
        return ReportOutcome.CONSENSUS_READY
    assembly = recommendations.consensus_assembly
    if (
        originals_ready
        and recommendations.consensus is None
        and assembly is not None
        and assembly.stop_reason == "no_actionable_consensus"
    ):
        return ReportOutcome.NO_ACTIONABLE_CONSENSUS
    return ReportOutcome.INCOMPLETE


def _recommendation_output_mode(
    state: ResearchGraphState,
) -> RecommendationOutputMode:
    if state.get("independent_recommendations_finalized"):
        return RecommendationOutputMode.DUAL_INDEPENDENT
    if (
        state.get("consensus_recommendation") is not None
        or state.get("consensus_gate_report") is not None
        or state.get("consensus_assembly_run_summary") is not None
    ):
        return RecommendationOutputMode.CONSENSUS
    return RecommendationOutputMode.INCOMPLETE


def _report_health(diagnostics: ReportDiagnostics) -> ReportHealth:
    if diagnostics.upstream_errors:
        return ReportHealth.WITH_ERRORS
    if diagnostics.integrity_warnings:
        return ReportHealth.WITH_WARNINGS
    return ReportHealth.CLEAN


def _source_fingerprint(**sections) -> str:
    payload = {
        key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for key, value in sections.items()
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _enum_counts[RecordT, EnumT: StrEnum](
    records: Iterable[RecordT],
    enum_type: type[EnumT],
    get_value,
) -> dict[str, int]:
    counts = {member.value: 0 for member in enum_type}
    for record in records:
        counts[get_value(record).value] += 1
    return counts


def _copy_optional(value):
    return value.model_copy(deep=True) if value is not None else None


def _unique[ValueT](values: Iterable[ValueT]) -> list[ValueT]:
    result: list[ValueT] = []
    seen: set[ValueT] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
