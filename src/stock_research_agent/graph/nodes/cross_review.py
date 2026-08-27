"""两位投资组合经理并行交叉评分，并在汇合后确定性写入提案池。"""

import asyncio

from stock_research_agent.agents.debate import (
    ConflictScoreValidationReport,
    CrossReviewApplicationRunSummary,
    CrossReviewCorrectionRunSummary,
    CrossReviewedProposalPool,
    CrossReviewProposalContext,
    NormalizedProposalPool,
    PortfolioCrossReviewInput,
    PortfolioCrossReviewLimits,
    PortfolioCrossReviewModel,
    PortfolioCrossReviewRecord,
    PortfolioCrossReviewRunSummary,
)
from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain.enums import (
    PortfolioManager,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.graph.nodes.conflict_score_validation import (
    conflict_score_source_fingerprint,
)
from stock_research_agent.graph.state import ResearchGraphState
from stock_research_agent.llm import describe_exception

_COMPLETED_THESIS_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.REFUTED,
    ThesisValidationStatus.MIXED,
    ThesisValidationStatus.INCONCLUSIVE,
}
_ELIGIBLE_THESIS_STATUSES = {
    ThesisValidationStatus.SUPPORTED,
    ThesisValidationStatus.MIXED,
}


def build_aggressive_cross_review_node(
    model: PortfolioCrossReviewModel,
    *,
    limits: PortfolioCrossReviewLimits | None = None,
):
    return _build_cross_review_node(
        model,
        reviewer=PortfolioManager.AGGRESSIVE,
        output_field="aggressive_cross_review",
        summary_field="aggressive_cross_review_run_summary",
        limits=limits,
    )


def build_conservative_cross_review_node(
    model: PortfolioCrossReviewModel,
    *,
    limits: PortfolioCrossReviewLimits | None = None,
):
    return _build_cross_review_node(
        model,
        reviewer=PortfolioManager.CONSERVATIVE,
        output_field="conservative_cross_review",
        summary_field="conservative_cross_review_run_summary",
        limits=limits,
    )


def build_cross_review_correction_node(
    aggressive_model: PortfolioCrossReviewModel,
    conservative_model: PortfolioCrossReviewModel,
    *,
    limits: PortfolioCrossReviewLimits | None = None,
):
    """只重评校验报告点名的经理；双方违规时并发调用并原子提交。"""

    configured = limits or PortfolioCrossReviewLimits()
    aggressive_node = build_aggressive_cross_review_node(
        aggressive_model,
        limits=configured,
    )
    conservative_node = build_conservative_cross_review_node(
        conservative_model,
        limits=configured,
    )

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        report = state.get("conflict_score_validation_report")
        if report is None or report.stop_reason != "retry_required":
            return _correction_failure_updates(
                state,
                report,
                stop_reason="missing_retry_report",
                errors=["CrossReviewCorrectionNode skipped: retry-required report is missing"],
            )
        if _existing_correction_matches_state(
            state,
            report,
            configured.max_attempts,
        ):
            return {}

        state_error = _correction_state_error(state, report, configured.max_attempts)
        if state_error is not None:
            return _correction_failure_updates(
                state,
                report,
                stop_reason="invalid_state",
                errors=[f"CrossReviewCorrectionNode skipped: {state_error}"],
            )

        jobs = []
        managers = []
        if PortfolioManager.AGGRESSIVE in report.invalid_managers:
            managers.append(PortfolioManager.AGGRESSIVE)
            jobs.append(aggressive_node(state))
        if PortfolioManager.CONSERVATIVE in report.invalid_managers:
            managers.append(PortfolioManager.CONSERVATIVE)
            jobs.append(conservative_node(state))

        try:
            results = await asyncio.gather(*jobs)
        except Exception as exc:
            attempted = {manager: _report_attempt(report, manager) + 1 for manager in managers}
            return _correction_failure_updates(
                state,
                report,
                stop_reason="attempt_failed",
                errors=[f"CrossReviewCorrectionNode failed unexpectedly: {type(exc).__name__}"],
                aggressive_attempt=attempted.get(
                    PortfolioManager.AGGRESSIVE,
                    state["aggressive_cross_review"].attempt,
                ),
                conservative_attempt=attempted.get(
                    PortfolioManager.CONSERVATIVE,
                    state["conservative_cross_review"].attempt,
                ),
            )
        output_fields = {
            PortfolioManager.AGGRESSIVE: (
                "aggressive_cross_review",
                "aggressive_cross_review_run_summary",
            ),
            PortfolioManager.CONSERVATIVE: (
                "conservative_cross_review",
                "conservative_cross_review_run_summary",
            ),
        }
        merged: ResearchGraphState = {}
        errors: list[str] = []
        completed: list[PortfolioManager] = []
        called: list[PortfolioManager] = []
        failed: list[PortfolioManager] = []
        attempted = {
            PortfolioManager.AGGRESSIVE: state["aggressive_cross_review"].attempt,
            PortfolioManager.CONSERVATIVE: state["conservative_cross_review"].attempt,
        }
        for manager, result in zip(managers, results, strict=True):
            record_field, summary_field = output_fields[manager]
            record = result.get(record_field)
            summary = result.get(summary_field)
            errors.extend(result.get("errors", []))
            if summary is not None:
                attempted[manager] = summary.attempt
                if summary.model_called:
                    called.append(manager)
            expected_attempt = _report_attempt(report, manager) + 1
            if (
                record is None
                or summary is None
                or summary.stop_reason != "complete"
                or summary.reviewer is not manager
                or summary.attempt != expected_attempt
                or record.reviewer is not manager
                or record.attempt != expected_attempt
            ):
                failed.append(manager)
                continue
            merged[record_field] = record
            merged[summary_field] = summary
            completed.append(manager)

        if failed:
            if not errors:
                errors = [
                    f"CrossReviewCorrectionNode failed: {manager.value}" for manager in failed
                ]
            return _correction_failure_updates(
                state,
                report,
                stop_reason="attempt_failed",
                errors=errors,
                called_managers=tuple(called),
                staged_managers=tuple(completed),
                aggressive_attempt=attempted[PortfolioManager.AGGRESSIVE],
                conservative_attempt=attempted[PortfolioManager.CONSERVATIVE],
            )

        aggressive_attempt = (
            merged.get("aggressive_cross_review") or state["aggressive_cross_review"]
        ).attempt
        conservative_attempt = (
            merged.get("conservative_cross_review") or state["conservative_cross_review"]
        ).attempt
        merged["cross_review_correction_run_summary"] = CrossReviewCorrectionRunSummary(
            source_validation_fingerprint=report.source_fingerprint,
            requested_managers=tuple(managers),
            called_managers=tuple(managers),
            staged_managers=tuple(completed),
            completed_managers=tuple(completed),
            aggressive_attempt=aggressive_attempt,
            conservative_attempt=conservative_attempt,
            stop_reason="complete",
        )
        return merged

    return node


def route_after_cross_review_correction(
    state: ResearchGraphState,
) -> str:
    summary = state.get("cross_review_correction_run_summary")
    if summary is not None and summary.stop_reason == "complete":
        return "apply"
    return "failed"


def _build_cross_review_node(
    model: PortfolioCrossReviewModel,
    *,
    reviewer: PortfolioManager,
    output_field: str,
    summary_field: str,
    limits: PortfolioCrossReviewLimits | None,
):
    configured = limits or PortfolioCrossReviewLimits()

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        existing = state.get(output_field)
        validation_report = state.get("conflict_score_validation_report")
        retry_feedback = _retry_feedback(validation_report, reviewer)
        retry_requested = bool(retry_feedback)
        attempt = 1
        previous_evaluations = ()
        if retry_requested:
            if existing is None:
                return _review_failure_updates(
                    reviewer,
                    summary_field,
                    attempt=1,
                    stop_reason="invalid_state",
                    error=f"{reviewer.value} correction skipped: previous review is missing",
                )
            expected_attempt = _report_attempt(validation_report, reviewer)
            if existing.attempt != expected_attempt:
                return _review_failure_updates(
                    reviewer,
                    summary_field,
                    attempt=existing.attempt,
                    stop_reason="invalid_state",
                    error=f"{reviewer.value} correction skipped: review attempt mismatch",
                )
            if existing.attempt >= configured.max_attempts:
                return _review_failure_updates(
                    reviewer,
                    summary_field,
                    attempt=existing.attempt,
                    stop_reason="invalid_state",
                    error=f"{reviewer.value} correction skipped: attempt limit exhausted",
                )
            attempt = existing.attempt + 1
            previous_evaluations = tuple(existing.evaluations)
        elif existing is not None:
            if _existing_review_matches_state(existing, state, reviewer):
                return {}
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=existing.attempt,
                stop_reason="invalid_state",
                error=f"{reviewer.value} refused to overwrite an incompatible cross-review",
            )

        pool = state.get("normalized_proposal_pool")
        if pool is None:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                stop_reason="missing_normalized_pool",
                error=f"{reviewer.value} cross-review skipped: normalized proposal pool is missing",
            )

        state_error = _cross_review_state_error(state, pool)
        if state_error is not None:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                stop_reason="invalid_state",
                error=f"{reviewer.value} cross-review skipped: {state_error}",
            )

        theses = list(state.get("thesis_pool", []))
        own_items = [item for item in pool.proposal_items if item.proposer is reviewer]
        counterpart = _counterpart_of(reviewer)
        counterpart_items = [item for item in pool.proposal_items if item.proposer is counterpart]
        if len(theses) > configured.max_input_theses:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                stop_reason="thesis_limit_exceeded",
                error=(
                    f"{reviewer.value} cross-review skipped: thesis count exceeds configured "
                    "hard limit; input was not truncated"
                ),
            )

        try:
            thesis_summaries = tuple(DecisionThesisSummary.from_record(thesis) for thesis in theses)
            eligible_ids = tuple(
                thesis.thesis_id
                for thesis in theses
                if thesis.validation.status in _ELIGIBLE_THESIS_STATUSES
            )
            own_recommendation_id, counterpart_recommendation_id = _recommendation_ids(
                pool,
                reviewer,
            )
            request = PortfolioCrossReviewInput(
                run_id=state["run_id"],
                as_of=state["as_of"],
                research_target=state["target"],
                reviewer=reviewer,
                own_recommendation_id=own_recommendation_id,
                counterpart_recommendation_id=counterpart_recommendation_id,
                attempt=attempt,
                own_proposals=tuple(
                    CrossReviewProposalContext.from_proposal_item(item) for item in own_items
                ),
                counterpart_proposals=tuple(
                    CrossReviewProposalContext.from_proposal_item(item)
                    for item in counterpart_items
                ),
                theses=thesis_summaries,
                eligible_supporting_thesis_ids=eligible_ids,
                previous_evaluations=previous_evaluations,
                validation_feedback=retry_feedback,
                policy_notes=(
                    "只评价 counterpart_proposals，不得重写任何原始条目。",
                    "conflicts_with 表示竞争同一决策槽，不表示某条建议事实错误。",
                    "冲突评分一致性将在后续确定性节点中再次校验。",
                    "首次交叉评分没有 previous_score 或 score_change_reason。",
                ),
            )
        except Exception as exc:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                stop_reason="invalid_state",
                error=(
                    f"{reviewer.value} cross-review skipped: input assembly failed: "
                    f"{type(exc).__name__}"
                ),
            )

        context_characters = len(request.model_dump_json(indent=2))
        if context_characters > configured.max_context_characters:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                context_character_count=context_characters,
                stop_reason="context_limit_exceeded",
                error=(
                    f"{reviewer.value} cross-review skipped: context exceeds configured "
                    "hard limit; input was not truncated"
                ),
            )

        try:
            draft = await model.review_recommendation(request)
        except Exception as exc:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                context_character_count=context_characters,
                model_called=True,
                stop_reason="model_error",
                error=f"{reviewer.value} cross-review model failed: {describe_exception(exc)}",
            )

        try:
            draft.validate_against(request)
            _validate_retry_output(draft, request)
        except ValueError as exc:
            return _review_failure_updates(
                reviewer,
                summary_field,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                context_character_count=context_characters,
                model_called=True,
                evaluation_count=len(draft.evaluations),
                stop_reason="rejected_output",
                error=(f"{reviewer.value} cross-review rejected: {_safe_rejection_reason(exc)}"),
            )

        evaluations_by_id = {evaluation.item_id: evaluation for evaluation in draft.evaluations}
        ordered_evaluations = tuple(
            evaluations_by_id[proposal.item_id] for proposal in request.counterpart_proposals
        )
        record = PortfolioCrossReviewRecord(
            run_id=state["run_id"],
            as_of=state["as_of"],
            reviewer=reviewer,
            attempt=attempt,
            own_recommendation_id=request.own_recommendation_id,
            counterpart_recommendation_id=request.counterpart_recommendation_id,
            evaluations=ordered_evaluations,
            previous_evaluations=previous_evaluations,
            correction_feedback=retry_feedback,
            created_at=state["as_of"],
        )
        return {
            output_field: record,
            summary_field: _review_summary(
                reviewer,
                attempt=attempt,
                own_proposal_count=len(own_items),
                counterpart_proposal_count=len(counterpart_items),
                input_thesis_count=len(theses),
                context_character_count=context_characters,
                model_called=True,
                evaluation_count=len(ordered_evaluations),
                stop_reason="complete",
            ),
        }

    return node


def apply_cross_reviews_node(state: ResearchGraphState) -> ResearchGraphState:
    """在双方并行评分结束后，为每条提案写入唯一的对方评价。"""

    existing = state.get("cross_reviewed_proposal_pool")
    if existing is not None:
        if _existing_applied_pool_matches_state(existing, state):
            return {}
        if not _can_replace_pool_after_correction(state):
            return _application_failure_updates(
                state,
                stop_reason="invalid_state",
                error="ApplyCrossReviewsNode refused to overwrite an incompatible pool",
            )

    pool = state.get("normalized_proposal_pool")
    aggressive_review = state.get("aggressive_cross_review")
    conservative_review = state.get("conservative_cross_review")
    if pool is None or aggressive_review is None or conservative_review is None:
        return _application_failure_updates(
            state,
            stop_reason="missing_cross_review",
            error="ApplyCrossReviewsNode skipped: normalized pool and both reviews are required",
        )

    application_error = _application_state_error(
        state,
        pool,
        aggressive_review,
        conservative_review,
    )
    if application_error is not None:
        return _application_failure_updates(
            state,
            stop_reason="invalid_state",
            error=f"ApplyCrossReviewsNode skipped: {application_error}",
        )

    reviews_by_manager = {
        PortfolioManager.AGGRESSIVE: aggressive_review,
        PortfolioManager.CONSERVATIVE: conservative_review,
    }
    try:
        updated_items = [
            _apply_counterpart_evaluation(item, reviews_by_manager) for item in pool.proposal_items
        ]
        reviewed_pool = CrossReviewedProposalPool(
            run_id=pool.run_id,
            as_of=pool.as_of,
            research_target=pool.research_target,
            aggressive_recommendation_id=pool.aggressive_recommendation_id,
            conservative_recommendation_id=pool.conservative_recommendation_id,
            proposal_items=tuple(updated_items),
        )
    except Exception as exc:
        return _application_failure_updates(
            state,
            stop_reason="invalid_state",
            error=f"ApplyCrossReviewsNode skipped: application failed: {type(exc).__name__}",
        )

    return {
        "cross_reviewed_proposal_pool": reviewed_pool,
        "cross_review_application_run_summary": CrossReviewApplicationRunSummary(
            input_proposal_count=len(pool.proposal_items),
            output_proposal_count=len(updated_items),
            applied_evaluation_count=len(updated_items),
            aggressive_review_attempt=aggressive_review.attempt,
            conservative_review_attempt=conservative_review.attempt,
            stop_reason="complete",
        ),
    }


def _cross_review_state_error(
    state: ResearchGraphState,
    pool: NormalizedProposalPool,
) -> str | None:
    if not state.get("run_id") or state.get("as_of") is None or state.get("target") is None:
        return "run_id, target and as_of are required"
    if pool.run_id != state["run_id"]:
        return "normalized proposal pool run_id mismatch"
    if pool.as_of != state["as_of"]:
        return "normalized proposal pool as_of mismatch"
    if pool.research_target != state["target"]:
        return "normalized proposal pool target mismatch"
    normalization_summary = state.get("proposal_normalization_run_summary")
    if normalization_summary is None or normalization_summary.stop_reason != "complete":
        return "completed proposal normalization summary is missing"
    aggressive = state.get("aggressive_recommendation")
    conservative = state.get("conservative_recommendation")
    if aggressive is None or conservative is None:
        return "source recommendations are missing"
    if (
        pool.aggressive_recommendation_id != aggressive.recommendation_id
        or pool.conservative_recommendation_id != conservative.recommendation_id
    ):
        return "normalized proposal pool source recommendation IDs mismatch"
    if state.get("thesis_validation_run_summary") is None:
        return "thesis validation run summary is missing"
    theses = list(state.get("thesis_pool", []))
    for thesis in theses:
        if thesis.run_id != state["run_id"] or thesis.as_of != state["as_of"]:
            return f"thesis scope mismatch: {thesis.thesis_id}"
        if thesis.validation.status not in _COMPLETED_THESIS_STATUSES:
            return f"unfinished thesis cannot enter cross-review: {thesis.thesis_id}"
        if thesis.validation.confidence is None or thesis.reasoning_summary is None:
            return f"completed thesis lacks decision context: {thesis.thesis_id}"
    return None


def _application_state_error(
    state: ResearchGraphState,
    pool: NormalizedProposalPool,
    aggressive_review: PortfolioCrossReviewRecord,
    conservative_review: PortfolioCrossReviewRecord,
) -> str | None:
    if aggressive_review.reviewer is not PortfolioManager.AGGRESSIVE:
        return "aggressive cross-review has the wrong reviewer"
    if conservative_review.reviewer is not PortfolioManager.CONSERVATIVE:
        return "conservative cross-review has the wrong reviewer"
    for review in (aggressive_review, conservative_review):
        if review.run_id != pool.run_id or review.run_id != state.get("run_id"):
            return f"cross-review run_id mismatch: {review.reviewer.value}"
        if review.as_of != pool.as_of or review.as_of != state.get("as_of"):
            return f"cross-review as_of mismatch: {review.reviewer.value}"
        expected_own, expected_counterpart = _recommendation_ids(pool, review.reviewer)
        if (
            review.own_recommendation_id != expected_own
            or review.counterpart_recommendation_id != expected_counterpart
        ):
            return f"cross-review recommendation IDs mismatch: {review.reviewer.value}"

        expected_item_ids = {
            item.item_id
            for item in pool.proposal_items
            if item.proposer is _counterpart_of(review.reviewer)
        }
        actual_item_ids = {evaluation.item_id for evaluation in review.evaluations}
        if actual_item_ids != expected_item_ids:
            return f"cross-review item coverage mismatch: {review.reviewer.value}"
    return None


def _apply_counterpart_evaluation(
    item: ProposalItem,
    reviews_by_manager: dict[PortfolioManager, PortfolioCrossReviewRecord],
) -> ProposalItem:
    reviewer = _counterpart_of(item.proposer)
    review = reviews_by_manager[reviewer]
    draft = next(
        evaluation for evaluation in review.evaluations if evaluation.item_id == item.item_id
    )
    previous = next(
        (
            evaluation
            for evaluation in review.previous_evaluations
            if evaluation.item_id == item.item_id
        ),
        None,
    )
    feedback_codes = sorted(
        {
            feedback.rule_code
            for feedback in review.correction_feedback
            if feedback.counterpart_item_id == item.item_id
        }
    )
    evaluation = ProposalEvaluation(
        manager=reviewer,
        previous_score=(previous.support_score if previous is not None else None),
        support_score=draft.support_score,
        hard_veto=draft.hard_veto,
        reason=draft.reason,
        modification_suggestion=draft.modification_suggestion,
        score_change_reason=(
            (
                "ConflictScoreValidatorNode 要求第 "
                f"{review.attempt} 次评分纠错：{', '.join(feedback_codes)}"
            )
            if feedback_codes
            else "本条未触发评分违规，在纠错重试中保持原评价不变。"
            if previous is not None
            else None
        ),
    )
    return item.model_copy(
        deep=True,
        update={"evaluations": [*item.evaluations, evaluation]},
    )


def _recommendation_ids(
    pool: NormalizedProposalPool,
    reviewer: PortfolioManager,
) -> tuple[str, str]:
    if reviewer is PortfolioManager.AGGRESSIVE:
        return (
            pool.aggressive_recommendation_id,
            pool.conservative_recommendation_id,
        )
    return (
        pool.conservative_recommendation_id,
        pool.aggressive_recommendation_id,
    )


def _counterpart_of(manager: PortfolioManager) -> PortfolioManager:
    if manager is PortfolioManager.AGGRESSIVE:
        return PortfolioManager.CONSERVATIVE
    return PortfolioManager.AGGRESSIVE


def _existing_review_matches_state(
    review: PortfolioCrossReviewRecord,
    state: ResearchGraphState,
    reviewer: PortfolioManager,
) -> bool:
    pool = state.get("normalized_proposal_pool")
    if pool is None:
        return False
    own_id, counterpart_id = _recommendation_ids(pool, reviewer)
    expected_item_ids = {
        item.item_id for item in pool.proposal_items if item.proposer is _counterpart_of(reviewer)
    }
    return (
        review.reviewer is reviewer
        and review.run_id == state.get("run_id")
        and review.as_of == state.get("as_of")
        and review.own_recommendation_id == own_id
        and review.counterpart_recommendation_id == counterpart_id
        and {evaluation.item_id for evaluation in review.evaluations} == expected_item_ids
    )


def _existing_applied_pool_matches_state(
    pool: CrossReviewedProposalPool,
    state: ResearchGraphState,
) -> bool:
    normalized = state.get("normalized_proposal_pool")
    aggressive_review = state.get("aggressive_cross_review")
    conservative_review = state.get("conservative_cross_review")
    if (
        normalized is None
        or aggressive_review is None
        or conservative_review is None
        or pool.run_id != normalized.run_id
        or pool.as_of != normalized.as_of
        or pool.research_target != normalized.research_target
        or pool.aggressive_recommendation_id != normalized.aggressive_recommendation_id
        or pool.conservative_recommendation_id != normalized.conservative_recommendation_id
        or _application_state_error(
            state,
            normalized,
            aggressive_review,
            conservative_review,
        )
        is not None
    ):
        return False

    reviews_by_manager = {
        PortfolioManager.AGGRESSIVE: aggressive_review,
        PortfolioManager.CONSERVATIVE: conservative_review,
    }
    try:
        expected_items = tuple(
            _apply_counterpart_evaluation(item, reviews_by_manager)
            for item in normalized.proposal_items
        )
    except (KeyError, ValueError):
        return False
    return pool.proposal_items == expected_items


def _retry_feedback(
    report: ConflictScoreValidationReport | None,
    reviewer: PortfolioManager,
):
    if report is None or report.stop_reason != "retry_required":
        return ()
    if reviewer not in report.invalid_managers:
        return ()
    return tuple(violation for violation in report.violations if violation.manager is reviewer)


def _report_attempt(
    report: ConflictScoreValidationReport | None,
    reviewer: PortfolioManager,
) -> int:
    if report is None:
        return 0
    if reviewer is PortfolioManager.AGGRESSIVE:
        return report.aggressive_review_attempt
    return report.conservative_review_attempt


def _validate_retry_output(draft, request: PortfolioCrossReviewInput) -> None:
    if request.attempt == 1:
        return
    editable_ids = {feedback.counterpart_item_id for feedback in request.validation_feedback}
    previous_by_id = {evaluation.item_id: evaluation for evaluation in request.previous_evaluations}
    for evaluation in draft.evaluations:
        if evaluation.item_id in editable_ids:
            continue
        if evaluation != previous_by_id[evaluation.item_id]:
            raise ValueError("correction changed an item that did not violate a score rule")


def _safe_rejection_reason(exc: ValueError) -> str:
    if "did not violate" in str(exc):
        return "correction changed an unrequested item"
    return "item coverage mismatch"


def _correction_state_error(
    state: ResearchGraphState,
    report: ConflictScoreValidationReport,
    max_attempts: int,
) -> str | None:
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    pool = state.get("cross_reviewed_proposal_pool")
    normalized = state.get("normalized_proposal_pool")
    if aggressive is None or conservative is None or pool is None or normalized is None:
        return "current reviews, normalized pool and cross-reviewed pool are required"
    if not pool.matches_normalized_source(normalized):
        return "cross-reviewed pool normalized source mismatch"
    if report.run_id != state.get("run_id") or report.as_of != state.get("as_of"):
        return "validation report scope mismatch"
    if report.max_attempts != max_attempts:
        return "validation report attempt limit mismatch"
    current_fingerprint = conflict_score_source_fingerprint(
        pool,
        aggressive.attempt,
        conservative.attempt,
        max_attempts,
    )
    if report.source_fingerprint != current_fingerprint:
        return "validation report source fingerprint mismatch"
    if (
        report.aggressive_review_attempt != aggressive.attempt
        or report.conservative_review_attempt != conservative.attempt
    ):
        return "validation report attempts do not match current records"
    attempts = {
        PortfolioManager.AGGRESSIVE: aggressive.attempt,
        PortfolioManager.CONSERVATIVE: conservative.attempt,
    }
    if any(attempts[manager] >= max_attempts for manager in report.invalid_managers):
        return "an invalid manager has exhausted the correction attempt limit"
    return None


def _existing_correction_matches_state(
    state: ResearchGraphState,
    report: ConflictScoreValidationReport,
    max_attempts: int,
) -> bool:
    summary = state.get("cross_review_correction_run_summary")
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    source_pool = state.get("cross_reviewed_proposal_pool")
    normalized = state.get("normalized_proposal_pool")
    if (
        summary is None
        or aggressive is None
        or conservative is None
        or source_pool is None
        or normalized is None
        or not source_pool.matches_normalized_source(normalized)
        or report.run_id != state.get("run_id")
        or report.as_of != state.get("as_of")
        or report.max_attempts != max_attempts
        or summary.source_validation_fingerprint != report.source_fingerprint
        or set(summary.requested_managers) != set(report.invalid_managers)
    ):
        return False
    expected_aggressive = report.aggressive_review_attempt + (
        1 if PortfolioManager.AGGRESSIVE in report.invalid_managers else 0
    )
    expected_conservative = report.conservative_review_attempt + (
        1 if PortfolioManager.CONSERVATIVE in report.invalid_managers else 0
    )
    if summary.stop_reason == "complete":
        if not (
            aggressive.attempt == expected_aggressive
            and conservative.attempt == expected_conservative
            and summary.aggressive_attempt == aggressive.attempt
            and summary.conservative_attempt == conservative.attempt
            and set(summary.completed_managers) == set(report.invalid_managers)
            and _corrected_record_matches_report(
                state,
                report,
                aggressive,
                PortfolioManager.AGGRESSIVE,
            )
            and _corrected_record_matches_report(
                state,
                report,
                conservative,
                PortfolioManager.CONSERVATIVE,
            )
        ):
            return False
        pool = state.get("cross_reviewed_proposal_pool")
        if pool is None:
            return False
        source_pool_still_present = (
            conflict_score_source_fingerprint(
                pool,
                report.aggressive_review_attempt,
                report.conservative_review_attempt,
                max_attempts,
            )
            == report.source_fingerprint
        )
        if source_pool_still_present:
            records = {
                PortfolioManager.AGGRESSIVE: aggressive,
                PortfolioManager.CONSERVATIVE: conservative,
            }
            return all(
                _previous_evaluations_match_pool(records[manager], pool, manager)
                if manager in report.invalid_managers
                else _record_evaluations_match_pool(records[manager], pool, manager)
                for manager in (
                    PortfolioManager.AGGRESSIVE,
                    PortfolioManager.CONSERVATIVE,
                )
            )
        return _existing_applied_pool_matches_state(pool, state)
    if summary.stop_reason == "attempt_failed":
        pool = state.get("cross_reviewed_proposal_pool")
        if pool is None:
            return False
        source_pool_unchanged = (
            conflict_score_source_fingerprint(
                pool,
                aggressive.attempt,
                conservative.attempt,
                max_attempts,
            )
            == report.source_fingerprint
        )
        return (
            source_pool_unchanged
            and set(summary.called_managers) <= set(report.invalid_managers)
            and set(summary.staged_managers) <= set(summary.called_managers)
            and aggressive.attempt == report.aggressive_review_attempt
            and conservative.attempt == report.conservative_review_attempt
            and summary.aggressive_attempt == expected_aggressive
            and summary.conservative_attempt == expected_conservative
        )
    return False


def _corrected_record_matches_report(
    state: ResearchGraphState,
    report: ConflictScoreValidationReport,
    record: PortfolioCrossReviewRecord,
    manager: PortfolioManager,
) -> bool:
    normalized = state.get("normalized_proposal_pool")
    if normalized is None:
        return False
    expected_own_id, expected_counterpart_id = _recommendation_ids(
        normalized,
        manager,
    )
    expected_attempt = _report_attempt(report, manager) + (
        1 if manager in report.invalid_managers else 0
    )
    expected_ids = {
        item.item_id
        for item in normalized.proposal_items
        if item.proposer is _counterpart_of(manager)
    }
    if (
        record.run_id != report.run_id
        or record.as_of != report.as_of
        or record.reviewer is not manager
        or record.attempt != expected_attempt
        or record.own_recommendation_id != expected_own_id
        or record.counterpart_recommendation_id != expected_counterpart_id
        or {evaluation.item_id for evaluation in record.evaluations} != expected_ids
    ):
        return False
    if manager not in report.invalid_managers:
        return True

    expected_feedback = _retry_feedback(report, manager)
    if (
        record.correction_feedback != expected_feedback
        or {evaluation.item_id for evaluation in record.previous_evaluations} != expected_ids
    ):
        return False
    editable_ids = {feedback.counterpart_item_id for feedback in expected_feedback}
    previous_by_id = {evaluation.item_id: evaluation for evaluation in record.previous_evaluations}
    return all(
        evaluation.item_id in editable_ids or evaluation == previous_by_id[evaluation.item_id]
        for evaluation in record.evaluations
    )


def _previous_evaluations_match_pool(
    record: PortfolioCrossReviewRecord,
    pool: CrossReviewedProposalPool,
    manager: PortfolioManager,
) -> bool:
    previous_by_id = {evaluation.item_id: evaluation for evaluation in record.previous_evaluations}
    counterpart_items = [
        item for item in pool.proposal_items if item.proposer is _counterpart_of(manager)
    ]
    if set(previous_by_id) != {item.item_id for item in counterpart_items}:
        return False
    for item in counterpart_items:
        persisted = next(
            (evaluation for evaluation in item.evaluations if evaluation.manager is manager),
            None,
        )
        previous = previous_by_id[item.item_id]
        if persisted is None or (
            previous.support_score != persisted.support_score
            or previous.hard_veto != persisted.hard_veto
            or previous.reason != persisted.reason
            or previous.modification_suggestion != persisted.modification_suggestion
        ):
            return False
    return True


def _record_evaluations_match_pool(
    record: PortfolioCrossReviewRecord,
    pool: CrossReviewedProposalPool,
    manager: PortfolioManager,
) -> bool:
    current_by_id = {evaluation.item_id: evaluation for evaluation in record.evaluations}
    counterpart_items = [
        item for item in pool.proposal_items if item.proposer is _counterpart_of(manager)
    ]
    if set(current_by_id) != {item.item_id for item in counterpart_items}:
        return False
    for item in counterpart_items:
        persisted = next(
            (evaluation for evaluation in item.evaluations if evaluation.manager is manager),
            None,
        )
        current = current_by_id[item.item_id]
        if persisted is None or (
            current.support_score != persisted.support_score
            or current.hard_veto != persisted.hard_veto
            or current.reason != persisted.reason
            or current.modification_suggestion != persisted.modification_suggestion
        ):
            return False
    return True


def _can_replace_pool_after_correction(state: ResearchGraphState) -> bool:
    report = state.get("conflict_score_validation_report")
    summary = state.get("cross_review_correction_run_summary")
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    source_pool = state.get("cross_reviewed_proposal_pool")
    normalized = state.get("normalized_proposal_pool")
    if (
        report is None
        or summary is None
        or aggressive is None
        or conservative is None
        or source_pool is None
        or normalized is None
        or not source_pool.matches_normalized_source(normalized)
        or report.stop_reason != "retry_required"
        or summary.stop_reason != "complete"
        or summary.source_validation_fingerprint != report.source_fingerprint
        or set(summary.requested_managers) != set(report.invalid_managers)
        or set(summary.completed_managers) != set(report.invalid_managers)
        or conflict_score_source_fingerprint(
            source_pool,
            report.aggressive_review_attempt,
            report.conservative_review_attempt,
            report.max_attempts,
        )
        != report.source_fingerprint
        or not _corrected_record_matches_report(
            state,
            report,
            aggressive,
            PortfolioManager.AGGRESSIVE,
        )
        or not _corrected_record_matches_report(
            state,
            report,
            conservative,
            PortfolioManager.CONSERVATIVE,
        )
    ):
        return False
    expected_aggressive = report.aggressive_review_attempt + (
        1 if PortfolioManager.AGGRESSIVE in report.invalid_managers else 0
    )
    expected_conservative = report.conservative_review_attempt + (
        1 if PortfolioManager.CONSERVATIVE in report.invalid_managers else 0
    )
    records = {
        PortfolioManager.AGGRESSIVE: aggressive,
        PortfolioManager.CONSERVATIVE: conservative,
    }
    provenance_matches = all(
        _previous_evaluations_match_pool(records[manager], source_pool, manager)
        if manager in report.invalid_managers
        else _record_evaluations_match_pool(records[manager], source_pool, manager)
        for manager in (
            PortfolioManager.AGGRESSIVE,
            PortfolioManager.CONSERVATIVE,
        )
    )
    return (
        aggressive.attempt == expected_aggressive
        and conservative.attempt == expected_conservative
        and summary.aggressive_attempt == aggressive.attempt
        and summary.conservative_attempt == conservative.attempt
        and provenance_matches
    )


def _correction_failure_updates(
    state: ResearchGraphState,
    report: ConflictScoreValidationReport | None,
    *,
    stop_reason,
    errors: list[str],
    called_managers: tuple[PortfolioManager, ...] = (),
    staged_managers: tuple[PortfolioManager, ...] = (),
    aggressive_attempt: int | None = None,
    conservative_attempt: int | None = None,
) -> ResearchGraphState:
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    return {
        "cross_review_correction_run_summary": CrossReviewCorrectionRunSummary(
            source_validation_fingerprint=(
                report.source_fingerprint if report is not None else "0" * 64
            ),
            requested_managers=(report.invalid_managers if report is not None else ()),
            called_managers=called_managers,
            staged_managers=staged_managers,
            completed_managers=(),
            aggressive_attempt=(
                aggressive_attempt
                if aggressive_attempt is not None
                else aggressive.attempt
                if aggressive is not None
                else 0
            ),
            conservative_attempt=(
                conservative_attempt
                if conservative_attempt is not None
                else conservative.attempt
                if conservative is not None
                else 0
            ),
            stop_reason=stop_reason,
        ),
        "errors": errors,
    }


def _review_failure_updates(
    reviewer: PortfolioManager,
    summary_field: str,
    *,
    attempt: int,
    stop_reason,
    error: str,
    own_proposal_count: int = 0,
    counterpart_proposal_count: int = 0,
    input_thesis_count: int = 0,
    context_character_count: int = 0,
    model_called: bool = False,
    evaluation_count: int = 0,
) -> ResearchGraphState:
    return {
        summary_field: _review_summary(
            reviewer,
            attempt=attempt,
            own_proposal_count=own_proposal_count,
            counterpart_proposal_count=counterpart_proposal_count,
            input_thesis_count=input_thesis_count,
            context_character_count=context_character_count,
            model_called=model_called,
            evaluation_count=evaluation_count,
            stop_reason=stop_reason,
        ),
        "errors": [error],
    }


def _review_summary(
    reviewer: PortfolioManager,
    *,
    attempt: int,
    own_proposal_count: int = 0,
    counterpart_proposal_count: int = 0,
    input_thesis_count: int = 0,
    context_character_count: int = 0,
    model_called: bool = False,
    evaluation_count: int = 0,
    stop_reason="invalid_state",
) -> PortfolioCrossReviewRunSummary:
    return PortfolioCrossReviewRunSummary(
        reviewer=reviewer,
        attempt=attempt,
        own_proposal_count=own_proposal_count,
        counterpart_proposal_count=counterpart_proposal_count,
        input_thesis_count=input_thesis_count,
        context_character_count=context_character_count,
        model_called=model_called,
        evaluation_count=evaluation_count,
        stop_reason=stop_reason,
    )


def _application_failure_updates(
    state: ResearchGraphState,
    *,
    stop_reason,
    error: str,
) -> ResearchGraphState:
    pool = state.get("normalized_proposal_pool")
    aggressive = state.get("aggressive_cross_review")
    conservative = state.get("conservative_cross_review")
    return {
        "cross_review_application_run_summary": CrossReviewApplicationRunSummary(
            input_proposal_count=(len(pool.proposal_items) if pool is not None else 0),
            output_proposal_count=0,
            applied_evaluation_count=0,
            aggressive_review_attempt=(aggressive.attempt if aggressive is not None else 0),
            conservative_review_attempt=(conservative.attempt if conservative is not None else 0),
            stop_reason=stop_reason,
        ),
        "errors": [error],
    }
