"""只用共识门批准的条目组装最终委员会建议。"""

from __future__ import annotations

import hashlib
import json

from stock_research_agent.agents.consensus_assembly import (
    ConsensusAssemblyRunSummary,
    ConsensusRecommendationSynthesisInput,
    ConsensusRecommendationSynthesisModel,
)
from stock_research_agent.agents.negotiation import REQUIRED_DECISION_DIMENSIONS
from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain.enums import (
    ConsensusRoute,
    DebateStatus,
    DecisionDimension,
    ProposalStatus,
    RecommendationProfile,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import DebateSummary, RecommendationRecord
from stock_research_agent.graph.nodes.consensus_gate import consensus_gate_source_fingerprint
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

_MAX_CONTEXT_CHARACTERS = 120_000
_ELIGIBLE_THESIS_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.MIXED,
}


def build_consensus_recommendation_assembler_node(
    model: ConsensusRecommendationSynthesisModel,
):
    """构造最终组装节点；模型无权决定任何条目是否进入最终建议。"""

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        base_error = _base_state_error(state)
        if base_error is not None:
            return _failure_updates(
                state,
                stop_reason="missing_input",
                error=f"ConsensusRecommendationAssemblerNode skipped: {base_error}",
            )

        gate = state["consensus_gate_report"]
        pool = state["negotiation_proposal_pool"]
        if gate.route is not ConsensusRoute.ASSEMBLE:
            return _failure_updates(
                state,
                stop_reason="invalid_state",
                error="ConsensusRecommendationAssemblerNode requires an ASSEMBLE gate report",
            )

        expected_gate_fingerprint = consensus_gate_source_fingerprint(pool, gate.debate_round)
        if gate.source_fingerprint != expected_gate_fingerprint:
            return _failure_updates(
                state,
                stop_reason="stale_input",
                error="ConsensusRecommendationAssemblerNode refused a stale gate report",
            )

        catalog_error = _catalog_error(state)
        if catalog_error is not None:
            return _failure_updates(
                state,
                stop_reason="invalid_state",
                error=f"ConsensusRecommendationAssemblerNode skipped: {catalog_error}",
            )

        agreed_ids = set(gate.agreed_item_ids)
        accepted_items = tuple(
            item for item in pool.proposal_items if item.item_id in agreed_ids
        )
        source_fingerprint = _assembly_source_fingerprint(state, accepted_items)

        existing_error = _existing_output_error(state, source_fingerprint)
        if existing_error == "idempotent":
            return {}
        if existing_error is not None:
            return {"errors": [existing_error]}

        if not accepted_items or gate.missing_required_dimensions:
            return {
                "consensus_assembly_run_summary": _summary(
                    state,
                    source_fingerprint=source_fingerprint,
                    stop_reason="no_actionable_consensus",
                )
            }

        supporting_ids = _ordered_supporting_thesis_ids(accepted_items)
        thesis_catalog = {thesis.thesis_id: thesis for thesis in state.get("thesis_pool", [])}
        thesis_error = _supporting_thesis_error(state, supporting_ids, thesis_catalog)
        if thesis_error is not None:
            return _failure_updates(
                state,
                source_fingerprint=source_fingerprint,
                stop_reason="invalid_state",
                error=f"ConsensusRecommendationAssemblerNode skipped: {thesis_error}",
            )

        supporting_theses = tuple(
            DecisionThesisSummary.from_record(thesis_catalog[thesis_id])
            for thesis_id in supporting_ids
        )
        request = ConsensusRecommendationSynthesisInput(
            run_id=state["run_id"],
            as_of=state["as_of"],
            research_target=state["target"],
            debate_round=gate.debate_round,
            source_fingerprint=source_fingerprint,
            accepted_items=tuple(item.model_copy(deep=True) for item in accepted_items),
            supporting_theses=supporting_theses,
            policy_notes=(
                "输入已由确定性共识门批准，不能新增、删除或修改原子建议。",
                "未达成共识的条目不会出现在输入中，也不得凭常识补回。",
                "只做顶层动作、期限、摘要、估值和风险文字的忠实压缩。",
            ),
        )
        context_characters = len(request.model_dump_json(indent=2))
        if context_characters > _MAX_CONTEXT_CHARACTERS:
            return _failure_updates(
                state,
                source_fingerprint=source_fingerprint,
                input_thesis_count=len(supporting_theses),
                context_character_count=context_characters,
                stop_reason="context_limit_exceeded",
                error=(
                    "ConsensusRecommendationAssemblerNode skipped: accepted context exceeds "
                    "the hard limit; input was not truncated"
                ),
            )

        try:
            draft = await model.synthesize(request)
        except Exception as exc:
            return _failure_updates(
                state,
                source_fingerprint=source_fingerprint,
                input_thesis_count=len(supporting_theses),
                context_character_count=context_characters,
                model_called=True,
                stop_reason="model_error",
                error=f"Consensus recommendation synthesis failed: {describe_exception(exc)}",
            )

        draft_error = _draft_error(draft, state=state, accepted_items=accepted_items)
        if draft_error is not None:
            return _failure_updates(
                state,
                source_fingerprint=source_fingerprint,
                input_thesis_count=len(supporting_theses),
                context_character_count=context_characters,
                model_called=True,
                stop_reason="rejected_output",
                error=f"Consensus recommendation synthesis rejected: {draft_error}",
            )

        confidence = min(
            thesis_catalog[thesis_id].validation.confidence for thesis_id in supporting_ids
        )
        assert confidence is not None
        recommendation = _assemble_record(
            state,
            draft=draft,
            accepted_items=accepted_items,
            supporting_ids=supporting_ids,
            confidence=confidence,
            source_fingerprint=source_fingerprint,
        )
        return {
            "consensus_recommendation": recommendation,
            "consensus_assembly_run_summary": _summary(
                state,
                source_fingerprint=source_fingerprint,
                input_thesis_count=len(supporting_theses),
                context_character_count=context_characters,
                model_called=True,
                recommendation_id=recommendation.recommendation_id,
                stop_reason="complete",
            ),
        }

    return node


def _base_state_error(state: ResearchGraphState) -> str | None:
    if not state.get("run_id") or state.get("as_of") is None or state.get("target") is None:
        return "run_id, as_of and target are required"
    if state.get("consensus_gate_report") is None:
        return "final consensus gate report is missing"
    if state.get("negotiation_proposal_pool") is None:
        return "negotiation proposal pool is missing"
    if state.get("aggressive_recommendation") is None:
        return "aggressive original recommendation is missing"
    if state.get("conservative_recommendation") is None:
        return "conservative original recommendation is missing"
    return None


def _catalog_error(state: ResearchGraphState) -> str | None:
    gate = state["consensus_gate_report"]
    pool = state["negotiation_proposal_pool"]
    aggressive = state["aggressive_recommendation"]
    conservative = state["conservative_recommendation"]
    run_id = state["run_id"]
    as_of = state["as_of"]
    target = state["target"]

    for record_name, record in (
        ("gate report", gate),
        ("negotiation pool", pool),
        ("aggressive recommendation", aggressive),
        ("conservative recommendation", conservative),
    ):
        if record.run_id != run_id or record.as_of != as_of:
            return f"{record_name} scope mismatch"
    if (
        pool.research_target != target
        or aggressive.target != target
        or conservative.target != target
    ):
        return "recommendation target scope mismatch"
    if gate.debate_round != state.get("debate_round", 0):
        return "gate debate_round is stale"
    if gate.negotiating_item_ids:
        return "ASSEMBLE gate cannot contain negotiating items"
    if pool.aggressive_recommendation_id != aggressive.recommendation_id:
        return "aggressive original recommendation ID mismatch"
    if pool.conservative_recommendation_id != conservative.recommendation_id:
        return "conservative original recommendation ID mismatch"

    pool_ids = [item.item_id for item in pool.proposal_items]
    decision_ids = [decision.item_id for decision in gate.item_decisions]
    if set(pool_ids) != set(decision_ids) or len(pool_ids) != len(decision_ids):
        return "gate decisions must cover the negotiation pool exactly once"
    by_id = {item.item_id: item for item in pool.proposal_items}
    for decision in gate.item_decisions:
        item = by_id[decision.item_id]
        if item.revision != decision.item_revision:
            return f"gate revision is stale for {item.item_id}"
    for item_id in gate.agreed_item_ids:
        if by_id[item_id].status is not ProposalStatus.AGREED:
            return f"gate AGREED catalog disagrees with pool status: {item_id}"
    for item_id in gate.excluded_item_ids:
        if by_id[item_id].status is not ProposalStatus.EXCLUDED:
            return f"gate EXCLUDED catalog disagrees with pool status: {item_id}"
    for item_id in gate.rejected_item_ids:
        if by_id[item_id].status is not ProposalStatus.REJECTED:
            return f"gate REJECTED catalog disagrees with pool status: {item_id}"
    for item_id in gate.withdrawn_item_ids:
        if by_id[item_id].status is not ProposalStatus.WITHDRAWN:
            return f"gate WITHDRAWN catalog disagrees with pool status: {item_id}"

    agreed_scope_dimensions = {
        item.decision_dimension
        for item_id in gate.agreed_item_ids
        if (item := by_id[item_id]).target == target
    }
    expected_missing = REQUIRED_DECISION_DIMENSIONS - agreed_scope_dimensions
    if set(gate.missing_required_dimensions) != expected_missing:
        return "gate missing_required_dimensions is inconsistent with AGREED items"
    return None


def _supporting_thesis_error(state, supporting_ids, thesis_catalog) -> str | None:
    for thesis_id in supporting_ids:
        thesis = thesis_catalog.get(thesis_id)
        if thesis is None:
            return f"AGREED item references unknown thesis: {thesis_id}"
        if thesis.run_id != state["run_id"] or thesis.as_of != state["as_of"]:
            return f"supporting thesis scope mismatch: {thesis_id}"
        if thesis.validation.status not in _ELIGIBLE_THESIS_STATUSES:
            return f"AGREED item references an ineligible thesis: {thesis_id}"
        if thesis.validation.confidence is None or thesis.reasoning_summary is None:
            return f"supporting thesis lacks completed decision context: {thesis_id}"
    return None


def _draft_error(draft, *, state, accepted_items) -> str | None:
    accepted_ids = tuple(item.item_id for item in accepted_items)
    target = state["target"]
    action_items = tuple(
        item.item_id
        for item in accepted_items
        if item.target == target and item.decision_dimension is DecisionDimension.ACTION
    )
    horizon_items = tuple(
        item.item_id
        for item in accepted_items
        if item.target == target and item.decision_dimension is DecisionDimension.HORIZON
    )
    risk_items = tuple(
        item.item_id
        for item in accepted_items
        if item.target == target and item.decision_dimension is DecisionDimension.RISK_CONTROL
    )
    valuation_items = tuple(
        item.item_id
        for item in accepted_items
        if item.decision_dimension is DecisionDimension.VALUATION
    )
    if len(action_items) != 1 or draft.action_source_item_id != action_items[0]:
        return "action must cite the sole AGREED ACTION item for the research target"
    if len(horizon_items) != 1 or draft.horizon_source_item_id != horizon_items[0]:
        return "horizon must cite the sole AGREED HORIZON item for the research target"
    if set(draft.risk_source_item_ids) != set(risk_items):
        return "risk sources must equal all AGREED RISK_CONTROL items for the research target"
    if set(draft.summary_source_item_ids) != set(accepted_ids):
        return "summary sources must equal all AGREED items"
    if set(draft.valuation_source_item_ids) != set(valuation_items):
        return "valuation sources must equal all AGREED VALUATION items"
    return None


def _assemble_record(
    state,
    *,
    draft,
    accepted_items,
    supporting_ids,
    confidence,
    source_fingerprint,
) -> RecommendationRecord:
    draft_json = draft.model_dump_json()
    digest = hashlib.sha256(
        f"{state['run_id']}|CONSENSUS|{source_fingerprint}|{draft_json}".encode()
    ).hexdigest()[:24]
    gate = state["consensus_gate_report"]
    pool = state["negotiation_proposal_pool"]
    excluded_ids = set(gate.excluded_item_ids)
    excluded_by_group: dict[str, list[str]] = {}
    for item in pool.proposal_items:
        if item.item_id in excluded_ids:
            excluded_by_group.setdefault(item.conflict_group, []).append(item.item_id)
    remaining_disagreements = [
        (
            f"{conflict_group}: {gate.debate_round} 轮后仍未达成共识；"
            f"条目 {', '.join(item_ids)} 未纳入委员会建议。"
        )
        for conflict_group, item_ids in excluded_by_group.items()
    ]
    return RecommendationRecord(
        recommendation_id=f"rec_{digest}",
        run_id=state["run_id"],
        as_of=state["as_of"],
        profile=RecommendationProfile.CONSENSUS,
        target=state["target"],
        action=draft.action,
        horizon=draft.horizon,
        confidence=confidence,
        supporting_thesis_ids=list(supporting_ids),
        summary=draft.summary,
        valuation_guidance=draft.valuation_guidance,
        risk_summary=draft.risk_summary,
        proposal_items=[item.model_copy(deep=True) for item in accepted_items],
        debate=DebateSummary(
            rounds=gate.debate_round,
            status=(
                DebateStatus.PARTIAL_CONSENSUS
                if gate.excluded_item_ids
                else DebateStatus.AGREED
            ),
            aggressive_original_recommendation_id=pool.aggressive_recommendation_id,
            conservative_original_recommendation_id=pool.conservative_recommendation_id,
            excluded_item_ids=list(gate.excluded_item_ids),
            remaining_disagreements=remaining_disagreements,
        ),
        generated_by="ConsensusRecommendationAssemblerNode",
        created_at=state["as_of"],
    )


def _assembly_source_fingerprint(state, accepted_items) -> str:
    gate = state["consensus_gate_report"]
    pool = state["negotiation_proposal_pool"]
    accepted_thesis_ids = {
        thesis_id for item in accepted_items for thesis_id in item.supporting_thesis_ids
    }
    theses = [
        thesis.model_dump(mode="json")
        for thesis in state.get("thesis_pool", [])
        if thesis.thesis_id in accepted_thesis_ids
    ]
    payload = {
        "run_id": state["run_id"],
        "as_of": state["as_of"].isoformat(),
        "target": state["target"].model_dump(mode="json"),
        "gate": gate.model_dump(mode="json"),
        "pool": pool.model_dump(mode="json"),
        "aggressive_recommendation_id": state["aggressive_recommendation"].recommendation_id,
        "conservative_recommendation_id": state[
            "conservative_recommendation"
        ].recommendation_id,
        "supporting_theses": theses,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _ordered_supporting_thesis_ids(accepted_items) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in accepted_items:
        for thesis_id in item.supporting_thesis_ids:
            if thesis_id not in seen:
                seen.add(thesis_id)
                result.append(thesis_id)
    return tuple(result)


def _existing_output_error(state, source_fingerprint: str) -> str | None:
    existing_summary = state.get("consensus_assembly_run_summary")
    existing_recommendation = state.get("consensus_recommendation")
    if existing_summary is None and existing_recommendation is None:
        return None
    if existing_summary is None:
        return "ConsensusRecommendationAssemblerNode refused an unaudited existing recommendation"
    if existing_summary.source_fingerprint != source_fingerprint:
        return "ConsensusRecommendationAssemblerNode refused to overwrite a different assembly"
    if existing_summary.stop_reason == "complete":
        if (
            existing_recommendation is not None
            and existing_recommendation.recommendation_id == existing_summary.recommendation_id
        ):
            return "idempotent"
        return "ConsensusRecommendationAssemblerNode found an incomplete successful assembly"
    if (
        existing_summary.stop_reason == "no_actionable_consensus"
        and existing_recommendation is None
    ):
        return "idempotent"
    return "ConsensusRecommendationAssemblerNode refused to overwrite a prior failed assembly"


def _summary(
    state,
    *,
    source_fingerprint=None,
    input_thesis_count=0,
    context_character_count=0,
    model_called=False,
    recommendation_id=None,
    stop_reason,
) -> ConsensusAssemblyRunSummary:
    gate = state.get("consensus_gate_report")
    return ConsensusAssemblyRunSummary(
        run_id=state["run_id"],
        as_of=state["as_of"],
        source_fingerprint=source_fingerprint,
        debate_round=gate.debate_round if gate is not None else state.get("debate_round", 0),
        agreed_item_ids=gate.agreed_item_ids if gate is not None else (),
        excluded_item_ids=gate.excluded_item_ids if gate is not None else (),
        rejected_item_ids=gate.rejected_item_ids if gate is not None else (),
        withdrawn_item_ids=gate.withdrawn_item_ids if gate is not None else (),
        missing_required_dimensions=(
            gate.missing_required_dimensions if gate is not None else ()
        ),
        input_thesis_count=input_thesis_count,
        context_character_count=context_character_count,
        model_called=model_called,
        recommendation_id=recommendation_id,
        stop_reason=stop_reason,
    )


def _failure_updates(
    state,
    *,
    stop_reason,
    error,
    source_fingerprint=None,
    input_thesis_count=0,
    context_character_count=0,
    model_called=False,
) -> ResearchGraphState:
    if not state.get("run_id") or state.get("as_of") is None:
        return {"errors": [error]}
    return {
        "consensus_assembly_run_summary": _summary(
            state,
            source_fingerprint=source_fingerprint,
            input_thesis_count=input_thesis_count,
            context_character_count=context_character_count,
            model_called=model_called,
            stop_reason=stop_reason,
        ),
        "errors": [error],
    }
