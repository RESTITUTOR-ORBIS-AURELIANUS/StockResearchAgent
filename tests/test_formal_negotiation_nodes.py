"""正式协商三阶段节点的原子性、闭包重评与轮次完成测试。"""

import asyncio
from collections.abc import Callable
from datetime import datetime

import pytest
from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.negotiation import (
    DebateScoreDraft,
    DebateScoreEntryDraft,
    NegotiationLimits,
    NegotiationProposalPool,
    ProposalRevisionDecisionDraft,
    ProposalRevisionDraft,
    ReasonExchangeDraft,
    ReasonExchangeItemDraft,
)
from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    ConsensusRoute,
    DecisionDimension,
    NegotiationArgumentType,
    NegotiationStance,
    PortfolioManager,
    ProposalRevisionAction,
    ProposalStatus,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import ProposalEvaluation, ProposalItem
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph.builder import build_research_graph
from stock_research_agent.graph.nodes.consensus_gate import (
    build_consensus_gate_node,
    route_after_consensus_gate,
)
from stock_research_agent.graph.nodes.formal_negotiation import (
    build_begin_negotiation_round_node,
    build_debate_score_stage_node,
    build_proposal_revision_stage_node,
    build_reason_exchange_stage_node,
    complete_negotiation_round_without_rescore_node,
    route_after_debate_score,
    route_after_proposal_revision,
    route_after_reason_exchange,
    route_after_round_completion,
)
from stock_research_agent.graph.state import ResearchGraphState

RUN_ID = "run_20260826_formal_negotiation"
AS_OF = datetime.fromisoformat("2026-08-26T18:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
THESIS_ID = "th_formal_negotiation_001"
AGGRESSIVE_ITEM_ID = "item_aggressive_action"
CONSERVATIVE_ITEM_ID = "item_conservative_action"
CONFLICT_GROUP = "MARKET:A_SHARE:ACTION"

_APPEND_FIELDS = {
    "consensus_gate_reports",
    "reason_exchange_records",
    "proposal_revision_records",
    "debate_score_records",
    "negotiation_model_run_summaries",
    "negotiation_stage_run_summaries",
    "negotiation_round_summaries",
    "errors",
}


class ScriptedNegotiationModel:
    """为三个协议方法分别注入同步脚本，并保留收到的强类型请求。"""

    def __init__(
        self,
        *,
        exchange: Callable | Exception | None = None,
        revision: Callable | Exception | None = None,
        score: Callable | Exception | None = None,
    ) -> None:
        self.exchange = exchange or _reason_draft
        self.revision = revision or _revision_draft()
        self.score = score or _score_draft
        self.exchange_calls = []
        self.revision_calls = []
        self.score_calls = []

    async def exchange_reasons(self, request):
        self.exchange_calls.append(request)
        return _resolve(self.exchange, request)

    async def revise_proposals(self, request):
        self.revision_calls.append(request)
        return _resolve(self.revision, request)

    async def score_revisions(self, request):
        self.score_calls.append(request)
        return _resolve(self.score, request)


def test_begin_round_advances_exactly_once_from_current_negotiable_gate() -> None:
    state = _gated_state()

    update = build_begin_negotiation_round_node()(state)

    assert state["consensus_gate_report"].route is ConsensusRoute.NEGOTIATE
    assert update == {
        "debate_round": 1,
        "proposal_revision_application_summary": None,
        "negotiation_score_validation_report": None,
    }


def test_reason_exchange_commits_both_records_only_after_both_models_succeed() -> None:
    state = _round_state()
    aggressive = ScriptedNegotiationModel()
    conservative = ScriptedNegotiationModel()

    result = asyncio.run(
        build_reason_exchange_stage_node(aggressive, conservative)(state)
    )

    assert [record.reviewer for record in result["reason_exchange_records"]] == [
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    ]
    assert result["negotiation_stage_run_summaries"][0].stop_reason == "complete"
    assert route_after_reason_exchange(_merge(state, result)) == "revise"
    assert len(aggressive.exchange_calls) == len(conservative.exchange_calls) == 1


def test_reason_exchange_discards_successful_peer_record_when_other_model_fails() -> None:
    state = _round_state()
    aggressive = ScriptedNegotiationModel()
    conservative = ScriptedNegotiationModel(
        exchange=RuntimeError("provider response must stay private")
    )

    result = asyncio.run(
        build_reason_exchange_stage_node(aggressive, conservative)(state)
    )

    assert "reason_exchange_records" not in result
    summary = result["negotiation_stage_run_summaries"][0]
    assert summary.staged_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.completed_managers == ()
    assert summary.stop_reason == "stage_failed"
    assert result["errors"] == [
        "ConservativePortfolioManager reason exchange failed: RuntimeError"
    ]
    assert "private" not in result["errors"][0]
    assert route_after_reason_exchange(_merge(state, result)) == "failed"


def test_keep_decisions_complete_revision_without_rescore() -> None:
    state = _state_after_reason_exchange()
    aggressive = ScriptedNegotiationModel(revision=_revision_draft())
    conservative = ScriptedNegotiationModel(revision=_revision_draft())

    result = asyncio.run(
        build_proposal_revision_stage_node(aggressive, conservative)(state)
    )

    application = result["proposal_revision_application_summary"]
    assert application.stop_reason == "no_material_change"
    assert application.material_change_item_ids == ()
    assert application.rescore_item_ids == ()
    assert result["negotiation_proposal_pool"] == state["negotiation_proposal_pool"]
    assert route_after_proposal_revision(_merge(state, result)) == "complete_without_score"
    assert aggressive.score_calls == conservative.score_calls == []


def test_modify_rescores_entire_live_conflict_group_closure() -> None:
    state = _state_after_reason_exchange()
    aggressive = ScriptedNegotiationModel(
        revision=_revision_draft({AGGRESSIVE_ITEM_ID: ProposalRevisionAction.MODIFY})
    )
    conservative = ScriptedNegotiationModel(revision=_revision_draft())

    result = asyncio.run(
        build_proposal_revision_stage_node(aggressive, conservative)(state)
    )

    application = result["proposal_revision_application_summary"]
    assert application.material_change_item_ids == (AGGRESSIVE_ITEM_ID,)
    assert application.touched_conflict_groups == (CONFLICT_GROUP,)
    assert application.rescore_item_ids == (
        AGGRESSIVE_ITEM_ID,
        CONSERVATIVE_ITEM_ID,
    )
    assert route_after_proposal_revision(_merge(state, result)) == "score"
    items = _items_by_id(result["negotiation_proposal_pool"])
    assert items[AGGRESSIVE_ITEM_ID].revision == 2
    assert items[CONSERVATIVE_ITEM_ID].revision == 1


def test_withdraw_excludes_withdrawn_item_and_rescores_only_live_sibling() -> None:
    state = _state_after_reason_exchange()
    aggressive = ScriptedNegotiationModel(
        revision=_revision_draft({AGGRESSIVE_ITEM_ID: ProposalRevisionAction.WITHDRAW})
    )
    conservative = ScriptedNegotiationModel(revision=_revision_draft())

    result = asyncio.run(
        build_proposal_revision_stage_node(aggressive, conservative)(state)
    )

    application = result["proposal_revision_application_summary"]
    assert application.withdrawn_item_ids == (AGGRESSIVE_ITEM_ID,)
    assert application.rescore_item_ids == (CONSERVATIVE_ITEM_ID,)
    items = _items_by_id(result["negotiation_proposal_pool"])
    assert items[AGGRESSIVE_ITEM_ID].status is ProposalStatus.WITHDRAWN
    assert items[CONSERVATIVE_ITEM_ID].status is ProposalStatus.NEGOTIATING


def test_both_rescores_commit_and_copy_each_current_score_to_previous_score() -> None:
    state = _state_after_material_revision()
    before = _items_by_id(state["negotiation_proposal_pool"])
    aggressive = ScriptedNegotiationModel(score=_score_draft)
    conservative = ScriptedNegotiationModel(score=_score_draft)

    result = asyncio.run(build_debate_score_stage_node(aggressive, conservative)(state))

    assert [record.manager for record in result["debate_score_records"]] == [
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    ]
    after = _items_by_id(result["negotiation_proposal_pool"])
    for item_id in (AGGRESSIVE_ITEM_ID, CONSERVATIVE_ITEM_ID):
        old_scores = {
            evaluation.manager: evaluation.support_score
            for evaluation in before[item_id].evaluations
        }
        for evaluation in after[item_id].evaluations:
            assert evaluation.previous_score == old_scores[evaluation.manager]
            expected = 0.75 if evaluation.manager is after[item_id].proposer else -0.75
            assert evaluation.support_score == expected
    assert route_after_debate_score(_merge(state, result)) == "validate"


def test_score_stage_rolls_back_entire_batch_when_one_manager_fails() -> None:
    state = _state_after_material_revision()
    original_pool = state["negotiation_proposal_pool"].model_copy(deep=True)
    aggressive = ScriptedNegotiationModel(score=_score_draft)
    conservative = ScriptedNegotiationModel(
        score=RuntimeError("provider response must stay private")
    )

    result = asyncio.run(build_debate_score_stage_node(aggressive, conservative)(state))

    assert "debate_score_records" not in result
    assert "negotiation_proposal_pool" not in result
    assert state["negotiation_proposal_pool"] == original_pool
    summary = result["negotiation_stage_run_summaries"][0]
    assert summary.staged_managers == (PortfolioManager.AGGRESSIVE,)
    assert summary.completed_managers == ()
    assert result["errors"] == [
        "ConservativePortfolioManager debate score failed: RuntimeError"
    ]
    assert "private" not in result["errors"][0]
    assert route_after_debate_score(_merge(state, result)) == "failed"


def test_round_completion_records_no_change_round_and_is_idempotent() -> None:
    state = _state_after_reason_exchange()
    revision = asyncio.run(
        build_proposal_revision_stage_node(
            ScriptedNegotiationModel(revision=_revision_draft()),
            ScriptedNegotiationModel(revision=_revision_draft()),
        )(state)
    )
    state = _merge(state, revision)

    result = complete_negotiation_round_without_rescore_node(state)

    summary = result["negotiation_round_summaries"][0]
    assert summary.debate_round == 1
    assert summary.exchanged_managers == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    assert summary.revised_managers == (
        PortfolioManager.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE,
    )
    assert summary.scored_managers == ()
    assert summary.material_change_count == 0
    assert summary.stop_reason == "no_material_change"
    completed = _merge(state, result)
    assert route_after_round_completion(completed) == "gate"
    assert complete_negotiation_round_without_rescore_node(completed) == {}


def test_builder_requires_formal_models_as_a_pair_after_cross_review() -> None:
    model = ScriptedNegotiationModel()

    with pytest.raises(ValueError, match="正式协商模型必须成对配置"):
        build_research_graph(aggressive_negotiation_model=model)
    with pytest.raises(ValueError, match="必须先配置双方交叉评分模型"):
        build_research_graph(
            aggressive_negotiation_model=model,
            conservative_negotiation_model=model,
        )

    with pytest.raises(ValueError, match="必须同时配置共识建议组装模型"):
        build_research_graph(
            lead_research_strategist_model=model,  # type: ignore[arg-type]
            thesis_validation_model=model,  # type: ignore[arg-type]
            aggressive_portfolio_manager_model=model,  # type: ignore[arg-type]
            conservative_portfolio_manager_model=model,  # type: ignore[arg-type]
            aggressive_cross_review_model=model,  # type: ignore[arg-type]
            conservative_cross_review_model=model,  # type: ignore[arg-type]
            aggressive_negotiation_model=model,
            conservative_negotiation_model=model,
        )
    with pytest.raises(ValueError, match="必须先配置双方正式协商模型"):
        build_research_graph(
            consensus_assembly_model=model,  # type: ignore[arg-type]
        )


def test_builder_contains_the_complete_bounded_formal_negotiation_loop() -> None:
    dependency = ScriptedNegotiationModel()
    graph = build_research_graph(
        lead_research_strategist_model=dependency,  # type: ignore[arg-type]
        thesis_validation_model=dependency,  # type: ignore[arg-type]
        aggressive_portfolio_manager_model=dependency,  # type: ignore[arg-type]
        conservative_portfolio_manager_model=dependency,  # type: ignore[arg-type]
        aggressive_cross_review_model=dependency,  # type: ignore[arg-type]
        conservative_cross_review_model=dependency,  # type: ignore[arg-type]
        aggressive_negotiation_model=dependency,
        conservative_negotiation_model=dependency,
        consensus_assembly_model=dependency,  # type: ignore[arg-type]
    )

    node_ids = set(graph.get_graph().nodes)
    assert {
        "consensus_gate",
        "begin_negotiation_round",
        "exchange_negotiation_reasons",
        "revise_negotiation_proposals",
        "score_revised_proposals",
        "validate_negotiation_scores",
        "complete_unscored_negotiation_round",
        "complete_scored_negotiation_round",
        "assemble_consensus_recommendation",
    } <= node_ids
    assert "chair_arbitration" not in node_ids
    gate_edges = {
        (edge.data, edge.target)
        for edge in graph.get_graph().edges
        if edge.source == "consensus_gate"
    }
    assert ("assemble", "assemble_consensus_recommendation") in gate_edges
    assert all(label != "arbitrate" for label, _ in gate_edges)


def test_no_change_negotiation_consumes_three_rounds_then_excludes_and_assembles() -> None:
    aggressive = ScriptedNegotiationModel(revision=_revision_draft())
    conservative = ScriptedNegotiationModel(revision=_revision_draft())
    limits = NegotiationLimits(max_rounds=3)
    builder = StateGraph(ResearchGraphState)
    builder.add_node("gate", build_consensus_gate_node(max_rounds=3))
    builder.add_node("begin", build_begin_negotiation_round_node(limits=limits))
    builder.add_node(
        "exchange",
        build_reason_exchange_stage_node(
            aggressive,
            conservative,
            limits=limits,
        ),
    )
    builder.add_node(
        "revise",
        build_proposal_revision_stage_node(
            aggressive,
            conservative,
            limits=limits,
        ),
    )
    builder.add_node("complete", complete_negotiation_round_without_rescore_node)
    builder.add_edge(START, "gate")
    builder.add_conditional_edges(
        "gate",
        route_after_consensus_gate,
        {
            "assemble": END,
            "negotiate": "begin",
            "failed": END,
        },
    )
    builder.add_edge("begin", "exchange")
    builder.add_conditional_edges(
        "exchange",
        route_after_reason_exchange,
        {"revise": "revise", "failed": END},
    )
    builder.add_conditional_edges(
        "revise",
        route_after_proposal_revision,
        {
            "score": END,
            "complete_without_score": "complete",
            "failed": END,
        },
    )
    builder.add_conditional_edges(
        "complete",
        route_after_round_completion,
        {"gate": "gate", "failed": END},
    )

    result = asyncio.run(
        builder.compile().ainvoke(_base_state(), {"recursion_limit": 30})
    )

    assert result["debate_round"] == 3
    assert result["consensus_gate_report"].route is ConsensusRoute.ASSEMBLE
    assert result["consensus_gate_report"].excluded_item_ids
    assert len(result["negotiation_round_summaries"]) == 3
    assert all(
        summary.stop_reason == "no_material_change"
        for summary in result["negotiation_round_summaries"]
    )
    assert len(aggressive.exchange_calls) == len(conservative.exchange_calls) == 3
    assert len(aggressive.revision_calls) == len(conservative.revision_calls) == 3


def _resolve(script, request):
    if isinstance(script, Exception):
        raise script
    return script(request)


def _reason_draft(request) -> ReasonExchangeDraft:
    responses = []
    for counterpart in request.counterpart_proposals:
        related = tuple(
            item.item_id
            for item in request.own_proposals
            if item.conflict_group == counterpart.conflict_group
        )
        responses.append(
            ReasonExchangeItemDraft(
                counterpart_item_id=counterpart.item_id,
                counterpart_revision=counterpart.revision,
                related_own_item_ids=related,
                stance=NegotiationStance.OPPOSE,
                arguments=(
                    {
                        "argument_type": NegotiationArgumentType.OBJECTION,
                        "content": "对方方案的收益风险取舍仍不足以获得本经理支持。",
                    },
                ),
                modification_suggestion="请收紧执行条件并重新说明风险边界。",
            )
        )
    return ReasonExchangeDraft(responses=tuple(responses))


def _revision_draft(
    actions: dict[str, ProposalRevisionAction] | None = None,
) -> Callable:
    selected = actions or {}

    def build(request) -> ProposalRevisionDraft:
        decisions = []
        responses = {
            response.counterpart_item_id: response
            for response in request.incoming_exchange.responses
        }
        for item in request.own_proposals:
            action = selected.get(item.item_id, ProposalRevisionAction.KEEP)
            argument_ids = tuple(
                argument.argument_id for argument in responses[item.item_id].arguments
            )
            payload = {
                "item_id": item.item_id,
                "decision": action,
                "responding_to_argument_ids": argument_ids,
                "revision_reason": f"针对对方理由决定 {action.value}。",
            }
            if action is ProposalRevisionAction.MODIFY:
                payload.update(
                    revised_proposal=f"{item.proposal} 修订后增加明确执行条件。",
                    revised_supporting_thesis_ids=tuple(item.supporting_thesis_ids),
                )
            decisions.append(ProposalRevisionDecisionDraft.model_validate(payload))
        return ProposalRevisionDraft(decisions=tuple(decisions))

    return build


def _score_draft(request) -> DebateScoreDraft:
    return DebateScoreDraft(
        evaluations=tuple(
            DebateScoreEntryDraft(
                item_id=item.item_id,
                item_revision=item.revision,
                support_score=0.75 if item.proposer is request.manager else -0.75,
                reason="结合修订后的完整冲突组重新评价。",
                score_change_reason="提案已发生实质变化，因此刷新当前评分。",
            )
            for item in request.items_to_score
        )
    )


def _state_after_material_revision() -> dict:
    state = _state_after_reason_exchange()
    revision = asyncio.run(
        build_proposal_revision_stage_node(
            ScriptedNegotiationModel(
                revision=_revision_draft(
                    {AGGRESSIVE_ITEM_ID: ProposalRevisionAction.MODIFY}
                )
            ),
            ScriptedNegotiationModel(revision=_revision_draft()),
        )(state)
    )
    assert route_after_proposal_revision(_merge(state, revision)) == "score"
    return _merge(state, revision)


def _state_after_reason_exchange() -> dict:
    state = _round_state()
    result = asyncio.run(
        build_reason_exchange_stage_node(
            ScriptedNegotiationModel(),
            ScriptedNegotiationModel(),
        )(state)
    )
    assert route_after_reason_exchange(_merge(state, result)) == "revise"
    return _merge(state, result)


def _round_state() -> dict:
    state = _gated_state()
    return _merge(state, build_begin_negotiation_round_node()(state))


def _gated_state() -> dict:
    state = _base_state()
    result = build_consensus_gate_node()(state)
    assert result["consensus_gate_report"].route is ConsensusRoute.NEGOTIATE
    return _merge(state, result)


def _base_state() -> dict:
    return {
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "target": MARKET,
        "debate_round": 0,
        "negotiation_proposal_pool": _pool(),
        "thesis_pool": [_thesis()],
        "consensus_gate_reports": [],
        "reason_exchange_records": [],
        "proposal_revision_records": [],
        "debate_score_records": [],
        "negotiation_model_run_summaries": [],
        "negotiation_stage_run_summaries": [],
        "negotiation_round_summaries": [],
        "errors": [],
    }


def _pool():
    return NegotiationProposalPool(
        run_id=RUN_ID,
        as_of=AS_OF,
        research_target=MARKET,
        aggressive_recommendation_id="rec_aggressive_formal",
        conservative_recommendation_id="rec_conservative_formal",
        proposal_items=(
            _item(
                item_id=AGGRESSIVE_ITEM_ID,
                proposer=PortfolioManager.AGGRESSIVE,
                conflict_id=CONSERVATIVE_ITEM_ID,
            ),
            _item(
                item_id=CONSERVATIVE_ITEM_ID,
                proposer=PortfolioManager.CONSERVATIVE,
                conflict_id=AGGRESSIVE_ITEM_ID,
            ),
        ),
    )


def _item(*, item_id: str, proposer: PortfolioManager, conflict_id: str) -> ProposalItem:
    counterpart = (
        PortfolioManager.CONSERVATIVE
        if proposer is PortfolioManager.AGGRESSIVE
        else PortfolioManager.AGGRESSIVE
    )
    return ProposalItem(
        item_id=item_id,
        target=MARKET,
        decision_dimension=DecisionDimension.ACTION,
        conflict_group=CONFLICT_GROUP,
        conflicts_with=[conflict_id],
        proposer=proposer,
        proposal=f"{proposer.value} 的动作建议。",
        supporting_thesis_ids=[THESIS_ID],
        evaluations=[
            ProposalEvaluation(
                manager=proposer,
                support_score=0.5,
                reason="原提议方保持正向支持。",
            ),
            ProposalEvaluation(
                manager=counterpart,
                support_score=-0.5,
                reason="对方暂不支持该建议。",
            ),
        ],
        status=ProposalStatus.PROPOSED,
    )


def _thesis() -> ThesisRecord:
    return ThesisRecord(
        thesis_id=THESIS_ID,
        run_id=RUN_ID,
        target=MARKET,
        as_of=AS_OF,
        title="正式协商测试观点",
        description="经查证后可供两位投资组合经理引用的共同观点。",
        direction=ThesisDirection.MIXED,
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
        supporting_evidence_ids=["ev_formal_negotiation_001"],
        reasoning_summary="支持事实和反向风险均已完成核验。",
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )


def _items_by_id(pool) -> dict[str, ProposalItem]:
    return {item.item_id: item for item in pool.proposal_items}


def _merge(state: dict, updates: dict) -> dict:
    merged = dict(state)
    for key, value in updates.items():
        if key in _APPEND_FIELDS:
            merged[key] = [*merged.get(key, []), *value]
        else:
            merged[key] = value
    return merged
