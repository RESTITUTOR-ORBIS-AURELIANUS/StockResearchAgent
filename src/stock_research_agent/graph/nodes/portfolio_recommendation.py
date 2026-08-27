"""两位投资组合经理独立生成建议并装配可审计领域记录。"""

from hashlib import sha256

from stock_research_agent.agents.portfolio import (
    DecisionThesisSummary,
    PortfolioManagerModel,
    PortfolioRecommendationDraft,
    PortfolioRecommendationInput,
    PortfolioRecommendationLimits,
    PortfolioRecommendationRunSummary,
)
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    RecommendationProfile,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import (
    ProposalEvaluation,
    ProposalItem,
    RecommendationRecord,
)
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

_COMPLETED_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.REFUTED,
    ThesisValidationStatus.MIXED,
    ThesisValidationStatus.INCONCLUSIVE,
}
_DECISION_ELIGIBLE_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.MIXED,
}
_REQUIRED_SCOPE_DIMENSIONS = {
    DecisionDimension.ACTION,
    DecisionDimension.HORIZON,
    DecisionDimension.RISK_CONTROL,
}


def build_aggressive_portfolio_recommendation_node(
    model: PortfolioManagerModel,
    *,
    limits: PortfolioRecommendationLimits | None = None,
):
    return _build_portfolio_recommendation_node(
        model,
        manager=PortfolioManager.AGGRESSIVE,
        profile=RecommendationProfile.AGGRESSIVE,
        output_field="aggressive_recommendation",
        summary_field="aggressive_recommendation_run_summary",
        limits=limits,
    )


def build_conservative_portfolio_recommendation_node(
    model: PortfolioManagerModel,
    *,
    limits: PortfolioRecommendationLimits | None = None,
):
    return _build_portfolio_recommendation_node(
        model,
        manager=PortfolioManager.CONSERVATIVE,
        profile=RecommendationProfile.CONSERVATIVE,
        output_field="conservative_recommendation",
        summary_field="conservative_recommendation_run_summary",
        limits=limits,
    )


def _build_portfolio_recommendation_node(
    model: PortfolioManagerModel,
    *,
    manager: PortfolioManager,
    profile: RecommendationProfile,
    output_field: str,
    summary_field: str,
    limits: PortfolioRecommendationLimits | None,
):
    configured = limits or PortfolioRecommendationLimits()

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        existing = state.get(output_field)
        if existing is not None:
            if (
                existing.run_id == state.get("run_id")
                and existing.profile is profile
                and existing.target == state.get("target")
            ):
                return {}
            return _failure_updates(
                manager,
                summary_field,
                stop_reason="invalid_state",
                error=f"{manager.value} refused to overwrite an incompatible recommendation",
            )

        state_error = _state_error(state)
        if state_error is not None:
            return _failure_updates(
                manager,
                summary_field,
                stop_reason="invalid_state",
                error=f"{manager.value} skipped: {state_error}",
            )

        theses = list(state.get("thesis_pool", []))
        if len(theses) > configured.max_input_theses:
            return _failure_updates(
                manager,
                summary_field,
                input_thesis_count=len(theses),
                stop_reason="thesis_limit_exceeded",
                error=(
                    f"{manager.value} skipped: thesis count exceeds configured hard limit; "
                    "input was not truncated"
                ),
            )

        try:
            summaries = tuple(DecisionThesisSummary.from_record(thesis) for thesis in theses)
        except Exception as exc:
            return _failure_updates(
                manager,
                summary_field,
                input_thesis_count=len(theses),
                stop_reason="invalid_state",
                error=(
                    f"{manager.value} skipped: thesis summary conversion failed: "
                    f"{type(exc).__name__}"
                ),
            )
        eligible_ids = tuple(
            thesis.thesis_id
            for thesis in theses
            if thesis.validation.status in _DECISION_ELIGIBLE_STATUSES
        )
        if not eligible_ids:
            return {
                summary_field: _run_summary(
                    manager,
                    input_thesis_count=len(theses),
                    stop_reason="no_decision_theses",
                )
            }

        request = PortfolioRecommendationInput(
            run_id=state["run_id"],
            as_of=state["as_of"],
            research_target=state["target"],
            manager=manager,
            profile=profile,
            theses=summaries,
            eligible_supporting_thesis_ids=eligible_ids,
            policy_notes=(
                "SUPPORTED 与 MIXED 可以作为直接建议依据。",
                "REFUTED 不代表相反观点自动成立；INCONCLUSIVE 只代表未知。",
                "两位经理独立作答，本节点看不到另一位经理的建议。",
                "所有实质性决策必须落成可逐条评分的 proposal_item。",
            ),
        )
        context_characters = len(request.model_dump_json(indent=2))
        if context_characters > configured.max_context_characters:
            return _failure_updates(
                manager,
                summary_field,
                input_thesis_count=len(theses),
                eligible_thesis_count=len(eligible_ids),
                context_character_count=context_characters,
                stop_reason="context_limit_exceeded",
                error=(
                    f"{manager.value} skipped: thesis context exceeds configured hard limit; "
                    "input was not truncated"
                ),
            )

        try:
            draft = await model.generate_recommendation(request)
        except Exception as exc:
            return _failure_updates(
                manager,
                summary_field,
                input_thesis_count=len(theses),
                eligible_thesis_count=len(eligible_ids),
                context_character_count=context_characters,
                model_called=True,
                stop_reason="model_error",
                error=f"{manager.value} model failed: {describe_exception(exc)}",
            )

        draft_error = _draft_error(
            draft,
            request=request,
        )
        if draft_error is not None:
            return _failure_updates(
                manager,
                summary_field,
                input_thesis_count=len(theses),
                eligible_thesis_count=len(eligible_ids),
                context_character_count=context_characters,
                model_called=True,
                proposal_count=len(draft.proposal_items),
                stop_reason="rejected_output",
                error=f"{manager.value} recommendation rejected: {draft_error}",
            )

        recommendation = _assemble_recommendation(
            draft,
            state=state,
            manager=manager,
            profile=profile,
        )
        return {
            output_field: recommendation,
            summary_field: _run_summary(
                manager,
                input_thesis_count=len(theses),
                eligible_thesis_count=len(eligible_ids),
                context_character_count=context_characters,
                model_called=True,
                proposal_count=len(draft.proposal_items),
                stop_reason="complete",
            ),
        }

    return node


def _state_error(state: ResearchGraphState) -> str | None:
    if not state.get("run_id") or state.get("target") is None or state.get("as_of") is None:
        return "run_id, target and as_of are required"
    if state.get("thesis_validation_run_summary") is None:
        return "thesis validation run summary is missing"
    if state.get("active_validation_session") is not None:
        return "active thesis validation session is not finished"
    if state.get("active_validation_request_id") is not None:
        return "active validation research request is not finished"
    theses = list(state.get("thesis_pool", []))
    for thesis in theses:
        if thesis.run_id != state["run_id"]:
            return f"thesis run_id mismatch: {thesis.thesis_id}"
        if thesis.as_of != state["as_of"]:
            return f"thesis as_of mismatch: {thesis.thesis_id}"
        if thesis.validation.status not in _COMPLETED_STATUSES:
            return f"unfinished thesis cannot enter portfolio stage: {thesis.thesis_id}"
        if thesis.validation.confidence is None or thesis.reasoning_summary is None:
            return f"completed thesis lacks decision context: {thesis.thesis_id}"
    return None


def _draft_error(
    draft: PortfolioRecommendationDraft,
    *,
    request: PortfolioRecommendationInput,
) -> str | None:
    thesis_catalog = {thesis.thesis_id: thesis for thesis in request.theses}
    eligible = set(request.eligible_supporting_thesis_ids)
    scope_dimensions = {
        item.decision_dimension
        for item in draft.proposal_items
        if item.target == request.research_target
    }
    missing_scope_dimensions = _REQUIRED_SCOPE_DIMENSIONS - scope_dimensions
    if missing_scope_dimensions:
        names = ",".join(sorted(item.value for item in missing_scope_dimensions))
        return f"research target is missing required proposal dimensions: {names}"

    for position, item in enumerate(draft.proposal_items, start=1):
        for thesis_id in item.supporting_thesis_ids:
            thesis = thesis_catalog.get(thesis_id)
            if thesis is None:
                return f"item {position} references unknown thesis_id: {thesis_id}"
            if thesis_id not in eligible:
                return f"item {position} references ineligible thesis_id: {thesis_id}"
        is_scope_summary = item.target == request.research_target
        has_exact_target_basis = any(
            thesis_catalog[thesis_id].target == item.target
            for thesis_id in item.supporting_thesis_ids
        )
        if not is_scope_summary and not has_exact_target_basis:
            return f"item {position} target is not grounded by a cited thesis"
    return None


def _assemble_recommendation(
    draft: PortfolioRecommendationDraft,
    *,
    state: ResearchGraphState,
    manager: PortfolioManager,
    profile: RecommendationProfile,
) -> RecommendationRecord:
    canonical_json = draft.model_dump_json()
    recommendation_digest = sha256(
        f"{state['run_id']}|{profile.value}|{canonical_json}".encode()
    ).hexdigest()[:24]
    recommendation_id = f"rec_{recommendation_digest}"
    proposal_items: list[ProposalItem] = []
    supporting_ids: list[str] = []
    seen_supporting_ids: set[str] = set()
    for position, item in enumerate(draft.proposal_items, start=1):
        conflict_group = _conflict_group(item.target, item.decision_dimension)
        item_digest = sha256(
            f"{recommendation_id}|{position}|{conflict_group}|{item.proposal}".encode()
        ).hexdigest()[:24]
        proposal_items.append(
            ProposalItem(
                item_id=f"item_{item_digest}",
                target=item.target,
                decision_dimension=item.decision_dimension,
                conflict_group=conflict_group,
                conflicts_with=[],
                proposer=manager,
                revision=1,
                proposal=item.proposal,
                supporting_thesis_ids=list(item.supporting_thesis_ids),
                evaluations=[
                    ProposalEvaluation(
                        manager=manager,
                        previous_score=None,
                        support_score=item.insistence_score,
                        hard_veto=False,
                        reason=item.score_reason,
                    )
                ],
                status=ProposalStatus.PROPOSED,
                arbitration=None,
            )
        )
        for thesis_id in item.supporting_thesis_ids:
            if thesis_id not in seen_supporting_ids:
                seen_supporting_ids.add(thesis_id)
                supporting_ids.append(thesis_id)

    return RecommendationRecord(
        recommendation_id=recommendation_id,
        run_id=state["run_id"],
        as_of=state["as_of"],
        profile=profile,
        target=state["target"],
        action=draft.action,
        horizon=draft.horizon,
        confidence=draft.confidence,
        supporting_thesis_ids=supporting_ids,
        summary=draft.summary,
        valuation_guidance=draft.valuation_guidance,
        risk_summary=draft.risk_summary,
        proposal_items=proposal_items,
        debate=None,
        generated_by=manager.value,
        created_at=state["as_of"],
    )


def _conflict_group(target, dimension: DecisionDimension) -> str:
    return f"{target.type.value}:{target.code.upper()}:{dimension.value}"


def _failure_updates(
    manager: PortfolioManager,
    summary_field: str,
    *,
    stop_reason,
    error: str,
    input_thesis_count: int = 0,
    eligible_thesis_count: int = 0,
    context_character_count: int = 0,
    model_called: bool = False,
    proposal_count: int = 0,
) -> ResearchGraphState:
    return {
        summary_field: _run_summary(
            manager,
            input_thesis_count=input_thesis_count,
            eligible_thesis_count=eligible_thesis_count,
            context_character_count=context_character_count,
            model_called=model_called,
            proposal_count=proposal_count,
            stop_reason=stop_reason,
        ),
        "errors": [error],
    }


def _run_summary(
    manager: PortfolioManager,
    *,
    input_thesis_count: int = 0,
    eligible_thesis_count: int = 0,
    context_character_count: int = 0,
    model_called: bool = False,
    proposal_count: int = 0,
    stop_reason="invalid_state",
) -> PortfolioRecommendationRunSummary:
    return PortfolioRecommendationRunSummary(
        manager=manager,
        input_thesis_count=input_thesis_count,
        eligible_thesis_count=eligible_thesis_count,
        context_character_count=context_character_count,
        model_called=model_called,
        proposal_count=proposal_count,
        stop_reason=stop_reason,
    )
