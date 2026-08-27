"""从 EvidenceCollection 生成未经查证的候选 ThesisRecord。"""

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256

from stock_research_agent.agents.strategist import (
    CandidateThesisDraft,
    CandidateThesisLimits,
    CandidateThesisRunSummary,
    CandidateThesisStopReason,
    LeadResearchStrategistModel,
    LeadStrategistInput,
)
from stock_research_agent.domain import (
    CollectedEvidenceSummary,
    EvidenceCollection,
    ThesisRecord,
)
from stock_research_agent.domain.enums import (
    ThesisOriginType,
    ThesisValidationStatus,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisValidation
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

_AGENT_NAME = "LeadResearchStrategist"


def build_candidate_thesis_generation_node(
    model: LeadResearchStrategistModel,
    *,
    limits: CandidateThesisLimits | None = None,
):
    """构建单次结构化 LLM 调用节点；候选观点不在此阶段验证。"""

    configured_limits = limits or CandidateThesisLimits()

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        collection = state.get("evidence_collection")
        target = state.get("target")
        run_id = state.get("run_id")
        as_of = state.get("as_of")
        invalid_reason = _collection_error(
            collection,
            run_id=run_id,
            as_of=as_of,
            target_present=target is not None,
        )
        if invalid_reason is not None:
            return {
                "candidate_thesis_run_summary": _run_summary(
                    stop_reason="invalid_collection",
                ),
                "errors": [f"lead strategist skipped: {invalid_reason}"],
            }

        assert collection is not None
        assert target is not None
        assert run_id is not None
        assert as_of is not None
        if collection.accepted_count == 0:
            return {
                "candidate_thesis_run_summary": _run_summary(
                    stop_reason="no_evidence",
                )
            }
        if collection.accepted_count > configured_limits.max_evidence_count:
            return {
                "candidate_thesis_run_summary": _run_summary(
                    input_evidence_count=collection.accepted_count,
                    stop_reason="evidence_limit_exceeded",
                ),
                "errors": [
                    "lead strategist skipped: evidence count exceeds configured hard limit; "
                    "input was not truncated"
                ],
            }

        request = LeadStrategistInput(
            run_id=run_id,
            research_target=target,
            as_of=as_of,
            counts_by_domain=collection.counts_by_domain,
            counts_by_verification_status=collection.counts_by_verification_status,
            counts_by_target_type=collection.counts_by_target_type,
            evidence=collection.evidence,
            policy_notes=collection.policy_notes,
            max_candidates=configured_limits.max_candidates,
        )
        context_characters = len(request.model_dump_json(indent=2))
        if context_characters > configured_limits.max_context_characters:
            return {
                "candidate_thesis_run_summary": _run_summary(
                    input_evidence_count=collection.accepted_count,
                    context_character_count=context_characters,
                    stop_reason="context_limit_exceeded",
                ),
                "errors": [
                    "lead strategist skipped: evidence context exceeds configured hard limit; "
                    "input was not truncated"
                ],
            }

        try:
            generation = await model.generate_candidates(request)
        except Exception as exc:
            return {
                "candidate_thesis_run_summary": _run_summary(
                    input_evidence_count=collection.accepted_count,
                    context_character_count=context_characters,
                    model_called=True,
                    stop_reason="model_error",
                ),
                "errors": [f"lead strategist failed: {describe_exception(exc)}"],
            }

        catalog = {item.evidence_id: item for item in collection.evidence}
        accepted: list[ThesisRecord] = []
        rejection_messages: list[str] = []
        seen_thesis_ids: set[str] = set()
        for position, draft in enumerate(generation.candidates, start=1):
            if position > configured_limits.max_candidates:
                rejection_messages.append(f"candidate {position}: MAX_CANDIDATES_EXCEEDED")
                continue
            invalid = _draft_error(draft, catalog)
            if invalid is not None:
                rejection_messages.append(f"candidate {position}: {invalid}")
                continue
            thesis = _assemble_thesis(draft, run_id=run_id, as_of=as_of)
            if thesis.thesis_id in seen_thesis_ids:
                rejection_messages.append(f"candidate {position}: EXACT_DUPLICATE_DRAFT")
                continue
            seen_thesis_ids.add(thesis.thesis_id)
            accepted.append(thesis)

        updates: ResearchGraphState = {
            "thesis_pool": accepted,
            "candidate_thesis_run_summary": _run_summary(
                input_evidence_count=collection.accepted_count,
                context_character_count=context_characters,
                model_called=True,
                generated_candidate_count=len(generation.candidates),
                accepted_candidate_count=len(accepted),
                rejected_candidate_count=len(rejection_messages),
                stop_reason="complete",
                generation_summary=generation.generation_summary,
            ),
        }
        if rejection_messages:
            updates["errors"] = [
                "lead strategist rejected candidate draft(s): " + "; ".join(rejection_messages)
            ]
        return updates

    return node


def _collection_error(
    collection: EvidenceCollection | None,
    *,
    run_id: str | None,
    as_of: datetime | None,
    target_present: bool,
) -> str | None:
    if not run_id or as_of is None or not target_present:
        return "run_id, target and as_of must be initialized"
    if collection is None:
        return "evidence_collection is missing"
    if collection.run_id != run_id:
        return "evidence_collection.run_id does not match current run"
    if collection.as_of != as_of:
        return "evidence_collection.as_of does not match current run"
    return None


def _draft_error(
    draft: CandidateThesisDraft,
    catalog: Mapping[str, CollectedEvidenceSummary],
) -> str | None:
    referenced_ids = (*draft.supporting_evidence_ids, *draft.contradicting_evidence_ids)
    unknown = sorted(set(referenced_ids) - catalog.keys())
    if unknown:
        return "UNKNOWN_EVIDENCE_ID=" + ",".join(unknown)
    if not any(
        catalog[evidence_id].target == draft.target for evidence_id in draft.supporting_evidence_ids
    ):
        return "TARGET_NOT_GROUNDED_BY_SUPPORTING_EVIDENCE"
    return None


def _assemble_thesis(
    draft: CandidateThesisDraft,
    *,
    run_id: str,
    as_of: datetime,
) -> ThesisRecord:
    payload = draft.model_dump_json()
    digest = sha256(f"{run_id}|{payload}".encode()).hexdigest()[:20]
    return ThesisRecord(
        thesis_id=f"th_{digest}",
        run_id=run_id,
        target=draft.target,
        as_of=as_of,
        title=draft.title,
        description=draft.description,
        direction=draft.direction,
        horizon=draft.horizon,
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent=_AGENT_NAME,
        ),
        validation=ThesisValidation(
            status=ThesisValidationStatus.UNVERIFIED,
            confidence=None,
            round=0,
        ),
        supporting_evidence_ids=list(draft.supporting_evidence_ids),
        contradicting_evidence_ids=list(draft.contradicting_evidence_ids),
        reasoning_summary=draft.reasoning_summary,
        missing_questions=list(draft.missing_questions),
        catalysts=list(draft.catalysts),
        invalidation_conditions=list(draft.invalidation_conditions),
        created_by=_AGENT_NAME,
        revision=1,
        created_at=as_of,
        updated_at=as_of,
    )


def _run_summary(
    *,
    input_evidence_count: int = 0,
    context_character_count: int = 0,
    model_called: bool = False,
    generated_candidate_count: int = 0,
    accepted_candidate_count: int = 0,
    rejected_candidate_count: int = 0,
    stop_reason: CandidateThesisStopReason,
    generation_summary: str | None = None,
) -> CandidateThesisRunSummary:
    return CandidateThesisRunSummary(
        input_evidence_count=input_evidence_count,
        context_character_count=context_character_count,
        model_called=model_called,
        generated_candidate_count=generated_candidate_count,
        accepted_candidate_count=accepted_candidate_count,
        rejected_candidate_count=rejected_candidate_count,
        stop_reason=stop_reason,
        generation_summary=generation_summary,
    )
