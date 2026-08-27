"""把新闻事件 Agent 私有子图适配成主 ResearchGraphState 节点。"""

from collections.abc import Awaitable, Mapping
from datetime import datetime
from inspect import isawaitable
from typing import Any, Protocol, cast

from stock_research_agent.agents.event import EventResearchMode
from stock_research_agent.domain import ResearchRequest
from stock_research_agent.domain.enums import EvidenceDomain, ResearchRequestStatus
from stock_research_agent.graph.state import ResearchGraphState

_ACTIVE_REQUEST_STATUSES = {
    ResearchRequestStatus.PENDING,
    ResearchRequestStatus.RUNNING,
}
MAX_EVENT_REQUESTS_PER_RUN = 12


class EventAgentGraph(Protocol):
    def ainvoke(self, state: Mapping[str, object]) -> Awaitable[Mapping[str, Any]]: ...


class EventAgentGraphFactory(Protocol):
    def __call__(
        self,
        *,
        run_id: str,
        as_of: datetime,
    ) -> EventAgentGraph | Awaitable[EventAgentGraph]: ...


class RunScopedEventGraphResolver:
    """同一运行复用 Event 子图，阶段结束显式释放。"""

    def __init__(self, factory: EventAgentGraphFactory) -> None:
        self._factory = factory
        self._graphs: dict[str, tuple[datetime, EventAgentGraph]] = {}

    async def resolve(self, state: ResearchGraphState) -> EventAgentGraph:
        run_id = state.get("run_id")
        as_of = state.get("as_of")
        if not run_id or as_of is None:
            raise ValueError("创建新闻事件子图前必须已经初始化 run_id 和 as_of")
        cached = self._graphs.get(run_id)
        if cached is not None:
            cached_as_of, graph = cached
            if cached_as_of != as_of:
                raise ValueError("同一 run_id 不能对应不同的 as_of")
            return graph
        candidate = self._factory(run_id=run_id, as_of=as_of)
        if isawaitable(candidate):
            candidate = await candidate
        if not callable(getattr(candidate, "ainvoke", None)):
            raise TypeError("event_agent_graph_factory 必须返回带 ainvoke 的子图")
        graph = cast(EventAgentGraph, candidate)
        self._graphs[run_id] = (as_of, graph)
        return graph

    def release(self, run_id: str) -> None:
        self._graphs.pop(run_id, None)


def has_active_event_request(state: ResearchGraphState) -> bool:
    return _next_event_request(state) is not None


def event_request_budget_exhausted(state: ResearchGraphState) -> bool:
    return state.get("event_request_count", 0) >= MAX_EVENT_REQUESTS_PER_RUN


def build_initial_event_evidence_node(resolver: RunScopedEventGraphResolver):
    async def node(state: ResearchGraphState) -> ResearchGraphState:
        try:
            graph = await resolver.resolve(state)
            result = await graph.ainvoke(
                {
                    "run_id": state["run_id"],
                    "target": state["target"],
                    "as_of": state["as_of"],
                    "mode": EventResearchMode.DAILY,
                }
            )
            return _result_updates(result)
        except Exception as exc:
            return {
                "evidence_stage_failed": True,
                "errors": [f"event daily failed: {type(exc).__name__}"],
            }

    return node


def build_targeted_event_research_node(resolver: RunScopedEventGraphResolver):
    async def node(state: ResearchGraphState) -> ResearchGraphState:
        request = _next_event_request(state)
        if request is None:
            return {}
        try:
            graph = await resolver.resolve(state)
            result = await graph.ainvoke(
                {
                    "run_id": state["run_id"],
                    "target": request.target,
                    "as_of": state["as_of"],
                    "mode": EventResearchMode.VERIFICATION,
                    "research_request": request,
                }
            )
            updates = _result_updates(result)
        except Exception as exc:
            return {
                "evidence_stage_failed": True,
                "research_requests": [_failed_request(request, state["as_of"])],
                "research_request_count": state.get("research_request_count", 0) + 1,
                "event_request_count": state.get("event_request_count", 0) + 1,
                "errors": [f"event request failed: {type(exc).__name__}"],
            }
        completed = result.get("completed_research_request")
        if not _is_valid_completion(completed, request):
            completed = _failed_request(request, state["as_of"])
            updates.setdefault("errors", []).append(
                f"event request {request.request_id} returned no valid terminal status"
            )
            updates["evidence_stage_failed"] = True
        updates["research_requests"] = [completed]
        updates["research_request_count"] = state.get("research_request_count", 0) + 1
        updates["event_request_count"] = state.get("event_request_count", 0) + 1
        return updates

    return node


def build_release_event_runtime_node(resolver: RunScopedEventGraphResolver):
    def node(state: ResearchGraphState) -> ResearchGraphState:
        resolver.release(state["run_id"])
        return {}

    return node


def build_cancel_pending_event_requests_node():
    def node(state: ResearchGraphState) -> ResearchGraphState:
        active = [
            request
            for request in state.get("research_requests", [])
            if request.assigned_domain is EvidenceDomain.EVENT
            and request.status in _ACTIVE_REQUEST_STATUSES
        ]
        if not active:
            return {}
        return {
            "evidence_stage_failed": True,
            "research_requests": [
                _terminal_request(
                    request,
                    status=ResearchRequestStatus.CANCELLED_BY_BUDGET,
                    completed_at=state["as_of"],
                )
                for request in active
            ],
            "errors": [f"event request budget reached; cancelled {len(active)} pending request(s)"],
        }

    return node


def _next_event_request(state: ResearchGraphState) -> ResearchRequest | None:
    return next(
        (
            request
            for request in state.get("research_requests", [])
            if request.assigned_domain is EvidenceDomain.EVENT
            and request.status in _ACTIVE_REQUEST_STATUSES
        ),
        None,
    )


def _result_updates(result: Mapping[str, Any]) -> ResearchGraphState:
    errors = [f"event: {message}" for message in result.get("errors", [])]
    updates: ResearchGraphState = {
        "evidence_pool": list(result.get("evidence_records", [])),
        "errors": errors,
    }
    summary = result.get("run_summary")
    if summary is not None:
        updates["event_run_summary"] = summary
    if errors and _run_summary_is_fatal(summary):
        updates["evidence_stage_failed"] = True
    return updates


def _run_summary_is_fatal(run_summary: object) -> bool:
    return run_summary is None or getattr(run_summary, "stop_reason", None) in {
        "failed_without_evidence",
        "verification_budget_reached",
    }


def _is_valid_completion(candidate: object, original: ResearchRequest) -> bool:
    return (
        isinstance(candidate, ResearchRequest)
        and candidate.request_id == original.request_id
        and candidate.run_id == original.run_id
        and candidate.target == original.target
        and candidate.assigned_domain is EvidenceDomain.EVENT
        and candidate.status not in _ACTIVE_REQUEST_STATUSES
    )


def _failed_request(request: ResearchRequest, completed_at: datetime) -> ResearchRequest:
    return _terminal_request(
        request,
        status=ResearchRequestStatus.FAILED,
        completed_at=completed_at,
    )


def _terminal_request(
    request: ResearchRequest,
    *,
    status: ResearchRequestStatus,
    completed_at: datetime,
) -> ResearchRequest:
    return ResearchRequest.model_validate(
        {
            **request.model_dump(),
            "status": status,
            "result_evidence_ids": [],
            "completed_at": completed_at,
        }
    )
