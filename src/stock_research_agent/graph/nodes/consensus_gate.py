"""按照固定分数门槛和生命周期规则判断建议是否进入正式协商。"""

import hashlib
import json
from collections import defaultdict
from typing import Literal

from stock_research_agent.agents.negotiation import (
    MAX_DEBATE_ROUNDS,
    REQUIRED_DECISION_DIMENSIONS,
    ConsensusGateItemDecision,
    ConsensusGateReport,
    NegotiationProposalPool,
)
from stock_research_agent.domain.enums import (
    ConsensusItemOutcome,
    ConsensusRoute,
    PortfolioManager,
    ProposalStatus,
)
from stock_research_agent.domain.recommendation import ProposalItem
from stock_research_agent.graph.nodes.conflict_score_validation import (
    conflict_score_source_fingerprint,
)
from stock_research_agent.graph.nodes.negotiation_score_validation import (
    collect_negotiation_score_violations,
)
from stock_research_agent.graph.state import ResearchGraphState

ConsensusGateGraphRoute = Literal["assemble", "negotiate", "failed"]


def build_consensus_gate_node(*, max_rounds: int = MAX_DEBATE_ROUNDS):
    if not 1 <= max_rounds <= MAX_DEBATE_ROUNDS:
        raise ValueError(f"max_rounds 必须位于 1 到 {MAX_DEBATE_ROUNDS} 之间")

    def node(state: ResearchGraphState) -> ResearchGraphState:
        debate_round = state.get("debate_round", 0)
        if not 0 <= debate_round <= max_rounds:
            return _failure("ConsensusGateNode skipped: debate_round is outside limits")

        pool = state.get("negotiation_proposal_pool")
        if pool is None:
            source = state.get("cross_reviewed_proposal_pool")
            if source is None:
                return _failure("ConsensusGateNode skipped: reviewed proposal pool is missing")
            initial_error = _initial_source_error(state)
            if initial_error is not None:
                return _failure(f"ConsensusGateNode skipped: {initial_error}")
            try:
                pool = NegotiationProposalPool.from_cross_reviewed(source)
            except Exception as exc:
                return _failure(
                    "ConsensusGateNode skipped: negotiation pool initialization failed: "
                    f"{type(exc).__name__}"
                )
        elif (
            pool.run_id != state.get("run_id")
            or pool.as_of != state.get("as_of")
            or pool.research_target != state.get("target")
        ):
            return _failure("ConsensusGateNode skipped: negotiation pool scope mismatch")

        fingerprint = consensus_gate_source_fingerprint(pool, debate_round)
        existing = state.get("consensus_gate_report")
        if existing is not None:
            if (
                existing.debate_round == debate_round
                and existing.source_fingerprint == fingerprint
                and _report_matches_pool(existing, pool)
            ):
                return {}
            if existing.debate_round == debate_round:
                return _failure(
                    "ConsensusGateNode refused an incompatible report for the same debate round"
                )

        violations = collect_negotiation_score_violations(pool.proposal_items)
        if violations:
            return _failure(
                "ConsensusGateNode skipped: current pool violates the mutual-exclusion score rule"
            )

        try:
            updated_pool, report = _evaluate_pool(
                pool,
                debate_round=debate_round,
                source_fingerprint=fingerprint,
                max_rounds=max_rounds,
            )
        except Exception as exc:
            return _failure(
                f"ConsensusGateNode skipped: gate evaluation failed: {type(exc).__name__}"
            )
        return {
            "negotiation_proposal_pool": updated_pool,
            "consensus_gate_report": report,
            "consensus_gate_reports": [report],
        }

    return node


def route_after_consensus_gate(state: ResearchGraphState) -> ConsensusGateGraphRoute:
    report = state.get("consensus_gate_report")
    pool = state.get("negotiation_proposal_pool")
    debate_round = state.get("debate_round", 0)
    if (
        report is None
        or pool is None
        or report.run_id != state.get("run_id")
        or report.as_of != state.get("as_of")
        or report.debate_round != debate_round
        or report.source_fingerprint
        != consensus_gate_source_fingerprint(pool, debate_round)
        or not _report_matches_pool(report, pool)
    ):
        return "failed"
    return {
        ConsensusRoute.ASSEMBLE: "assemble",
        ConsensusRoute.NEGOTIATE: "negotiate",
    }[report.route]


def consensus_gate_source_fingerprint(pool: NegotiationProposalPool, debate_round: int) -> str:
    """忽略 Gate 自己写入的 status，使同一评分状态可幂等重放。"""

    items = []
    for item in pool.proposal_items:
        payload = item.model_dump(mode="json")
        payload.pop("status", None)
        payload.pop("arbitration", None)
        items.append(payload)
    payload = json.dumps(
        {
            "run_id": pool.run_id,
            "as_of": pool.as_of.isoformat(),
            "research_target": pool.research_target.model_dump(mode="json"),
            "aggressive_recommendation_id": pool.aggressive_recommendation_id,
            "conservative_recommendation_id": pool.conservative_recommendation_id,
            "proposal_items": items,
            "debate_round": debate_round,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evaluate_pool(
    pool: NegotiationProposalPool,
    *,
    debate_round: int,
    source_fingerprint: str,
    max_rounds: int,
) -> tuple[NegotiationProposalPool, ConsensusGateReport]:
    items_by_group: dict[str, list[ProposalItem]] = defaultdict(list)
    for item in pool.proposal_items:
        items_by_group[item.conflict_group].append(item)

    outcomes: dict[str, tuple[ConsensusItemOutcome, tuple[str, ...]]] = {}
    for group_items in items_by_group.values():
        frozen_agreed = [item for item in group_items if item.status is ProposalStatus.AGREED]
        if len(frozen_agreed) > 1:
            raise ValueError("multiple frozen AGREED items in one conflict group")
        if frozen_agreed:
            winner = frozen_agreed[0]
            outcomes[winner.item_id] = (ConsensusItemOutcome.AGREED, ("FROZEN_AGREED",))
            for item in group_items:
                if item.item_id == winner.item_id:
                    continue
                if item.status is ProposalStatus.WITHDRAWN:
                    outcomes[item.item_id] = (
                        ConsensusItemOutcome.WITHDRAWN,
                        ("PROPOSER_WITHDREW",),
                    )
                else:
                    outcomes[item.item_id] = (
                        ConsensusItemOutcome.REJECTED,
                        ("CONFLICTING_ITEM_AGREED",),
                    )
            continue

        candidates = [
            item
            for item in group_items
            if item.status not in {ProposalStatus.REJECTED, ProposalStatus.WITHDRAWN}
        ]
        passing = [item for item in candidates if _passes_gate(item)]
        if len(passing) > 1:
            raise ValueError("mutually exclusive items both passed the gate")
        if passing:
            winner = passing[0]
            outcomes[winner.item_id] = (ConsensusItemOutcome.AGREED, ("GATE_PASSED",))
            for item in group_items:
                if item.item_id == winner.item_id:
                    continue
                if item.status is ProposalStatus.WITHDRAWN:
                    outcomes[item.item_id] = (
                        ConsensusItemOutcome.WITHDRAWN,
                        ("PROPOSER_WITHDREW",),
                    )
                else:
                    outcomes[item.item_id] = (
                        ConsensusItemOutcome.REJECTED,
                        ("CONFLICTING_ITEM_AGREED",),
                    )
            continue

        for item in group_items:
            if item.status is ProposalStatus.WITHDRAWN:
                outcomes[item.item_id] = (
                    ConsensusItemOutcome.WITHDRAWN,
                    ("PROPOSER_WITHDREW",),
                )
            elif item.status is ProposalStatus.REJECTED:
                outcomes[item.item_id] = (
                    ConsensusItemOutcome.REJECTED,
                    ("FROZEN_REJECTED",),
                )
            else:
                outcome = (
                    ConsensusItemOutcome.EXCLUDED
                    if debate_round >= max_rounds
                    else ConsensusItemOutcome.NEGOTIATING
                )
                outcomes[item.item_id] = (outcome, _failure_reason_codes(item))

    updated_items: list[ProposalItem] = []
    decisions: list[ConsensusGateItemDecision] = []
    for item in pool.proposal_items:
        outcome, reason_codes = outcomes[item.item_id]
        status = {
            ConsensusItemOutcome.AGREED: ProposalStatus.AGREED,
            ConsensusItemOutcome.NEGOTIATING: ProposalStatus.NEGOTIATING,
            ConsensusItemOutcome.EXCLUDED: ProposalStatus.EXCLUDED,
            ConsensusItemOutcome.REJECTED: ProposalStatus.REJECTED,
            ConsensusItemOutcome.WITHDRAWN: ProposalStatus.WITHDRAWN,
        }[outcome]
        updated = item.model_copy(deep=True, update={"status": status})
        updated_items.append(updated)
        scores = _scores(updated)
        decisions.append(
            ConsensusGateItemDecision(
                item_id=updated.item_id,
                item_revision=updated.revision,
                aggressive_score=scores[PortfolioManager.AGGRESSIVE],
                conservative_score=scores[PortfolioManager.CONSERVATIVE],
                combined_score=sum(scores.values()),
                minimum_score=min(scores.values()),
                hard_veto=any(evaluation.hard_veto for evaluation in updated.evaluations),
                outcome=outcome,
                reason_codes=reason_codes,
            )
        )

    updated_pool = pool.model_copy(deep=True, update={"proposal_items": tuple(updated_items)})
    agreed = tuple(
        decision.item_id
        for decision in decisions
        if decision.outcome is ConsensusItemOutcome.AGREED
    )
    negotiating = tuple(
        decision.item_id
        for decision in decisions
        if decision.outcome is ConsensusItemOutcome.NEGOTIATING
    )
    rejected = tuple(
        decision.item_id
        for decision in decisions
        if decision.outcome is ConsensusItemOutcome.REJECTED
    )
    withdrawn = tuple(
        decision.item_id
        for decision in decisions
        if decision.outcome is ConsensusItemOutcome.WITHDRAWN
    )
    excluded = tuple(
        decision.item_id
        for decision in decisions
        if decision.outcome is ConsensusItemOutcome.EXCLUDED
    )
    agreed_dimensions = {
        item.decision_dimension
        for item in updated_items
        if item.status is ProposalStatus.AGREED and item.target == pool.research_target
    }
    missing_dimensions = tuple(
        sorted(REQUIRED_DECISION_DIMENSIONS - agreed_dimensions, key=lambda value: value.value)
    )
    if negotiating:
        route = ConsensusRoute.NEGOTIATE
    else:
        route = ConsensusRoute.ASSEMBLE
    report = ConsensusGateReport(
        run_id=pool.run_id,
        as_of=pool.as_of,
        debate_round=debate_round,
        max_rounds=max_rounds,
        source_fingerprint=source_fingerprint,
        item_decisions=tuple(decisions),
        agreed_item_ids=agreed,
        negotiating_item_ids=negotiating,
        rejected_item_ids=rejected,
        withdrawn_item_ids=withdrawn,
        excluded_item_ids=excluded,
        missing_required_dimensions=missing_dimensions,
        all_required_dimensions_resolved=not missing_dimensions,
        route=route,
    )
    return updated_pool, report


def _passes_gate(item: ProposalItem) -> bool:
    scores = _scores(item)
    return (
        sum(scores.values()) > 0
        and min(scores.values()) >= -0.25
        and not any(evaluation.hard_veto for evaluation in item.evaluations)
    )


def _failure_reason_codes(item: ProposalItem) -> tuple[str, ...]:
    scores = _scores(item)
    reasons: list[str] = []
    if sum(scores.values()) <= 0:
        reasons.append("COMBINED_SCORE_NOT_POSITIVE")
    if min(scores.values()) < -0.25:
        reasons.append("MINIMUM_SCORE_BELOW_NEGATIVE_QUARTER")
    if any(evaluation.hard_veto for evaluation in item.evaluations):
        reasons.append("HARD_VETO")
    return tuple(reasons)


def _scores(item: ProposalItem) -> dict[PortfolioManager, float]:
    return {evaluation.manager: evaluation.support_score for evaluation in item.evaluations}


def _initial_source_error(state: ResearchGraphState) -> str | None:
    source = state.get("cross_reviewed_proposal_pool")
    report = state.get("conflict_score_validation_report")
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    if source is None or report is None or aggressive is None or conservative is None:
        return "valid initial score report and both cross-review records are required"
    if not report.valid or report.stop_reason != "valid":
        return "initial conflict-score report is not valid"
    expected = conflict_score_source_fingerprint(
        source,
        aggressive.attempt,
        conservative.attempt,
        report.max_attempts,
    )
    if report.source_fingerprint != expected:
        return "initial conflict-score report fingerprint is stale"
    return None


def _report_matches_pool(report: ConsensusGateReport, pool: NegotiationProposalPool) -> bool:
    expected_status = {
        ConsensusItemOutcome.AGREED: ProposalStatus.AGREED,
        ConsensusItemOutcome.NEGOTIATING: ProposalStatus.NEGOTIATING,
        ConsensusItemOutcome.EXCLUDED: ProposalStatus.EXCLUDED,
        ConsensusItemOutcome.REJECTED: ProposalStatus.REJECTED,
        ConsensusItemOutcome.WITHDRAWN: ProposalStatus.WITHDRAWN,
    }
    by_id = {item.item_id: item for item in pool.proposal_items}
    decision_ids = [decision.item_id for decision in report.item_decisions]
    return set(decision_ids) == set(by_id) and len(decision_ids) == len(by_id) and all(
        decision.item_id in by_id
        and by_id[decision.item_id].revision == decision.item_revision
        and by_id[decision.item_id].status is expected_status[decision.outcome]
        for decision in report.item_decisions
    )


def _failure(error: str) -> ResearchGraphState:
    return {"consensus_gate_report": None, "errors": [error]}
