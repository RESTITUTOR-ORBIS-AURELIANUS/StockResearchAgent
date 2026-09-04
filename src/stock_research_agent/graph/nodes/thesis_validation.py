"""逐观点串行查证：每次补证后立即回到同一观点的连续上下文。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

from stock_research_agent.agents.event import EventResearchMode
from stock_research_agent.agents.fundamental import FundamentalResearchMode
from stock_research_agent.agents.sentiment_flow import SentimentFlowResearchMode
from stock_research_agent.agents.strategist import CandidateThesisDraft
from stock_research_agent.agents.technical import TechnicalResearchMode
from stock_research_agent.agents.validator import (
    ThesisValidationAction,
    ThesisValidationAnalystModel,
    ThesisValidationInput,
    ThesisValidationLimits,
    ThesisValidationRunSummary,
    ThesisValidationSession,
    ValidationResearchRequestDraft,
    ValidationResearchTurn,
)
from stock_research_agent.domain import (
    CollectedEvidenceSummary,
    EvidenceRecord,
    ResearchFinding,
    ResearchRequest,
    ThesisRecord,
    build_research_finding_id,
)
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchFindingOutcome,
    ResearchRequestStatus,
    TargetType,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisValidation
from stock_research_agent.graph.nodes.evidence_collector import evidence_collector_node
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

_AGENT_NAME = "ThesisValidationAnalyst"
_FINAL_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.REFUTED,
    ThesisValidationStatus.MIXED,
    ThesisValidationStatus.INCONCLUSIVE,
}
_DECISIVE_EVIDENCE_STATUSES = {
    VerificationStatus.VERIFIED,
    VerificationStatus.REVISED,
}
_ACTIVE_REQUEST_STATUSES = {
    ResearchRequestStatus.PENDING,
    ResearchRequestStatus.RUNNING,
}
_TERMINAL_REQUEST_STATUSES = {
    ResearchRequestStatus.COMPLETED,
    ResearchRequestStatus.NO_NEW_EVIDENCE,
    ResearchRequestStatus.FAILED,
    ResearchRequestStatus.CANCELLED_BY_BUDGET,
}
_NETWORK_FAILURE_MARKERS = (
    "timeout",
    "timed out",
    "transport",
    "network",
    "connection",
    "unavailable",
    "provider",
    "上游",
    "网络",
    "连接",
    "超时",
)
_TECHNICAL_BENCHMARK_CODES = {"000300.SH", "000905.SH", "000852.SH"}


class ValidationAgentGraph(Protocol):
    def ainvoke(self, state: Mapping[str, object]) -> Awaitable[Mapping[str, Any]]: ...


class ValidationGraphResolver(Protocol):
    async def resolve(self, state: ResearchGraphState) -> ValidationAgentGraph: ...

    def release(self, run_id: str) -> None: ...


def build_select_thesis_for_validation_node():
    """稳定选择第一条未查证观点；当前观点结束前不会选择下一条。"""

    def node(state: ResearchGraphState) -> ResearchGraphState:
        if state.get("active_validation_session") is not None:
            return {}
        thesis = next(
            (
                item
                for item in state.get("thesis_pool", [])
                if item.validation.status is ThesisValidationStatus.UNVERIFIED
            ),
            None,
        )
        if thesis is None:
            return {}
        under_review = _replace_thesis(
            thesis,
            validation=ThesisValidation(
                status=ThesisValidationStatus.UNDER_REVIEW,
                confidence=None,
                round=0,
            ),
            as_of=state["as_of"],
        )
        updates: ResearchGraphState = {
            "thesis_pool": [under_review],
            "active_validation_session": ThesisValidationSession(
                thesis_id=thesis.thesis_id,
            ),
            "active_validation_request_id": None,
            "validation_round": 0,
        }
        if state.get("validation_input_thesis_count", 0) == 0:
            updates["validation_input_thesis_count"] = len(state.get("thesis_pool", []))
        return updates

    return node


def route_after_validation_selection(state: ResearchGraphState) -> Literal["review", "done"]:
    return "review" if state.get("active_validation_session") is not None else "done"


def build_review_active_thesis_node(
    model: ThesisValidationAnalystModel,
    *,
    limits: ThesisValidationLimits | None = None,
):
    """重放当前观点全部本地 turn；每轮只接受终结或一个补证请求。"""

    configured = limits or ThesisValidationLimits()

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        session = state.get("active_validation_session")
        if session is None:
            if state.get("active_validation_request_id") is not None:
                return _recover_missing_validation_session(state)
            return {}
        thesis = _find_thesis(state, session.thesis_id)
        if thesis is None or thesis.validation.status is not ThesisValidationStatus.UNDER_REVIEW:
            return _abandon_invalid_session(
                state,
                session,
                "active validation thesis is missing or is not UNDER_REVIEW",
            )
        if session.pending_request_fingerprint is not None:
            return _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="上一轮查证请求尚未形成 ResearchFinding，不能提前再次审阅。",
                error="validator review blocked: active research request has no finding",
            )

        evidence = _session_evidence(state, thesis, session)
        if isinstance(evidence, str):
            return _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason=evidence,
                error=f"validator context invalid: {evidence}",
            )
        remaining_rounds = max(
            0,
            min(
                configured.max_research_rounds_per_thesis - len(session.previous_turns),
                configured.max_research_requests_per_run
                - state.get("validation_research_request_count", 0),
            ),
        )
        remaining_discoveries = max(
            0,
            configured.max_discovered_candidates_per_run
            - state.get("validation_discovered_candidate_count", 0),
        )
        request = ThesisValidationInput(
            run_id=state["run_id"],
            as_of=state["as_of"],
            thesis=thesis,
            evidence=tuple(evidence),
            previous_turns=session.previous_turns,
            used_request_fingerprints=session.used_request_fingerprints,
            current_round=len(session.previous_turns) + 1,
            remaining_research_rounds=remaining_rounds,
            max_discovered_candidates=min(
                configured.max_discovered_candidates_per_turn,
                remaining_discoveries,
            ),
            policy_notes=(
                "当前观点完成前不会切换到其他观点。",
                "每轮只能提出一个 ResearchRequest，响应会立即回到本会话。",
                "每条观点最多执行两轮补证；全运行最多接受两条审查中衍生的新观点。",
                "非 EVIDENCE_FOUND 的 ResearchFinding 既不是支持证据也不是反向证据。",
                "只有 VERIFIED 或 REVISED 证据可以作为最终方向判断的决定性依据。",
            ),
        )
        context_characters = len(request.model_dump_json())
        if context_characters > configured.max_context_characters:
            return _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="当前观点的连续查证上下文超过硬上限，输入未被静默截断。",
                error="validator context limit exceeded; input was not truncated",
            )

        call_count = state.get("validation_model_call_count", 0) + 1
        try:
            decision = await model.review_thesis(request)
        except Exception as exc:
            updates = _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="观点审查模型调用失败，当前运行无法可靠完成该观点查证。",
                error=f"validator model failed: {describe_exception(exc)}",
            )
            updates["validation_model_call_count"] = call_count
            updates["validation_stop_reason"] = "model_error"
            return updates

        discovered, discovery_errors = _assemble_discovered_candidates(
            decision.discovered_candidates,
            parent=thesis,
            evidence=evidence,
            existing=state.get("thesis_pool", []),
            run_id=state["run_id"],
            as_of=state["as_of"],
            limit=min(
                request.max_discovered_candidates,
                configured.max_discovered_candidates_per_turn,
            ),
        )
        common_updates: ResearchGraphState = {
            "validation_model_call_count": call_count,
            "validation_discovered_candidate_count": (
                state.get("validation_discovered_candidate_count", 0) + len(discovered)
            ),
        }
        if discovered:
            common_updates["thesis_pool"] = discovered
        if discovery_errors:
            common_updates["errors"] = discovery_errors

        if decision.action is ThesisValidationAction.FINALIZE:
            assert decision.finalization is not None
            finalization_error = _finalization_error(
                decision.finalization,
                thesis=thesis,
                evidence=evidence,
            )
            if finalization_error is not None:
                forced = _force_inconclusive_updates(
                    state,
                    thesis,
                    session,
                    reason="模型给出的最终判断未通过证据引用和状态约束。",
                    error=f"validator finalization rejected: {finalization_error}",
                )
                return _merge_scalar_updates(common_updates, forced)
            finalized = _finalize_thesis(
                thesis,
                decision.finalization,
                round_number=len(session.previous_turns),
                as_of=state["as_of"],
            )
            existing_updates = list(common_updates.get("thesis_pool", []))
            common_updates.update(
                {
                    "thesis_pool": [finalized, *existing_updates],
                    "active_validation_session": None,
                    "active_validation_request_id": None,
                    "validation_round": len(session.previous_turns),
                }
            )
            return common_updates

        assert decision.research_request is not None
        if remaining_rounds <= 0:
            forced = _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="当前观点已经耗尽单观点或全局补证预算。",
                error="validator rejected research request: budget exhausted",
            )
            return _merge_scalar_updates(common_updates, forced)
        if decision.research_request.time_range.end > state["as_of"].date():
            forced = _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="模型提出的查证时间范围晚于本轮冻结时点。",
                error="validator rejected research request: time_range exceeds as_of",
            )
            return _merge_scalar_updates(common_updates, forced)

        fingerprint = build_validation_request_fingerprint(decision.research_request)
        if fingerprint in session.used_request_fingerprints:
            forced = _force_inconclusive_updates(
                state,
                thesis,
                session,
                reason="模型重复提出已经执行过的等价查证请求，程序停止该观点的补证循环。",
                error="validator rejected duplicate research request fingerprint",
            )
            return _merge_scalar_updates(common_updates, forced)
        attempt = len(session.previous_turns) + 1
        research_request = _assemble_research_request(
            decision.research_request,
            run_id=state["run_id"],
            thesis_id=thesis.thesis_id,
            attempt=attempt,
            fingerprint=fingerprint,
            as_of=state["as_of"],
        )
        pending_session = ThesisValidationSession(
            thesis_id=session.thesis_id,
            previous_turns=session.previous_turns,
            used_request_fingerprints=(*session.used_request_fingerprints, fingerprint),
            pending_request_fingerprint=fingerprint,
            pending_reviewer_reasoning=decision.review_summary,
        )
        under_review = _replace_thesis(
            thesis,
            validation=ThesisValidation(
                status=ThesisValidationStatus.UNDER_REVIEW,
                confidence=None,
                round=attempt,
            ),
            as_of=state["as_of"],
        )
        existing_updates = list(common_updates.get("thesis_pool", []))
        common_updates.update(
            {
                "thesis_pool": [under_review, *existing_updates],
                "research_requests": [research_request],
                "active_validation_session": pending_session,
                "active_validation_request_id": research_request.request_id,
                "validation_round": attempt,
            }
        )
        return common_updates

    return node


def route_after_validation_review(
    state: ResearchGraphState,
) -> Literal["research", "next"]:
    return "research" if state.get("active_validation_request_id") else "next"


def build_execute_validation_research_node(
    *,
    technical_resolver: ValidationGraphResolver | None = None,
    fundamental_resolver: ValidationGraphResolver | None = None,
    sentiment_flow_resolver: ValidationGraphResolver | None = None,
    event_resolver: ValidationGraphResolver | None = None,
):
    """精确执行 active_request_id，并在同一步生成 Finding 与连续 turn。"""

    resolvers = {
        EvidenceDomain.TECHNICAL: technical_resolver,
        EvidenceDomain.FUNDAMENTAL: fundamental_resolver,
        EvidenceDomain.SENTIMENT_FLOW: sentiment_flow_resolver,
        EvidenceDomain.EVENT: event_resolver,
    }
    modes = {
        EvidenceDomain.TECHNICAL: TechnicalResearchMode.VERIFICATION,
        EvidenceDomain.FUNDAMENTAL: FundamentalResearchMode.VERIFICATION,
        EvidenceDomain.SENTIMENT_FLOW: SentimentFlowResearchMode.VERIFICATION,
        EvidenceDomain.EVENT: EventResearchMode.VERIFICATION,
    }

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        request_id = state.get("active_validation_request_id")
        session = state.get("active_validation_session")
        if session is None and request_id is not None:
            return _recover_missing_validation_session(state)
        if request_id is None or session is None:
            return {"errors": ["validation research skipped: active request/session missing"]}
        request = _find_request(state, request_id)
        if request is None or request.status not in _ACTIVE_REQUEST_STATUSES:
            return _abandon_invalid_session(
                state,
                session,
                "active ResearchRequest is missing or not executable",
            )
        resolver = resolvers.get(request.assigned_domain)
        if resolver is None or request.assigned_domain not in modes:
            return _complete_without_agent(
                state,
                session,
                request,
                outcome=ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE,
                summary="当前程序没有能够执行该领域和目标组合的定向查证链。",
                limitations=["没有调用任何 Tool，不能据此支持或反驳当前观点。"],
            )

        try:
            graph = await resolver.resolve(state)
            result = await graph.ainvoke(
                {
                    "run_id": state["run_id"],
                    "target": request.target,
                    "as_of": state["as_of"],
                    "mode": modes[request.assigned_domain],
                    "research_request": request,
                }
            )
            return _complete_from_agent_result(state, session, request, result)
        except Exception as exc:
            return _complete_without_agent(
                state,
                session,
                request,
                outcome=_failure_outcome([type(exc).__name__, str(exc)]),
                summary="领域研究子图执行失败，没有形成可引用证据。",
                limitations=[f"{type(exc).__name__}: {str(exc)[:500] or 'no detail'}"],
                invoked=True,
                errors=[f"validation research failed: {type(exc).__name__}"],
            )

    return node


def build_finalize_thesis_validation_run_node(
    *,
    resolvers: tuple[ValidationGraphResolver | None, ...] = (),
):
    """生成运行摘要并释放验证阶段重新创建的领域子图。"""

    def node(state: ResearchGraphState) -> ResearchGraphState:
        run_id = state["run_id"]
        for resolver in resolvers:
            if resolver is not None:
                resolver.release(run_id)
        statuses = {status.value: 0 for status in _FINAL_STATUSES}
        completed = 0
        for thesis in state.get("thesis_pool", []):
            status = thesis.validation.status
            if status in _FINAL_STATUSES:
                statuses[status.value] += 1
                completed += 1
        input_count = state.get("validation_input_thesis_count", 0)
        stop_reason = state.get("validation_stop_reason") or (
            "no_theses" if input_count == 0 else "complete"
        )
        return {
            "thesis_validation_run_summary": ThesisValidationRunSummary(
                input_thesis_count=input_count,
                completed_thesis_count=completed,
                status_counts=statuses,
                model_call_count=state.get("validation_model_call_count", 0),
                research_request_count=state.get("validation_research_request_count", 0),
                finding_count=len(state.get("research_findings", [])),
                discovered_candidate_count=state.get("validation_discovered_candidate_count", 0),
                stop_reason=stop_reason,
            ),
            "active_validation_session": None,
            "active_validation_request_id": None,
        }

    return node


def build_validation_request_fingerprint(
    draft: ValidationResearchRequestDraft,
) -> str:
    """对结构相同的请求生成稳定指纹；Prompt 另行禁止语义换词重试。"""

    payload = {
        "target_type": draft.target.type.value,
        "target_code": draft.target.code,
        "assigned_domain": draft.assigned_domain.value,
        "question": _normalize_request_text(draft.question),
        "requested_evidence": _normalize_request_text(draft.requested_evidence),
        "time_range_start": draft.time_range.start.isoformat(),
        "time_range_end": draft.time_range.end.isoformat(),
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"rqfp_{digest}"


def _normalize_request_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _find_thesis(state: ResearchGraphState, thesis_id: str) -> ThesisRecord | None:
    return next(
        (item for item in state.get("thesis_pool", []) if item.thesis_id == thesis_id),
        None,
    )


def _find_request(state: ResearchGraphState, request_id: str) -> ResearchRequest | None:
    return next(
        (item for item in state.get("research_requests", []) if item.request_id == request_id),
        None,
    )


def _session_evidence(
    state: ResearchGraphState,
    thesis: ThesisRecord,
    session: ThesisValidationSession,
) -> list[CollectedEvidenceSummary] | str:
    collection = state.get("evidence_collection")
    if collection is None:
        return "evidence_collection is missing"
    required_ids = {
        *thesis.supporting_evidence_ids,
        *thesis.contradicting_evidence_ids,
    }
    for turn in session.previous_turns:
        required_ids.update(turn.finding.evidence_ids)
    catalog = {item.evidence_id: item for item in collection.evidence}
    required_ids.update(
        item.evidence_id
        for item in collection.evidence
        if _same_target_identity(item.target, thesis.target)
    )
    missing = sorted(required_ids - catalog.keys())
    if missing:
        return "session evidence missing from Collector: " + ",".join(missing)
    return [item for item in collection.evidence if item.evidence_id in required_ids]


def _assemble_research_request(
    draft: ValidationResearchRequestDraft,
    *,
    run_id: str,
    thesis_id: str,
    attempt: int,
    fingerprint: str,
    as_of: datetime,
) -> ResearchRequest:
    digest = sha256(f"{run_id}|{thesis_id}|{attempt}|{fingerprint}".encode()).hexdigest()[:20]
    return ResearchRequest(
        request_id=f"rq_{digest}",
        run_id=run_id,
        thesis_id=thesis_id,
        target=draft.target,
        assigned_domain=draft.assigned_domain,
        question=draft.question,
        requested_evidence=draft.requested_evidence,
        time_range=draft.time_range,
        priority=draft.priority,
        attempt=attempt,
        status=ResearchRequestStatus.PENDING,
        requested_by=_AGENT_NAME,
        created_at=as_of,
    )


def _complete_from_agent_result(
    state: ResearchGraphState,
    session: ThesisValidationSession,
    request: ResearchRequest,
    result: Mapping[str, Any],
) -> ResearchGraphState:
    raw_errors = [str(item) for item in result.get("errors", [])]
    candidate_completion = result.get("completed_research_request")
    candidate_records = list(result.get("evidence_records", []))
    completion_error = _completion_error(
        candidate_completion,
        request=request,
        records=candidate_records,
    )
    searched_sources = _searched_sources(result.get("observations", []), candidate_records)
    if completion_error is not None:
        return _complete_without_agent(
            state,
            session,
            request,
            outcome=ResearchFindingOutcome.REQUEST_FAILED,
            summary="领域研究子图返回了不一致的完成对象，结果已整体拒绝。",
            searched_sources=searched_sources,
            limitations=[completion_error],
            invoked=True,
            errors=[*raw_errors, f"validation research rejected: {completion_error}"],
            run_summary=result.get("run_summary"),
        )

    assert isinstance(candidate_completion, ResearchRequest)
    conflict = _conflicting_evidence_id(state.get("evidence_pool", []), candidate_records)
    if conflict is not None:
        return _complete_without_agent(
            state,
            session,
            request,
            outcome=ResearchFindingOutcome.REQUEST_FAILED,
            summary="领域研究返回了与全局同 ID 证据不一致的内容，结果已拒绝。",
            searched_sources=searched_sources,
            limitations=[f"conflicting evidence_id: {conflict}"],
            invoked=True,
            errors=[*raw_errors, f"validation evidence ID conflict: {conflict}"],
            run_summary=result.get("run_summary"),
        )

    if candidate_completion.status is not ResearchRequestStatus.COMPLETED:
        outcome = _outcome_for_terminal_request(
            candidate_completion.status,
            errors=raw_errors,
            searched_sources=searched_sources,
        )
        return _complete_without_agent(
            state,
            session,
            request,
            outcome=outcome,
            summary=_finding_summary(outcome),
            searched_sources=searched_sources,
            limitations=[_finding_limitation(outcome)],
            invoked=True,
            errors=raw_errors,
            run_summary=result.get("run_summary"),
        )

    combined_pool = _merge_evidence(state.get("evidence_pool", []), candidate_records)
    collector_result = evidence_collector_node(
        {
            **state,
            "evidence_pool": combined_pool,
        }
    )
    collection = collector_result["evidence_collection"]
    accepted_ids = {item.evidence_id for item in collection.evidence}
    prior_ids = {item.evidence_id for item in state.get("evidence_pool", [])}
    returned_ids = {item.evidence_id for item in candidate_records}
    accepted_result_ids = sorted(returned_ids & accepted_ids)
    accepted_new_ids = sorted(set(accepted_result_ids) - prior_ids)
    rejected_returned = sorted(returned_ids - accepted_ids)
    if not accepted_result_ids:
        outcome = (
            ResearchFindingOutcome.REQUEST_FAILED
            if rejected_returned
            else ResearchFindingOutcome.NO_MATCHING_EVIDENCE
        )
        terminal = _terminal_request(
            request,
            status=(
                ResearchRequestStatus.FAILED
                if rejected_returned
                else ResearchRequestStatus.NO_NEW_EVIDENCE
            ),
            evidence_ids=[],
            completed_at=state["as_of"],
        )
        limitations = (
            ["Collector 拒绝了本次返回的全部记录：" + ",".join(rejected_returned)]
            if rejected_returned
            else ["返回记录与当前全局证据重复，没有形成新的可引用证据。"]
        )
        return _finish_turn(
            state,
            session,
            terminal,
            outcome=outcome,
            summary=_finding_summary(outcome),
            searched_sources=searched_sources or [f"{request.assigned_domain.value}_AGENT"],
            limitations=limitations,
            evidence_ids=[],
            evidence_pool=candidate_records,
            evidence_collection=collection,
            errors=[*raw_errors, *_new_collector_errors(state, collection)],
            run_summary=result.get("run_summary"),
            invoked=True,
        )

    terminal = _terminal_request(
        request,
        status=ResearchRequestStatus.COMPLETED,
        evidence_ids=accepted_result_ids,
        completed_at=state["as_of"],
    )
    limitations = []
    if rejected_returned:
        limitations.append("同次返回中有记录未通过 Collector：" + ",".join(rejected_returned))
    return _finish_turn(
        state,
        session,
        terminal,
        outcome=ResearchFindingOutcome.EVIDENCE_FOUND,
        summary=(
            f"查证命中 {len(accepted_result_ids)} 条可引用证据，"
            f"其中 {len(accepted_new_ids)} 条首次进入全局证据池。"
        ),
        searched_sources=searched_sources
        or _sources_from_evidence(candidate_records)
        or [f"{request.assigned_domain.value}_AGENT"],
        limitations=limitations,
        evidence_ids=accepted_result_ids,
        evidence_pool=candidate_records,
        evidence_collection=collection,
        errors=[*raw_errors, *_new_collector_errors(state, collection)],
        run_summary=result.get("run_summary"),
        invoked=True,
    )


def _completion_error(
    candidate: object,
    *,
    request: ResearchRequest,
    records: list[object],
) -> str | None:
    if not isinstance(candidate, ResearchRequest):
        return "completed_research_request is missing or has invalid type"
    immutable = {
        "request_id",
        "run_id",
        "thesis_id",
        "target",
        "assigned_domain",
        "question",
        "requested_evidence",
        "time_range",
        "priority",
        "attempt",
        "requested_by",
        "created_at",
    }
    for field in immutable:
        if getattr(candidate, field) != getattr(request, field):
            return f"completed request mutated immutable field: {field}"
    if candidate.status not in _TERMINAL_REQUEST_STATUSES:
        return "completed request is not terminal"
    if candidate.completed_at is None:
        return "completed request has no completed_at"
    if not all(isinstance(item, EvidenceRecord) for item in records):
        return "evidence_records contains invalid item"
    typed_records = [item for item in records if isinstance(item, EvidenceRecord)]
    record_ids = [item.evidence_id for item in typed_records]
    if len(set(record_ids)) != len(record_ids):
        return "evidence_records contains duplicate evidence_id"
    evidence_scope_error = _returned_evidence_scope_error(
        request=request,
        records=typed_records,
    )
    if evidence_scope_error is not None:
        return evidence_scope_error
    if candidate.status is ResearchRequestStatus.COMPLETED:
        if not record_ids:
            return "COMPLETED request returned no EvidenceRecord"
        if set(candidate.result_evidence_ids) != set(record_ids):
            return "result_evidence_ids do not match this execution's records"
    elif record_ids or candidate.result_evidence_ids:
        return "non-COMPLETED request returned evidence"
    return None


def _returned_evidence_scope_error(
    *,
    request: ResearchRequest,
    records: list[EvidenceRecord],
) -> str | None:
    for record in records:
        if record.run_id != request.run_id:
            return f"returned evidence has wrong run_id: {record.evidence_id}"
        if record.domain is not request.assigned_domain:
            return f"returned evidence has wrong domain: {record.evidence_id}"

    if request.assigned_domain is not EvidenceDomain.TECHNICAL:
        wrong_target = next(
            (
                record.evidence_id
                for record in records
                if not _same_target_identity(record.target, request.target)
            ),
            None,
        )
        return (
            f"returned evidence has unauthorized target: {wrong_target}"
            if wrong_target is not None
            else None
        )

    if records and not any(
        _same_target_identity(record.target, request.target) for record in records
    ):
        return "technical result has no evidence for the requested primary target"
    unauthorized = next(
        (
            record.evidence_id
            for record in records
            if not _same_target_identity(record.target, request.target)
            and not (
                record.target.type is TargetType.MARKET
                and record.target.code in _TECHNICAL_BENCHMARK_CODES
            )
        ),
        None,
    )
    return (
        f"returned technical evidence has unauthorized target: {unauthorized}"
        if unauthorized is not None
        else None
    )


def _complete_without_agent(
    state: ResearchGraphState,
    session: ThesisValidationSession,
    request: ResearchRequest,
    *,
    outcome: ResearchFindingOutcome,
    summary: str,
    limitations: list[str],
    searched_sources: list[str] | None = None,
    invoked: bool = False,
    errors: list[str] | None = None,
    run_summary: object | None = None,
) -> ResearchGraphState:
    status = (
        ResearchRequestStatus.CANCELLED_BY_BUDGET
        if outcome is ResearchFindingOutcome.BUDGET_EXHAUSTED
        else ResearchRequestStatus.NO_NEW_EVIDENCE
        if outcome is ResearchFindingOutcome.NO_MATCHING_EVIDENCE
        else ResearchRequestStatus.FAILED
    )
    terminal = _terminal_request(
        request,
        status=status,
        evidence_ids=[],
        completed_at=state["as_of"],
    )
    return _finish_turn(
        state,
        session,
        terminal,
        outcome=outcome,
        summary=summary,
        searched_sources=searched_sources or [],
        limitations=limitations,
        evidence_ids=[],
        errors=errors or [],
        run_summary=run_summary,
        invoked=invoked,
    )


def _finish_turn(
    state: ResearchGraphState,
    session: ThesisValidationSession,
    terminal_request: ResearchRequest,
    *,
    outcome: ResearchFindingOutcome,
    summary: str,
    searched_sources: list[str],
    limitations: list[str],
    evidence_ids: list[str],
    evidence_pool: list[EvidenceRecord] | None = None,
    evidence_collection=None,
    errors: list[str],
    run_summary: object | None,
    invoked: bool,
) -> ResearchGraphState:
    if session.pending_request_fingerprint is None or session.pending_reviewer_reasoning is None:
        return _abandon_invalid_session(
            state,
            session,
            "pending request context is missing while recording ResearchFinding",
        )
    finding = ResearchFinding(
        finding_id=build_research_finding_id(
            run_id=terminal_request.run_id,
            request_id=terminal_request.request_id,
            attempt=terminal_request.attempt,
        ),
        run_id=terminal_request.run_id,
        request_id=terminal_request.request_id,
        thesis_id=terminal_request.thesis_id,
        target=terminal_request.target,
        assigned_domain=terminal_request.assigned_domain,
        outcome=outcome,
        summary=summary,
        searched_sources=searched_sources,
        limitations=limitations,
        evidence_ids=evidence_ids,
        attempt=terminal_request.attempt,
        created_at=state["as_of"],
    )
    turn = ValidationResearchTurn(
        round_number=len(session.previous_turns) + 1,
        request_fingerprint=session.pending_request_fingerprint,
        request=terminal_request,
        finding=finding,
        reviewer_reasoning_before_request=session.pending_reviewer_reasoning,
    )
    updated_session = ThesisValidationSession(
        thesis_id=session.thesis_id,
        previous_turns=(*session.previous_turns, turn),
        used_request_fingerprints=session.used_request_fingerprints,
    )
    updates: ResearchGraphState = {
        "research_requests": [terminal_request],
        "research_findings": [finding],
        "active_validation_session": updated_session,
        "active_validation_request_id": None,
        "research_request_count": state.get("research_request_count", 0) + 1,
        "validation_research_request_count": (
            state.get("validation_research_request_count", 0) + 1
        ),
        "validation_round": len(updated_session.previous_turns),
    }
    if evidence_pool:
        updates["evidence_pool"] = evidence_pool
    if evidence_collection is not None:
        updates["evidence_collection"] = evidence_collection
    if errors:
        updates["errors"] = [f"validation research: {item}" for item in errors]
    if invoked:
        count_field = _domain_count_field(terminal_request.assigned_domain)
        updates[count_field] = state.get(count_field, 0) + 1
    summary_field = _domain_summary_field(terminal_request.assigned_domain)
    if run_summary is not None and summary_field is not None:
        updates[summary_field] = run_summary
    return updates


def _terminal_request(
    request: ResearchRequest,
    *,
    status: ResearchRequestStatus,
    evidence_ids: list[str],
    completed_at: datetime,
) -> ResearchRequest:
    return ResearchRequest.model_validate(
        {
            **request.model_dump(),
            "status": status,
            "result_evidence_ids": evidence_ids,
            "completed_at": completed_at,
        }
    )


def _outcome_for_terminal_request(
    status: ResearchRequestStatus,
    *,
    errors: list[str],
    searched_sources: list[str],
) -> ResearchFindingOutcome:
    if status is ResearchRequestStatus.NO_NEW_EVIDENCE:
        return (
            ResearchFindingOutcome.NO_MATCHING_EVIDENCE
            if searched_sources
            else ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE
        )
    if status is ResearchRequestStatus.CANCELLED_BY_BUDGET:
        return ResearchFindingOutcome.BUDGET_EXHAUSTED
    return _failure_outcome(errors)


def _failure_outcome(messages: list[str]) -> ResearchFindingOutcome:
    joined = " ".join(messages).casefold()
    if any(marker in joined for marker in _NETWORK_FAILURE_MARKERS):
        return ResearchFindingOutcome.SOURCE_UNAVAILABLE
    return ResearchFindingOutcome.REQUEST_FAILED


def _finding_summary(outcome: ResearchFindingOutcome) -> str:
    return {
        ResearchFindingOutcome.NO_MATCHING_EVIDENCE: (
            "已执行当前领域查证，但没有形成新的可引用证据。"
        ),
        ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE: (
            "当前领域 Tool 链没有形成能够回答该问题的查询。"
        ),
        ResearchFindingOutcome.SOURCE_UNAVAILABLE: "查证所需数据源暂时不可用。",
        ResearchFindingOutcome.REQUEST_FAILED: "查证请求执行失败，没有形成可引用证据。",
        ResearchFindingOutcome.BUDGET_EXHAUSTED: "查证预算已耗尽，本次请求未执行完成。",
        ResearchFindingOutcome.EVIDENCE_FOUND: "查证产生了新的可引用证据。",
    }[outcome]


def _finding_limitation(outcome: ResearchFindingOutcome) -> str:
    return {
        ResearchFindingOutcome.NO_MATCHING_EVIDENCE: (
            "没有匹配结果不能证明相关事实不存在，也不能作为反向证据。"
        ),
        ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE: (
            "现有 Tool 覆盖不足，不能据此支持或反驳当前观点。"
        ),
        ResearchFindingOutcome.SOURCE_UNAVAILABLE: (
            "来源不可用只代表本轮无法取数，不代表事实不存在。"
        ),
        ResearchFindingOutcome.REQUEST_FAILED: ("执行失败不构成支持或反向证据。"),
        ResearchFindingOutcome.BUDGET_EXHAUSTED: ("预算停止不改变观点本身的事实状态。"),
        ResearchFindingOutcome.EVIDENCE_FOUND: "",
    }[outcome]


def _searched_sources(observations: object, records: list[object]) -> list[str]:
    names: set[str] = set()
    if isinstance(observations, (list, tuple)):
        for item in observations:
            name = getattr(item, "tool_name", None)
            if name is None and isinstance(item, Mapping):
                name = item.get("tool_name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    if not names:
        names.update(
            _sources_from_evidence([item for item in records if isinstance(item, EvidenceRecord)])
        )
    return sorted(names)


def _sources_from_evidence(records: list[EvidenceRecord]) -> list[str]:
    return sorted(
        {
            f"{source.provider}:{source.interface}"
            for record in records
            for source in record.source_refs
        }
    )


def _conflicting_evidence_id(current: list[EvidenceRecord], updates: list[object]) -> str | None:
    catalog = {item.evidence_id: item for item in current}
    for item in updates:
        if not isinstance(item, EvidenceRecord):
            continue
        existing = catalog.get(item.evidence_id)
        if existing is not None and existing != item:
            return item.evidence_id
    return None


def _merge_evidence(
    current: list[EvidenceRecord], updates: list[EvidenceRecord]
) -> list[EvidenceRecord]:
    merged = list(current)
    positions = {item.evidence_id: index for index, item in enumerate(merged)}
    for item in updates:
        if item.evidence_id in positions:
            merged[positions[item.evidence_id]] = item
        else:
            positions[item.evidence_id] = len(merged)
            merged.append(item)
    return merged


def _new_collector_errors(state: ResearchGraphState, collection) -> list[str]:
    previous = state.get("evidence_collection")
    previous_rejected = (
        {item.evidence_id for item in previous.rejected} if previous is not None else set()
    )
    new_ids = sorted(
        item.evidence_id
        for item in collection.rejected
        if item.evidence_id not in previous_rejected
    )
    return ["Collector rejected newly returned evidence: " + ",".join(new_ids)] if new_ids else []


def _finalization_error(finalization, *, thesis, evidence) -> str | None:
    catalog = {item.evidence_id: item for item in evidence}
    referenced = {
        *finalization.supporting_evidence_ids,
        *finalization.contradicting_evidence_ids,
    }
    unknown = sorted(referenced - catalog.keys())
    if unknown:
        return "UNKNOWN_EVIDENCE_ID=" + ",".join(unknown)

    def decisive(ids: tuple[str, ...]) -> bool:
        return any(
            _same_target_identity(catalog[item].target, thesis.target)
            and catalog[item].verification_status in _DECISIVE_EVIDENCE_STATUSES
            for item in ids
        )

    if finalization.final_status is ThesisValidationStatus.SUPPORTED and not decisive(
        finalization.supporting_evidence_ids
    ):
        return "SUPPORTED_WITHOUT_DIRECT_DECISIVE_SUPPORT"
    if finalization.final_status is ThesisValidationStatus.REFUTED and not decisive(
        finalization.contradicting_evidence_ids
    ):
        return "REFUTED_WITHOUT_DIRECT_DECISIVE_CONTRADICTION"
    if finalization.final_status is ThesisValidationStatus.MIXED and not (
        decisive(finalization.supporting_evidence_ids)
        and decisive(finalization.contradicting_evidence_ids)
    ):
        return "MIXED_REQUIRES_DIRECT_DECISIVE_EVIDENCE_ON_BOTH_SIDES"
    return None


def _finalize_thesis(thesis, finalization, *, round_number: int, as_of: datetime):
    return ThesisRecord.model_validate(
        {
            **thesis.model_dump(),
            "validation": ThesisValidation(
                status=finalization.final_status,
                confidence=finalization.confidence,
                round=round_number,
            ),
            "supporting_evidence_ids": list(finalization.supporting_evidence_ids),
            "contradicting_evidence_ids": list(finalization.contradicting_evidence_ids),
            "reasoning_summary": finalization.reasoning_summary,
            "missing_questions": list(finalization.remaining_questions),
            "revision": thesis.revision + 1,
            "updated_at": as_of,
        }
    )


def _force_inconclusive_updates(
    state,
    thesis,
    session,
    *,
    reason: str,
    error: str,
) -> ResearchGraphState:
    missing_questions = list(thesis.missing_questions)
    if reason not in missing_questions:
        missing_questions.append(reason)
    forced = ThesisRecord.model_validate(
        {
            **thesis.model_dump(),
            "validation": ThesisValidation(
                status=ThesisValidationStatus.INCONCLUSIVE,
                confidence=1.0,
                round=len(session.previous_turns),
            ),
            "reasoning_summary": (
                f"{thesis.reasoning_summary or ''}\n查证停止原因：{reason}"
            ).strip(),
            "missing_questions": missing_questions,
            "revision": thesis.revision + 1,
            "updated_at": state["as_of"],
        }
    )
    return {
        "thesis_pool": [forced],
        "active_validation_session": None,
        "active_validation_request_id": None,
        "validation_round": len(session.previous_turns),
        "errors": [error],
    }


def _abandon_invalid_session(state, session, reason: str) -> ResearchGraphState:
    thesis = _find_thesis(state, session.thesis_id)
    if thesis is None:
        return {
            "active_validation_session": None,
            "active_validation_request_id": None,
            "validation_stop_reason": "invalid_state",
            "errors": [f"validator invalid state: {reason}"],
        }
    updates = _force_inconclusive_updates(
        state,
        thesis,
        session,
        reason=reason,
        error=f"validator invalid state: {reason}",
    )
    updates["validation_stop_reason"] = "invalid_state"
    return updates


def _recover_missing_validation_session(state: ResearchGraphState) -> ResearchGraphState:
    """损坏的恢复状态不能靠 active_request_id 在 review/research 间无限往返。"""

    request_id = state.get("active_validation_request_id")
    request = _find_request(state, request_id) if request_id is not None else None
    error = "validator invalid state: active request exists without validation session"
    updates: ResearchGraphState = {
        "active_validation_request_id": None,
        "validation_stop_reason": "invalid_state",
        "errors": [error],
    }
    if request is None:
        return updates
    if request.status in _ACTIVE_REQUEST_STATUSES:
        updates["research_requests"] = [
            _terminal_request(
                request,
                status=ResearchRequestStatus.FAILED,
                evidence_ids=[],
                completed_at=state["as_of"],
            )
        ]
    thesis = _find_thesis(state, request.thesis_id)
    if thesis is None or thesis.validation.status is not ThesisValidationStatus.UNDER_REVIEW:
        return updates
    forced = _force_inconclusive_updates(
        state,
        thesis,
        ThesisValidationSession(thesis_id=thesis.thesis_id),
        reason="活动查证请求缺失连续会话状态，无法可靠恢复。",
        error=error,
    )
    if "research_requests" in updates:
        forced["research_requests"] = updates["research_requests"]
    forced["validation_stop_reason"] = "invalid_state"
    return forced


def _replace_thesis(
    thesis: ThesisRecord,
    *,
    validation: ThesisValidation,
    as_of: datetime,
) -> ThesisRecord:
    return ThesisRecord.model_validate(
        {
            **thesis.model_dump(),
            "validation": validation,
            "revision": thesis.revision + 1,
            "updated_at": as_of,
        }
    )


def _assemble_discovered_candidates(
    drafts: tuple[CandidateThesisDraft, ...],
    *,
    parent: ThesisRecord,
    evidence: list[CollectedEvidenceSummary],
    existing: list[ThesisRecord],
    run_id: str,
    as_of: datetime,
    limit: int,
) -> tuple[list[ThesisRecord], list[str]]:
    catalog = {item.evidence_id: item for item in evidence}
    existing_ids = {item.thesis_id for item in existing}
    accepted: list[ThesisRecord] = []
    errors: list[str] = []
    for draft in drafts:
        if len(accepted) >= limit:
            errors.append("validator discovery rejected: per-run candidate limit reached")
            continue
        referenced = {*draft.supporting_evidence_ids, *draft.contradicting_evidence_ids}
        unknown = sorted(referenced - catalog.keys())
        if unknown:
            errors.append("validator discovery rejected unknown evidence: " + ",".join(unknown))
            continue
        if not any(
            _same_target_identity(catalog[item].target, draft.target)
            for item in draft.supporting_evidence_ids
        ):
            errors.append("validator discovery rejected: target not grounded by support")
            continue
        if (
            draft.target == parent.target
            and draft.title == parent.title
            and draft.description == parent.description
        ):
            errors.append("validator discovery rejected: exact restatement of parent thesis")
            continue
        digest = sha256(
            f"{run_id}|{parent.thesis_id}|{draft.model_dump_json()}".encode()
        ).hexdigest()[:20]
        thesis_id = f"th_{digest}"
        if thesis_id in existing_ids:
            errors.append("validator discovery rejected: exact duplicate candidate")
            continue
        existing_ids.add(thesis_id)
        accepted.append(
            ThesisRecord(
                thesis_id=thesis_id,
                run_id=run_id,
                target=draft.target,
                as_of=as_of,
                title=draft.title,
                description=draft.description,
                direction=draft.direction,
                horizon=draft.horizon,
                origin=ThesisOrigin(
                    type=ThesisOriginType.VALIDATOR_DISCOVERY,
                    agent=_AGENT_NAME,
                    parent_thesis_ids=[parent.thesis_id],
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
        )
    return accepted, errors


def _merge_scalar_updates(
    first: ResearchGraphState, second: ResearchGraphState
) -> ResearchGraphState:
    merged: ResearchGraphState = {**first, **second}
    if "thesis_pool" in first and "thesis_pool" in second:
        merged["thesis_pool"] = [*second["thesis_pool"], *first["thesis_pool"]]
    if "errors" in first and "errors" in second:
        merged["errors"] = [*first["errors"], *second["errors"]]
    return merged


def _domain_count_field(domain: EvidenceDomain) -> str:
    return {
        EvidenceDomain.TECHNICAL: "technical_request_count",
        EvidenceDomain.FUNDAMENTAL: "fundamental_request_count",
        EvidenceDomain.SENTIMENT_FLOW: "sentiment_flow_request_count",
        EvidenceDomain.EVENT: "event_request_count",
    }.get(domain, "research_request_count")


def _domain_summary_field(domain: EvidenceDomain) -> str | None:
    return {
        EvidenceDomain.TECHNICAL: "technical_run_summary",
        EvidenceDomain.FUNDAMENTAL: "fundamental_run_summary",
        EvidenceDomain.SENTIMENT_FLOW: "sentiment_flow_run_summary",
        EvidenceDomain.EVENT: "event_run_summary",
    }.get(domain)


def _same_target_identity(left, right) -> bool:
    return left.type is right.type and left.code == right.code
