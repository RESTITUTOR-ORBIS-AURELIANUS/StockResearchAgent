"""正式协商的原子三阶段：交换理由、原提议方修订、受影响冲突组重评。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from typing import Literal

from stock_research_agent.agents.negotiation import (
    MAX_DEBATE_ROUNDS,
    DebateScoreDraft,
    DebateScoreEntry,
    DebateScoreInput,
    DebateScoreRecord,
    NegotiationArgument,
    NegotiationLimits,
    NegotiationModelRunSummary,
    NegotiationProposalPool,
    NegotiationRoundSummary,
    NegotiationStageRunSummary,
    PortfolioNegotiationModel,
    ProposalRevisionApplicationSummary,
    ProposalRevisionDecision,
    ProposalRevisionDraft,
    ProposalRevisionInput,
    ProposalRevisionRecord,
    ProposalRevisionSnapshot,
    ReasonExchangeDraft,
    ReasonExchangeInput,
    ReasonExchangeItem,
    ReasonExchangeRecord,
    counterpart_of,
)
from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain.enums import (
    ConsensusRoute,
    PortfolioManager,
    ProposalRevisionAction,
    ProposalStatus,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.graph.nodes.consensus_gate import (
    consensus_gate_source_fingerprint,
)
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

ReasonExchangeRoute = Literal["revise", "failed"]
ProposalRevisionRoute = Literal["score", "complete_without_score", "failed"]
DebateScoreRoute = Literal["validate", "failed"]
RoundCompletionRoute = Literal["gate", "failed"]

_MANAGERS = (
    PortfolioManager.AGGRESSIVE,
    PortfolioManager.CONSERVATIVE,
)
_ELIGIBLE_THESIS_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.MIXED,
}
_INACTIVE_STATUSES = {
    ProposalStatus.REJECTED,
    ProposalStatus.WITHDRAWN,
    ProposalStatus.EXCLUDED,
}


def build_begin_negotiation_round_node(*, limits: NegotiationLimits | None = None):
    configured = limits or NegotiationLimits()

    def node(state: ResearchGraphState) -> ResearchGraphState:
        report = state.get("consensus_gate_report")
        current_round = state.get("debate_round", 0)
        if (
            report is None
            or report.route is not ConsensusRoute.NEGOTIATE
            or report.debate_round != current_round
            or report.max_rounds != configured.max_rounds
            or not report.negotiating_item_ids
            or current_round >= configured.max_rounds
        ):
            return _error("BeginNegotiationRoundNode skipped: current gate is not negotiable")
        return {
            "debate_round": current_round + 1,
            "proposal_revision_application_summary": None,
            "negotiation_score_validation_report": None,
        }

    return node


def build_reason_exchange_stage_node(
    aggressive_model: PortfolioNegotiationModel,
    conservative_model: PortfolioNegotiationModel,
    *,
    limits: NegotiationLimits | None = None,
):
    configured = limits or NegotiationLimits()
    models = {
        PortfolioManager.AGGRESSIVE: aggressive_model,
        PortfolioManager.CONSERVATIVE: conservative_model,
    }

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        pool = state.get("negotiation_proposal_pool")
        debate_round = state.get("debate_round", 0)
        gate = state.get("consensus_gate_report")
        state_error = _negotiation_state_error(state, pool, debate_round, gate_round_offset=1)
        if state_error is not None or gate is None:
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                _zero_fingerprint(),
                error=f"ReasonExchangeStage skipped: {state_error or 'gate is missing'}",
            )

        assert pool is not None
        work_by_manager = {
            manager: tuple(
                item
                for item in pool.proposal_items
                if item.proposer is counterpart_of(manager)
                and item.status is ProposalStatus.NEGOTIATING
            )
            for manager in _MANAGERS
        }
        active_managers = tuple(
            manager for manager in _MANAGERS if work_by_manager[manager]
        )
        fingerprint = _fingerprint(
            {
                "stage": "REASON_EXCHANGE",
                "round": debate_round,
                "pool": pool.model_dump(mode="json"),
                "gate": gate.model_dump(mode="json"),
            }
        )
        if not active_managers:
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                fingerprint,
                error="ReasonExchangeStage skipped: gate listed no live negotiating items",
            )
        existing = _records_for_round(state.get("reason_exchange_records", []), debate_round)
        if existing:
            if (
                {record.reviewer for record in existing} == set(active_managers)
                and all(record.source_fingerprint == fingerprint for record in existing)
            ):
                return {}
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                fingerprint,
                requested=active_managers,
                error="ReasonExchangeStage refused incompatible records for the same round",
            )

        try:
            theses = _thesis_summaries(state, configured)
            requests = {
                manager: _reason_request(
                    state,
                    pool,
                    manager,
                    debate_round,
                    work_by_manager[manager],
                    theses,
                )
                for manager in active_managers
            }
        except Exception as exc:
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                fingerprint,
                requested=active_managers,
                error=f"ReasonExchangeStage input rejected: {type(exc).__name__}",
            )

        contexts = {
            manager: len(request.model_dump_json(indent=2))
            for manager, request in requests.items()
        }
        oversized = [
            manager
            for manager, size in contexts.items()
            if size > configured.max_context_characters
        ]
        if oversized:
            summaries = [
                _model_summary(
                    "REASON_EXCHANGE",
                    manager,
                    debate_round,
                    len(requests[manager].counterpart_proposals),
                    0,
                    contexts[manager],
                    model_called=False,
                    stop_reason=(
                        "context_limit_exceeded" if manager in oversized else "invalid_state"
                    ),
                )
                for manager in active_managers
            ]
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                fingerprint,
                requested=active_managers,
                summaries=summaries,
                error="ReasonExchangeStage context exceeds configured hard limit",
            )

        results = await _gather_calls(
            tuple(
                models[manager].exchange_reasons(requests[manager])
                for manager in active_managers
            )
        )
        records: list[ReasonExchangeRecord] = []
        summaries: list[NegotiationModelRunSummary] = []
        staged: list[PortfolioManager] = []
        errors: list[str] = []
        for manager, result in zip(active_managers, results, strict=True):
            request = requests[manager]
            if isinstance(result, BaseException):
                summaries.append(
                    _model_summary(
                        "REASON_EXCHANGE",
                        manager,
                        debate_round,
                        len(request.counterpart_proposals),
                        0,
                        contexts[manager],
                        model_called=True,
                        stop_reason="model_error",
                    )
                )
                errors.append(f"{manager.value} reason exchange failed: {type(result).__name__}")
                continue
            try:
                draft = ReasonExchangeDraft.model_validate(result)
                record = _assemble_reason_record(
                    draft,
                    request=request,
                    source_fingerprint=fingerprint,
                )
            except Exception as exc:
                summaries.append(
                    _model_summary(
                        "REASON_EXCHANGE",
                        manager,
                        debate_round,
                        len(request.counterpart_proposals),
                        len(getattr(result, "responses", ())),
                        contexts[manager],
                        model_called=True,
                        stop_reason="rejected_output",
                    )
                )
                errors.append(
                    f"{manager.value} reason exchange rejected: {describe_exception(exc)}"
                )
                continue
            records.append(record)
            staged.append(manager)
            summaries.append(
                _model_summary(
                    "REASON_EXCHANGE",
                    manager,
                    debate_round,
                    len(request.counterpart_proposals),
                    len(record.responses),
                    contexts[manager],
                    model_called=True,
                    stop_reason="complete",
                )
            )
        if errors:
            return _stage_failure(
                "REASON_EXCHANGE",
                debate_round,
                fingerprint,
                requested=active_managers,
                called=active_managers,
                staged=tuple(staged),
                summaries=summaries,
                error="; ".join(errors),
            )
        return {
            "reason_exchange_records": records,
            "negotiation_model_run_summaries": summaries,
            "negotiation_stage_run_summaries": [
                _stage_summary(
                    "REASON_EXCHANGE",
                    debate_round,
                    fingerprint,
                    active_managers,
                    active_managers,
                    active_managers,
                    active_managers,
                    "complete",
                )
            ],
        }

    return node


def route_after_reason_exchange(state: ResearchGraphState) -> ReasonExchangeRoute:
    round_number = state.get("debate_round", 0)
    summary = _current_stage_summary(state, "REASON_EXCHANGE", round_number)
    if summary is not None and summary.stop_reason == "complete":
        return "revise"
    return "failed"


def build_proposal_revision_stage_node(
    aggressive_model: PortfolioNegotiationModel,
    conservative_model: PortfolioNegotiationModel,
    *,
    limits: NegotiationLimits | None = None,
):
    configured = limits or NegotiationLimits()
    models = {
        PortfolioManager.AGGRESSIVE: aggressive_model,
        PortfolioManager.CONSERVATIVE: conservative_model,
    }

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        pool = state.get("negotiation_proposal_pool")
        debate_round = state.get("debate_round", 0)
        state_error = _negotiation_state_error(state, pool, debate_round, gate_round_offset=1)
        if state_error is not None:
            return _revision_stage_failure(
                debate_round,
                _zero_fingerprint(),
                error=f"ProposalRevisionStage skipped: {state_error}",
            )
        assert pool is not None
        own_by_manager = {
            manager: tuple(
                item
                for item in pool.proposal_items
                if item.proposer is manager and item.status is ProposalStatus.NEGOTIATING
            )
            for manager in _MANAGERS
        }
        active_managers = tuple(manager for manager in _MANAGERS if own_by_manager[manager])
        exchanges = tuple(
            record
            for record in state.get("reason_exchange_records", [])
            if record.debate_round == debate_round
        )
        exchange_by_reviewer = {record.reviewer: record for record in exchanges}
        required_reviewers = {counterpart_of(manager) for manager in active_managers}
        fingerprint = _fingerprint(
            {
                "stage": "PROPOSAL_REVISION",
                "round": debate_round,
                "pool": pool.model_dump(mode="json"),
                "exchanges": [record.model_dump(mode="json") for record in exchanges],
            }
        )
        if not active_managers or set(exchange_by_reviewer) != required_reviewers:
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                error="ProposalRevisionStage requires exact current-round exchange coverage",
            )
        existing = _records_for_round(state.get("proposal_revision_records", []), debate_round)
        if existing:
            application = state.get("proposal_revision_application_summary")
            if (
                {record.proposer for record in existing} == set(active_managers)
                and all(record.source_fingerprint == fingerprint for record in existing)
                and application is not None
                and application.debate_round == debate_round
                and application.source_fingerprint == fingerprint
            ):
                return {}
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                error="ProposalRevisionStage refused incompatible records for the same round",
            )

        try:
            theses = _thesis_summaries(state, configured)
            requests = {
                manager: ProposalRevisionInput(
                    run_id=state["run_id"],
                    as_of=state["as_of"],
                    debate_round=debate_round,
                    proposer=manager,
                    own_proposals=own_by_manager[manager],
                    incoming_exchange=exchange_by_reviewer[counterpart_of(manager)],
                    theses=theses,
                    prior_revisions=tuple(
                        record
                        for record in state.get("proposal_revision_records", [])
                        if record.proposer is manager and record.debate_round < debate_round
                    ),
                    policy_notes=(
                        "只允许 KEEP、真正的 MODIFY 或 WITHDRAW。",
                        "只有正文、支持观点集合或撤回发生变化才触发重评分。",
                        "SUPPORTED/MIXED 是修订后唯一可直接引用的观点。",
                    ),
                )
                for manager in active_managers
            }
        except Exception as exc:
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                error=f"ProposalRevisionStage input rejected: {type(exc).__name__}",
            )
        contexts = {
            manager: len(request.model_dump_json(indent=2))
            for manager, request in requests.items()
        }
        if any(size > configured.max_context_characters for size in contexts.values()):
            summaries = [
                _model_summary(
                    "PROPOSAL_REVISION",
                    manager,
                    debate_round,
                    len(requests[manager].own_proposals),
                    0,
                    contexts[manager],
                    model_called=False,
                    stop_reason="context_limit_exceeded",
                )
                for manager in active_managers
            ]
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                summaries=summaries,
                error="ProposalRevisionStage context exceeds configured hard limit",
            )

        results = await _gather_calls(
            tuple(
                models[manager].revise_proposals(requests[manager])
                for manager in active_managers
            )
        )
        records: list[ProposalRevisionRecord] = []
        summaries: list[NegotiationModelRunSummary] = []
        staged: list[PortfolioManager] = []
        errors: list[str] = []
        updates_by_id: dict[str, ProposalItem] = {}
        for manager, result in zip(active_managers, results, strict=True):
            request = requests[manager]
            if isinstance(result, BaseException):
                summaries.append(
                    _model_summary(
                        "PROPOSAL_REVISION",
                        manager,
                        debate_round,
                        len(request.own_proposals),
                        0,
                        contexts[manager],
                        model_called=True,
                        stop_reason="model_error",
                    )
                )
                errors.append(f"{manager.value} proposal revision failed: {type(result).__name__}")
                continue
            try:
                draft = ProposalRevisionDraft.model_validate(result)
                record, manager_updates = _assemble_revision_record(
                    draft,
                    request=request,
                    source_fingerprint=fingerprint,
                )
            except Exception as exc:
                summaries.append(
                    _model_summary(
                        "PROPOSAL_REVISION",
                        manager,
                        debate_round,
                        len(request.own_proposals),
                        len(getattr(result, "decisions", ())),
                        contexts[manager],
                        model_called=True,
                        stop_reason="rejected_output",
                    )
                )
                errors.append(
                    f"{manager.value} proposal revision rejected: {describe_exception(exc)}"
                )
                continue
            records.append(record)
            updates_by_id.update(manager_updates)
            staged.append(manager)
            summaries.append(
                _model_summary(
                    "PROPOSAL_REVISION",
                    manager,
                    debate_round,
                    len(request.own_proposals),
                    len(record.decisions),
                    contexts[manager],
                    model_called=True,
                    stop_reason="complete",
                )
            )
        if errors:
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                called=active_managers,
                staged=tuple(staged),
                summaries=summaries,
                error="; ".join(errors),
            )

        updated_items = tuple(updates_by_id.get(item.item_id, item) for item in pool.proposal_items)
        try:
            updated_pool = NegotiationProposalPool(
                **pool.model_dump(exclude={"proposal_items"}),
                proposal_items=updated_items,
            )
        except Exception as exc:
            return _revision_stage_failure(
                debate_round,
                fingerprint,
                requested=active_managers,
                called=active_managers,
                staged=active_managers,
                summaries=summaries,
                error=f"ProposalRevisionStage atomic application failed: {type(exc).__name__}",
            )

        decisions = [decision for record in records for decision in record.decisions]
        material_ids = tuple(
            decision.item_id for decision in decisions if decision.material_change
        )
        if material_ids:
            touched_groups = tuple(
                dict.fromkeys(
                    decision.conflict_group
                    for decision in decisions
                    if decision.material_change
                )
            )
            rescore_ids = tuple(
                item.item_id
                for item in updated_pool.proposal_items
                if item.conflict_group in touched_groups
                and item.status not in _INACTIVE_STATUSES
                and item.status is not ProposalStatus.AGREED
            )
            application = ProposalRevisionApplicationSummary(
                debate_round=debate_round,
                source_fingerprint=fingerprint,
                material_change_item_ids=material_ids,
                withdrawn_item_ids=tuple(
                    decision.item_id
                    for decision in decisions
                    if decision.decision is ProposalRevisionAction.WITHDRAW
                ),
                touched_conflict_groups=touched_groups,
                rescore_item_ids=rescore_ids,
                stop_reason="complete",
            )
        else:
            application = ProposalRevisionApplicationSummary(
                debate_round=debate_round,
                source_fingerprint=fingerprint,
                stop_reason="no_material_change",
            )
        return {
            "negotiation_proposal_pool": updated_pool,
            "proposal_revision_records": records,
            "proposal_revision_application_summary": application,
            "negotiation_model_run_summaries": summaries,
            "negotiation_stage_run_summaries": [
                _stage_summary(
                    "PROPOSAL_REVISION",
                    debate_round,
                    fingerprint,
                    active_managers,
                    active_managers,
                    active_managers,
                    active_managers,
                    "complete",
                )
            ],
        }

    return node


def route_after_proposal_revision(state: ResearchGraphState) -> ProposalRevisionRoute:
    round_number = state.get("debate_round", 0)
    stage = _current_stage_summary(state, "PROPOSAL_REVISION", round_number)
    application = state.get("proposal_revision_application_summary")
    if (
        stage is None
        or stage.stop_reason != "complete"
        or application is None
        or application.debate_round != round_number
        or application.source_fingerprint != stage.source_fingerprint
        or application.stop_reason == "invalid_state"
    ):
        return "failed"
    if application.rescore_item_ids:
        return "score"
    return "complete_without_score"


def build_debate_score_stage_node(
    aggressive_model: PortfolioNegotiationModel,
    conservative_model: PortfolioNegotiationModel,
    *,
    limits: NegotiationLimits | None = None,
):
    configured = limits or NegotiationLimits()
    models = {
        PortfolioManager.AGGRESSIVE: aggressive_model,
        PortfolioManager.CONSERVATIVE: conservative_model,
    }

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        pool = state.get("negotiation_proposal_pool")
        debate_round = state.get("debate_round", 0)
        application = state.get("proposal_revision_application_summary")
        if (
            pool is None
            or application is None
            or application.debate_round != debate_round
            or application.stop_reason != "complete"
            or not application.rescore_item_ids
        ):
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                _zero_fingerprint(),
                error="DebateScoreStage skipped: no valid rescore application",
            )
        current_revisions = tuple(
            record
            for record in state.get("proposal_revision_records", [])
            if record.debate_round == debate_round
        )
        if not current_revisions:
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                _zero_fingerprint(),
                error="DebateScoreStage skipped: current revision records are missing",
            )
        by_id = {item.item_id: item for item in pool.proposal_items}
        try:
            items_to_score = tuple(by_id[item_id] for item_id in application.rescore_item_ids)
        except KeyError:
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                _zero_fingerprint(),
                error="DebateScoreStage skipped: rescore catalog references a missing item",
            )
        if any(item.status in _INACTIVE_STATUSES for item in items_to_score):
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                _zero_fingerprint(),
                error="DebateScoreStage skipped: inactive item entered the rescore closure",
            )
        fingerprint = _fingerprint(
            {
                "stage": "DEBATE_SCORE",
                "round": debate_round,
                "pool": pool.model_dump(mode="json"),
                "application": application.model_dump(mode="json"),
                "revisions": [record.model_dump(mode="json") for record in current_revisions],
            }
        )
        existing = _records_for_round(state.get("debate_score_records", []), debate_round)
        if existing:
            if (
                {record.manager for record in existing} == set(_MANAGERS)
                and all(record.source_fingerprint == fingerprint for record in existing)
            ):
                return {}
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                fingerprint,
                requested=_MANAGERS,
                error="DebateScoreStage refused incompatible records for the same round",
            )
        try:
            theses = _thesis_summaries(state, configured)
            active_pool = tuple(
                item for item in pool.proposal_items if item.status not in _INACTIVE_STATUSES
            )
            requests = {
                manager: DebateScoreInput(
                    run_id=state["run_id"],
                    as_of=state["as_of"],
                    debate_round=debate_round,
                    manager=manager,
                    items_to_score=items_to_score,
                    active_proposal_pool=active_pool,
                    source_revision_records=current_revisions,
                    theses=theses,
                    policy_notes=(
                        "必须重评受影响决策槽闭包中的全部存活建议。",
                        "同一经理对每对互斥建议的评分和必须小于或等于 0。",
                        "对自己仍存活的建议必须保持正分，否则应在修订阶段撤回。",
                    ),
                )
                for manager in _MANAGERS
            }
        except Exception as exc:
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                fingerprint,
                requested=_MANAGERS,
                error=f"DebateScoreStage input rejected: {type(exc).__name__}",
            )
        contexts = {
            manager: len(request.model_dump_json(indent=2))
            for manager, request in requests.items()
        }
        if any(size > configured.max_context_characters for size in contexts.values()):
            summaries = [
                _model_summary(
                    "DEBATE_SCORE",
                    manager,
                    debate_round,
                    len(items_to_score),
                    0,
                    contexts[manager],
                    model_called=False,
                    stop_reason="context_limit_exceeded",
                )
                for manager in _MANAGERS
            ]
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                fingerprint,
                requested=_MANAGERS,
                summaries=summaries,
                error="DebateScoreStage context exceeds configured hard limit",
            )

        results = await _gather_calls(
            tuple(models[manager].score_revisions(requests[manager]) for manager in _MANAGERS)
        )
        records: list[DebateScoreRecord] = []
        summaries: list[NegotiationModelRunSummary] = []
        staged: list[PortfolioManager] = []
        errors: list[str] = []
        for manager, result in zip(_MANAGERS, results, strict=True):
            request = requests[manager]
            if isinstance(result, BaseException):
                summaries.append(
                    _model_summary(
                        "DEBATE_SCORE",
                        manager,
                        debate_round,
                        len(items_to_score),
                        0,
                        contexts[manager],
                        model_called=True,
                        stop_reason="model_error",
                    )
                )
                errors.append(f"{manager.value} debate score failed: {type(result).__name__}")
                continue
            try:
                draft = DebateScoreDraft.model_validate(result)
                record = _assemble_score_record(
                    draft,
                    request=request,
                    source_fingerprint=fingerprint,
                )
            except Exception as exc:
                summaries.append(
                    _model_summary(
                        "DEBATE_SCORE",
                        manager,
                        debate_round,
                        len(items_to_score),
                        len(getattr(result, "evaluations", ())),
                        contexts[manager],
                        model_called=True,
                        stop_reason="rejected_output",
                    )
                )
                errors.append(
                    f"{manager.value} debate score rejected: {describe_exception(exc)}"
                )
                continue
            records.append(record)
            staged.append(manager)
            summaries.append(
                _model_summary(
                    "DEBATE_SCORE",
                    manager,
                    debate_round,
                    len(items_to_score),
                    len(record.evaluations),
                    contexts[manager],
                    model_called=True,
                    stop_reason="complete",
                )
            )
        if errors:
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                fingerprint,
                requested=_MANAGERS,
                called=_MANAGERS,
                staged=tuple(staged),
                summaries=summaries,
                error="; ".join(errors),
            )
        try:
            updated_pool = _apply_score_records(pool, records, application.rescore_item_ids)
        except Exception as exc:
            return _stage_failure(
                "DEBATE_SCORE",
                debate_round,
                fingerprint,
                requested=_MANAGERS,
                called=_MANAGERS,
                staged=_MANAGERS,
                summaries=summaries,
                error=f"DebateScoreStage atomic application failed: {type(exc).__name__}",
            )
        return {
            "negotiation_proposal_pool": updated_pool,
            "debate_score_records": records,
            "negotiation_model_run_summaries": summaries,
            "negotiation_stage_run_summaries": [
                _stage_summary(
                    "DEBATE_SCORE",
                    debate_round,
                    fingerprint,
                    _MANAGERS,
                    _MANAGERS,
                    _MANAGERS,
                    _MANAGERS,
                    "complete",
                )
            ],
        }

    return node


def route_after_debate_score(state: ResearchGraphState) -> DebateScoreRoute:
    summary = _current_stage_summary(state, "DEBATE_SCORE", state.get("debate_round", 0))
    if summary is not None and summary.stop_reason == "complete":
        return "validate"
    return "failed"


def complete_negotiation_round_without_rescore_node(
    state: ResearchGraphState,
) -> ResearchGraphState:
    return _complete_round(state, scored_managers=())


def complete_scored_negotiation_round_node(state: ResearchGraphState) -> ResearchGraphState:
    validation = state.get("negotiation_score_validation_report")
    if (
        validation is None
        or not validation.valid
        or validation.debate_round != state.get("debate_round")
    ):
        return _error("CompleteNegotiationRoundNode skipped: formal score validation is not valid")
    return _complete_round(state, scored_managers=_MANAGERS)


def route_after_round_completion(state: ResearchGraphState) -> RoundCompletionRoute:
    debate_round = state.get("debate_round", 0)
    if any(
        summary.debate_round == debate_round
        for summary in state.get("negotiation_round_summaries", [])
    ):
        return "gate"
    return "failed"


def _complete_round(
    state: ResearchGraphState,
    *,
    scored_managers: tuple[PortfolioManager, ...],
) -> ResearchGraphState:
    debate_round = state.get("debate_round", 0)
    gate = state.get("consensus_gate_report")
    application = state.get("proposal_revision_application_summary")
    if (
        gate is None
        or gate.debate_round != debate_round - 1
        or application is None
        or application.debate_round != debate_round
    ):
        return _error("CompleteNegotiationRoundNode skipped: round provenance is incomplete")
    existing = next(
        (
            summary
            for summary in state.get("negotiation_round_summaries", [])
            if summary.debate_round == debate_round
        ),
        None,
    )
    exchanged = tuple(
        manager
        for manager in _MANAGERS
        if any(
            record.debate_round == debate_round and record.reviewer is manager
            for record in state.get("reason_exchange_records", [])
        )
    )
    revised = tuple(
        manager
        for manager in _MANAGERS
        if any(
            record.debate_round == debate_round and record.proposer is manager
            for record in state.get("proposal_revision_records", [])
        )
    )
    summary = NegotiationRoundSummary(
        run_id=state["run_id"],
        as_of=state["as_of"],
        debate_round=debate_round,
        source_gate_fingerprint=gate.source_fingerprint,
        exchanged_managers=exchanged,
        revised_managers=revised,
        scored_managers=scored_managers,
        material_change_count=len(application.material_change_item_ids),
        stop_reason=(
            "no_material_change"
            if application.stop_reason == "no_material_change"
            else "complete"
        ),
    )
    if existing is not None:
        if existing == summary:
            return {}
        return _error("CompleteNegotiationRoundNode refused an incompatible round summary")
    return {"negotiation_round_summaries": [summary]}


def _negotiation_state_error(
    state: ResearchGraphState,
    pool: NegotiationProposalPool | None,
    debate_round: int,
    *,
    gate_round_offset: int,
) -> str | None:
    if pool is None:
        return "negotiation proposal pool is missing"
    if debate_round < 1:
        return "debate_round must be positive"
    if (
        pool.run_id != state.get("run_id")
        or pool.as_of != state.get("as_of")
        or pool.research_target != state.get("target")
    ):
        return "proposal pool scope mismatch"
    gate = state.get("consensus_gate_report")
    if (
        gate is None
        or gate.route is not ConsensusRoute.NEGOTIATE
        or gate.debate_round != debate_round - gate_round_offset
        or gate.run_id != pool.run_id
        or gate.as_of != pool.as_of
        or gate.source_fingerprint
        != consensus_gate_source_fingerprint(pool, gate.debate_round)
    ):
        return "source consensus gate is stale or not negotiable"
    return None


def _thesis_summaries(
    state: ResearchGraphState,
    limits: NegotiationLimits,
) -> tuple[DecisionThesisSummary, ...]:
    theses = list(state.get("thesis_pool", []))
    if len(theses) > limits.max_input_theses:
        raise ValueError("thesis count exceeds hard limit")
    summaries = tuple(DecisionThesisSummary.from_record(thesis) for thesis in theses)
    if any(
        thesis.run_id != state.get("run_id") or thesis.as_of != state.get("as_of")
        for thesis in theses
    ):
        raise ValueError("thesis scope mismatch")
    return summaries


def _reason_request(
    state: ResearchGraphState,
    pool: NegotiationProposalPool,
    manager: PortfolioManager,
    debate_round: int,
    counterpart_items: tuple[ProposalItem, ...],
    theses: tuple[DecisionThesisSummary, ...],
) -> ReasonExchangeInput:
    counterpart_groups = {item.conflict_group for item in counterpart_items}
    own_items = tuple(
        item
        for item in pool.proposal_items
        if item.proposer is manager
        and item.status is ProposalStatus.NEGOTIATING
        and item.conflict_group in counterpart_groups
    )
    return ReasonExchangeInput(
        run_id=state["run_id"],
        as_of=state["as_of"],
        debate_round=debate_round,
        reviewer=manager,
        own_proposals=own_items,
        counterpart_proposals=counterpart_items,
        theses=theses,
        prior_exchanges=tuple(
            record
            for record in state.get("reason_exchange_records", [])
            if record.reviewer is manager and record.debate_round < debate_round
        ),
        policy_notes=(
            "逐条回应对方仍处于 NEGOTIATING 的建议。",
            "本阶段只交换理由，不得修改提案或重新评分。",
            "新增理由本身不触发重评分。",
        ),
    )


def _assemble_reason_record(
    draft: ReasonExchangeDraft,
    *,
    request: ReasonExchangeInput,
    source_fingerprint: str,
) -> ReasonExchangeRecord:
    expected_ids = [item.item_id for item in request.counterpart_proposals]
    actual_ids = [response.counterpart_item_id for response in draft.responses]
    if actual_ids != expected_ids:
        raise ValueError("reason response coverage/order mismatch")
    counterpart_by_id = {item.item_id: item for item in request.counterpart_proposals}
    own_by_id = {item.item_id: item for item in request.own_proposals}
    thesis_ids = {thesis.thesis_id for thesis in request.theses}
    exchange_id = (
        f"exchange_r{request.debate_round}_{request.reviewer.name.lower()}_"
        f"{source_fingerprint[:12]}"
    )
    responses: list[ReasonExchangeItem] = []
    for response_index, response in enumerate(draft.responses, start=1):
        counterpart = counterpart_by_id[response.counterpart_item_id]
        if response.counterpart_revision != counterpart.revision:
            raise ValueError("counterpart revision mismatch")
        if not set(response.related_own_item_ids) <= set(own_by_id):
            raise ValueError("related own item is outside request scope")
        if any(
            own_by_id[item_id].conflict_group != counterpart.conflict_group
            for item_id in response.related_own_item_ids
        ):
            raise ValueError("related own item belongs to another decision slot")
        arguments: list[NegotiationArgument] = []
        for argument_index, argument in enumerate(response.arguments, start=1):
            if not set(argument.supporting_thesis_ids) <= thesis_ids:
                raise ValueError("argument cites an unknown thesis")
            arguments.append(
                NegotiationArgument(
                    argument_id=(
                        f"arg_r{request.debate_round}_{request.reviewer.name.lower()}_"
                        f"{response_index}_{argument_index}_{source_fingerprint[:8]}"
                    ),
                    **argument.model_dump(),
                )
            )
        responses.append(
            ReasonExchangeItem(
                counterpart_item_id=counterpart.item_id,
                counterpart_revision=counterpart.revision,
                conflict_group=counterpart.conflict_group,
                related_own_item_ids=response.related_own_item_ids,
                stance=response.stance,
                arguments=tuple(arguments),
                modification_suggestion=response.modification_suggestion,
            )
        )
    return ReasonExchangeRecord(
        exchange_id=exchange_id,
        run_id=request.run_id,
        as_of=request.as_of,
        debate_round=request.debate_round,
        reviewer=request.reviewer,
        source_fingerprint=source_fingerprint,
        responses=tuple(responses),
        created_at=request.as_of,
    )


def _assemble_revision_record(
    draft: ProposalRevisionDraft,
    *,
    request: ProposalRevisionInput,
    source_fingerprint: str,
) -> tuple[ProposalRevisionRecord, dict[str, ProposalItem]]:
    expected_ids = [item.item_id for item in request.own_proposals]
    actual_ids = [decision.item_id for decision in draft.decisions]
    if actual_ids != expected_ids:
        raise ValueError("revision coverage/order mismatch")
    own_by_id = {item.item_id: item for item in request.own_proposals}
    responses_by_id = {
        response.counterpart_item_id: response
        for response in request.incoming_exchange.responses
    }
    eligible_ids = {
        thesis.thesis_id
        for thesis in request.theses
        if thesis.validation_status in _ELIGIBLE_THESIS_STATUSES
    }
    record_id = (
        f"revision_r{request.debate_round}_{request.proposer.name.lower()}_"
        f"{source_fingerprint[:12]}"
    )
    decisions: list[ProposalRevisionDecision] = []
    updates: dict[str, ProposalItem] = {}
    for draft_decision in draft.decisions:
        item = own_by_id[draft_decision.item_id]
        response = responses_by_id.get(item.item_id)
        if response is None:
            raise ValueError("own proposal has no counterpart exchange")
        valid_argument_ids = {
            argument.argument_id for argument in response.arguments
        }
        if not set(draft_decision.responding_to_argument_ids) <= valid_argument_ids:
            raise ValueError("revision cites an unrelated argument")
        before = ProposalRevisionSnapshot(
            revision=item.revision,
            proposal=item.proposal,
            supporting_thesis_ids=tuple(item.supporting_thesis_ids),
            status=item.status,
        )
        if draft_decision.decision is ProposalRevisionAction.KEEP:
            updated = item.model_copy(deep=True)
        elif draft_decision.decision is ProposalRevisionAction.MODIFY:
            assert draft_decision.revised_proposal is not None
            assert draft_decision.revised_supporting_thesis_ids is not None
            if not set(draft_decision.revised_supporting_thesis_ids) <= eligible_ids:
                raise ValueError("revised proposal cites an ineligible thesis")
            updated = item.model_copy(
                deep=True,
                update={
                    "revision": item.revision + 1,
                    "proposal": draft_decision.revised_proposal,
                    "supporting_thesis_ids": list(
                        draft_decision.revised_supporting_thesis_ids
                    ),
                    "status": ProposalStatus.NEGOTIATING,
                },
            )
        else:
            updated = item.model_copy(
                deep=True,
                update={
                    "revision": item.revision + 1,
                    "status": ProposalStatus.WITHDRAWN,
                },
            )
        after = ProposalRevisionSnapshot(
            revision=updated.revision,
            proposal=updated.proposal,
            supporting_thesis_ids=tuple(updated.supporting_thesis_ids),
            status=updated.status,
        )
        changed_fields = tuple(
            field
            for field, changed in (
                ("proposal", before.proposal != after.proposal),
                (
                    "supporting_thesis_ids",
                    set(before.supporting_thesis_ids) != set(after.supporting_thesis_ids),
                ),
                ("status", before.status is not after.status),
            )
            if changed
        )
        decision = ProposalRevisionDecision(
            item_id=item.item_id,
            conflict_group=item.conflict_group,
            decision=draft_decision.decision,
            responding_to_argument_ids=draft_decision.responding_to_argument_ids,
            before=before,
            after=after,
            revision_reason=draft_decision.revision_reason,
            changed_fields=changed_fields,
            material_change=bool(changed_fields),
        )
        decisions.append(decision)
        updates[item.item_id] = updated
    record = ProposalRevisionRecord(
        revision_record_id=record_id,
        run_id=request.run_id,
        as_of=request.as_of,
        debate_round=request.debate_round,
        proposer=request.proposer,
        source_exchange_ids=(request.incoming_exchange.exchange_id,),
        source_fingerprint=source_fingerprint,
        decisions=tuple(decisions),
        created_at=request.as_of,
    )
    return record, updates


def _assemble_score_record(
    draft: DebateScoreDraft,
    *,
    request: DebateScoreInput,
    source_fingerprint: str,
) -> DebateScoreRecord:
    expected_ids = [item.item_id for item in request.items_to_score]
    actual_ids = [evaluation.item_id for evaluation in draft.evaluations]
    if actual_ids != expected_ids:
        raise ValueError("score coverage/order mismatch")
    items_by_id = {item.item_id: item for item in request.items_to_score}
    triggers_by_group: dict[str, list[str]] = {}
    for record in request.source_revision_records:
        for decision in record.decisions:
            if decision.material_change:
                triggers_by_group.setdefault(decision.conflict_group, []).append(
                    record.revision_record_id
                )
    entries: list[DebateScoreEntry] = []
    for draft_entry in draft.evaluations:
        item = items_by_id[draft_entry.item_id]
        if draft_entry.item_revision != item.revision:
            raise ValueError("score item revision mismatch")
        if item.proposer is request.manager and draft_entry.support_score <= 0:
            raise ValueError("manager must keep a positive score on its active proposal")
        previous = next(
            evaluation
            for evaluation in item.evaluations
            if evaluation.manager is request.manager
        )
        trigger_ids = sorted(set(triggers_by_group.get(item.conflict_group, [])))
        if not trigger_ids:
            raise ValueError("score item has no material revision trigger")
        entries.append(
            DebateScoreEntry(
                item_id=item.item_id,
                item_revision=item.revision,
                previous_score=previous.support_score,
                support_score=draft_entry.support_score,
                hard_veto=draft_entry.hard_veto,
                reason=draft_entry.reason,
                modification_suggestion=draft_entry.modification_suggestion,
                score_change_reason=draft_entry.score_change_reason,
                trigger_revision_record_id=trigger_ids[0],
            )
        )
    return DebateScoreRecord(
        score_record_id=(
            f"score_r{request.debate_round}_{request.manager.name.lower()}_"
            f"{source_fingerprint[:12]}"
        ),
        run_id=request.run_id,
        as_of=request.as_of,
        debate_round=request.debate_round,
        manager=request.manager,
        source_revision_record_ids=tuple(
            record.revision_record_id for record in request.source_revision_records
        ),
        source_fingerprint=source_fingerprint,
        evaluations=tuple(entries),
        created_at=request.as_of,
    )


def _apply_score_records(
    pool: NegotiationProposalPool,
    records: list[DebateScoreRecord],
    rescore_item_ids: tuple[str, ...],
) -> NegotiationProposalPool:
    entries = {
        (record.manager, entry.item_id): entry
        for record in records
        for entry in record.evaluations
    }
    rescore_set = set(rescore_item_ids)
    updated_items: list[ProposalItem] = []
    for item in pool.proposal_items:
        if item.item_id not in rescore_set:
            updated_items.append(item)
            continue
        evaluations: list[ProposalEvaluation] = []
        for manager in (item.proposer, counterpart_of(item.proposer)):
            entry = entries[(manager, item.item_id)]
            evaluations.append(
                ProposalEvaluation(
                    manager=manager,
                    previous_score=entry.previous_score,
                    support_score=entry.support_score,
                    hard_veto=entry.hard_veto,
                    reason=entry.reason,
                    modification_suggestion=entry.modification_suggestion,
                    score_change_reason=entry.score_change_reason,
                )
            )
        updated_items.append(item.model_copy(deep=True, update={"evaluations": evaluations}))
    return NegotiationProposalPool(
        **pool.model_dump(exclude={"proposal_items"}),
        proposal_items=tuple(updated_items),
    )


async def _gather_calls[T](
    calls: tuple[Awaitable[T], ...],
) -> tuple[T | BaseException, ...]:
    results = await asyncio.gather(*calls, return_exceptions=True)
    return tuple(results)


def _records_for_round(records, debate_round: int):
    return tuple(record for record in records if record.debate_round == debate_round)


def _current_stage_summary(state: ResearchGraphState, stage: str, debate_round: int):
    return next(
        (
            summary
            for summary in reversed(state.get("negotiation_stage_run_summaries", []))
            if summary.stage == stage and summary.debate_round == debate_round
        ),
        None,
    )


def _model_summary(
    stage,
    manager: PortfolioManager,
    debate_round: int,
    input_count: int,
    output_count: int,
    context_count: int,
    *,
    model_called: bool,
    stop_reason,
) -> NegotiationModelRunSummary:
    return NegotiationModelRunSummary(
        stage=stage,
        manager=manager,
        debate_round=debate_round,
        input_item_count=input_count,
        output_item_count=output_count,
        context_character_count=context_count,
        model_called=model_called,
        stop_reason=stop_reason,
    )


def _stage_summary(
    stage,
    debate_round: int,
    fingerprint: str,
    requested: tuple[PortfolioManager, ...],
    called: tuple[PortfolioManager, ...],
    staged: tuple[PortfolioManager, ...],
    completed: tuple[PortfolioManager, ...],
    stop_reason,
) -> NegotiationStageRunSummary:
    return NegotiationStageRunSummary(
        stage=stage,
        debate_round=debate_round,
        source_fingerprint=fingerprint,
        requested_managers=requested,
        called_managers=called,
        staged_managers=staged,
        completed_managers=completed,
        stop_reason=stop_reason,
    )


def _stage_failure(
    stage,
    debate_round: int,
    fingerprint: str,
    *,
    requested: tuple[PortfolioManager, ...] = (),
    called: tuple[PortfolioManager, ...] = (),
    staged: tuple[PortfolioManager, ...] = (),
    summaries: list[NegotiationModelRunSummary] | None = None,
    error: str,
) -> ResearchGraphState:
    updates: ResearchGraphState = {"errors": [error]}
    if 1 <= debate_round <= MAX_DEBATE_ROUNDS:
        updates["negotiation_stage_run_summaries"] = [
            _stage_summary(
                stage,
                debate_round,
                fingerprint,
                requested,
                called,
                staged,
                (),
                "stage_failed" if called else "invalid_state",
            )
        ]
    if summaries:
        updates["negotiation_model_run_summaries"] = summaries
    return updates


def _revision_stage_failure(
    debate_round: int,
    fingerprint: str,
    *,
    requested: tuple[PortfolioManager, ...] = (),
    called: tuple[PortfolioManager, ...] = (),
    staged: tuple[PortfolioManager, ...] = (),
    summaries: list[NegotiationModelRunSummary] | None = None,
    error: str,
) -> ResearchGraphState:
    updates = _stage_failure(
        "PROPOSAL_REVISION",
        debate_round,
        fingerprint,
        requested=requested,
        called=called,
        staged=staged,
        summaries=summaries,
        error=error,
    )
    updates["proposal_revision_application_summary"] = None
    return updates


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _zero_fingerprint() -> str:
    return "0" * 64


def _error(message: str) -> ResearchGraphState:
    return {"errors": [message]}
