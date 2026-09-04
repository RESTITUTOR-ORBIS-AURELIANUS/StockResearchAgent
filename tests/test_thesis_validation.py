"""逐观点连续查证节点的状态、路由和安全边界测试。"""

import asyncio
from datetime import datetime

from stock_research_agent.agents.strategist import (
    CandidateThesisDraft,
    CandidateThesisGeneration,
)
from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
)
from stock_research_agent.agents.validator import (
    OpenAIThesisValidationAnalystModel,
    ThesisFinalizationDraft,
    ThesisValidationAction,
    ThesisValidationDecision,
    ThesisValidationLimits,
    ThesisValidationSession,
    ValidationResearchRequestDraft,
)
from stock_research_agent.agents.validator.prompts import (
    THESIS_VALIDATION_SYSTEM_PROMPT,
)
from stock_research_agent.domain import (
    EvidenceRecord,
    ResearchRequest,
    ResearchTarget,
    SourceReference,
    TimeRange,
)
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchFindingOutcome,
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph import build_research_graph
from stock_research_agent.graph.nodes.evidence_collector import evidence_collector_node
from stock_research_agent.graph.nodes.thesis_validation import (
    build_execute_validation_research_node,
    build_review_active_thesis_node,
    build_validation_request_fingerprint,
)

AS_OF = datetime.fromisoformat("2026-08-25T16:00:00+08:00")
RUN_ID = "run_20260825_160000_A_SHARE_validator"
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK_A = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
STOCK_B = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


class ScriptedStrategist:
    def __init__(self, drafts: tuple[CandidateThesisDraft, ...]) -> None:
        self.drafts = drafts

    async def generate_candidates(self, _request):
        return CandidateThesisGeneration(
            candidates=self.drafts,
            generation_summary="测试候选观点。",
        )


class ScriptedValidator:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls = []

    async def review_thesis(self, request):
        self.calls.append(request)
        return self.handler(request)


def test_default_validation_limits_bound_rounds_and_derived_theses() -> None:
    limits = ThesisValidationLimits()

    assert limits.max_research_rounds_per_thesis == 2
    assert limits.max_research_requests_per_run == 20
    assert limits.max_discovered_candidates_per_turn == 1
    assert limits.max_discovered_candidates_per_run == 2


def test_new_evidence_returns_to_same_thesis_context_before_next_thesis() -> None:
    initial = _evidence(
        "ev_initial_a",
        target=STOCK_A,
        title="价格相对行业走强",
        description="近二十日相对收益为正。",
    )
    follow_up = _evidence(
        "ev_follow_up_a",
        target=STOCK_A,
        title="中期趋势继续向上",
        description="区间趋势和动量指标继续改善。",
    )

    class TechnicalGraph:
        async def ainvoke(self, state):
            if state["mode"] is TechnicalResearchMode.DAILY:
                return _technical_result(state, [initial])
            request = state["research_request"]
            completed = _complete_request(request, [follow_up.evidence_id])
            return {
                **_technical_result(state, [follow_up]),
                "completed_research_request": completed,
                "observations": [
                    {"tool_name": "get_stock_price_context", "result": {"status": "ok"}}
                ],
            }

    def validator_handler(request):
        if not request.previous_turns:
            return _research_decision(request.thesis.target)
        assert len(request.previous_turns) == 1
        turn = request.previous_turns[0]
        assert turn.finding.outcome is ResearchFindingOutcome.EVIDENCE_FOUND
        assert turn.finding.evidence_ids == [follow_up.evidence_id]
        assert {item.evidence_id for item in request.evidence} == {
            initial.evidence_id,
            follow_up.evidence_id,
        }
        return _final_decision(
            ThesisValidationStatus.SUPPORTED,
            supporting=(initial.evidence_id, follow_up.evidence_id),
        )

    validator = ScriptedValidator(validator_handler)
    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: TechnicalGraph(),
        lead_research_strategist_model=ScriptedStrategist(
            (_candidate(STOCK_A, "趋势改善可能延续", initial.evidence_id),)
        ),
        thesis_validation_model=validator,
    )

    result = asyncio.run(
        graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})
    )

    assert len(validator.calls) == 2
    assert result["thesis_pool"][0].validation.status is ThesisValidationStatus.SUPPORTED
    assert result["thesis_pool"][0].validation.round == 1
    assert result["evidence_collection"].accepted_count == 2
    assert len(result["research_findings"]) == 1
    assert result["research_findings"][0].evidence_ids == [follow_up.evidence_id]
    assert result["active_validation_session"] is None
    assert result["thesis_validation_run_summary"].completed_thesis_count == 1


def test_no_match_is_not_fake_evidence_and_current_thesis_finishes_before_next() -> None:
    evidence_a = _evidence(
        "ev_serial_a",
        target=STOCK_A,
        title="A初始事实",
        description="A存在一个需要继续查证的事实。",
    )
    evidence_b = _evidence(
        "ev_serial_b",
        target=STOCK_B,
        title="B初始事实",
        description="B存在一条直接事实。",
    )

    class TechnicalGraph:
        async def ainvoke(self, state):
            if state["mode"] is TechnicalResearchMode.DAILY:
                return _technical_result(state, [evidence_a, evidence_b])
            request = state["research_request"]
            return {
                **_technical_result(state, []),
                "completed_research_request": _complete_request(
                    request,
                    [],
                    status=ResearchRequestStatus.NO_NEW_EVIDENCE,
                ),
                "observations": [
                    {"tool_name": "get_stock_price_context", "result": {"status": "empty"}}
                ],
            }

    call_order = []

    def validator_handler(request):
        call_order.append(request.thesis.title)
        if request.thesis.target == STOCK_A and not request.previous_turns:
            return _research_decision(STOCK_A)
        if request.thesis.target == STOCK_A:
            assert request.previous_turns[0].finding.outcome is (
                ResearchFindingOutcome.NO_MATCHING_EVIDENCE
            )
            return _final_decision(
                ThesisValidationStatus.INCONCLUSIVE,
                supporting=(evidence_a.evidence_id,),
            )
        return _final_decision(
            ThesisValidationStatus.SUPPORTED,
            supporting=(evidence_b.evidence_id,),
        )

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: TechnicalGraph(),
        lead_research_strategist_model=ScriptedStrategist(
            (
                _candidate(STOCK_A, "观点A", evidence_a.evidence_id),
                _candidate(STOCK_B, "观点B", evidence_b.evidence_id),
            )
        ),
        thesis_validation_model=ScriptedValidator(validator_handler),
    )

    result = asyncio.run(
        graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})
    )

    assert call_order == ["观点A", "观点A", "观点B"]
    assert len(result["evidence_pool"]) == 2
    finding = result["research_findings"][0]
    assert finding.outcome is ResearchFindingOutcome.NO_MATCHING_EVIDENCE
    assert finding.evidence_ids == []
    statuses = [item.validation.status for item in result["thesis_pool"]]
    assert statuses == [
        ThesisValidationStatus.INCONCLUSIVE,
        ThesisValidationStatus.SUPPORTED,
    ]


def test_duplicate_research_request_is_blocked_instead_of_looping() -> None:
    initial = _evidence(
        "ev_duplicate_guard",
        target=STOCK_A,
        title="重复请求保护的初始事实",
        description="同义或完全相同的查证请求不能反复消耗预算。",
    )

    class TechnicalGraph:
        async def ainvoke(self, state):
            if state["mode"] is TechnicalResearchMode.DAILY:
                return _technical_result(state, [initial])
            request = state["research_request"]
            return {
                **_technical_result(state, []),
                "completed_research_request": _complete_request(
                    request,
                    [],
                    status=ResearchRequestStatus.NO_NEW_EVIDENCE,
                ),
                "observations": [{"tool_name": "get_stock_price_context"}],
            }

    validator = ScriptedValidator(lambda request: _research_decision(request.thesis.target))
    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: TechnicalGraph(),
        lead_research_strategist_model=ScriptedStrategist(
            (_candidate(STOCK_A, "重复请求不应循环", initial.evidence_id),)
        ),
        thesis_validation_model=validator,
    )

    result = asyncio.run(
        graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})
    )

    assert len(validator.calls) == 2
    assert len(result["research_findings"]) == 1
    assert result["thesis_pool"][0].validation.status is (
        ThesisValidationStatus.INCONCLUSIVE
    )
    assert any("duplicate research request" in item for item in result["errors"])


def test_global_validation_research_budget_is_a_hard_limit() -> None:
    evidence_a = _evidence(
        "ev_budget_a",
        target=STOCK_A,
        title="A预算事实",
        description="A观点会消耗唯一的一次全局补证预算。",
    )
    evidence_b = _evidence(
        "ev_budget_b",
        target=STOCK_B,
        title="B预算事实",
        description="B观点不能越过已经耗尽的全局补证预算。",
    )

    class TechnicalGraph:
        async def ainvoke(self, state):
            if state["mode"] is TechnicalResearchMode.DAILY:
                return _technical_result(state, [evidence_a, evidence_b])
            request = state["research_request"]
            return {
                **_technical_result(state, []),
                "completed_research_request": _complete_request(
                    request,
                    [],
                    status=ResearchRequestStatus.NO_NEW_EVIDENCE,
                ),
                "observations": [{"tool_name": "get_stock_price_context"}],
            }

    def validator_handler(request):
        if request.thesis.target == STOCK_A and request.previous_turns:
            return _final_decision(
                ThesisValidationStatus.INCONCLUSIVE,
                supporting=(evidence_a.evidence_id,),
            )
        return _research_decision(request.thesis.target)

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: TechnicalGraph(),
        lead_research_strategist_model=ScriptedStrategist(
            (
                _candidate(STOCK_A, "预算观点A", evidence_a.evidence_id),
                _candidate(STOCK_B, "预算观点B", evidence_b.evidence_id),
            )
        ),
        thesis_validation_model=ScriptedValidator(validator_handler),
        thesis_validation_limits=ThesisValidationLimits(
            max_research_requests_per_run=1,
        ),
    )

    result = asyncio.run(
        graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})
    )

    assert len(result["research_findings"]) == 1
    assert result["research_request_count"] == 1
    assert [item.validation.status for item in result["thesis_pool"]] == [
        ThesisValidationStatus.INCONCLUSIVE,
        ThesisValidationStatus.INCONCLUSIVE,
    ]
    assert any("budget exhausted" in item for item in result["errors"])


def test_single_thesis_stops_after_two_research_rounds() -> None:
    initial = _evidence(
        "ev_three_round_guard",
        target=STOCK_A,
        title="单观点预算初始事实",
        description="即使模型持续追问，单观点也只能执行两轮补证。",
    )

    class TechnicalGraph:
        async def ainvoke(self, state):
            if state["mode"] is TechnicalResearchMode.DAILY:
                return _technical_result(state, [initial])
            request = state["research_request"]
            return {
                **_technical_result(state, []),
                "completed_research_request": _complete_request(
                    request,
                    [],
                    status=ResearchRequestStatus.NO_NEW_EVIDENCE,
                ),
                "observations": [{"tool_name": "get_stock_price_context"}],
            }

    def validator_handler(request):
        round_number = len(request.previous_turns) + 1
        return ThesisValidationDecision(
            action=ThesisValidationAction.REQUEST_RESEARCH,
            review_summary=f"第 {round_number} 轮仍希望补证。",
            research_request=ValidationResearchRequestDraft(
                target=STOCK_A,
                assigned_domain=EvidenceDomain.TECHNICAL,
                question=f"第 {round_number} 个不同的趋势问题是什么？",
                requested_evidence=f"读取第 {round_number} 个不同维度的正反事实。",
                time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
                priority=ResearchPriority.HIGH,
                rationale="测试单观点硬预算。",
                novelty_explanation="每轮的问题文字和研究维度均不同。",
            ),
        )

    validator = ScriptedValidator(validator_handler)
    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: TechnicalGraph(),
        lead_research_strategist_model=ScriptedStrategist(
            (_candidate(STOCK_A, "最多两轮", initial.evidence_id),)
        ),
        thesis_validation_model=validator,
    )

    result = asyncio.run(
        graph.ainvoke({"run_id": RUN_ID, "target": MARKET, "as_of": AS_OF})
    )

    assert len(validator.calls) == 3
    assert len(result["research_findings"]) == 2
    assert result["research_request_count"] == 2
    assert result["thesis_pool"][0].validation.status is (
        ThesisValidationStatus.INCONCLUSIVE
    )
    assert any("budget exhausted" in item for item in result["errors"])


def test_missing_domain_executor_returns_tool_gap_to_same_session() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.EVENT,
        question="是否存在直接公告？",
        requested_evidence="查找支持或否定该事件的公告。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        priority=ResearchPriority.HIGH,
        rationale="公告可以直接验证事件。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)
    state = _pending_state(thesis, request, fingerprint)
    node = build_execute_validation_research_node()

    result = asyncio.run(node(state))

    finding = result["research_findings"][0]
    assert finding.outcome is ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE
    assert finding.evidence_ids == []
    assert result["research_requests"][0].status is ResearchRequestStatus.FAILED
    assert result["active_validation_request_id"] is None
    assert len(result["active_validation_session"].previous_turns) == 1


def test_invalid_completion_cannot_pollute_global_evidence_pool() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="趋势是否延续？",
        requested_evidence="读取趋势的支持和反向事实。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        rationale="趋势决定观点能否成立。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)
    malicious = _evidence(
        "ev_malicious",
        target=STOCK_A,
        title="不应进入全局池",
        description="完成对象已经篡改，整组结果必须拒绝。",
    )

    class InvalidGraph:
        async def ainvoke(self, _state):
            mutated = ResearchRequest.model_validate(
                {
                    **request.model_dump(),
                    "question": "被篡改的问题",
                    "status": ResearchRequestStatus.COMPLETED,
                    "result_evidence_ids": [malicious.evidence_id],
                    "completed_at": AS_OF,
                }
            )
            return {
                "evidence_records": [malicious],
                "completed_research_request": mutated,
                "observations": [{"tool_name": "bad_tool"}],
                "errors": [],
            }

    class Resolver:
        async def resolve(self, _state):
            return InvalidGraph()

        def release(self, _run_id):
            return None

    state = _pending_state(thesis, request, fingerprint)
    node = build_execute_validation_research_node(technical_resolver=Resolver())

    result = asyncio.run(node(state))

    assert "evidence_pool" not in result
    assert result["research_findings"][0].outcome is ResearchFindingOutcome.REQUEST_FAILED
    assert result["research_findings"][0].evidence_ids == []
    assert "mutated immutable field" in result["errors"][0]


def test_matching_evidence_already_in_global_pool_is_reused_as_real_evidence() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="已有证据是否直接回答当前问题？",
        requested_evidence="重新命中已经存在于全局池中的同一条真实记录。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        rationale="验证全局证据可以复用而不是被误判成查无结果。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)
    reusable = _evidence(
        "ev_reusable_global",
        target=STOCK_A,
        title="可复用的全局证据",
        description="这条记录此前已由其他观点加入全局证据池。",
    )

    class ReusingGraph:
        async def ainvoke(self, state):
            return {
                **_technical_result(state, [reusable]),
                "completed_research_request": _complete_request(
                    request,
                    [reusable.evidence_id],
                ),
                "observations": [{"tool_name": "get_stock_price_context"}],
            }

    class Resolver:
        async def resolve(self, _state):
            return ReusingGraph()

        def release(self, _run_id):
            return None

    state = _pending_state(thesis, request, fingerprint)
    state["evidence_pool"].append(reusable)
    state.update(evidence_collector_node(state))
    node = build_execute_validation_research_node(technical_resolver=Resolver())

    result = asyncio.run(node(state))

    finding = result["research_findings"][0]
    assert finding.outcome is ResearchFindingOutcome.EVIDENCE_FOUND
    assert finding.evidence_ids == [reusable.evidence_id]
    assert "0 条首次进入全局证据池" in finding.summary


def test_executor_rejects_evidence_for_an_unauthorized_target() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="A股票的趋势是否延续？",
        requested_evidence="只允许返回A股票及显式基准的技术证据。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        rationale="防止领域子图把其他股票的事实串到当前观点。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)
    wrong_target = _evidence(
        "ev_wrong_target",
        target=STOCK_B,
        title="错误标的证据",
        description="这条万科A证据不能回答平安银行请求。",
    )

    class WrongTargetGraph:
        async def ainvoke(self, state):
            return {
                **_technical_result(state, [wrong_target]),
                "completed_research_request": _complete_request(
                    request,
                    [wrong_target.evidence_id],
                ),
                "observations": [{"tool_name": "get_stock_price_context"}],
            }

    class Resolver:
        async def resolve(self, _state):
            return WrongTargetGraph()

        def release(self, _run_id):
            return None

    node = build_execute_validation_research_node(technical_resolver=Resolver())
    result = asyncio.run(node(_pending_state(thesis, request, fingerprint)))

    assert "evidence_pool" not in result
    assert result["research_findings"][0].outcome is (
        ResearchFindingOutcome.REQUEST_FAILED
    )
    assert "no evidence for the requested primary target" in result["errors"][0]


def test_malformed_agent_result_becomes_failed_finding_instead_of_crashing() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="子图是否返回合法结构？",
        requested_evidence="验证畸形返回会失败关闭。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        rationale="主图不能因第三方子图畸形输出直接中断。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)

    class MalformedGraph:
        async def ainvoke(self, _state):
            return None

    class Resolver:
        async def resolve(self, _state):
            return MalformedGraph()

        def release(self, _run_id):
            return None

    node = build_execute_validation_research_node(technical_resolver=Resolver())
    result = asyncio.run(node(_pending_state(thesis, request, fingerprint)))

    assert result["research_findings"][0].outcome is (
        ResearchFindingOutcome.REQUEST_FAILED
    )
    assert result["active_validation_request_id"] is None
    assert "validation research failed" in result["errors"][0]


def test_missing_session_state_is_closed_instead_of_routing_forever() -> None:
    thesis = _under_review_thesis()
    draft = ValidationResearchRequestDraft(
        target=STOCK_A,
        assigned_domain=EvidenceDomain.TECHNICAL,
        question="损坏状态能否安全停止？",
        requested_evidence="不实际执行，只测试恢复边界。",
        time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
        rationale="避免 review 和 research 节点死循环。",
        novelty_explanation="首次查询。",
    )
    fingerprint = build_validation_request_fingerprint(draft)
    request = _request_from_draft(thesis, draft)
    state = _pending_state(thesis, request, fingerprint)
    state["active_validation_session"] = None
    model = ScriptedValidator(lambda _request: _final_decision(
        ThesisValidationStatus.INCONCLUSIVE,
        supporting=("ev_initial_context",),
    ))

    result = asyncio.run(build_review_active_thesis_node(model)(state))

    assert model.calls == []
    assert result["active_validation_request_id"] is None
    assert result["research_requests"][0].status is ResearchRequestStatus.FAILED
    assert result["thesis_pool"][0].validation.status is (
        ThesisValidationStatus.INCONCLUSIVE
    )
    assert result["validation_stop_reason"] == "invalid_state"


def test_validator_adapter_and_prompt_use_hard_structured_contract() -> None:
    class RecordingChatModel:
        def __init__(self) -> None:
            self.calls = []

        def with_structured_output(self, schema, *, method, include_raw, strict):
            self.calls.append((schema, method, include_raw, strict))
            return object()

    chat_model = RecordingChatModel()
    OpenAIThesisValidationAnalystModel(  # type: ignore[arg-type]
        chat_model,
        structured_output_method="json_schema",
    )

    assert chat_model.calls[0][0]["title"] == "ThesisValidationModelOutput"
    assert chat_model.calls[0][1] == "json_schema"
    assert chat_model.calls[0][2:] == (True, True)
    assert "每轮只允许一个问题" in THESIS_VALIDATION_SYSTEM_PROMPT
    assert "不等于事实不存在，不是反证" in THESIS_VALIDATION_SYSTEM_PROMPT
    assert "previous_turns" in THESIS_VALIDATION_SYSTEM_PROMPT
    assert "VERIFIED / REVISED" in THESIS_VALIDATION_SYSTEM_PROMPT
    assert "未来预测的判定规则" in THESIS_VALIDATION_SYSTEM_PROMPT
    assert "不要仅因为未来尚未到来" in THESIS_VALIDATION_SYSTEM_PROMPT


def _candidate(target: ResearchTarget, title: str, evidence_id: str):
    return CandidateThesisDraft(
        target=target,
        title=title,
        description="根据当前事实提出一个需要逐步查证的机制解释。",
        direction=ThesisDirection.MIXED,
        horizon="未来一个季度",
        supporting_evidence_ids=(evidence_id,),
        reasoning_summary="当前只是候选解释。",
        missing_questions=("关键机制是否能由新增事实确认？",),
        invalidation_conditions=("后续事实与当前解释方向相反",),
    )


def _research_decision(target: ResearchTarget):
    return ThesisValidationDecision(
        action=ThesisValidationAction.REQUEST_RESEARCH,
        review_summary="需要取得区间技术证据后立即回审当前观点。",
        research_request=ValidationResearchRequestDraft(
            target=target,
            assigned_domain=EvidenceDomain.TECHNICAL,
            question="当前目标的中期趋势是否延续？",
            requested_evidence="寻找趋势延续与趋势失效两方面的可核验数据。",
            time_range=TimeRange(start=AS_OF.date(), end=AS_OF.date()),
            priority=ResearchPriority.HIGH,
            rationale="这项事实会直接改变当前观点判断。",
            novelty_explanation="当前连续会话尚未执行过该请求。",
        ),
        discovered_candidates=(),
    )


def _final_decision(status, *, supporting=(), contradicting=()):
    return ThesisValidationDecision(
        action=ThesisValidationAction.FINALIZE,
        review_summary="已经结合当前观点的完整连续查证上下文重新判断。",
        finalization=ThesisFinalizationDraft(
            final_status=status,
            confidence=0.9,
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
            reasoning_summary="区分现有事实、推断和仍未解决的限制后完成判断。",
            remaining_questions=("仍需在后续日期观察持续性。",),
        ),
    )


def _technical_result(state, evidence):
    return {
        "evidence_records": evidence,
        "errors": [],
        "run_summary": TechnicalAgentRunSummary(
            mode=state["mode"],
            verification_rounds=0,
            tool_call_count=1,
            accepted_evidence_count=len(evidence),
            rejected_evidence_count=0,
            stop_reason="test_complete",
        ),
    }


def _complete_request(request, evidence_ids, status=ResearchRequestStatus.COMPLETED):
    return ResearchRequest.model_validate(
        {
            **request.model_dump(),
            "status": status,
            "result_evidence_ids": evidence_ids,
            "completed_at": AS_OF,
        }
    )


def _under_review_thesis():
    return ThesisRecord(
        thesis_id="th_validation_test_001",
        run_id=RUN_ID,
        target=STOCK_A,
        as_of=AS_OF,
        title="待查证观点",
        description="需要进一步查证的观点。",
        direction=ThesisDirection.MIXED,
        horizon="未来一个季度",
        origin=ThesisOrigin(
            type=ThesisOriginType.LEAD_STRATEGIST,
            agent="LeadResearchStrategist",
        ),
        validation=ThesisValidation(
            status=ThesisValidationStatus.UNDER_REVIEW,
            confidence=None,
            round=1,
        ),
        supporting_evidence_ids=["ev_initial_context"],
        reasoning_summary="初始推断。",
        missing_questions=["还缺少什么？"],
        invalidation_conditions=["出现相反事实"],
        created_by="LeadResearchStrategist",
        created_at=AS_OF,
        updated_at=AS_OF,
    )


def _request_from_draft(thesis, draft):
    return ResearchRequest(
        request_id="rq_validation_test_001",
        run_id=RUN_ID,
        thesis_id=thesis.thesis_id,
        target=draft.target,
        assigned_domain=draft.assigned_domain,
        question=draft.question,
        requested_evidence=draft.requested_evidence,
        time_range=draft.time_range,
        priority=draft.priority,
        attempt=1,
        requested_by="ThesisValidationAnalyst",
        created_at=AS_OF,
    )


def _pending_state(thesis, request, fingerprint):
    initial = _evidence(
        "ev_initial_context",
        target=STOCK_A,
        title="初始证据",
        description="用于当前观点的初始真实证据。",
    )
    state = {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "evidence_pool": [initial],
        "thesis_pool": [thesis],
        "research_requests": [request],
        "research_findings": [],
        "active_validation_session": ThesisValidationSession(
            thesis_id=thesis.thesis_id,
            used_request_fingerprints=(fingerprint,),
            pending_request_fingerprint=fingerprint,
            pending_reviewer_reasoning="需要立即执行当前唯一查证请求。",
        ),
        "active_validation_request_id": request.request_id,
        "research_request_count": 0,
        "technical_request_count": 0,
        "fundamental_request_count": 0,
        "sentiment_flow_request_count": 0,
        "event_request_count": 0,
        "errors": [],
    }
    state.update(evidence_collector_node(state))
    return state


def _evidence(
    evidence_id: str,
    *,
    target: ResearchTarget,
    title: str,
    description: str,
):
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=RUN_ID,
        target=target,
        domain=EvidenceDomain.TECHNICAL,
        as_of=AS_OF,
        title=title,
        description=description,
        source_refs=[
            SourceReference(
                provider="test_provider",
                interface="technical_test",
                record_key=f"row:{evidence_id}",
                fetched_at=AS_OF,
                data_as_of=AS_OF.date(),
            )
        ],
        verification_status=VerificationStatus.VERIFIED,
        tags=["测试"],
        collected_by="TechnicalResearchAnalyst",
        created_at=AS_OF,
    )
