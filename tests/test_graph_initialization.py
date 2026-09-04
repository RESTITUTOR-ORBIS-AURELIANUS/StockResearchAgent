"""正式工作流入口节点与技术子图集成测试。"""

import asyncio
from datetime import date, datetime

import pytest

from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
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
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph import build_research_graph


def test_initialize_run_creates_budgets_and_empty_pools() -> None:
    graph = build_research_graph()
    target = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")

    result = graph.invoke({"target": target, "as_of": as_of})

    assert result["run_id"].startswith("run_20260818_153000_000001_SZ_")
    assert result["target"] == target
    assert result["as_of"] == as_of
    assert result["evidence_pool"] == []
    assert result["evidence_collection"].accepted_count == 0
    assert result["evidence_collection"].rejected_count == 0
    assert result["thesis_pool"] == []
    assert result["aggressive_recommendation"] is None
    assert result["conservative_recommendation"] is None
    assert result["consensus_recommendation"] is None
    assert result["consensus_assembly_run_summary"] is None
    assert result["aggressive_recommendation_run_summary"] is None
    assert result["conservative_recommendation_run_summary"] is None
    assert result["normalized_proposal_pool"] is None
    assert result["proposal_normalization_run_summary"] is None
    assert result["aggressive_cross_review"] is None
    assert result["conservative_cross_review"] is None
    assert result["aggressive_cross_review_run_summary"] is None
    assert result["conservative_cross_review_run_summary"] is None
    assert result["cross_reviewed_proposal_pool"] is None
    assert result["cross_review_application_run_summary"] is None
    assert result["conflict_score_validation_report"] is None
    assert result["cross_review_correction_run_summary"] is None
    assert result["negotiation_proposal_pool"] is None
    assert result["consensus_gate_report"] is None
    assert result["consensus_gate_reports"] == []
    assert result["reason_exchange_records"] == []
    assert result["proposal_revision_records"] == []
    assert result["debate_score_records"] == []
    assert result["negotiation_model_run_summaries"] == []
    assert result["negotiation_stage_run_summaries"] == []
    assert result["proposal_revision_application_summary"] is None
    assert result["negotiation_score_validation_report"] is None
    assert result["negotiation_round_summaries"] == []
    assert result["validation_round"] == 0
    assert result["token_budget_remaining"] > 0
    assert result["evidence_stage_failed"] is False


def test_initialize_run_rejects_state_from_a_previous_execution() -> None:
    graph = build_research_graph()
    target = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")

    with pytest.raises(ValueError, match="不能携带上一轮运行状态"):
        graph.invoke(
            {
                "target": target,
                "as_of": as_of,
                "errors": ["stale error from previous execution"],
            }
        )


def test_factory_receives_the_run_id_generated_by_initialize_run() -> None:
    class StubTechnicalGraph:
        def __init__(self, expected_run_id: str, expected_as_of: datetime) -> None:
            self.expected_run_id = expected_run_id
            self.expected_as_of = expected_as_of

        async def ainvoke(self, state):
            assert state["run_id"] == self.expected_run_id
            assert state["as_of"] == self.expected_as_of
            assert state["mode"] is TechnicalResearchMode.DAILY
            return {
                "evidence_records": [
                    EvidenceRecord(
                        evidence_id="ev_graph_collector_001",
                        run_id=state["run_id"],
                        target=state["target"],
                        domain=EvidenceDomain.TECHNICAL,
                        as_of=state["as_of"],
                        title="主图证据汇总测试",
                        description="验证技术子图证据会在主流程末尾进入 Collector。",
                        source_refs=[
                            SourceReference(
                                provider="test_provider",
                                interface="daily",
                                record_key="daily:A_SHARE:20260818",
                                published_at=state["as_of"],
                            )
                        ],
                        verification_status=VerificationStatus.VERIFIED,
                        collected_by="TechnicalResearchAnalyst",
                        created_at=state["as_of"],
                    )
                ],
                "errors": [],
                "run_summary": _summary(TechnicalResearchMode.DAILY),
            }

    async def scenario() -> None:
        factory_calls: list[tuple[str, datetime]] = []

        async def factory(*, run_id: str, as_of: datetime):
            factory_calls.append((run_id, as_of))
            return StubTechnicalGraph(run_id, as_of)

        graph = build_research_graph(technical_agent_graph_factory=factory)
        target = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
        input_as_of = datetime.fromisoformat("2026-08-18T07:30:00.123456+00:00")
        normalized_as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")

        result = await graph.ainvoke({"target": target, "as_of": input_as_of})

        assert result["as_of"] == normalized_as_of
        assert factory_calls == [(result["run_id"], normalized_as_of)]
        assert result["technical_run_summary"].mode is TechnicalResearchMode.DAILY
        assert result["technical_run_summary"].tool_call_count == 1
        assert result["evidence_collection"].accepted_count == 1
        assert result["evidence_collection"].evidence[0].evidence_id == ("ev_graph_collector_001")

    asyncio.run(scenario())


def test_evidence_agent_error_stops_before_the_next_agent() -> None:
    class FailingTechnicalGraph:
        async def ainvoke(self, state):
            return {
                "evidence_records": [],
                "errors": ["daily structured output failed"],
                "run_summary": _summary(
                    state["mode"],
                    stop_reason="failed_without_evidence",
                ),
            }

    sentiment_factory_called = False

    def sentiment_factory(**_):
        nonlocal sentiment_factory_called
        sentiment_factory_called = True
        raise AssertionError("证据阶段失败后不应创建后续 Agent")

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: FailingTechnicalGraph(),
        sentiment_flow_agent_graph_factory=sentiment_factory,
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "target": ResearchTarget(
                    type=TargetType.MARKET,
                    code="A_SHARE",
                    name="A股市场",
                ),
                "as_of": datetime.fromisoformat("2026-08-26T15:30:00+08:00"),
            }
        )
    )

    assert result["evidence_stage_failed"] is True
    assert sentiment_factory_called is False
    assert result["research_report"].outcome.value == "INCOMPLETE"
    assert "technical: daily structured output failed" in result["errors"]


def test_rejected_evidence_warning_does_not_abort_the_stage() -> None:
    class PartiallyAcceptedTechnicalGraph:
        async def ainvoke(self, state):
            return {
                "evidence_records": [],
                "errors": ["一条证据草稿引用无法核验，已拒绝"],
                "run_summary": _summary(
                    state["mode"],
                    stop_reason="verification_complete",
                ),
            }

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: PartiallyAcceptedTechnicalGraph()
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "target": ResearchTarget(
                    type=TargetType.MARKET,
                    code="A_SHARE",
                    name="A股市场",
                ),
                "as_of": datetime.fromisoformat("2026-08-26T15:30:00+08:00"),
            }
        )
    )

    assert result["evidence_stage_failed"] is False
    assert result["evidence_collection"] is not None
    assert "technical: 一条证据草稿引用无法核验，已拒绝" in result["errors"]


def test_targeted_technical_requests_loop_and_other_domains_remain_pending() -> None:
    run_id = "run_20260818_153000_A_SHARE_aaaaaaaa"
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")
    target = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
    stock = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
    technical_requests = [
        _request(
            request_id="rq_technical_001",
            run_id=run_id,
            target=stock,
            domain=EvidenceDomain.TECHNICAL,
            as_of=as_of,
        ),
        _request(
            request_id="rq_technical_002",
            run_id=run_id,
            target=stock,
            domain=EvidenceDomain.TECHNICAL,
            as_of=as_of,
        ),
    ]
    event_request = _request(
        request_id="rq_event_001",
        run_id=run_id,
        target=stock,
        domain=EvidenceDomain.EVENT,
        as_of=as_of,
    )

    class CompletingTechnicalGraph:
        def __init__(self) -> None:
            self.modes: list[TechnicalResearchMode] = []

        async def ainvoke(self, state):
            mode = state["mode"]
            self.modes.append(mode)
            result = {
                "evidence_records": [],
                "errors": [],
                "run_summary": _summary(mode),
            }
            if mode is TechnicalResearchMode.VERIFICATION:
                request = state["research_request"]
                result["completed_research_request"] = ResearchRequest.model_validate(
                    {
                        **request.model_dump(),
                        "status": ResearchRequestStatus.NO_NEW_EVIDENCE,
                        "completed_at": state["as_of"],
                    }
                )
            return result

    async def scenario() -> None:
        technical_graph = CompletingTechnicalGraph()
        factory_calls = 0

        def factory(*, run_id: str, as_of: datetime):
            nonlocal factory_calls
            factory_calls += 1
            assert run_id == "run_20260818_153000_A_SHARE_aaaaaaaa"
            assert as_of == datetime.fromisoformat("2026-08-18T15:30:00+08:00")
            return technical_graph

        graph = build_research_graph(technical_agent_graph_factory=factory)
        result = await graph.ainvoke(
            {
                "run_id": run_id,
                "target": target,
                "as_of": as_of,
                "research_requests": [*technical_requests, event_request],
            }
        )

        requests = {request.request_id: request for request in result["research_requests"]}
        assert factory_calls == 1
        assert technical_graph.modes == [
            TechnicalResearchMode.DAILY,
            TechnicalResearchMode.VERIFICATION,
            TechnicalResearchMode.VERIFICATION,
        ]
        assert requests["rq_technical_001"].status is ResearchRequestStatus.NO_NEW_EVIDENCE
        assert requests["rq_technical_002"].status is ResearchRequestStatus.NO_NEW_EVIDENCE
        assert requests["rq_event_001"].status is ResearchRequestStatus.PENDING
        assert result["research_request_count"] == 2

    asyncio.run(scenario())


def test_invalid_targeted_result_is_marked_failed_instead_of_looping_forever() -> None:
    run_id = "run_20260818_153000_A_SHARE_bbbbbbbb"
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")
    target = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
    request = _request(
        request_id="rq_technical_broken",
        run_id=run_id,
        target=ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行"),
        domain=EvidenceDomain.TECHNICAL,
        as_of=as_of,
    )

    class IncompleteTechnicalGraph:
        async def ainvoke(self, state):
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": _summary(state["mode"]),
                "completed_research_request": None,
            }

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: IncompleteTechnicalGraph()
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "run_id": run_id,
                "target": target,
                "as_of": as_of,
                "research_requests": [request],
            }
        )
    )

    assert result["research_requests"][0].status is ResearchRequestStatus.FAILED
    assert result["research_request_count"] == 1
    assert "returned no valid terminal status" in result["errors"][-1]


def test_targeted_request_budget_cancels_the_remaining_queue() -> None:
    run_id = "run_20260818_153000_A_SHARE_cccccccc"
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")
    target = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
    stock = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
    requests = [
        _request(
            request_id=f"rq_budget_{index:02d}",
            run_id=run_id,
            target=stock,
            domain=EvidenceDomain.TECHNICAL,
            as_of=as_of,
        )
        for index in range(21)
    ]

    class CompletingTechnicalGraph:
        async def ainvoke(self, state):
            result = {
                "evidence_records": [],
                "errors": [],
                "run_summary": _summary(state["mode"]),
            }
            if state["mode"] is TechnicalResearchMode.VERIFICATION:
                request = state["research_request"]
                result["completed_research_request"] = ResearchRequest.model_validate(
                    {
                        **request.model_dump(),
                        "status": ResearchRequestStatus.NO_NEW_EVIDENCE,
                        "completed_at": state["as_of"],
                    }
                )
            return result

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: CompletingTechnicalGraph()
    )
    result = asyncio.run(
        graph.ainvoke(
            {
                "run_id": run_id,
                "target": target,
                "as_of": as_of,
                "research_requests": requests,
            }
        )
    )

    statuses = [request.status for request in result["research_requests"]]
    assert statuses.count(ResearchRequestStatus.NO_NEW_EVIDENCE) == 20
    assert statuses.count(ResearchRequestStatus.CANCELLED_BY_BUDGET) == 1
    assert result["research_request_count"] == 20
    assert "technical request budget reached" in result["errors"][-1]


def _summary(
    mode: TechnicalResearchMode,
    *,
    stop_reason: str = "test_complete",
) -> TechnicalAgentRunSummary:
    return TechnicalAgentRunSummary(
        mode=mode,
        verification_rounds=0,
        tool_call_count=1,
        accepted_evidence_count=0,
        rejected_evidence_count=0,
        stop_reason=stop_reason,
    )


def _request(
    *,
    request_id: str,
    run_id: str,
    target: ResearchTarget,
    domain: EvidenceDomain,
    as_of: datetime,
) -> ResearchRequest:
    return ResearchRequest(
        request_id=request_id,
        run_id=run_id,
        thesis_id="th_candidate_001",
        target=target,
        assigned_domain=domain,
        question="这条假设是否得到数据支持？",
        requested_evidence="查找可复核的数据。",
        time_range=TimeRange(start=date(2026, 5, 1), end=date(2026, 8, 18)),
        priority=ResearchPriority.HIGH,
        requested_by="ThesisValidationAnalyst",
        created_at=as_of,
    )
