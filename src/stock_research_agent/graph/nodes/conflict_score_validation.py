"""确定性校验每位投资组合经理在竞争决策槽中的评分一致性。"""

import hashlib
import json
from typing import Literal

from stock_research_agent.agents.debate import (
    MAX_CROSS_REVIEW_ATTEMPTS,
    ConflictScoreRuleCode,
    ConflictScoreValidationReport,
    ConflictScoreViolation,
    CrossReviewedProposalPool,
    PortfolioCrossReviewRecord,
)
from stock_research_agent.domain.enums import PortfolioManager
from stock_research_agent.domain.recommendation import ProposalItem, SupportScore
from stock_research_agent.graph.state import ResearchGraphState

ConflictScoreValidationRoute = Literal["valid", "retry", "failed"]


def build_conflict_score_validator_node(
    *,
    max_attempts: int = MAX_CROSS_REVIEW_ATTEMPTS,
):
    """创建评分校验节点；max_attempts 包含首次评分。"""

    if not 1 <= max_attempts <= MAX_CROSS_REVIEW_ATTEMPTS:
        raise ValueError(f"max_attempts 必须位于 1 到 {MAX_CROSS_REVIEW_ATTEMPTS} 之间")

    def node(state: ResearchGraphState) -> ResearchGraphState:
        pool = state.get("cross_reviewed_proposal_pool")
        aggressive_review = state.get("aggressive_cross_review")
        conservative_review = state.get("conservative_cross_review")
        aggressive_attempt = aggressive_review.attempt if aggressive_review else 0
        conservative_attempt = conservative_review.attempt if conservative_review else 0

        if pool is None:
            return _validation_failure_updates(
                state,
                max_attempts=max_attempts,
                aggressive_attempt=aggressive_attempt,
                conservative_attempt=conservative_attempt,
                stop_reason="missing_cross_reviewed_pool",
                error="ConflictScoreValidatorNode skipped: cross-reviewed pool is missing",
            )

        fingerprint = conflict_score_source_fingerprint(
            pool,
            aggressive_attempt,
            conservative_attempt,
            max_attempts,
        )
        existing = state.get("conflict_score_validation_report")

        state_error = _validation_state_error(
            state,
            pool,
            aggressive_review,
            conservative_review,
            max_attempts,
        )
        if state_error is not None:
            return _validation_failure_updates(
                state,
                max_attempts=max_attempts,
                source_fingerprint=fingerprint,
                aggressive_attempt=aggressive_attempt,
                conservative_attempt=conservative_attempt,
                stop_reason="invalid_state",
                error=f"ConflictScoreValidatorNode skipped: {state_error}",
            )

        assert aggressive_review is not None
        assert conservative_review is not None
        violations = tuple(_collect_violations(pool))
        invalid_managers = tuple(
            manager
            for manager in (
                PortfolioManager.AGGRESSIVE,
                PortfolioManager.CONSERVATIVE,
            )
            if any(violation.manager is manager for violation in violations)
        )
        if not violations:
            report = ConflictScoreValidationReport(
                run_id=pool.run_id,
                as_of=pool.as_of,
                source_fingerprint=fingerprint,
                max_attempts=max_attempts,
                aggressive_review_attempt=aggressive_review.attempt,
                conservative_review_attempt=conservative_review.attempt,
                valid=True,
                stop_reason="valid",
            )
            if existing == report:
                return {}
            return {"conflict_score_validation_report": report}

        attempts = {
            PortfolioManager.AGGRESSIVE: aggressive_review.attempt,
            PortfolioManager.CONSERVATIVE: conservative_review.attempt,
        }
        exhausted = any(attempts[manager] >= max_attempts for manager in invalid_managers)
        stop_reason = "retry_exhausted" if exhausted else "retry_required"
        report = ConflictScoreValidationReport(
            run_id=pool.run_id,
            as_of=pool.as_of,
            source_fingerprint=fingerprint,
            max_attempts=max_attempts,
            aggressive_review_attempt=aggressive_review.attempt,
            conservative_review_attempt=conservative_review.attempt,
            valid=False,
            invalid_managers=invalid_managers,
            violations=violations,
            stop_reason=stop_reason,
        )
        updates: ResearchGraphState = {"conflict_score_validation_report": report}
        if exhausted:
            manager_names = ", ".join(manager.value for manager in invalid_managers)
            updates["errors"] = [
                "ConflictScoreValidatorNode exhausted cross-review correction attempts: "
                f"{manager_names}"
            ]
        if existing == report and (
            not exhausted or updates["errors"][0] in state.get("errors", [])
        ):
            return {}
        return updates

    return node


def route_after_conflict_score_validation(
    state: ResearchGraphState,
) -> ConflictScoreValidationRoute:
    """把合法评分送往下一阶段，把可纠错评分送回两个并行分支。"""

    report = state.get("conflict_score_validation_report")
    if report is None:
        return "failed"
    if report.stop_reason == "valid":
        return "valid"
    if report.stop_reason == "retry_required":
        return "retry"
    return "failed"


def conflict_score_source_fingerprint(
    pool: CrossReviewedProposalPool,
    aggressive_attempt: int,
    conservative_attempt: int,
    max_attempts: int = MAX_CROSS_REVIEW_ATTEMPTS,
) -> str:
    """为被校验的完整评分池生成稳定内容指纹。"""

    payload = json.dumps(
        {
            "pool": pool.model_dump(mode="json"),
            "aggressive_review_attempt": aggressive_attempt,
            "conservative_review_attempt": conservative_attempt,
            "max_attempts": max_attempts,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_violations(
    pool: CrossReviewedProposalPool,
) -> list[ConflictScoreViolation]:
    by_id = {item.item_id: item for item in pool.proposal_items}
    violations: list[ConflictScoreViolation] = []
    for manager in (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    ):
        own_items = [item for item in pool.proposal_items if item.proposer is manager]
        for own_item in own_items:
            own_score = _score_by_manager(own_item, manager)
            for counterpart_id in own_item.conflicts_with:
                counterpart = by_id[counterpart_id]
                counterpart_score = _score_by_manager(counterpart, manager)
                violation = _validate_conflict_pair(
                    manager=manager,
                    own_item=own_item,
                    counterpart_item=counterpart,
                    own_score=own_score,
                    counterpart_score=counterpart_score,
                )
                if violation is not None:
                    violations.append(violation)
    return violations


def _validate_conflict_pair(
    *,
    manager: PortfolioManager,
    own_item: ProposalItem,
    counterpart_item: ProposalItem,
    own_score: SupportScore,
    counterpart_score: SupportScore,
) -> ConflictScoreViolation | None:
    if own_score + counterpart_score > 0:
        return _violation(
            rule_code="CONFLICT_GROUP_SUM_POSITIVE",
            manager=manager,
            own_item=own_item,
            counterpart_item=counterpart_item,
            own_score=own_score,
            counterpart_score=counterpart_score,
            message="同一经理对同一互斥决策槽中两个原始版本的评分和必须小于或等于 0。",
        )
    return None


def _violation(
    *,
    rule_code: ConflictScoreRuleCode,
    manager: PortfolioManager,
    own_item: ProposalItem,
    counterpart_item: ProposalItem,
    own_score: SupportScore,
    counterpart_score: SupportScore,
    message: str,
) -> ConflictScoreViolation:
    return ConflictScoreViolation(
        rule_code=rule_code,
        manager=manager,
        own_item_id=own_item.item_id,
        counterpart_item_id=counterpart_item.item_id,
        own_support_score=own_score,
        counterpart_support_score=counterpart_score,
        message=message,
    )


def _score_by_manager(item: ProposalItem, manager: PortfolioManager) -> SupportScore:
    matches = [
        evaluation.support_score for evaluation in item.evaluations if evaluation.manager is manager
    ]
    if len(matches) != 1:
        raise ValueError("每位经理必须且只能对每条建议保留一个当前评分")
    return matches[0]


def _validation_state_error(
    state: ResearchGraphState,
    pool: CrossReviewedProposalPool,
    aggressive_review: PortfolioCrossReviewRecord | None,
    conservative_review: PortfolioCrossReviewRecord | None,
    max_attempts: int,
) -> str | None:
    if aggressive_review is None or conservative_review is None:
        return "both cross-review records are required"
    if aggressive_review.attempt > max_attempts or conservative_review.attempt > max_attempts:
        return "cross-review attempt exceeds the configured hard limit"
    if pool.run_id != state.get("run_id") or pool.as_of != state.get("as_of"):
        return "cross-reviewed pool scope mismatch"
    if pool.research_target != state.get("target"):
        return "cross-reviewed pool target mismatch"
    application_summary = state.get("cross_review_application_run_summary")
    if application_summary is None or application_summary.stop_reason != "complete":
        return "completed cross-review application summary is missing"
    normalized = state.get("normalized_proposal_pool")
    if normalized is None or not pool.matches_normalized_source(normalized):
        return "cross-reviewed pool normalized source mismatch"
    if (
        application_summary.aggressive_review_attempt != aggressive_review.attempt
        or application_summary.conservative_review_attempt != conservative_review.attempt
    ):
        return "cross-review application attempts do not match current records"
    if (
        aggressive_review.run_id != pool.run_id
        or conservative_review.run_id != pool.run_id
        or aggressive_review.as_of != pool.as_of
        or conservative_review.as_of != pool.as_of
    ):
        return "cross-review record scope mismatch"
    if (
        aggressive_review.own_recommendation_id != pool.aggressive_recommendation_id
        or aggressive_review.counterpart_recommendation_id != pool.conservative_recommendation_id
        or conservative_review.own_recommendation_id != pool.conservative_recommendation_id
        or conservative_review.counterpart_recommendation_id != pool.aggressive_recommendation_id
    ):
        return "cross-review recommendation IDs do not match the applied pool"
    records = {
        PortfolioManager.AGGRESSIVE: aggressive_review,
        PortfolioManager.CONSERVATIVE: conservative_review,
    }
    for item in pool.proposal_items:
        proposer_scores = [
            evaluation.support_score
            for evaluation in item.evaluations
            if evaluation.manager is item.proposer
        ]
        if len(proposer_scores) != 1 or proposer_scores[0] not in {
            0.25,
            0.5,
            0.75,
            1.0,
        }:
            return f"invalid proposer insistence score: {item.item_id}"
        reviewer = (
            PortfolioManager.CONSERVATIVE
            if item.proposer is PortfolioManager.AGGRESSIVE
            else PortfolioManager.AGGRESSIVE
        )
        draft = next(
            (
                evaluation
                for evaluation in records[reviewer].evaluations
                if evaluation.item_id == item.item_id
            ),
            None,
        )
        persisted = next(
            evaluation for evaluation in item.evaluations if evaluation.manager is reviewer
        )
        if draft is None or (
            draft.support_score != persisted.support_score
            or draft.hard_veto != persisted.hard_veto
            or draft.reason != persisted.reason
            or draft.modification_suggestion != persisted.modification_suggestion
        ):
            return f"applied evaluation does not match current review: {item.item_id}"
    return None


def _validation_failure_updates(
    state: ResearchGraphState,
    *,
    stop_reason,
    error: str,
    max_attempts: int,
    aggressive_attempt: int,
    conservative_attempt: int,
    source_fingerprint: str | None = None,
) -> ResearchGraphState:
    run_id = state.get("run_id") or "run_invalid_state"
    as_of = state.get("as_of")
    if as_of is None:
        raise ValueError("ConflictScoreValidatorNode requires as_of to report failure")
    report = ConflictScoreValidationReport(
        run_id=run_id,
        as_of=as_of,
        source_fingerprint=source_fingerprint or ("0" * 64),
        max_attempts=max_attempts,
        aggressive_review_attempt=aggressive_attempt,
        conservative_review_attempt=conservative_attempt,
        valid=False,
        stop_reason=stop_reason,
    )
    if state.get("conflict_score_validation_report") == report and error in state.get("errors", []):
        return {}
    return {
        "conflict_score_validation_report": report,
        "errors": [error],
    }
