"""EventDrivenResearchAnalyst 的每日模式与定向查证 LangGraph 子图。"""

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.event.model import EventReasoningModel
from stock_research_agent.agents.event.models import (
    DailyEventInput,
    EventAgentLimits,
    EventAgentRunSummary,
    EventCheck,
    EventEvidenceDraft,
    EventResearchMode,
    EventReviewInput,
    EventToolObservation,
    EventVerificationRequestDraft,
    EventVerificationTask,
    TargetedEventInput,
)
from stock_research_agent.agents.event.state import EventAgentState
from stock_research_agent.domain import (
    EvidenceRecord,
    ResearchRequest,
    ResearchTarget,
    SourceReference,
)
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.llm import describe_exception
from stock_research_agent.research_data import ResearchDataStoreError
from stock_research_agent.services.daily_event_snapshot import (
    event_broker_recommendation_record_key,
    event_report_record_key,
)
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_AGENT_NAME = "EventDrivenResearchAnalyst"
_SNAPSHOT_TOOL = "get_daily_event_snapshot"
_IDENTITY_TOOL = "resolve_stock_identity"
_TOOL_BY_CHECK = {
    EventCheck.NEWS_DISCLOSURES: "get_targeted_news_and_disclosures",
    EventCheck.SELL_SIDE_RESEARCH: "get_sell_side_research_context",
    EventCheck.CORPORATE_ACTIONS: "get_corporate_action_events",
    EventCheck.EARNINGS_DISCLOSURE: "get_earnings_and_disclosure",
}
_EVIDENCE_CITABLE_STATUSES = {"ok", "partial"}
_REUSABLE_STATUSES = {"ok", "partial", "empty"}


def build_event_agent_graph(
    *,
    model: EventReasoningModel,
    tool_context: ResearchToolContext,
    limits: EventAgentLimits | None = None,
    tools: tuple[BaseTool, ...] | None = None,
):
    """构建可独立运行、也可嵌入主图的新闻事件研究子图。"""

    configured_limits = limits or EventAgentLimits()
    event_tools = tools or build_agent_tool_registry(tool_context).event
    tools_by_name = {tool.name: tool for tool in event_tools}
    _require_tools(tools_by_name)

    async def prepare(state: EventAgentState) -> EventAgentState:
        run_id = state.get("run_id")
        target = state.get("target")
        as_of = state.get("as_of")
        if not run_id or target is None or as_of is None:
            raise ValueError("新闻事件 Agent 必须提供 run_id、target 和 as_of")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("新闻事件 Agent 的 as_of 必须包含时区")
        if run_id != tool_context.run_id or as_of != tool_context.as_of:
            raise ValueError("新闻事件 Agent 输入必须与 ToolContext 的 run_id/as_of 一致")

        mode = EventResearchMode(state.get("mode", EventResearchMode.DAILY))
        research_request = state.get("research_request")
        if mode is EventResearchMode.VERIFICATION:
            _validate_external_request(research_request, run_id, as_of)

        return {
            "mode": mode,
            "research_request": research_request,
            "snapshot_result": None,
            "evidence_drafts": [],
            "pending_tasks": [],
            "observations": [],
            "seen_request_fingerprints": [],
            "verification_round": 0,
            "tool_call_count": 0,
            "budget_exhausted": False,
            "skipped_task_ids": [],
            "unresolved_questions": [],
            "errors": [],
            "evidence_records": [],
            "completed_research_request": None,
        }

    async def acquire_daily_snapshot(state: EventAgentState) -> EventAgentState:
        arguments = {
            "candidate_count": configured_limits.daily_candidate_count,
            "news_lookback_hours": configured_limits.news_lookback_hours,
            "announcement_lookback_days": configured_limits.announcement_lookback_days,
            "research_lookback_days": configured_limits.research_lookback_days,
        }
        result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], arguments)
        observations = [
            EventToolObservation(
                call_id="ec_daily_snapshot_1",
                tool_name=_SNAPSHOT_TOOL,
                arguments=arguments,
                result=result,
            )
        ]
        call_count = 1
        retry_needed = (
            result.get("status") == "too_large" and configured_limits.daily_candidate_count > 3
        )
        if retry_needed and call_count < configured_limits.max_total_tool_calls:
            retry_arguments = {**arguments, "candidate_count": 3}
            result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], retry_arguments)
            observations.append(
                EventToolObservation(
                    call_id="ec_daily_snapshot_2",
                    tool_name=_SNAPSHOT_TOOL,
                    arguments=retry_arguments,
                    result=result,
                )
            )
            call_count += 1

        errors: list[str] = []
        if result.get("status") not in {"ok", "partial"} or result.get("snapshot") is None:
            errors.append(f"每日新闻事件快照不可用：status={result.get('status', 'unknown')}")
        return {
            "snapshot_result": result,
            "observations": observations,
            "tool_call_count": call_count,
            "budget_exhausted": retry_needed
            and call_count >= configured_limits.max_total_tool_calls
            and result.get("status") == "too_large",
            "errors": errors,
        }

    async def analyze_daily(state: EventAgentState) -> EventAgentState:
        snapshot_result = state["snapshot_result"]
        snapshot_observation = state["observations"][-1]
        try:
            decision = await model.analyze_daily(
                DailyEventInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    snapshot_call_id=snapshot_observation.call_id,
                    snapshot_result=snapshot_result,
                )
            )
        except Exception as exc:
            return {"errors": [f"每日新闻事件结构化分析失败：{describe_exception(exc)}"]}

        candidate_catalog = _snapshot_record_catalog(snapshot_result)
        catalog = candidate_catalog
        context_ref = snapshot_result.get("context_ref")
        if isinstance(context_ref, str):
            try:
                bundle = await tool_context.data_store.get(state["run_id"], context_ref)
                catalog = _bundle_record_catalog(bundle, candidate_catalog)
            except ResearchDataStoreError:
                catalog = {}
        evidence_targets, stock_targets = _daily_snapshot_permissions(
            snapshot_result,
            state["target"],
        )
        drafts, draft_errors = _filter_daily_drafts(
            decision.snapshot_evidence,
            allowed_call_id=snapshot_observation.call_id,
            allowed_targets=evidence_targets,
            record_catalog=catalog,
        )
        tasks, fingerprints, task_errors = _build_tasks(
            decision.verification_requests,
            origin="DAILY",
            round_number=1,
            allowed_targets=stock_targets,
            existing_fingerprints=set(),
            limit=configured_limits.max_requests_per_round,
            as_of_date=state["as_of"].date(),
        )
        return {
            "evidence_drafts": drafts,
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": [*draft_errors, *task_errors],
        }

    async def plan_targeted(state: EventAgentState) -> EventAgentState:
        research_request = state["research_request"]
        try:
            plan = await model.plan_targeted(
                TargetedEventInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    research_request=research_request,
                )
            )
        except Exception as exc:
            return {"errors": [f"定向新闻事件计划生成失败：{describe_exception(exc)}"]}

        allowed = {research_request.target.code: {research_request.target.type}}
        tasks, fingerprints, task_errors = _build_tasks(
            plan.verification_requests,
            origin="RESEARCH_REQUEST",
            round_number=1,
            allowed_targets=allowed,
            existing_fingerprints=set(),
            limit=configured_limits.max_requests_per_round,
            required_primary_target=research_request.target,
            as_of_date=state["as_of"].date(),
            allowed_date_range=(
                research_request.time_range.start,
                research_request.time_range.end,
            ),
        )
        return {
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": task_errors,
        }

    async def execute_verification(state: EventAgentState) -> EventAgentState:
        round_number = state["verification_round"] + 1
        requested = state["pending_tasks"][: configured_limits.max_requests_per_round]
        previous = list(state["observations"])
        remaining_budget = max(
            configured_limits.max_total_tool_calls - state["tool_call_count"],
            0,
        )
        accepted_tasks: list[EventVerificationTask] = []
        skipped = [
            task.task_id
            for task in state["pending_tasks"][configured_limits.max_requests_per_round :]
        ]
        errors: list[str] = []
        new_observations: list[EventToolObservation] = []
        invocation_count = 0
        for position, task in enumerate(requested, start=1):
            calls = _planned_task_calls(task, state)
            prior_observations = [*previous, *new_observations]
            fresh_call_count = sum(
                _find_observation(prior_observations, tool_name, arguments) is None
                for _, tool_name, arguments in calls
            )
            if fresh_call_count > remaining_budget:
                skipped.append(task.task_id)
                errors.append(f"新闻事件 Tool 调用预算不足，未执行：{task.target.code}")
                continue
            observations, task_errors, invoked = await _execute_task(
                task=task,
                position=position,
                round_number=round_number,
                state=state,
                tools_by_name=tools_by_name,
                prior_observations=prior_observations,
            )
            # 任务逐个预留并在真实调用后扣减。即使共享调用第一次失败、下一任务
            # 决定重试，也会重新占用预算，实际调用数不会越过硬上限。
            if invoked > remaining_budget:  # pragma: no cover - 内部不变量保护
                raise RuntimeError("新闻事件 Tool 实际调用数突破预留预算")
            accepted_tasks.append(task)
            new_observations.extend(observations)
            errors.extend(task_errors)
            invocation_count += invoked
            remaining_budget -= invoked
        return {
            "observations": [*previous, *new_observations],
            "pending_tasks": accepted_tasks,
            "tool_call_count": state["tool_call_count"] + invocation_count,
            "verification_round": round_number,
            "budget_exhausted": state["budget_exhausted"] or bool(skipped),
            "skipped_task_ids": [*state["skipped_task_ids"], *skipped],
            "errors": [*state["errors"], *errors],
        }

    async def review_verification(state: EventAgentState) -> EventAgentState:
        if not state["pending_tasks"]:
            return {"pending_tasks": []}
        try:
            decision = await model.review_verification(
                EventReviewInput(
                    run_id=state["run_id"],
                    as_of=state["as_of"],
                    round_number=state["verification_round"],
                    tasks=tuple(state["pending_tasks"]),
                    observations=tuple(state["observations"]),
                    existing_evidence=tuple(state["evidence_drafts"]),
                )
            )
        except Exception as exc:
            return {
                "pending_tasks": [],
                "errors": [
                    *state["errors"],
                    f"新闻事件查证结果结构化审阅失败：{describe_exception(exc)}",
                ],
            }

        available = {
            item.call_id
            for item in state["observations"]
            if item.tool_name != _IDENTITY_TOOL
            and item.result.get("status") in _EVIDENCE_CITABLE_STATUSES
        }
        drafts, draft_errors = _filter_review_drafts(
            decision.evidence,
            available_call_ids=available,
            observations=state["observations"],
            snapshot_result=state.get("snapshot_result"),
            scope_target=state["target"],
        )
        next_tasks: list[EventVerificationTask] = []
        next_fingerprints: list[str] = []
        task_errors: list[str] = []
        if (
            not state["budget_exhausted"]
            and state["verification_round"] < configured_limits.max_verification_rounds
        ):
            allowed = {task.target.code: {task.target.type} for task in state["pending_tasks"]}
            request = state.get("research_request")
            next_tasks, next_fingerprints, task_errors = _build_tasks(
                decision.follow_up_requests,
                origin="FOLLOW_UP",
                round_number=state["verification_round"] + 1,
                allowed_targets=allowed,
                existing_fingerprints=set(state["seen_request_fingerprints"]),
                limit=configured_limits.max_requests_per_round,
                as_of_date=state["as_of"].date(),
                allowed_date_range=(
                    (request.time_range.start, request.time_range.end)
                    if request is not None
                    else None
                ),
            )
        return {
            "evidence_drafts": [*state["evidence_drafts"], *drafts],
            "pending_tasks": next_tasks,
            "seen_request_fingerprints": [
                *state["seen_request_fingerprints"],
                *next_fingerprints,
            ],
            "unresolved_questions": [
                *state["unresolved_questions"],
                *decision.unresolved_questions,
            ],
            "errors": [*state["errors"], *draft_errors, *task_errors],
        }

    async def finalize(state: EventAgentState) -> EventAgentState:
        observations = {item.call_id: item for item in state["observations"]}
        evidence_records: list[EvidenceRecord] = []
        rejected = 0
        final_errors = list(state["errors"])
        seen: set[tuple[str, str, str]] = set()
        for draft in state["evidence_drafts"]:
            fingerprint = (draft.target.code, draft.title, draft.description)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            record = await _materialize_evidence(
                draft,
                run_id=state["run_id"],
                as_of=state["as_of"],
                evidence_scope=_evidence_scope(state),
                observations=observations,
                snapshot_result=state.get("snapshot_result"),
                tool_context=tool_context,
            )
            if record is None:
                rejected += 1
                final_errors.append(f"证据草稿逐行引用无法核验，已拒绝：{draft.title}")
            else:
                evidence_records.append(record)

        completed_request = _complete_research_request(
            state.get("research_request"),
            evidence_records,
            state["as_of"],
            failed=bool(final_errors and not evidence_records),
            budget_exhausted=state["budget_exhausted"],
        )
        if state["budget_exhausted"]:
            stop_reason = "verification_budget_reached"
        elif final_errors and not evidence_records:
            stop_reason = "failed_without_evidence"
        elif state["verification_round"]:
            stop_reason = "verification_complete"
        else:
            stop_reason = "snapshot_evidence_complete"
        return {
            "evidence_records": evidence_records,
            "completed_research_request": completed_request,
            "errors": final_errors,
            "run_summary": EventAgentRunSummary(
                mode=state["mode"],
                snapshot_status=(
                    state["snapshot_result"].get("status") if state.get("snapshot_result") else None
                ),
                verification_rounds=state["verification_round"],
                tool_call_count=state["tool_call_count"],
                accepted_evidence_count=len(evidence_records),
                rejected_evidence_count=rejected,
                budget_exhausted=state["budget_exhausted"],
                skipped_task_ids=tuple(state["skipped_task_ids"]),
                unresolved_questions=tuple(state["unresolved_questions"]),
                stop_reason=stop_reason,
            ),
        }

    builder = StateGraph(EventAgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("acquire_daily_snapshot", acquire_daily_snapshot)
    builder.add_node("analyze_daily", analyze_daily)
    builder.add_node("plan_targeted", plan_targeted)
    builder.add_node("execute_verification", execute_verification)
    builder.add_node("review_verification", review_verification)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare",
        lambda state: state["mode"].value,
        {
            EventResearchMode.DAILY.value: "acquire_daily_snapshot",
            EventResearchMode.VERIFICATION.value: "plan_targeted",
        },
    )
    builder.add_conditional_edges(
        "acquire_daily_snapshot",
        _route_after_snapshot,
        {"analyze": "analyze_daily", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "analyze_daily",
        _route_after_plan,
        {"verify": "execute_verification", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "plan_targeted",
        _route_after_plan,
        {"verify": "execute_verification", "finalize": "finalize"},
    )
    builder.add_edge("execute_verification", "review_verification")
    builder.add_conditional_edges(
        "review_verification",
        lambda state: (
            "verify"
            if state.get("pending_tasks")
            and not state["budget_exhausted"]
            and state["tool_call_count"] < configured_limits.max_total_tool_calls
            else "finalize"
        ),
        {"verify": "execute_verification", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)
    return builder.compile()


def _route_after_snapshot(state: EventAgentState) -> Literal["analyze", "finalize"]:
    result = state.get("snapshot_result") or {}
    return "analyze" if result.get("status") in {"ok", "partial"} else "finalize"


def _route_after_plan(state: EventAgentState) -> Literal["verify", "finalize"]:
    return "verify" if state.get("pending_tasks") else "finalize"


def _require_tools(tools: dict[str, BaseTool]) -> None:
    required = {_SNAPSHOT_TOOL, _IDENTITY_TOOL, *_TOOL_BY_CHECK.values()}
    missing = sorted(required - tools.keys())
    if missing:
        raise ValueError(f"新闻事件 Agent 缺少 Tool：{', '.join(missing)}")


def _validate_external_request(
    request: ResearchRequest | None,
    run_id: str,
    as_of: datetime,
) -> None:
    if request is None:
        raise ValueError("VERIFICATION 模式必须提供 research_request")
    if request.run_id != run_id:
        raise ValueError("ResearchRequest.run_id 与当前运行不一致")
    if request.assigned_domain is not EvidenceDomain.EVENT:
        raise ValueError("新闻事件 Agent 只能执行 assigned_domain=EVENT 的请求")
    if request.target.type is not TargetType.STOCK:
        raise ValueError("新闻事件定向查证第一版只接受 STOCK 请求")
    if request.status not in {ResearchRequestStatus.PENDING, ResearchRequestStatus.RUNNING}:
        raise ValueError("只能执行 PENDING 或 RUNNING 的 ResearchRequest")
    if request.time_range.end > as_of.date():
        raise ValueError("ResearchRequest 时间范围不能晚于 as_of")


def _daily_snapshot_permissions(
    result: dict[str, Any],
    scope_target: ResearchTarget,
) -> tuple[dict[str, set[TargetType]], dict[str, set[TargetType]]]:
    snapshot = result.get("snapshot") or {}
    evidence: dict[str, set[TargetType]] = {"A_SHARE": {TargetType.MARKET}}
    if scope_target.type is TargetType.MARKET:
        evidence.setdefault(scope_target.code, set()).add(TargetType.MARKET)
    stocks: dict[str, set[TargetType]] = {}

    for news in snapshot.get("market_news") or []:
        if not isinstance(news, dict):
            continue
        for related in news.get("related_stocks") or []:
            code = related.get("ts_code") if isinstance(related, dict) else None
            if isinstance(code, str) and code:
                evidence.setdefault(code, set()).add(TargetType.STOCK)
                stocks.setdefault(code, set()).add(TargetType.STOCK)
    for group in ("announcements", "sell_side_reports", "broker_recommendations"):
        for item in snapshot.get(group) or []:
            if not isinstance(item, dict):
                continue
            code = item.get("ts_code")
            if isinstance(code, str) and code:
                evidence.setdefault(code, set()).add(TargetType.STOCK)
                stocks.setdefault(code, set()).add(TargetType.STOCK)
    return evidence, stocks


def _snapshot_record_catalog(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把快照候选转成逐行许可表；没有明确股票映射就没有 STOCK 权限。"""

    snapshot = result.get("snapshot") or {}
    catalog: dict[str, dict[str, Any]] = {}
    for news in snapshot.get("market_news") or []:
        if not isinstance(news, dict) or not news.get("citable"):
            continue
        for key in news.get("record_keys") or []:
            if key:
                supporting_stocks = [
                    item
                    for item in news.get("related_stocks") or []
                    if isinstance(item, dict)
                    and item.get("ts_code")
                    and str(key)
                    in {str(value) for value in item.get("supporting_record_keys") or []}
                ]
                stock_codes = {str(item["ts_code"]) for item in supporting_stocks}
                catalog[str(key)] = {
                    "target_codes": {"A_SHARE", *stock_codes},
                    "published_at": news.get("published_at"),
                    "url": next(iter(news.get("source_urls") or []), None),
                    "provider": "+".join(news.get("source_names") or ["PUBLIC_NEWS"]),
                    "interface": _SNAPSHOT_TOOL,
                    "citable": True,
                    "target_names": {
                        str(item["ts_code"]): str(
                            item.get("stock_name") or item.get("name") or item["ts_code"]
                        )
                        for item in supporting_stocks
                    },
                    "match_values": {
                        "title": news.get("title"),
                        "published_at": news.get("published_at"),
                    },
                }
    for group in ("announcements", "sell_side_reports", "broker_recommendations"):
        for item in snapshot.get(group) or []:
            if not isinstance(item, dict) or item.get("citable") is False:
                continue
            code = item.get("ts_code")
            keys = (
                item.get("supporting_record_keys")
                or item.get("record_keys")
                or [item.get("record_key")]
            )
            required_record_keys = {str(key) for key in keys if key}
            for key in keys:
                if not key:
                    continue
                catalog[str(key)] = {
                    "target_codes": ({str(code)} if code else set()),
                    "published_at": (
                        item.get("published_at")
                        or item.get("announcement_date")
                        or item.get("report_date")
                    ),
                    "url": item.get("source_url") or item.get("url"),
                    "provider": str(
                        item.get("org_name")
                        or item.get("broker")
                        or item.get("source_name")
                        or "PUBLIC_DISCLOSURE"
                    ),
                    "interface": _SNAPSHOT_TOOL,
                    "citable": True,
                    "required_record_keys": required_record_keys,
                    "target_names": (
                        {str(code): str(item.get("stock_name") or item.get("name") or code)}
                        if code
                        else {}
                    ),
                    "match_values": {
                        field: item.get(field)
                        for field in (
                            "ts_code",
                            "title",
                            "announcement_date",
                            "report_date",
                            "report_title",
                            "org_name",
                            "author_name",
                            "quarter",
                            "month",
                            "broker",
                        )
                        if item.get(field) is not None
                    },
                }
    return catalog


def _bundle_record_catalog(
    bundle: Any,
    candidate_catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """把候选 key 反查到 DataStore 中的真实原始行及其逐行 trace。"""

    located: dict[str, dict[str, Any]] = {}
    for label, dataset in bundle.datasets.items():
        for row, trace in zip(dataset.items, dataset.item_traces, strict=True):
            derived_key = None
            if dataset.api_name == "report_rc":
                derived_key = event_report_record_key(row)
            elif dataset.api_name == "broker_recommend":
                derived_key = event_broker_recommendation_record_key(row)
            explicit_key = row.get("record_key")
            matching_keys: list[str] = []
            if derived_key is not None:
                if derived_key in candidate_catalog:
                    matching_keys.append(derived_key)
            elif isinstance(explicit_key, str) and explicit_key in candidate_catalog:
                matching_keys.append(explicit_key)
            else:
                for key, meta in candidate_catalog.items():
                    if key in located:
                        continue
                    match_values = meta.get("match_values")
                    if isinstance(match_values, dict) and _row_matches_candidate(
                        row,
                        match_values,
                    ):
                        matching_keys.append(key)
            for key in matching_keys:
                candidate = candidate_catalog[key]
                located[key] = {
                    **candidate,
                    "provider": trace.provider.value,
                    "interface": f"{_SNAPSHOT_TOOL}:{dataset.api_name}:{label}",
                    "fetched_at": trace.fetched_at,
                    "data_as_of": dataset.data_as_of,
                    "published_at": (
                        row.get("published_at")
                        or row.get("announcement_date")
                        or row.get("report_date")
                        or candidate.get("published_at")
                    ),
                    "url": row.get("source_url") or row.get("url") or candidate.get("url"),
                    "complete": dataset.complete,
                    "citable": bool(row.get("citable", candidate.get("citable", True))),
                }
    return located


def _row_matches_candidate(row: dict[str, Any], values: dict[str, Any]) -> bool:
    """只有至少两个稳定字段一致时，才把无原生 key 的行定位到快照候选。"""

    matched = 0
    for field, expected in values.items():
        if expected is None:
            continue
        actual = row.get(field)
        if field in {"published_at", "announcement_date", "report_date"}:
            expected_date = _coerce_date(expected)
            actual_date = _coerce_date(actual)
            if expected_date is not None and expected_date == actual_date:
                matched += 1
            continue
        if str(actual or "").strip() == str(expected).strip():
            matched += 1
    required = 1 if "record_key" in row else 2
    return matched >= required


def _filter_daily_drafts(
    drafts: tuple[EventEvidenceDraft, ...],
    *,
    allowed_call_id: str,
    allowed_targets: dict[str, set[TargetType]],
    record_catalog: dict[str, dict[str, Any]],
) -> tuple[list[EventEvidenceDraft], list[str]]:
    accepted: list[EventEvidenceDraft] = []
    errors: list[str] = []
    for draft in drafts:
        if set(draft.source_call_ids) != {allowed_call_id}:
            errors.append(f"快照证据引用了非快照调用：{draft.title}")
            continue
        if draft.target.type not in allowed_targets.get(draft.target.code, set()):
            errors.append(f"快照证据使用了未授权标的：{draft.target.code}")
            continue
        if not _record_keys_support_target(
            draft.source_record_keys,
            draft.target,
            record_catalog,
        ):
            errors.append(f"快照证据 record_key 不存在、不可引用或不支持标的：{draft.title}")
            continue
        accepted.append(_canonicalize_snapshot_draft(draft, record_catalog))
    return accepted, errors


def _canonicalize_snapshot_draft(
    draft: EventEvidenceDraft,
    catalog: dict[str, dict[str, Any]],
) -> EventEvidenceDraft:
    """模型只选择 code；展示名必须取自被引用快照行，不能由模型自由改写。"""

    if draft.target.type is not TargetType.STOCK:
        return draft
    names = [
        meta.get("target_names", {}).get(draft.target.code)
        for key in draft.source_record_keys
        if (meta := catalog.get(key)) is not None
    ]
    canonical = next((str(name).strip() for name in names if name), None)
    if canonical is None:
        return draft
    return draft.model_copy(
        update={
            "target": ResearchTarget(
                type=TargetType.STOCK,
                code=draft.target.code,
                name=canonical,
            )
        }
    )


def _filter_review_drafts(
    drafts: tuple[EventEvidenceDraft, ...],
    *,
    available_call_ids: set[str],
    observations: list[EventToolObservation],
    snapshot_result: dict[str, Any] | None,
    scope_target: ResearchTarget,
) -> tuple[list[EventEvidenceDraft], list[str]]:
    by_call = {item.call_id: item for item in observations}
    snapshot_catalog = _snapshot_record_catalog(snapshot_result or {})
    snapshot_targets, _ = _daily_snapshot_permissions(snapshot_result or {}, scope_target)
    accepted: list[EventEvidenceDraft] = []
    errors: list[str] = []
    for draft in drafts:
        if not set(draft.source_call_ids).issubset(available_call_ids):
            errors.append(f"证据草稿引用了不存在或失败的调用：{draft.title}")
            continue
        record_catalog: dict[str, dict[str, Any]] = {}
        valid = True
        for call_id in draft.source_call_ids:
            observation = by_call[call_id]
            if observation.tool_name == _SNAPSHOT_TOOL:
                valid = draft.target.type in snapshot_targets.get(draft.target.code, set())
                record_catalog.update(snapshot_catalog)
            else:
                valid = _observation_supports_target(observation, draft.target)
                record_catalog.update(_result_record_catalog(observation))
            if not valid:
                break
        if valid and _record_keys_support_target(
            draft.source_record_keys,
            draft.target,
            record_catalog,
        ):
            accepted.append(draft)
        else:
            errors.append(f"证据草稿逐行引用不支持标的：{draft.title}")
    return accepted, errors


def _record_keys_support_target(
    keys: tuple[str, ...],
    target: ResearchTarget,
    catalog: dict[str, dict[str, Any]],
) -> bool:
    cited_keys = set(keys)
    for key in keys:
        meta = catalog.get(key)
        if meta is None or not meta.get("citable"):
            return False
        if target.code not in meta.get("target_codes", set()):
            return False
        required_keys = set(meta.get("required_record_keys") or ())
        if not required_keys.issubset(cited_keys):
            return False
    return bool(keys)


def _build_tasks(
    requests: tuple[EventVerificationRequestDraft, ...],
    *,
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"],
    round_number: int,
    allowed_targets: dict[str, set[TargetType]],
    existing_fingerprints: set[str],
    limit: int,
    as_of_date: date,
    required_primary_target: ResearchTarget | None = None,
    allowed_date_range: tuple[date, date] | None = None,
) -> tuple[list[EventVerificationTask], list[str], list[str]]:
    tasks: list[EventVerificationTask] = []
    fingerprints: list[str] = []
    errors: list[str] = []
    for request in requests[:limit]:
        if request.target.type not in allowed_targets.get(request.target.code, set()):
            errors.append(f"拒绝未授权的新闻事件查证标的：{request.target.code}")
            continue
        if request.report_period is not None:
            report_date = _coerce_date(request.report_period)
            if report_date is None or report_date > as_of_date:
                errors.append(f"拒绝非法或未来 report_period：{request.target.code}")
                continue
            if allowed_date_range and not (
                allowed_date_range[0] <= report_date <= allowed_date_range[1]
            ):
                errors.append(f"report_period 不在 ResearchRequest 窗口内：{request.target.code}")
                continue
        fingerprint = _request_fingerprint(request)
        if fingerprint in existing_fingerprints or fingerprint in fingerprints:
            errors.append(f"拒绝重复新闻事件查证：{request.target.code}")
            continue
        task_id = f"evr_r{round_number}_{len(tasks) + 1}_{fingerprint[:8]}"
        tasks.append(
            EventVerificationTask(
                **request.model_dump(),
                task_id=task_id,
                origin=origin,
            )
        )
        fingerprints.append(fingerprint)
    if required_primary_target is not None and not any(
        _same_target_identity(task.target, required_primary_target) for task in tasks
    ):
        errors.append("定向查证计划没有研究 ResearchRequest.target，已拒绝该计划")
        return [], [], errors
    return tasks, fingerprints, errors


def _planned_task_calls(
    task: EventVerificationTask,
    state: EventAgentState,
) -> list[tuple[str, str, dict[str, Any]]]:
    end_date = state["as_of"].date()
    start_date = end_date - timedelta(days=task.lookback_days - 1)
    request = state.get("research_request")
    if request is not None:
        start_date = max(start_date, request.time_range.start)
        end_date = min(end_date, request.time_range.end)
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("identity", _IDENTITY_TOOL, {"ts_code": task.target.code, "list_status": "L"})
    ]
    for check in task.checks:
        tool_name = _TOOL_BY_CHECK[check]
        if check is EventCheck.NEWS_DISCLOSURES:
            news_start_date = max(start_date, end_date - timedelta(days=30))
            arguments: dict[str, Any] = {
                "ts_code": task.target.code,
                "start_date": news_start_date,
                "end_date": end_date,
                "announcement_category": task.announcement_category,
            }
        elif check is EventCheck.SELL_SIDE_RESEARCH:
            arguments = {
                "ts_code": task.target.code,
                "start_date": start_date,
                "end_date": end_date,
            }
        elif check is EventCheck.CORPORATE_ACTIONS:
            arguments = {
                "ts_code": task.target.code,
                "start_date": start_date,
                "end_date": end_date,
            }
        else:
            arguments = {"ts_code": task.target.code, "period": task.report_period}
        calls.append((check.value.lower(), tool_name, arguments))
    return calls


async def _execute_task(
    *,
    task: EventVerificationTask,
    position: int,
    round_number: int,
    state: EventAgentState,
    tools_by_name: dict[str, BaseTool],
    prior_observations: list[EventToolObservation],
) -> tuple[list[EventToolObservation], list[str], int]:
    observations: list[EventToolObservation] = []
    invoked = 0
    planned = _planned_task_calls(task, state)
    _, _, identity_args = planned[0]
    identity = _find_observation(prior_observations, _IDENTITY_TOOL, identity_args)
    if identity is None:
        identity = await _call_observation(
            tools_by_name[_IDENTITY_TOOL],
            call_id=f"ec_r{round_number}_{position}_identity",
            task_id=task.task_id,
            arguments=identity_args,
        )
        observations.append(identity)
        invoked += 1
    if not _identity_confirms_target(identity, task.target.code):
        return observations, [f"股票身份核对失败，已停止查证：{task.target.code}"], invoked

    for suffix, tool_name, arguments in planned[1:]:
        existing = _find_observation(
            [*prior_observations, *observations],
            tool_name,
            arguments,
        )
        if existing is not None:
            continue
        observations.append(
            await _call_observation(
                tools_by_name[tool_name],
                call_id=f"ec_r{round_number}_{position}_{suffix}",
                task_id=task.task_id,
                arguments=arguments,
            )
        )
        invoked += 1
    return observations, [], invoked


async def _call_observation(
    tool: BaseTool,
    *,
    call_id: str,
    task_id: str,
    arguments: dict[str, Any],
) -> EventToolObservation:
    result = await _invoke_tool(tool, arguments)
    if tool.name != _IDENTITY_TOOL:
        result = _ensure_result_record_keys(result)
    return EventToolObservation(
        call_id=call_id,
        task_id=task_id,
        tool_name=tool.name,
        arguments=arguments,
        result=result,
    )


async def _invoke_tool(tool: BaseTool, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await tool.ainvoke(arguments)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "tool_name": tool.name,
            "status": "error",
            "issues": [{"code": "INTERNAL_ERROR", "message": type(exc).__name__}],
            "complete": False,
        }
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {
        "tool_name": tool.name,
        "status": "error",
        "issues": [{"code": "INTERNAL_ERROR", "message": "Tool 返回值不是对象"}],
        "complete": False,
    }


def _ensure_result_record_keys(result: dict[str, Any]) -> dict[str, Any]:
    """为没有原生 record_key 的 Tushare 行补稳定键，让 LLM 可以逐行引用。"""

    copied = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    for dataset in copied.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        label = str(dataset.get("label") or "dataset")
        api_name = str(dataset.get("api_name") or "api")
        for index, row in enumerate(dataset.get("rows") or []):
            if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                continue
            data = row["data"]
            if data.get("record_key"):
                continue
            payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
            digest = sha256(f"{api_name}:{label}:{index}:{payload}".encode()).hexdigest()[:24]
            data["record_key"] = f"{api_name}:{digest}"
            data.setdefault("citable", True)
    return copied


def _find_observation(
    observations: list[EventToolObservation],
    tool_name: str,
    arguments: dict[str, Any],
) -> EventToolObservation | None:
    fingerprint = _tool_fingerprint(tool_name, arguments)
    return next(
        (
            item
            for item in observations
            if _tool_fingerprint(item.tool_name, item.arguments) == fingerprint
            and item.result.get("status") in _REUSABLE_STATUSES
        ),
        None,
    )


def _request_fingerprint(request: EventVerificationRequestDraft) -> str:
    payload = request.model_dump(
        mode="json",
        exclude={"question", "requested_evidence", "reason", "priority"},
    )
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _tool_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    return sha256(
        json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def _result_record_catalog(observation: EventToolObservation) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for dataset in observation.result.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        label = str(dataset.get("label") or "dataset")
        api_name = str(dataset.get("api_name") or observation.tool_name)
        for row in dataset.get("rows") or []:
            if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                continue
            data = row["data"]
            key = data.get("record_key")
            if not key:
                continue
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            code = data.get("ts_code") or observation.arguments.get("ts_code")
            catalog[str(key)] = {
                "target_codes": ({str(code)} if code else set()),
                "published_at": (
                    data.get("published_at")
                    or data.get("announcement_date")
                    or data.get("report_date")
                    or data.get("ann_date")
                ),
                "url": data.get("source_url") or data.get("url"),
                "provider": str(source.get("provider") or "UNKNOWN"),
                "interface": f"{observation.tool_name}:{api_name}:{label}",
                "fetched_at": source.get("fetched_at"),
                "data_as_of": dataset.get("data_as_of"),
                "citable": bool(data.get("citable", True)),
                "context_ref": observation.result.get("context_ref"),
                "complete": bool(dataset.get("complete")),
            }
    return catalog


async def _materialize_evidence(
    draft: EventEvidenceDraft,
    *,
    run_id: str,
    as_of: datetime,
    evidence_scope: str,
    observations: dict[str, EventToolObservation],
    snapshot_result: dict[str, Any] | None,
    tool_context: ResearchToolContext,
) -> EvidenceRecord | None:
    referenced = [observations.get(call_id) for call_id in draft.source_call_ids]
    if any(item is None for item in referenced):
        return None
    typed = [item for item in referenced if item is not None]
    if any(
        item.tool_name == _IDENTITY_TOOL
        or item.result.get("status") not in _EVIDENCE_CITABLE_STATUSES
        for item in typed
    ):
        return None

    catalog: dict[str, dict[str, Any]] = {}
    raw_payload_refs: list[str] = []
    all_complete = all(
        item.result.get("status") == "ok" and item.result.get("complete") is True for item in typed
    )
    snapshot_catalog = _snapshot_record_catalog(snapshot_result or {})
    snapshot_context_ref: str | None = None
    for observation in typed:
        if observation.tool_name == _SNAPSHOT_TOOL:
            snapshot_context_ref = observation.result.get("context_ref")
            if not isinstance(snapshot_context_ref, str):
                return None
            try:
                bundle = await tool_context.data_store.get(run_id, snapshot_context_ref)
            except ResearchDataStoreError:
                return None
            pages = [page for dataset in bundle.datasets.values() for page in dataset.pages]
            if not pages:
                return None
            raw_payload_refs.append(snapshot_context_ref)
            catalog.update(_bundle_record_catalog(bundle, snapshot_catalog))
        else:
            catalog.update(_result_record_catalog(observation))
            context_ref = observation.result.get("context_ref")
            if isinstance(context_ref, str):
                raw_payload_refs.append(context_ref)

    if not _record_keys_support_target(draft.source_record_keys, draft.target, catalog):
        return None
    source_refs: list[SourceReference] = []
    for key in draft.source_record_keys:
        meta = catalog[key]
        published_at = _coerce_datetime(meta.get("published_at"))
        fetched_at = _coerce_datetime(meta.get("fetched_at"))
        if published_at is None and fetched_at is None:
            return None
        source_refs.append(
            SourceReference(
                provider=str(meta.get("provider") or "UNKNOWN"),
                interface=str(meta.get("interface") or _SNAPSHOT_TOOL),
                record_key=key,
                published_at=published_at,
                fetched_at=fetched_at,
                data_as_of=_coerce_date(meta.get("data_as_of")),
                url=(str(meta["url"]) if meta.get("url") else None),
            )
        )
        all_complete = all_complete and bool(meta.get("complete", True))

    target = draft.target
    if target.type is TargetType.STOCK:
        canonical = _canonical_stock_name(observations.values(), target.code)
        if canonical:
            target = ResearchTarget(type=TargetType.STOCK, code=target.code, name=canonical)
    description = draft.description
    automatic_limitations: list[str] = []
    for observation in typed:
        status = observation.result.get("status")
        if status == "partial":
            missing_labels = sorted(
                {
                    str(issue.get("dataset_label"))
                    for issue in observation.result.get("issues") or []
                    if isinstance(issue, dict) and issue.get("dataset_label")
                }
            )
            suffix = f"；缺失数据集：{', '.join(missing_labels)}" if missing_labels else ""
            automatic_limitations.append(f"调用 {observation.tool_name} 仅返回 partial{suffix}")
        if observation.result.get("complete") is False:
            automatic_limitations.append(f"调用 {observation.tool_name} 未声明结果完整")
    limitations = tuple(dict.fromkeys([*draft.limitations, *automatic_limitations]))
    if limitations:
        description = f"{description}\n限制：{'；'.join(limitations)}"
    run_suffix = re.sub(r"[^A-Za-z0-9_]", "_", run_id.removeprefix("run_"))
    scope_suffix = re.sub(r"[^A-Za-z0-9_]", "_", evidence_scope)
    fingerprint = sha256(
        json.dumps(
            {
                "scope": evidence_scope,
                "draft": draft.model_dump(mode="json"),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return EvidenceRecord(
        evidence_id=f"ev_{run_suffix}_{scope_suffix}_{fingerprint[:16]}",
        run_id=run_id,
        target=target,
        domain=EvidenceDomain.EVENT,
        as_of=as_of,
        title=draft.title,
        description=description,
        source_refs=source_refs,
        verification_status=(
            VerificationStatus.VERIFIED if all_complete else VerificationStatus.UNVERIFIED
        ),
        tags=list(draft.tags),
        raw_payload_ref=raw_payload_refs[0] if raw_payload_refs else None,
        collected_by=_AGENT_NAME,
        created_at=as_of,
    )


def _observation_supports_target(
    observation: EventToolObservation,
    target: ResearchTarget,
) -> bool:
    if (
        observation.tool_name == _IDENTITY_TOOL
        or target.type is not TargetType.STOCK
        or observation.task_id is None
        or observation.arguments.get("ts_code") != target.code
    ):
        return False
    codes = _returned_stock_codes(observation.result)
    if observation.result.get("status") == "empty":
        return not codes
    return bool(codes) and codes == {target.code}


def _returned_stock_codes(result: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for dataset in result.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        for row in dataset.get("rows") or []:
            data = row.get("data") if isinstance(row, dict) else None
            if isinstance(data, dict) and isinstance(data.get("ts_code"), str):
                codes.add(data["ts_code"])
    return codes


def _identity_confirms_target(observation: EventToolObservation, target_code: str) -> bool:
    if observation.result.get("status") not in {"ok", "partial"}:
        return False
    for dataset in observation.result.get("datasets") or []:
        if not isinstance(dataset, dict) or dataset.get("label") != "stock_basic":
            continue
        codes = {
            row["data"].get("ts_code")
            for row in dataset.get("rows") or []
            if isinstance(row, dict) and isinstance(row.get("data"), dict)
        }
        return codes == {target_code}
    return False


def _canonical_stock_name(
    observations: Iterable[EventToolObservation],
    target_code: str,
) -> str | None:
    for observation in observations:
        if observation.tool_name != _IDENTITY_TOOL:
            continue
        if not _identity_confirms_target(observation, target_code):
            continue
        for dataset in observation.result.get("datasets") or []:
            if not isinstance(dataset, dict) or dataset.get("label") != "stock_basic":
                continue
            for row in dataset.get("rows") or []:
                data = row.get("data") if isinstance(row, dict) else None
                if isinstance(data, dict) and data.get("ts_code") == target_code:
                    name = data.get("name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
    return None


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    compact = value.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        return None
    try:
        return date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))
    except ValueError:
        return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.astimezone(_SHANGHAI)
            if value.tzinfo is not None and value.utcoffset() is not None
            else value.replace(tzinfo=_SHANGHAI)
        )
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=_SHANGHAI)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed_date = _coerce_date(value)
        return (
            datetime.combine(parsed_date, time.min, tzinfo=_SHANGHAI)
            if parsed_date is not None
            else None
        )
    return (
        parsed.astimezone(_SHANGHAI)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None
        else parsed.replace(tzinfo=_SHANGHAI)
    )


def _same_target_identity(left: ResearchTarget, right: ResearchTarget) -> bool:
    return left.type is right.type and left.code == right.code


def _evidence_scope(state: EventAgentState) -> str:
    request = state.get("research_request")
    return request.request_id if request is not None else state["mode"].value.lower()


def _complete_research_request(
    request: ResearchRequest | None,
    evidence: list[EvidenceRecord],
    completed_at: datetime,
    *,
    failed: bool,
    budget_exhausted: bool,
) -> ResearchRequest | None:
    if request is None:
        return None
    if budget_exhausted:
        status = ResearchRequestStatus.CANCELLED_BY_BUDGET
    elif evidence:
        status = ResearchRequestStatus.COMPLETED
    elif failed:
        status = ResearchRequestStatus.FAILED
    else:
        status = ResearchRequestStatus.NO_NEW_EVIDENCE
    return ResearchRequest.model_validate(
        {
            **request.model_dump(),
            "status": status,
            "result_evidence_ids": [record.evidence_id for record in evidence],
            "completed_at": completed_at,
        }
    )
