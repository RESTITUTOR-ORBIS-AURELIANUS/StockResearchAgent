"""确定性汇合两位经理的独立建议并建立对称决策槽冲突。"""

from collections import defaultdict

from stock_research_agent.agents.debate import (
    NormalizedProposalPool,
    ProposalNormalizationRunSummary,
)
from stock_research_agent.domain.enums import (
    PortfolioManager,
    RecommendationProfile,
)
from stock_research_agent.domain.recommendation import ProposalItem, RecommendationRecord
from stock_research_agent.graph.state import ResearchGraphState


def proposal_normalization_node(state: ResearchGraphState) -> ResearchGraphState:
    """复制两份原始方案，并把同一决策槽中的跨经理条目标记为互斥。"""

    existing = state.get("normalized_proposal_pool")
    if existing is not None:
        if _existing_pool_matches_state(existing, state):
            return {}
        return _failure_updates(
            state,
            stop_reason="invalid_state",
            error="ProposalNormalizationNode refused to overwrite an incompatible pool",
        )

    aggressive = state.get("aggressive_recommendation")
    conservative = state.get("conservative_recommendation")
    if aggressive is None or conservative is None:
        return _failure_updates(
            state,
            stop_reason="missing_recommendation",
            error=(
                "ProposalNormalizationNode skipped: both aggressive and conservative "
                "recommendations are required"
            ),
        )

    state_error = _state_error(state, aggressive, conservative)
    if state_error is not None:
        return _failure_updates(
            state,
            stop_reason="invalid_state",
            error=f"ProposalNormalizationNode skipped: {state_error}",
        )

    source_items = [*aggressive.proposal_items, *conservative.proposal_items]
    try:
        normalized_items = _normalize_items(source_items)
        pool = NormalizedProposalPool(
            run_id=state["run_id"],
            as_of=state["as_of"],
            research_target=state["target"],
            aggressive_recommendation_id=aggressive.recommendation_id,
            conservative_recommendation_id=conservative.recommendation_id,
            proposal_items=tuple(normalized_items),
        )
    except Exception as exc:
        return _failure_updates(
            state,
            stop_reason="invalid_state",
            error=f"ProposalNormalizationNode skipped: normalization failed: {type(exc).__name__}",
        )

    return {
        "normalized_proposal_pool": pool,
        "proposal_normalization_run_summary": ProposalNormalizationRunSummary(
            aggressive_recommendation_id=aggressive.recommendation_id,
            conservative_recommendation_id=conservative.recommendation_id,
            input_proposal_count=len(source_items),
            output_proposal_count=len(normalized_items),
            conflict_pair_count=_conflict_pair_count(normalized_items),
            stop_reason="complete",
        ),
    }


def _normalize_items(source_items: list[ProposalItem]) -> list[ProposalItem]:
    by_group: dict[str, list[ProposalItem]] = defaultdict(list)
    seen_ids: set[str] = set()
    seen_slots: set[tuple[str, PortfolioManager]] = set()
    for item in source_items:
        if item.item_id in seen_ids:
            raise ValueError("duplicate item_id")
        seen_ids.add(item.item_id)
        expected_group = (
            f"{item.target.type.value}:{item.target.code.upper()}:{item.decision_dimension.value}"
        )
        if item.conflict_group != expected_group:
            raise ValueError("non-canonical conflict_group")
        slot = (item.conflict_group, item.proposer)
        if slot in seen_slots:
            raise ValueError("duplicate manager decision slot")
        seen_slots.add(slot)
        by_group[item.conflict_group].append(item)

    result: list[ProposalItem] = []
    for item in source_items:
        conflicts = sorted(
            candidate.item_id
            for candidate in by_group[item.conflict_group]
            if candidate.proposer is not item.proposer
        )
        result.append(
            item.model_copy(
                deep=True,
                update={"conflicts_with": conflicts},
            )
        )
    return result


def _state_error(
    state: ResearchGraphState,
    aggressive: RecommendationRecord,
    conservative: RecommendationRecord,
) -> str | None:
    run_id = state.get("run_id")
    as_of = state.get("as_of")
    target = state.get("target")
    if not run_id or as_of is None or target is None:
        return "run_id, target and as_of are required"
    if aggressive.profile is not RecommendationProfile.AGGRESSIVE:
        return "aggressive recommendation has the wrong profile"
    if conservative.profile is not RecommendationProfile.CONSERVATIVE:
        return "conservative recommendation has the wrong profile"
    for record in (aggressive, conservative):
        if record.run_id != run_id:
            return f"recommendation run_id mismatch: {record.recommendation_id}"
        if record.as_of != as_of:
            return f"recommendation as_of mismatch: {record.recommendation_id}"
        if record.target != target:
            return f"recommendation target mismatch: {record.recommendation_id}"
    return None


def _existing_pool_matches_state(
    pool: NormalizedProposalPool,
    state: ResearchGraphState,
) -> bool:
    aggressive = state.get("aggressive_recommendation")
    conservative = state.get("conservative_recommendation")
    return (
        aggressive is not None
        and conservative is not None
        and pool.run_id == state.get("run_id")
        and pool.as_of == state.get("as_of")
        and pool.research_target == state.get("target")
        and pool.aggressive_recommendation_id == aggressive.recommendation_id
        and pool.conservative_recommendation_id == conservative.recommendation_id
    )


def _conflict_pair_count(items: list[ProposalItem]) -> int:
    pairs = {
        tuple(sorted((item.item_id, conflict_id)))
        for item in items
        for conflict_id in item.conflicts_with
    }
    return len(pairs)


def _failure_updates(
    state: ResearchGraphState,
    *,
    stop_reason,
    error: str,
) -> ResearchGraphState:
    aggressive = state.get("aggressive_recommendation")
    conservative = state.get("conservative_recommendation")
    return {
        "proposal_normalization_run_summary": ProposalNormalizationRunSummary(
            aggressive_recommendation_id=(
                aggressive.recommendation_id if aggressive is not None else None
            ),
            conservative_recommendation_id=(
                conservative.recommendation_id if conservative is not None else None
            ),
            input_proposal_count=(len(aggressive.proposal_items) if aggressive is not None else 0)
            + (len(conservative.proposal_items) if conservative is not None else 0),
            output_proposal_count=0,
            conflict_pair_count=0,
            stop_reason=stop_reason,
        ),
        "errors": [error],
    }
