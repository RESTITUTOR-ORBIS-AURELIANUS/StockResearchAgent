"""把情绪与资金流 Agent 私有子图适配成主 ResearchGraphState 节点。"""

from collections.abc import Awaitable, Mapping
from datetime import datetime
from inspect import isawaitable
from typing import Any, Protocol, cast

from stock_research_agent.agents.sentiment_flow import SentimentFlowResearchMode
from stock_research_agent.domain import ResearchRequest
from stock_research_agent.domain.enums import EvidenceDomain, ResearchRequestStatus
from stock_research_agent.graph.state import ResearchGraphState

_ACTIVE_REQUEST_STATUSES = {
    ResearchRequestStatus.PENDING,
    ResearchRequestStatus.RUNNING,
}
MAX_SENTIMENT_FLOW_REQUESTS_PER_RUN = 12


class SentimentFlowAgentGraph(Protocol):
    """主图真正依赖的最小情绪资金子图接口。"""

    def ainvoke(self, state: Mapping[str, object]) -> Awaitable[Mapping[str, Any]]: ...


class SentimentFlowAgentGraphFactory(Protocol):
    """用已初始化的运行标识创建本轮专属情绪资金子图。"""

    def __call__(
        self,
        *,
        run_id: str,
        as_of: datetime,
    ) -> SentimentFlowAgentGraph | Awaitable[SentimentFlowAgentGraph]: ...


class RunScopedSentimentFlowGraphResolver:
    """按 run_id 延迟创建并复用子图，阶段结束后显式释放。"""

    def __init__(self, factory: SentimentFlowAgentGraphFactory) -> None:
        self._factory = factory
        self._graphs: dict[str, tuple[datetime, SentimentFlowAgentGraph]] = {}

    async def resolve(self, state: ResearchGraphState) -> SentimentFlowAgentGraph:
        run_id = state.get("run_id")
        as_of = state.get("as_of")
        if not run_id or as_of is None:
            raise ValueError("创建情绪资金子图前必须已经初始化 run_id 和 as_of")

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
            raise TypeError("sentiment_flow_agent_graph_factory 必须返回带 ainvoke 的子图")

        graph = cast(SentimentFlowAgentGraph, candidate)
        self._graphs[run_id] = (as_of, graph)
        return graph

    def release(self, run_id: str) -> None:
        self._graphs.pop(run_id, None)


def has_active_sentiment_flow_request(state: ResearchGraphState) -> bool:
    return _next_sentiment_flow_request(state) is not None


def sentiment_flow_request_budget_exhausted(state: ResearchGraphState) -> bool:
    return state.get("sentiment_flow_request_count", 0) >= MAX_SENTIMENT_FLOW_REQUESTS_PER_RUN


def build_initial_sentiment_flow_evidence_node(
    resolver: RunScopedSentimentFlowGraphResolver,
):
    """每日运行入口：执行市场快照分析与必要的内部查证。"""

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        try:
            graph = await resolver.resolve(state)
            result = await graph.ainvoke(
                {
                    "run_id": state["run_id"],
                    "target": state["target"],
                    "as_of": state["as_of"],
                    "mode": SentimentFlowResearchMode.DAILY,
                }
            )
            return _result_updates(result)
        except Exception as exc:
            return {
                "evidence_stage_failed": True,
                "errors": [f"sentiment_flow daily failed: {type(exc).__name__}"],
            }

    return node


def build_targeted_sentiment_flow_research_node(
    resolver: RunScopedSentimentFlowGraphResolver,
):
    """一次消费队列中的第一条情绪资金 ResearchRequest。"""

    async def node(state: ResearchGraphState) -> ResearchGraphState:
        request = _next_sentiment_flow_request(state)
        if request is None:
            return {}

        try:
            graph = await resolver.resolve(state)
            result = await graph.ainvoke(
                {
                    "run_id": state["run_id"],
                    "target": request.target,
                    "as_of": state["as_of"],
                    "mode": SentimentFlowResearchMode.VERIFICATION,
                    "research_request": request,
                }
            )
            updates = _result_updates(result)
        except Exception as exc:
            return {
                "evidence_stage_failed": True,
                "research_requests": [_failed_request(request, state["as_of"])],
                "research_request_count": state.get("research_request_count", 0) + 1,
                "sentiment_flow_request_count": (state.get("sentiment_flow_request_count", 0) + 1),
                "errors": [f"sentiment_flow request failed: {type(exc).__name__}"],
            }

        completed_request = result.get("completed_research_request")
        if not _is_valid_completion(completed_request, request):
            completed_request = _failed_request(request, state["as_of"])
            updates.setdefault("errors", []).append(
                f"sentiment_flow request {request.request_id} returned no valid terminal status"
            )
            updates["evidence_stage_failed"] = True

        updates["research_requests"] = [completed_request]
        updates["research_request_count"] = state.get("research_request_count", 0) + 1
        updates["sentiment_flow_request_count"] = state.get("sentiment_flow_request_count", 0) + 1
        return updates

    return node


def build_release_sentiment_flow_runtime_node(
    resolver: RunScopedSentimentFlowGraphResolver,
):
    def node(state: ResearchGraphState) -> ResearchGraphState:
        resolver.release(state["run_id"])
        return {}

    return node


def build_cancel_pending_sentiment_flow_requests_node():
    def node(state: ResearchGraphState) -> ResearchGraphState:
        active_requests = [
            request
            for request in state.get("research_requests", [])
            if request.assigned_domain is EvidenceDomain.SENTIMENT_FLOW
            and request.status in _ACTIVE_REQUEST_STATUSES
        ]
        if not active_requests:
            return {}
        return {
            "evidence_stage_failed": True,
            "research_requests": [
                _terminal_request(
                    request,
                    status=ResearchRequestStatus.CANCELLED_BY_BUDGET,
                    completed_at=state["as_of"],
                )
                for request in active_requests
            ],
            "errors": [
                "sentiment_flow request budget reached; "
                f"cancelled {len(active_requests)} pending request(s)"
            ],
        }

    return node


def _next_sentiment_flow_request(state: ResearchGraphState) -> ResearchRequest | None:
    return next(
        (
            item
            for item in state.get("research_requests", [])
            if item.assigned_domain is EvidenceDomain.SENTIMENT_FLOW
            and item.status in _ACTIVE_REQUEST_STATUSES
        ),
        None,
    )


def _result_updates(result: Mapping[str, Any]) -> ResearchGraphState:
    errors = [f"sentiment_flow: {message}" for message in result.get("errors", [])]
    updates: ResearchGraphState = {
        "evidence_pool": list(result.get("evidence_records", [])),
        "errors": errors,
    }
    run_summary = result.get("run_summary")
    if run_summary is not None:
        updates["sentiment_flow_run_summary"] = run_summary
    if errors and _run_summary_is_fatal(run_summary):
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
        and candidate.assigned_domain is EvidenceDomain.SENTIMENT_FLOW
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
