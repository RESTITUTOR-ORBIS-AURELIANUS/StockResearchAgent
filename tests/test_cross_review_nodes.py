"""投资组合经理交叉评分模型适配器、并行节点和确定性写入测试。"""

import asyncio
from datetime import datetime

import pytest
from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.debate import (
    CrossReviewEvaluationDraft,
    OpenAIAggressivePortfolioCrossReviewModel,
    OpenAIConservativePortfolioCrossReviewModel,
    PortfolioCrossReviewDraft,
    PortfolioCrossReviewLimits,
)
from stock_research_agent.agents.debate.prompts import (
    AGGRESSIVE_CROSS_REVIEW_SYSTEM_PROMPT,
    CONSERVATIVE_CROSS_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.agents.validator import ThesisValidationRunSummary
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    RecommendationAction,
    RecommendationProfile,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import (
    ProposalEvaluation,
    ProposalItem,
    RecommendationRecord,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph.builder import build_research_graph
from stock_research_agent.graph.nodes.conflict_score_validation import (
    build_conflict_score_validator_node,
    route_after_conflict_score_validation,
)
from stock_research_agent.graph.nodes.cross_review import (
    apply_cross_reviews_node,
    build_aggressive_cross_review_node,
    build_conservative_cross_review_node,
    build_cross_review_correction_node,
    route_after_cross_review_correction,
)
from stock_research_agent.graph.nodes.proposal_normalization import (
    proposal_normalization_node,
)
from stock_research_agent.graph.state import ResearchGraphState

RUN_ID = "run_20260825_180000_A_SHARE_cross_review"
AS_OF = datetime.fromisoformat("2026-08-25T18:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
THESIS_ID = "th_cross_review_supported_001"


class ScriptedCrossReviewModel:
    def __init__(self, result=None, *, reverse: bool = False) -> None:
        self.result = result
        self.reverse = reverse
        self.calls = []

    async def review_recommendation(self, request):
        self.calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        proposals = list(request.counterpart_proposals)
        if self.reverse:
            proposals.reverse()
        return PortfolioCrossReviewDraft(
            evaluations=tuple(
                CrossReviewEvaluationDraft(
                    item_id=proposal.item_id,
                    support_score=-1.0 if proposal.conflicts_with else 0.25,
                    hard_veto=False,
                    reason="该原始版本需要与本经理的风险偏好和决策槽约束协调。",
                    modification_suggestion="保留核心方向，同时调整执行条件。",
                )
                for proposal in proposals
            )
        )


class CallbackCrossReviewModel:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls = []

    async def review_recommendation(self, request):
        self.calls.append(request)
        result = self.responder(request)
        if isinstance(result, Exception):
            raise result
        return result


def test_cross_review_node_receives_theses_and_normalizes_evaluation_order() -> None:
    state = _normalized_state()
    model = ScriptedCrossReviewModel(reverse=True)

    result = asyncio.run(build_aggressive_cross_review_node(model)(state))

    assert len(model.calls) == 1
    request = model.calls[0]
    assert request.reviewer is PortfolioManager.AGGRESSIVE
    assert all(
        proposal.proposer is PortfolioManager.AGGRESSIVE for proposal in request.own_proposals
    )
    assert all(
        proposal.proposer is PortfolioManager.CONSERVATIVE
        for proposal in request.counterpart_proposals
    )
    assert [thesis.thesis_id for thesis in request.theses] == [THESIS_ID]
    assert request.eligible_supporting_thesis_ids == (THESIS_ID,)

    record = result["aggressive_cross_review"]
    assert [evaluation.item_id for evaluation in record.evaluations] == [
        proposal.item_id for proposal in request.counterpart_proposals
    ]
    assert record.reviewer is PortfolioManager.AGGRESSIVE
    assert result["aggressive_cross_review_run_summary"].stop_reason == "complete"


def test_cross_review_rejects_incomplete_or_unknown_item_coverage() -> None:
    state = _normalized_state()
    pool = state["normalized_proposal_pool"]
    first_counterpart = next(
        item for item in pool.proposal_items if item.proposer is PortfolioManager.CONSERVATIVE
    )
    model = ScriptedCrossReviewModel(
        PortfolioCrossReviewDraft(
            evaluations=(
                CrossReviewEvaluationDraft(
                    item_id=first_counterpart.item_id,
                    support_score=-1.0,
                    hard_veto=False,
                    reason="故意漏掉其他条目。",
                    modification_suggestion=None,
                ),
            )
        )
    )

    result = asyncio.run(build_aggressive_cross_review_node(model)(state))

    assert "aggressive_cross_review" not in result
    assert result["aggressive_cross_review_run_summary"].stop_reason == ("rejected_output")
    assert result["errors"] == [
        "AggressivePortfolioManager cross-review rejected: item coverage mismatch"
    ]


def test_cross_review_limits_and_model_errors_fail_closed() -> None:
    state = _normalized_state()
    model = ScriptedCrossReviewModel()
    limited = asyncio.run(
        build_aggressive_cross_review_node(
            model,
            limits=PortfolioCrossReviewLimits(max_context_characters=1_000),
        )(state)
    )
    assert model.calls == []
    assert limited["aggressive_cross_review_run_summary"].stop_reason == ("context_limit_exceeded")
    assert "input was not truncated" in limited["errors"][0]

    failed_model = ScriptedCrossReviewModel(RuntimeError("secret provider payload"))
    failed = asyncio.run(build_conservative_cross_review_node(failed_model)(state))
    assert "conservative_cross_review" not in failed
    assert failed["conservative_cross_review_run_summary"].stop_reason == "model_error"
    assert failed["errors"] == [
        "ConservativePortfolioManager cross-review model failed: RuntimeError"
    ]


def test_apply_cross_reviews_adds_one_counterpart_evaluation_without_mutation() -> None:
    state = _normalized_state()
    aggressive_result, conservative_result = asyncio.run(
        _run_both_reviews(
            state,
            ScriptedCrossReviewModel(),
            ScriptedCrossReviewModel(),
        )
    )
    combined = {**state, **aggressive_result, **conservative_result}

    result = apply_cross_reviews_node(combined)

    reviewed = result["cross_reviewed_proposal_pool"]
    assert len(reviewed.proposal_items) == len(state["normalized_proposal_pool"].proposal_items)
    assert all(len(item.evaluations) == 2 for item in reviewed.proposal_items)
    for item in reviewed.proposal_items:
        assert item.evaluations[0].manager is item.proposer
        assert item.evaluations[1].manager is not item.proposer
        assert item.evaluations[1].previous_score is None
        assert item.evaluations[1].score_change_reason is None
    assert all(
        len(item.evaluations) == 1 for item in state["normalized_proposal_pool"].proposal_items
    )
    assert result["cross_review_application_run_summary"].stop_reason == "complete"


def test_parallel_graph_joins_distinct_review_fields_without_update_conflict() -> None:
    state = _normalized_state()
    started: set[PortfolioManager] = set()
    both_started = asyncio.Event()

    class BarrierModel(ScriptedCrossReviewModel):
        async def review_recommendation(self, request):
            started.add(request.reviewer)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return await super().review_recommendation(request)

    async def scenario() -> None:
        builder = StateGraph(ResearchGraphState)
        builder.add_node(
            "aggressive",
            build_aggressive_cross_review_node(BarrierModel()),
        )
        builder.add_node(
            "conservative",
            build_conservative_cross_review_node(BarrierModel()),
        )
        builder.add_node("apply", apply_cross_reviews_node)
        builder.add_edge(START, "aggressive")
        builder.add_edge(START, "conservative")
        builder.add_edge(["aggressive", "conservative"], "apply")
        builder.add_edge("apply", END)

        result = await builder.compile().ainvoke(state)

        assert started == {
            PortfolioManager.AGGRESSIVE,
            PortfolioManager.CONSERVATIVE,
        }
        assert result["aggressive_cross_review"] is not None
        assert result["conservative_cross_review"] is not None
        assert result["cross_reviewed_proposal_pool"] is not None

    asyncio.run(scenario())


def test_application_fails_closed_when_one_review_is_missing() -> None:
    state = _normalized_state()
    aggressive = asyncio.run(build_aggressive_cross_review_node(ScriptedCrossReviewModel())(state))

    result = apply_cross_reviews_node({**state, **aggressive})

    assert "cross_reviewed_proposal_pool" not in result
    assert result["cross_review_application_run_summary"].stop_reason == ("missing_cross_review")


def test_single_manager_correction_only_retries_the_invalid_reviewer() -> None:
    aggressive_model = CallbackCrossReviewModel(
        lambda request: (
            _uniform_review_draft(request, 0.25)
            if request.attempt == 1
            else _correction_draft(request, replacement_score=-0.75)
        )
    )
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))

    assert state["conflict_score_validation_report"].invalid_managers == (
        PortfolioManager.AGGRESSIVE,
    )
    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
        )(state)
    )

    summary = correction["cross_review_correction_run_summary"]
    assert summary.requested_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.completed_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.aggressive_attempt == 2
    assert summary.conservative_attempt == 1
    assert correction["aggressive_cross_review"].attempt == 2
    assert "conservative_cross_review" not in correction
    assert [request.attempt for request in aggressive_model.calls] == [1, 2]
    assert [request.attempt for request in conservative_model.calls] == [1]

    corrected_state = _apply_and_validate_correction(state, correction)
    assert corrected_state["conflict_score_validation_report"].valid is True
    assert corrected_state["conservative_cross_review"] == state["conservative_cross_review"]


def test_both_invalid_managers_are_corrected_and_committed_atomically() -> None:
    def responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(request, replacement_score=-0.75)

    aggressive_model = CallbackCrossReviewModel(responder)
    conservative_model = CallbackCrossReviewModel(responder)
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))

    assert state["conflict_score_validation_report"].invalid_managers == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
        )(state)
    )

    summary = correction["cross_review_correction_run_summary"]
    assert summary.requested_managers == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    assert summary.completed_managers == summary.requested_managers
    assert correction["aggressive_cross_review"].attempt == 2
    assert correction["conservative_cross_review"].attempt == 2
    assert [request.attempt for request in aggressive_model.calls] == [1, 2]
    assert [request.attempt for request in conservative_model.calls] == [1, 2]

    corrected_state = _apply_and_validate_correction(state, correction)
    report = corrected_state["conflict_score_validation_report"]
    assert report.valid is True
    assert report.aggressive_review_attempt == 2
    assert report.conservative_review_attempt == 2


def test_correction_exhausts_after_initial_review_and_two_retries() -> None:
    def always_invalid(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(request, replacement_score=0.25)

    aggressive_model = CallbackCrossReviewModel(always_invalid)
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))
    correction_node = build_cross_review_correction_node(
        aggressive_model,
        conservative_model,
    )

    for expected_attempt in (2, 3):
        correction = asyncio.run(correction_node(state))
        state = _apply_and_validate_correction(state, correction)
        assert state["aggressive_cross_review"].attempt == expected_attempt

    report = state["conflict_score_validation_report"]
    assert report.valid is False
    assert report.stop_reason == "retry_exhausted"
    assert report.aggressive_review_attempt == 3
    assert report.conservative_review_attempt == 1
    assert [request.attempt for request in aggressive_model.calls] == [1, 2, 3]
    assert [request.attempt for request in conservative_model.calls] == [1]
    assert state["errors"] == [
        "ConflictScoreValidatorNode exhausted cross-review correction attempts: "
        "AggressivePortfolioManager"
    ]


def test_correction_cannot_change_an_item_not_named_by_validation_feedback() -> None:
    def aggressive_responder(request):
        if request.attempt == 1:
            return _review_draft_by_dimension(
                request,
                {
                    DecisionDimension.ACTION: 0.25,
                    DecisionDimension.HORIZON: -0.75,
                    DecisionDimension.RISK_CONTROL: -0.75,
                },
            )
        return _correction_draft(
            request,
            replacement_score=-0.75,
            mutate_unrequested=True,
        )

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))

    report = state["conflict_score_validation_report"]
    assert len(report.violations) == 1
    assert report.violations[0].counterpart_item_id == "item_conservative_action"
    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
        )(state)
    )

    assert "aggressive_cross_review" not in correction
    assert "conservative_cross_review" not in correction
    assert correction["cross_review_correction_run_summary"].stop_reason == ("attempt_failed")
    assert correction["errors"] == [
        "AggressivePortfolioManager cross-review rejected: correction changed an unrequested item"
    ]
    assert state["aggressive_cross_review"].attempt == 1


def test_apply_refuses_forged_correction_that_changes_an_unrequested_item() -> None:
    """即使外部绕过模型节点伪造了合法 Schema，Apply 也不得接受越权修改。"""

    def aggressive_responder(request):
        if request.attempt == 1:
            return _review_draft_by_dimension(
                request,
                {
                    DecisionDimension.ACTION: 0.25,
                    DecisionDimension.HORIZON: -0.75,
                    DecisionDimension.RISK_CONTROL: -0.75,
                },
            )
        return _correction_draft(request, replacement_score=-0.75)

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))
    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
        )(state)
    )

    record = correction["aggressive_cross_review"]
    editable_ids = {
        violation.counterpart_item_id
        for violation in state["conflict_score_validation_report"].violations
    }
    forged_evaluations = list(record.evaluations)
    untouched_index = next(
        index
        for index, evaluation in enumerate(forged_evaluations)
        if evaluation.item_id not in editable_ids
    )
    forged_evaluations[untouched_index] = forged_evaluations[untouched_index].model_copy(
        update={"reason": "伪造记录擅自改动了未被校验器点名的评分。"}
    )
    correction["aggressive_cross_review"] = type(record).model_validate(
        {
            **record.model_dump(),
            "evaluations": [evaluation.model_dump() for evaluation in forged_evaluations],
        }
    )

    result = apply_cross_reviews_node({**state, **correction})

    assert "cross_reviewed_proposal_pool" not in result
    assert result["cross_review_application_run_summary"].stop_reason == "invalid_state"
    assert result["errors"] == ["ApplyCrossReviewsNode refused to overwrite an incompatible pool"]


def test_apply_refuses_normalized_source_pool_changed_after_validation() -> None:
    def aggressive_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(request, replacement_score=-0.75)

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))
    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
        )(state)
    )
    normalized = state["normalized_proposal_pool"]
    forged_items = list(normalized.proposal_items)
    forged_items[0] = forged_items[0].model_copy(
        deep=True,
        update={"proposal": "纠错与应用之间被替换的伪造提案正文。"},
    )
    forged_normalized = type(normalized).model_validate(
        {
            **normalized.model_dump(),
            "proposal_items": [item.model_dump() for item in forged_items],
        }
    )

    result = apply_cross_reviews_node(
        {
            **state,
            **correction,
            "normalized_proposal_pool": forged_normalized,
        }
    )

    assert "cross_reviewed_proposal_pool" not in result
    assert result["cross_review_application_run_summary"].stop_reason == "invalid_state"
    assert result["errors"] == ["ApplyCrossReviewsNode refused to overwrite an incompatible pool"]


def test_two_manager_correction_failure_is_atomic_and_checkpoint_idempotent() -> None:
    def aggressive_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(request, replacement_score=-0.75)

    def conservative_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return RuntimeError("provider payload must stay private")

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(conservative_responder)
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))
    correction_node = build_cross_review_correction_node(
        aggressive_model,
        conservative_model,
    )

    first = asyncio.run(correction_node(state))

    assert "aggressive_cross_review" not in first
    assert "conservative_cross_review" not in first
    summary = first["cross_review_correction_run_summary"]
    assert summary.stop_reason == "attempt_failed"
    assert set(summary.called_managers) == {
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    }
    assert summary.staged_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.completed_managers == ()
    assert summary.aggressive_attempt == 2
    assert summary.conservative_attempt == 2
    assert "RuntimeError" in first["errors"][0]
    assert "provider payload" not in first["errors"][0]
    assert state["aggressive_cross_review"].attempt == 1
    assert state["conservative_cross_review"].attempt == 1
    assert asyncio.run(correction_node({**state, **first})) == {}
    assert [request.attempt for request in aggressive_model.calls] == [1, 2]
    assert [request.attempt for request in conservative_model.calls] == [1, 2]


def test_correction_audit_does_not_claim_a_model_call_rejected_before_invocation() -> None:
    aggressive_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, 0.25)
    )
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))

    correction = asyncio.run(
        build_cross_review_correction_node(
            aggressive_model,
            conservative_model,
            limits=PortfolioCrossReviewLimits(max_context_characters=1_000),
        )(state)
    )

    summary = correction["cross_review_correction_run_summary"]
    assert summary.stop_reason == "attempt_failed"
    assert summary.requested_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.called_managers == ()
    assert summary.staged_managers == ()
    assert summary.completed_managers == ()
    assert summary.aggressive_attempt == 2
    assert [request.attempt for request in aggressive_model.calls] == [1]
    assert (
        asyncio.run(
            build_cross_review_correction_node(
                aggressive_model,
                conservative_model,
                limits=PortfolioCrossReviewLimits(max_context_characters=1_000),
            )({**state, **correction})
        )
        == {}
    )


def test_langgraph_correction_loop_reaches_third_attempt_and_then_stops() -> None:
    def aggressive_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(
            request,
            replacement_score=(-0.75 if request.attempt == 3 else 0.25),
        )

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(
        lambda request: _uniform_review_draft(request, -0.75)
    )
    builder = StateGraph(ResearchGraphState)
    builder.add_node(
        "aggressive",
        build_aggressive_cross_review_node(aggressive_model),
    )
    builder.add_node(
        "conservative",
        build_conservative_cross_review_node(conservative_model),
    )
    builder.add_node("apply", apply_cross_reviews_node)
    builder.add_node("validate", build_conflict_score_validator_node())
    builder.add_node(
        "correct",
        build_cross_review_correction_node(aggressive_model, conservative_model),
    )
    builder.add_edge(START, "aggressive")
    builder.add_edge(START, "conservative")
    builder.add_edge(["aggressive", "conservative"], "apply")
    builder.add_edge("apply", "validate")
    builder.add_conditional_edges(
        "validate",
        route_after_conflict_score_validation,
        {"valid": END, "retry": "correct", "failed": END},
    )
    builder.add_conditional_edges(
        "correct",
        route_after_cross_review_correction,
        {"apply": "apply", "failed": END},
    )

    result = asyncio.run(builder.compile().ainvoke(_normalized_state()))

    assert result["conflict_score_validation_report"].valid is True
    assert result["aggressive_cross_review"].attempt == 3
    assert result["conservative_cross_review"].attempt == 1
    assert [request.attempt for request in aggressive_model.calls] == [1, 2, 3]
    assert [request.attempt for request in conservative_model.calls] == [1]
    assert result.get("debate_round", 0) == 0


def test_second_correction_is_idempotent_when_other_manager_was_fixed_earlier() -> None:
    def aggressive_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(request, replacement_score=-0.75)

    def conservative_responder(request):
        if request.attempt == 1:
            return _uniform_review_draft(request, 0.25)
        return _correction_draft(
            request,
            replacement_score=(-0.75 if request.attempt == 3 else 0.25),
        )

    aggressive_model = CallbackCrossReviewModel(aggressive_responder)
    conservative_model = CallbackCrossReviewModel(conservative_responder)
    correction_node = build_cross_review_correction_node(
        aggressive_model,
        conservative_model,
    )
    state = asyncio.run(_initially_validated_state(aggressive_model, conservative_model))
    first_correction = asyncio.run(correction_node(state))
    state = _apply_and_validate_correction(state, first_correction)

    assert state["conflict_score_validation_report"].invalid_managers == (
        PortfolioManager.CONSERVATIVE,
    )
    second_correction = asyncio.run(correction_node(state))
    replay_state = {**state, **second_correction}

    assert asyncio.run(correction_node(replay_state)) == {}
    assert [request.attempt for request in aggressive_model.calls] == [1, 2]
    assert [request.attempt for request in conservative_model.calls] == [1, 2, 3]


def test_builder_requires_cross_review_models_as_a_pair_and_after_managers() -> None:
    cross_model = ScriptedCrossReviewModel()

    with pytest.raises(ValueError, match="交叉评分模型必须成对配置"):
        build_research_graph(aggressive_cross_review_model=cross_model)
    with pytest.raises(ValueError, match="必须先配置两位投资组合经理"):
        build_research_graph(
            aggressive_cross_review_model=cross_model,
            conservative_cross_review_model=cross_model,
        )


def test_openai_adapters_bind_common_schema_to_distinct_review_prompts() -> None:
    class RecordingChatModel:
        def __init__(self) -> None:
            self.calls = []

        def with_structured_output(self, schema, *, method, include_raw, strict):
            self.calls.append((schema, method, include_raw, strict))
            return object()

    aggressive_chat = RecordingChatModel()
    conservative_chat = RecordingChatModel()
    OpenAIAggressivePortfolioCrossReviewModel(  # type: ignore[arg-type]
        aggressive_chat,
        structured_output_method="json_schema",
    )
    OpenAIConservativePortfolioCrossReviewModel(  # type: ignore[arg-type]
        conservative_chat,
        structured_output_method="json_schema",
    )

    assert aggressive_chat.calls[0][0]["title"] == "PortfolioCrossReviewDraft"
    assert conservative_chat.calls[0][0]["title"] == "PortfolioCrossReviewDraft"
    assert aggressive_chat.calls[0][1:] == ("json_schema", True, True)
    assert conservative_chat.calls[0][1:] == ("json_schema", True, True)
    assert "更早承担风险" not in CONSERVATIVE_CROSS_REVIEW_SYSTEM_PROMPT
    for prompt in (
        AGGRESSIVE_CROSS_REVIEW_SYSTEM_PROMPT,
        CONSERVATIVE_CROSS_REVIEW_SYSTEM_PROMPT,
    ):
        assert "只输出符合 PortfolioCrossReviewDraft Schema 的 JSON" in prompt
        assert "评分之和必须小于或等于 0" in prompt
        assert "support_score=-1.00 不自动等于 hard veto" in prompt
        assert "Few-shot" in prompt


def _uniform_review_draft(request, score: float) -> PortfolioCrossReviewDraft:
    return PortfolioCrossReviewDraft(
        evaluations=tuple(
            CrossReviewEvaluationDraft(
                item_id=proposal.item_id,
                support_score=score,
                hard_veto=False,
                reason=f"第 {request.attempt} 次评分使用统一测试分值。",
                modification_suggestion="按确定性评分规则调整当前原始版本。",
            )
            for proposal in request.counterpart_proposals
        )
    )


def _review_draft_by_dimension(
    request,
    scores: dict[DecisionDimension, float],
) -> PortfolioCrossReviewDraft:
    return PortfolioCrossReviewDraft(
        evaluations=tuple(
            CrossReviewEvaluationDraft(
                item_id=proposal.item_id,
                support_score=scores[proposal.decision_dimension],
                hard_veto=False,
                reason=f"按 {proposal.decision_dimension.value} 决策维度给出测试评分。",
                modification_suggestion="按当前决策维度调整执行条件。",
            )
            for proposal in request.counterpart_proposals
        )
    )


def _correction_draft(
    request,
    *,
    replacement_score: float,
    mutate_unrequested: bool = False,
) -> PortfolioCrossReviewDraft:
    editable_ids = {violation.counterpart_item_id for violation in request.validation_feedback}
    previous_by_id = {evaluation.item_id: evaluation for evaluation in request.previous_evaluations}
    changed_unrequested = False
    evaluations = []
    for proposal in request.counterpart_proposals:
        previous = previous_by_id[proposal.item_id]
        if proposal.item_id in editable_ids:
            evaluations.append(
                previous.model_copy(
                    update={
                        "support_score": replacement_score,
                        "hard_veto": False,
                        "reason": "根据确定性违规反馈纠正该条评分。",
                    }
                )
            )
        elif mutate_unrequested and not changed_unrequested:
            changed_unrequested = True
            evaluations.append(
                previous.model_copy(update={"reason": "违规纠错时擅自改变未被点名的条目。"})
            )
        else:
            evaluations.append(previous)
    return PortfolioCrossReviewDraft(evaluations=tuple(evaluations))


async def _initially_validated_state(aggressive_model, conservative_model):
    state = _normalized_state()
    aggressive, conservative = await _run_both_reviews(
        state,
        aggressive_model,
        conservative_model,
    )
    state = {**state, **aggressive, **conservative}
    state = {**state, **apply_cross_reviews_node(state)}
    return {**state, **build_conflict_score_validator_node()(state)}


def _apply_and_validate_correction(state, correction):
    corrected = {**state, **correction}
    corrected = {**corrected, **apply_cross_reviews_node(corrected)}
    return {**corrected, **build_conflict_score_validator_node()(corrected)}


async def _run_both_reviews(state, aggressive_model, conservative_model):
    return await asyncio.gather(
        build_aggressive_cross_review_node(aggressive_model)(state),
        build_conservative_cross_review_node(conservative_model)(state),
    )


def _normalized_state():
    thesis = _thesis()
    aggressive = _record(PortfolioManager.AGGRESSIVE)
    conservative = _record(PortfolioManager.CONSERVATIVE)
    base = {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "thesis_pool": [thesis],
        "thesis_validation_run_summary": ThesisValidationRunSummary(
            input_thesis_count=1,
            completed_thesis_count=1,
            status_counts={
                ThesisValidationStatus.SUPPORTED.value: 1,
                ThesisValidationStatus.REFUTED.value: 0,
                ThesisValidationStatus.MIXED.value: 0,
                ThesisValidationStatus.INCONCLUSIVE.value: 0,
            },
            model_call_count=1,
            research_request_count=0,
            finding_count=0,
            discovered_candidate_count=0,
            stop_reason="complete",
        ),
        "aggressive_recommendation": aggressive,
        "conservative_recommendation": conservative,
        "errors": [],
    }
    return {**base, **proposal_normalization_node(base)}


def _record(manager: PortfolioManager) -> RecommendationRecord:
    profile = {
        PortfolioManager.AGGRESSIVE: RecommendationProfile.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE: RecommendationProfile.CONSERVATIVE,
    }[manager]
    prefix = "aggressive" if manager is PortfolioManager.AGGRESSIVE else "conservative"
    items = [
        _item(f"item_{prefix}_action", manager, DecisionDimension.ACTION),
        _item(f"item_{prefix}_horizon", manager, DecisionDimension.HORIZON),
        _item(f"item_{prefix}_risk", manager, DecisionDimension.RISK_CONTROL),
    ]
    return RecommendationRecord(
        recommendation_id=f"rec_{prefix}_cross_review",
        run_id=RUN_ID,
        as_of=AS_OF,
        profile=profile,
        target=MARKET,
        action=RecommendationAction.HOLD,
        horizon="未来一个季度",
        confidence=0.7,
        supporting_thesis_ids=[THESIS_ID],
        summary="基于终态观点形成的独立投资建议。",
        risk_summary="核心观点失效时降低风险暴露。",
        proposal_items=items,
        generated_by=manager.value,
        created_at=AS_OF,
    )


def _item(
    item_id: str,
    manager: PortfolioManager,
    dimension: DecisionDimension,
) -> ProposalItem:
    return ProposalItem(
        item_id=item_id,
        target=MARKET,
        decision_dimension=dimension,
        conflict_group=f"MARKET:A_SHARE:{dimension.value}",
        proposer=manager,
        proposal=f"针对 {dimension.value} 的原始建议。",
        supporting_thesis_ids=[THESIS_ID],
        evaluations=[
            ProposalEvaluation(
                manager=manager,
                support_score=0.5,
                reason="终态观点支持该经理的独立判断。",
            )
        ],
        status=ProposalStatus.PROPOSED,
    )


def _thesis() -> ThesisRecord:
    return ThesisRecord(
        thesis_id=THESIS_ID,
        run_id=RUN_ID,
        target=MARKET,
        as_of=AS_OF,
        title="市场风险收益比改善",
        description="市场趋势和基本面条件共同改善。",
        direction=ThesisDirection.BULLISH,
        horizon="未来一个季度",
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent="LeadResearchStrategist",
        ),
        validation=ThesisValidation(
            status=ThesisValidationStatus.SUPPORTED,
            confidence=0.75,
            round=1,
        ),
        supporting_evidence_ids=["ev_cross_review_001"],
        reasoning_summary="多类证据共同支持该观点，同时保留失效条件。",
        missing_questions=["改善能否持续？"],
        invalidation_conditions=["市场广度和盈利预期同时转弱"],
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )
