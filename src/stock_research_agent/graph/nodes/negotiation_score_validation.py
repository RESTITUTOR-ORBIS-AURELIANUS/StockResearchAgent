"""正式协商当前提案池的纯评分不变量校验。"""

import hashlib
import json
from typing import Literal

from stock_research_agent.agents.negotiation import (
    NegotiationProposalPool,
    NegotiationScoreValidationReport,
    NegotiationScoreViolation,
)
from stock_research_agent.domain.enums import PortfolioManager, ProposalStatus
from stock_research_agent.domain.recommendation import ProposalItem
from stock_research_agent.graph.state import ResearchGraphState

NegotiationScoreValidationRoute = Literal["valid", "failed"]
_INACTIVE_STATUSES = {
    ProposalStatus.REJECTED,
    ProposalStatus.WITHDRAWN,
    ProposalStatus.EXCLUDED,
}


def collect_negotiation_score_violations(
    items: tuple[ProposalItem, ...],
) -> tuple[NegotiationScoreViolation, ...]:
    """只检查当前仍存活的互斥条目；不绑定首评 attempt 或原始提案指纹。"""

    active = [item for item in items if item.status not in _INACTIVE_STATUSES]
    by_id = {item.item_id: item for item in active}
    seen: set[tuple[PortfolioManager, str, str]] = set()
    violations: list[NegotiationScoreViolation] = []
    for item in active:
        for conflict_id in item.conflicts_with:
            conflict = by_id.get(conflict_id)
            if conflict is None:
                continue
            left, right = sorted((item, conflict), key=lambda proposal: proposal.item_id)
            for manager in (PortfolioManager.AGGRESSIVE, PortfolioManager.CONSERVATIVE):
                key = (manager, left.item_id, right.item_id)
                if key in seen:
                    continue
                seen.add(key)
                left_score = _score_by_manager(left, manager)
                right_score = _score_by_manager(right, manager)
                if left_score + right_score > 0:
                    violations.append(
                        NegotiationScoreViolation(
                            manager=manager,
                            left_item_id=left.item_id,
                            right_item_id=right.item_id,
                            left_score=left_score,
                            right_score=right_score,
                            message=(
                                "同一经理对同一互斥决策槽中两个存活版本的评分和必须"
                                "小于或等于 0。"
                            ),
                        )
                    )
    return tuple(violations)


def negotiation_score_source_fingerprint(
    pool: NegotiationProposalPool,
    debate_round: int,
) -> str:
    payload = json.dumps(
        {
            "pool": pool.model_dump(mode="json"),
            "debate_round": debate_round,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_negotiation_scores_node(state: ResearchGraphState) -> ResearchGraphState:
    pool = state.get("negotiation_proposal_pool")
    if pool is None:
        return {
            "negotiation_score_validation_report": None,
            "errors": ["NegotiationScoreValidatorNode skipped: proposal pool is missing"],
        }
    debate_round = state.get("debate_round", 0)
    if (
        pool.run_id != state.get("run_id")
        or pool.as_of != state.get("as_of")
        or pool.research_target != state.get("target")
        or debate_round < 1
    ):
        return {
            "negotiation_score_validation_report": None,
            "errors": ["NegotiationScoreValidatorNode skipped: state scope mismatch"],
        }

    fingerprint = negotiation_score_source_fingerprint(pool, debate_round)
    existing = state.get("negotiation_score_validation_report")
    violations = collect_negotiation_score_violations(pool.proposal_items)
    report = NegotiationScoreValidationReport(
        run_id=pool.run_id,
        as_of=pool.as_of,
        debate_round=debate_round,
        source_fingerprint=fingerprint,
        valid=not violations,
        violations=violations,
        stop_reason="valid" if not violations else "invalid_scores",
    )
    if existing == report:
        return {}
    updates: ResearchGraphState = {"negotiation_score_validation_report": report}
    if violations:
        updates["errors"] = [
            "NegotiationScoreValidatorNode rejected a formal score batch: "
            f"{len(violations)} conflict invariant violation(s)"
        ]
    return updates


def route_after_negotiation_score_validation(
    state: ResearchGraphState,
) -> NegotiationScoreValidationRoute:
    report = state.get("negotiation_score_validation_report")
    pool = state.get("negotiation_proposal_pool")
    debate_round = state.get("debate_round", 0)
    if (
        report is not None
        and pool is not None
        and report.run_id == state.get("run_id")
        and report.as_of == state.get("as_of")
        and report.debate_round == debate_round
        and report.source_fingerprint
        == negotiation_score_source_fingerprint(pool, debate_round)
        and report.valid
        and report.stop_reason == "valid"
    ):
        return "valid"
    return "failed"


def _score_by_manager(item: ProposalItem, manager: PortfolioManager):
    return next(
        evaluation.support_score for evaluation in item.evaluations if evaluation.manager is manager
    )
