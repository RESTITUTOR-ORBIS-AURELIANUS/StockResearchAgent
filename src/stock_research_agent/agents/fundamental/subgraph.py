"""FundamentalResearchAnalyst 的每日模式与定向查证 LangGraph 子图。"""

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import date, timedelta
from hashlib import sha256
from typing import Any, Literal

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.fundamental.model import FundamentalReasoningModel
from stock_research_agent.agents.fundamental.models import (
    DailyFundamentalInput,
    FundamentalAgentLimits,
    FundamentalAgentRunSummary,
    FundamentalCheck,
    FundamentalEvidenceDraft,
    FundamentalResearchMode,
    FundamentalReviewInput,
    FundamentalToolObservation,
    FundamentalVerificationRequestDraft,
    FundamentalVerificationTask,
    TargetedFundamentalInput,
)
from stock_research_agent.agents.fundamental.state import FundamentalAgentState
from stock_research_agent.domain import (
    EvidenceRecord,
    ResearchRequest,
    ResearchTarget,
    SourceReference,
    TimeRange,
)
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.llm import describe_exception
from stock_research_agent.research_data import ResearchDataStoreError
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

_AGENT_NAME = "FundamentalResearchAnalyst"
_SNAPSHOT_TOOL = "get_daily_fundamental_snapshot"
_IDENTITY_TOOL = "resolve_stock_identity"
_TOOL_BY_CHECK = {
    FundamentalCheck.FINANCIAL_STATEMENTS: "get_financial_statements",
    FundamentalCheck.FINANCIAL_QUALITY: "get_financial_quality",
    FundamentalCheck.EARNINGS_DISCLOSURE: "get_earnings_and_disclosure",
    FundamentalCheck.VALUATION_HISTORY: "get_valuation_context",
    FundamentalCheck.DIVIDEND_OWNERSHIP: "get_dividend_and_ownership_context",
    FundamentalCheck.PLEDGE_RISK: "get_pledge_risk_context",
}
_EVIDENCE_CITABLE_STATUSES = {"ok", "partial", "empty"}
_REUSABLE_STATUSES = {"ok", "partial", "empty"}


def build_fundamental_agent_graph(
    *,
    model: FundamentalReasoningModel,
    tool_context: ResearchToolContext,
    limits: FundamentalAgentLimits | None = None,
    tools: tuple[BaseTool, ...] | None = None,
):
    """构建可独立运行、也可嵌入主图的基本面研究子图。"""

    configured_limits = limits or FundamentalAgentLimits()
    fundamental_tools = tools or build_agent_tool_registry(tool_context).fundamental
    tools_by_name = {tool.name: tool for tool in fundamental_tools}
    _require_tools(tools_by_name)

    async def prepare(state: FundamentalAgentState) -> FundamentalAgentState:
        run_id = state.get("run_id")
        target = state.get("target")
        as_of = state.get("as_of")
        if not run_id or target is None or as_of is None:
            raise ValueError("基本面 Agent 必须提供 run_id、target 和 as_of")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("基本面 Agent 的 as_of 必须包含时区")
        if run_id != tool_context.run_id or as_of != tool_context.as_of:
            raise ValueError("基本面 Agent 输入必须与 ToolContext 的 run_id/as_of 一致")

        mode = FundamentalResearchMode(state.get("mode", FundamentalResearchMode.DAILY))
        research_request = state.get("research_request")
        if mode is FundamentalResearchMode.VERIFICATION:
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

    async def acquire_daily_snapshot(
        state: FundamentalAgentState,
    ) -> FundamentalAgentState:
        arguments = {
            "candidate_count": configured_limits.daily_candidate_count,
            "announcement_lookback_days": configured_limits.announcement_lookback_days,
        }
        result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], arguments)
        observations = [
            FundamentalToolObservation(
                call_id="fc_daily_snapshot_1",
                tool_name=_SNAPSHOT_TOOL,
                arguments=arguments,
                result=result,
            )
        ]
        call_count = 1

        retry_needed = (
            result.get("status") == "too_large" and configured_limits.daily_candidate_count > 3
        )
        retry_allowed = call_count < configured_limits.max_total_tool_calls
        if retry_needed and retry_allowed:
            retry_arguments = {
                "candidate_count": 3,
                "announcement_lookback_days": configured_limits.announcement_lookback_days,
            }
            result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], retry_arguments)
            observations.append(
                FundamentalToolObservation(
                    call_id="fc_daily_snapshot_2",
                    tool_name=_SNAPSHOT_TOOL,
                    arguments=retry_arguments,
                    result=result,
                )
            )
            call_count += 1

        errors: list[str] = []
        if result.get("status") not in {"ok", "partial"} or result.get("snapshot") is None:
            errors.append(f"每日基本面快照不可用：status={result.get('status', 'unknown')}")
        return {
            "snapshot_result": result,
            "observations": observations,
            "tool_call_count": call_count,
            "budget_exhausted": retry_needed and not retry_allowed,
            "errors": errors,
        }

    async def analyze_daily(state: FundamentalAgentState) -> FundamentalAgentState:
        snapshot_result = state["snapshot_result"]
        snapshot_observation = state["observations"][-1]
        try:
            decision = await model.analyze_daily(
                DailyFundamentalInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    snapshot_call_id=snapshot_observation.call_id,
                    snapshot_result=snapshot_result,
                )
            )
        except Exception as exc:
            return {"errors": [f"每日基本面结构化分析失败：{describe_exception(exc)}"]}

        allowed_evidence, allowed_stocks = _daily_snapshot_permissions(
            snapshot_result,
            state["target"],
        )
        drafts, draft_errors = _filter_daily_drafts(
            decision.snapshot_evidence,
            allowed_call_id=snapshot_observation.call_id,
            allowed_targets=allowed_evidence,
        )
        tasks, fingerprints, task_errors = _build_tasks(
            decision.verification_requests,
            origin="DAILY",
            round_number=1,
            allowed_targets=allowed_stocks,
            existing_fingerprints=set(),
            limit=configured_limits.max_requests_per_round,
            as_of_date=state["as_of"].date(),
            daily_report_period=_snapshot_period(snapshot_result, "report_period"),
            daily_comparison_period=_snapshot_period(snapshot_result, "comparison_period"),
        )
        return {
            "evidence_drafts": drafts,
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": [*draft_errors, *task_errors],
        }

    async def plan_targeted(state: FundamentalAgentState) -> FundamentalAgentState:
        research_request = state["research_request"]
        try:
            plan = await model.plan_targeted(
                TargetedFundamentalInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    research_request=research_request,
                )
            )
        except Exception as exc:
            return {"errors": [f"定向基本面计划生成失败：{describe_exception(exc)}"]}

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
            allowed_report_range=research_request.time_range,
        )
        return {
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": task_errors,
        }

    async def execute_verification(
        state: FundamentalAgentState,
    ) -> FundamentalAgentState:
        round_number = state["verification_round"] + 1
        requested_tasks = state["pending_tasks"][: configured_limits.max_requests_per_round]
        previous_observations = list(state["observations"])
        remaining_budget = max(
            configured_limits.max_total_tool_calls - state["tool_call_count"],
            0,
        )
        reservable_calls = remaining_budget
        tasks: list[FundamentalVerificationTask] = []
        skipped_task_ids = [
            task.task_id
            for task in state["pending_tasks"][configured_limits.max_requests_per_round :]
        ]
        reservation_errors: list[str] = []
        reserved_fingerprints = {
            _tool_fingerprint(item.tool_name, item.arguments)
            for item in previous_observations
            if item.result.get("status") in _REUSABLE_STATUSES
        }
        for task in requested_tasks:
            task_fingerprints = {
                _tool_fingerprint(tool_name, arguments)
                for _, tool_name, arguments in _planned_task_calls(task, state)
            }
            new_fingerprints = task_fingerprints - reserved_fingerprints
            estimated_calls = len(new_fingerprints)
            if estimated_calls > reservable_calls:
                skipped_task_ids.append(task.task_id)
                reservation_errors.append(f"基本面 Tool 调用预算不足，未执行：{task.target.code}")
                continue
            tasks.append(task)
            reservable_calls -= estimated_calls
            reserved_fingerprints.update(new_fingerprints)

        new_observations: list[FundamentalToolObservation] = []
        errors = list(reservation_errors)
        actual_invocations = 0
        for position, task in enumerate(tasks, start=1):
            observations, task_errors, invocation_count = await _execute_task(
                task=task,
                position=position,
                round_number=round_number,
                state=state,
                tools_by_name=tools_by_name,
                prior_observations=[*previous_observations, *new_observations],
            )
            new_observations.extend(observations)
            errors.extend(task_errors)
            actual_invocations += invocation_count

        return {
            "observations": [*previous_observations, *new_observations],
            "pending_tasks": tasks,
            "tool_call_count": state["tool_call_count"] + actual_invocations,
            "verification_round": round_number,
            "budget_exhausted": state["budget_exhausted"] or bool(skipped_task_ids),
            "skipped_task_ids": [*state["skipped_task_ids"], *skipped_task_ids],
            "errors": [*state["errors"], *errors],
        }

    async def review_verification(
        state: FundamentalAgentState,
    ) -> FundamentalAgentState:
        if not state["pending_tasks"]:
            return {"pending_tasks": []}
        try:
            decision = await model.review_verification(
                FundamentalReviewInput(
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
                    f"基本面查证结果结构化审阅失败：{describe_exception(exc)}",
                ],
            }

        available_call_ids = {
            item.call_id
            for item in state["observations"]
            if item.tool_name != _IDENTITY_TOOL
            and item.result.get("status") in _EVIDENCE_CITABLE_STATUSES
        }
        drafts, draft_errors = _filter_review_drafts(
            decision.evidence,
            available_call_ids=available_call_ids,
            observations=state["observations"],
            snapshot_result=state.get("snapshot_result"),
            scope_target=state["target"],
        )

        next_tasks: list[FundamentalVerificationTask] = []
        next_fingerprints: list[str] = []
        task_errors: list[str] = []
        if (
            not state["budget_exhausted"]
            and state["verification_round"] < configured_limits.max_verification_rounds
        ):
            allowed = {task.target.code: {task.target.type} for task in state["pending_tasks"]}
            next_tasks, next_fingerprints, task_errors = _build_tasks(
                decision.follow_up_requests,
                origin="FOLLOW_UP",
                round_number=state["verification_round"] + 1,
                allowed_targets=allowed,
                existing_fingerprints=set(state["seen_request_fingerprints"]),
                limit=configured_limits.max_requests_per_round,
                as_of_date=state["as_of"].date(),
                daily_report_period=(
                    _snapshot_period(state.get("snapshot_result"), "report_period")
                    if state.get("research_request") is None
                    else None
                ),
                daily_comparison_period=(
                    _snapshot_period(state.get("snapshot_result"), "comparison_period")
                    if state.get("research_request") is None
                    else None
                ),
                allowed_report_range=(
                    state["research_request"].time_range
                    if state.get("research_request") is not None
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

    async def finalize(state: FundamentalAgentState) -> FundamentalAgentState:
        observations = {item.call_id: item for item in state["observations"]}
        evidence_records: list[EvidenceRecord] = []
        rejected = 0
        final_errors = list(state["errors"])
        seen_evidence: set[tuple[str, str, str]] = set()
        for draft in state["evidence_drafts"]:
            fingerprint = (draft.target.code, draft.title, draft.description)
            if fingerprint in seen_evidence:
                continue
            seen_evidence.add(fingerprint)
            record = await _materialize_evidence(
                draft,
                run_id=state["run_id"],
                as_of=state["as_of"],
                scope_target=state["target"],
                evidence_scope=_evidence_scope(state),
                observations=observations,
                snapshot_result=state.get("snapshot_result"),
                tool_context=tool_context,
            )
            if record is None:
                rejected += 1
                final_errors.append(f"证据草稿引用无法核验，已拒绝：{draft.title}")
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
            "run_summary": FundamentalAgentRunSummary(
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

    builder = StateGraph(FundamentalAgentState)
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
            FundamentalResearchMode.DAILY.value: "acquire_daily_snapshot",
            FundamentalResearchMode.VERIFICATION.value: "plan_targeted",
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


def _route_after_snapshot(state: FundamentalAgentState) -> Literal["analyze", "finalize"]:
    result = state.get("snapshot_result") or {}
    return "analyze" if result.get("status") in {"ok", "partial"} else "finalize"


def _route_after_plan(state: FundamentalAgentState) -> Literal["verify", "finalize"]:
    return "verify" if state.get("pending_tasks") else "finalize"


def _require_tools(tools: dict[str, BaseTool]) -> None:
    required = {_SNAPSHOT_TOOL, _IDENTITY_TOOL, *_TOOL_BY_CHECK.values()}
    missing = sorted(required - tools.keys())
    if missing:
        raise ValueError(f"基本面 Agent 缺少 Tool：{', '.join(missing)}")


def _validate_external_request(
    request: ResearchRequest | None,
    run_id: str,
    as_of,
) -> None:
    if request is None:
        raise ValueError("VERIFICATION 模式必须提供 research_request")
    if request.run_id != run_id:
        raise ValueError("ResearchRequest.run_id 与当前运行不一致")
    if request.assigned_domain is not EvidenceDomain.FUNDAMENTAL:
        raise ValueError("基本面 Agent 只能执行 assigned_domain=FUNDAMENTAL 的请求")
    if request.target.type is not TargetType.STOCK:
        raise ValueError("基本面定向查证第一版只接受 STOCK 请求")
    if request.status not in {ResearchRequestStatus.PENDING, ResearchRequestStatus.RUNNING}:
        raise ValueError("只能执行 PENDING 或 RUNNING 的 ResearchRequest")
    if request.time_range.end > as_of.date():
        raise ValueError("ResearchRequest 时间范围不能晚于 as_of")


def _daily_snapshot_permissions(
    result: dict[str, Any],
    scope_target: ResearchTarget,
) -> tuple[dict[str, set[TargetType]], dict[str, set[TargetType]]]:
    snapshot = result.get("snapshot") or {}
    evidence_targets: dict[str, set[TargetType]] = {"A_SHARE": {TargetType.MARKET}}
    if scope_target.type is TargetType.MARKET and scope_target.code == "A_SHARE":
        evidence_targets[scope_target.code] = {scope_target.type}
    stock_targets: dict[str, set[TargetType]] = {}

    sector_groups = snapshot.get("sector_fundamentals") or {}
    if isinstance(sector_groups, dict):
        for rows in sector_groups.values():
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = item.get("sector_code")
                if code:
                    evidence_targets.setdefault(str(code), set()).add(TargetType.SECTOR)
                representatives = item.get("representative_stocks")
                if not isinstance(representatives, list):
                    continue
                for representative in representatives:
                    if not isinstance(representative, dict):
                        continue
                    stock_code = representative.get("ts_code")
                    if not stock_code:
                        continue
                    normalized = str(stock_code)
                    evidence_targets.setdefault(normalized, set()).add(TargetType.STOCK)
                    stock_targets.setdefault(normalized, set()).add(TargetType.STOCK)

    for section_name in ("valuations", "earnings_events", "financial_quality"):
        section = snapshot.get(section_name) or {}
        if not isinstance(section, dict):
            continue
        for rows in section.values():
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = item.get("ts_code")
                if not code:
                    continue
                normalized = str(code)
                evidence_targets.setdefault(normalized, set()).add(TargetType.STOCK)
                stock_targets.setdefault(normalized, set()).add(TargetType.STOCK)
    return evidence_targets, stock_targets


def _filter_daily_drafts(
    drafts: tuple[FundamentalEvidenceDraft, ...],
    *,
    allowed_call_id: str,
    allowed_targets: dict[str, set[TargetType]],
) -> tuple[list[FundamentalEvidenceDraft], list[str]]:
    accepted: list[FundamentalEvidenceDraft] = []
    errors: list[str] = []
    for draft in drafts:
        if set(draft.source_call_ids) != {allowed_call_id}:
            errors.append(f"快照证据引用了非快照调用：{draft.title}")
            continue
        if draft.target.type not in allowed_targets.get(draft.target.code, set()):
            errors.append(f"快照证据使用了未授权标的或错误类型：{draft.target.code}")
            continue
        accepted.append(draft)
    return accepted, errors


def _filter_review_drafts(
    drafts: tuple[FundamentalEvidenceDraft, ...],
    *,
    available_call_ids: set[str],
    observations: list[FundamentalToolObservation],
    snapshot_result: dict[str, Any] | None,
    scope_target: ResearchTarget,
) -> tuple[list[FundamentalEvidenceDraft], list[str]]:
    by_call = {item.call_id: item for item in observations}
    snapshot_targets, _ = _daily_snapshot_permissions(snapshot_result or {}, scope_target)
    accepted: list[FundamentalEvidenceDraft] = []
    errors: list[str] = []
    for draft in drafts:
        if not set(draft.source_call_ids).issubset(available_call_ids):
            errors.append(f"证据草稿引用了不存在或失败的调用：{draft.title}")
            continue
        valid = True
        for call_id in draft.source_call_ids:
            observation = by_call[call_id]
            if observation.tool_name == _IDENTITY_TOOL:
                valid = False
            elif observation.tool_name == _SNAPSHOT_TOOL:
                valid = draft.target.type in snapshot_targets.get(draft.target.code, set())
            else:
                valid = _observation_supports_target(observation, draft.target)
            if not valid:
                break
        if not valid:
            errors.append(f"证据草稿把调用结果写给了错误标的：{draft.target.code}")
            continue
        accepted.append(draft)
    return accepted, errors


def _build_tasks(
    requests: tuple[FundamentalVerificationRequestDraft, ...],
    *,
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"],
    round_number: int,
    allowed_targets: dict[str, set[TargetType]],
    existing_fingerprints: set[str],
    limit: int,
    as_of_date: date,
    required_primary_target: ResearchTarget | None = None,
    daily_report_period: str | None = None,
    daily_comparison_period: str | None = None,
    allowed_report_range: TimeRange | None = None,
) -> tuple[list[FundamentalVerificationTask], list[str], list[str]]:
    tasks: list[FundamentalVerificationTask] = []
    fingerprints: list[str] = []
    errors: list[str] = []
    for request in requests[:limit]:
        if request.target.type not in allowed_targets.get(request.target.code, set()):
            errors.append(f"拒绝未授权的基本面查证标的：{request.target.code}")
            continue
        period_error = _period_validation_error(
            request,
            as_of_date=as_of_date,
            daily_report_period=daily_report_period,
            daily_comparison_period=daily_comparison_period,
            allowed_report_range=allowed_report_range,
        )
        if period_error is not None:
            errors.append(f"拒绝 {request.target.code} 的基本面报告期：{period_error}")
            continue
        fingerprint = _request_fingerprint(request)
        if fingerprint in existing_fingerprints or fingerprint in fingerprints:
            errors.append(f"拒绝重复基本面查证：{request.target.code}")
            continue
        task_id = f"fv_r{round_number}_{len(tasks) + 1}_{fingerprint[:8]}"
        tasks.append(
            FundamentalVerificationTask(
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


def _period_validation_error(
    request: FundamentalVerificationRequestDraft,
    *,
    as_of_date: date,
    daily_report_period: str | None,
    daily_comparison_period: str | None,
    allowed_report_range: TimeRange | None,
) -> str | None:
    parsed_report = _parse_period(request.report_period)
    parsed_comparison = _parse_period(request.comparison_period)
    if request.report_period is not None and parsed_report is None:
        return "report_period 不是合法季度末"
    if request.comparison_period is not None and parsed_comparison is None:
        return "comparison_period 不是合法季度末"
    if parsed_report is not None and parsed_report > as_of_date:
        return "report_period 晚于 as_of"
    if allowed_report_range is not None and parsed_report is not None:
        # ResearchRequest.time_range 约束“可搜索到何时”，而财报报告期天然可能
        # 早于这个新闻/事件窗口；这里只拒绝未来于窗口结束日的报告期。
        if parsed_report > allowed_report_range.end:
            return "report_period 晚于 ResearchRequest.time_range.end"
    if parsed_comparison is not None and parsed_comparison > as_of_date:
        return "comparison_period 晚于 as_of"
    if parsed_report is not None and parsed_comparison is not None:
        if parsed_comparison >= parsed_report:
            return "comparison_period 必须早于 report_period"
        if (
            request.comparison_period[4:] != request.report_period[4:]
            or parsed_comparison.year + 1 != parsed_report.year
        ):
            return "comparison_period 必须是 report_period 的上年同期"
    if (
        daily_report_period is not None
        and request.report_period is not None
        and request.report_period != daily_report_period
    ):
        return f"每日模式 report_period 必须等于快照 {daily_report_period}"
    if request.comparison_period is not None and daily_comparison_period is not None:
        if request.comparison_period != daily_comparison_period:
            return f"每日模式 comparison_period 必须等于快照 {daily_comparison_period}"
    return None


def _parse_period(value: str | None) -> date | None:
    if value is None or len(value) != 8 or not value.isdigit():
        return None
    try:
        parsed = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None
    return parsed if value[4:] in {"0331", "0630", "0930", "1231"} else None


async def _execute_task(
    *,
    task: FundamentalVerificationTask,
    position: int,
    round_number: int,
    state: FundamentalAgentState,
    tools_by_name: dict[str, BaseTool],
    prior_observations: list[FundamentalToolObservation],
) -> tuple[list[FundamentalToolObservation], list[str], int]:
    observations: list[FundamentalToolObservation] = []
    errors: list[str] = []
    invocation_count = 0

    planned_calls = _planned_task_calls(task, state)
    _, _, identity_arguments = planned_calls[0]
    identity = _find_observation(
        [*prior_observations, *observations],
        _IDENTITY_TOOL,
        identity_arguments,
    )
    if identity is None:
        identity = await _call_observation(
            tools_by_name[_IDENTITY_TOOL],
            call_id=f"fc_r{round_number}_{position}_identity",
            task_id=task.task_id,
            arguments=identity_arguments,
        )
        observations.append(identity)
        invocation_count += 1
    if not _identity_confirms_target(identity, task.target.code):
        errors.append(f"股票身份核对失败，已停止查证：{task.target.code}")
        return observations, errors, invocation_count

    for call_suffix, tool_name, arguments in planned_calls[1:]:
        existing = _find_observation(
            [*prior_observations, *observations],
            tool_name,
            arguments,
        )
        if existing is not None:
            continue
        observation = await _call_observation(
            tools_by_name[tool_name],
            call_id=f"fc_r{round_number}_{position}_{call_suffix}",
            task_id=task.task_id,
            arguments=arguments,
        )
        observations.append(observation)
        if error := _uncitable_observation_error(observation, target_code=task.target.code):
            errors.append(error)
        invocation_count += 1
    return observations, errors, invocation_count


def _planned_task_calls(
    task: FundamentalVerificationTask,
    state: FundamentalAgentState,
) -> list[tuple[str, str, dict[str, Any]]]:
    """把一项查证确定性展开为可去重、可预留预算的真实 Tool 调用。"""

    daily_trade_date = _snapshot_trade_date(state.get("snapshot_result"))
    end_date = daily_trade_date or state["as_of"].date()
    research_request = state.get("research_request")
    if research_request is not None:
        end_date = min(end_date, research_request.time_range.end)
    start_date = end_date - timedelta(days=task.lookback_days)
    if research_request is not None:
        start_date = max(start_date, research_request.time_range.start)

    calls: list[tuple[str, str, dict[str, Any]]] = [
        (
            "identity",
            _IDENTITY_TOOL,
            {"ts_code": task.target.code, "list_status": "L"},
        )
    ]
    for check in task.checks:
        tool_name = _TOOL_BY_CHECK[check]
        if check is FundamentalCheck.FINANCIAL_STATEMENTS:
            argument_sets = _period_argument_sets(task, include_comparison=True)
        elif check is FundamentalCheck.FINANCIAL_QUALITY:
            argument_sets = [
                (suffix, {**arguments, "composition_type": task.composition_type})
                for suffix, arguments in _period_argument_sets(task, include_comparison=True)
            ]
        elif check is FundamentalCheck.EARNINGS_DISCLOSURE:
            argument_sets = _period_argument_sets(task, include_comparison=True)
        elif check is FundamentalCheck.VALUATION_HISTORY:
            argument_sets = [
                (
                    "window",
                    {
                        "ts_code": task.target.code,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
            ]
        elif check is FundamentalCheck.DIVIDEND_OWNERSHIP:
            if task.report_period is None:  # Pydantic 已保证不会发生，保留防御分支。
                continue
            argument_sets = [
                (
                    "current",
                    {
                        "ts_code": task.target.code,
                        "period": task.report_period,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
            ]
        else:
            argument_sets = [("current", {"ts_code": task.target.code})]

        calls.extend(
            (f"{check.value.lower()}_{suffix}", tool_name, arguments)
            for suffix, arguments in argument_sets
        )
    return calls


def _period_argument_sets(
    task: FundamentalVerificationTask,
    *,
    include_comparison: bool,
) -> list[tuple[str, dict[str, Any]]]:
    if task.report_period is None:
        return []
    result: list[tuple[str, dict[str, Any]]] = [
        ("current", {"ts_code": task.target.code, "period": task.report_period})
    ]
    if include_comparison and task.comparison_period is not None:
        result.append(
            (
                "comparison",
                {"ts_code": task.target.code, "period": task.comparison_period},
            )
        )
    return result


def _snapshot_trade_date(snapshot_result: dict[str, Any] | None) -> date | None:
    """读取每日快照对应的真实交易日；格式异常时安全退回 ``as_of``。"""

    if not snapshot_result:
        return None
    value = (snapshot_result.get("snapshot") or {}).get("trade_date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _snapshot_period(snapshot_result: dict[str, Any] | None, field: str) -> str | None:
    if not snapshot_result:
        return None
    value = (snapshot_result.get("snapshot") or {}).get(field)
    return str(value) if isinstance(value, str) and value else None


async def _call_observation(
    tool: BaseTool,
    *,
    call_id: str,
    task_id: str,
    arguments: dict[str, Any],
) -> FundamentalToolObservation:
    return FundamentalToolObservation(
        call_id=call_id,
        task_id=task_id,
        tool_name=tool.name,
        arguments=arguments,
        result=await _invoke_tool(tool, arguments),
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
    return {
        "tool_name": tool.name,
        "status": "error",
        "issues": [{"code": "INTERNAL_ERROR", "message": "Tool 返回值不是对象"}],
        "complete": False,
    }


def _uncitable_observation_error(
    observation: FundamentalToolObservation,
    *,
    target_code: str,
) -> str | None:
    """把不可引用的 Tool 结果记入诊断；empty/partial 仍保留其业务语义。"""

    status = str(observation.result.get("status") or "unknown")
    if status in _EVIDENCE_CITABLE_STATUSES:
        return None
    return (
        f"基本面 Tool 结果不可用于证据：target={target_code} "
        f"tool={observation.tool_name} status={status}"
    )


def _find_observation(
    observations: list[FundamentalToolObservation],
    tool_name: str,
    arguments: dict[str, Any],
) -> FundamentalToolObservation | None:
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


def _request_fingerprint(request: FundamentalVerificationRequestDraft) -> str:
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


async def _materialize_evidence(
    draft: FundamentalEvidenceDraft,
    *,
    run_id: str,
    as_of,
    scope_target: ResearchTarget,
    evidence_scope: str,
    observations: dict[str, FundamentalToolObservation],
    snapshot_result: dict[str, Any] | None,
    tool_context: ResearchToolContext,
) -> EvidenceRecord | None:
    referenced = [observations.get(call_id) for call_id in draft.source_call_ids]
    if any(item is None for item in referenced):
        return None
    typed = [item for item in referenced if item is not None]
    if any(item.tool_name == _IDENTITY_TOOL for item in typed):
        return None
    if any(item.result.get("status") not in _EVIDENCE_CITABLE_STATUSES for item in typed):
        return None

    snapshot_targets, _ = _daily_snapshot_permissions(snapshot_result or {}, scope_target)
    for observation in typed:
        if observation.tool_name == _SNAPSHOT_TOOL:
            if draft.target.type not in snapshot_targets.get(draft.target.code, set()):
                return None
        else:
            if not _observation_supports_target(observation, draft.target):
                return None

    source_refs: list[SourceReference] = []
    raw_payload_refs: list[str] = []
    all_complete = True
    for observation in typed:
        status = observation.result.get("status")
        all_complete = all_complete and status in {"ok", "empty"}
        if observation.tool_name == _SNAPSHOT_TOOL:
            context_ref = observation.result.get("context_ref")
            if not isinstance(context_ref, str):
                return None
            try:
                bundle = await tool_context.data_store.get(run_id, context_ref)
            except ResearchDataStoreError:
                return None
            raw_payload_refs.append(context_ref)
            pages = [page for dataset in bundle.datasets.values() for page in dataset.pages]
            if not pages:
                return None
            source_refs.append(
                SourceReference(
                    provider="+".join(sorted({page.provider.value for page in pages})),
                    interface=observation.tool_name,
                    record_key=context_ref,
                    fetched_at=max(page.fetched_at for page in pages),
                    data_as_of=max(
                        (
                            dataset.data_as_of
                            for dataset in bundle.datasets.values()
                            if dataset.data_as_of is not None
                        ),
                        default=None,
                    ),
                )
            )
            all_complete = all_complete and all(
                dataset.complete for dataset in bundle.datasets.values()
            )
            continue

        result_refs, result_complete = _source_refs_from_result(observation)
        if not result_refs:
            return None
        source_refs.extend(result_refs)
        all_complete = all_complete and result_complete
    if not source_refs:
        return None

    target = draft.target
    if draft.target.type is TargetType.STOCK:
        canonical_name = _canonical_stock_name(observations.values(), draft.target.code)
        if canonical_name is not None:
            target = ResearchTarget(
                type=TargetType.STOCK,
                code=draft.target.code,
                name=canonical_name,
            )

    description = draft.description
    if draft.limitations:
        description = f"{description}\n限制：{'；'.join(draft.limitations)}"
    evidence_suffix = re.sub(r"[^A-Za-z0-9_]", "_", run_id.removeprefix("run_"))
    scope_suffix = re.sub(r"[^A-Za-z0-9_]", "_", evidence_scope)
    content_fingerprint = _evidence_fingerprint(draft, evidence_scope, typed)
    return EvidenceRecord(
        evidence_id=f"ev_{evidence_suffix}_{scope_suffix}_{content_fingerprint[:16]}",
        run_id=run_id,
        target=target,
        domain=EvidenceDomain.FUNDAMENTAL,
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


def _source_refs_from_result(
    observation: FundamentalToolObservation,
) -> tuple[list[SourceReference], bool]:
    datasets = observation.result.get("datasets")
    if not isinstance(datasets, list):
        return [], False
    refs: list[SourceReference] = []
    complete = bool(observation.result.get("complete"))
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        complete = complete and bool(dataset.get("complete"))
        label = str(dataset.get("label") or "dataset")
        api_name = str(dataset.get("api_name") or observation.tool_name)
        summaries = dataset.get("source_summary")
        if not isinstance(summaries, list):
            continue
        for index, summary in enumerate(summaries):
            if not isinstance(summary, dict) or not summary.get("latest_fetched_at"):
                continue
            refs.append(
                SourceReference(
                    provider=str(summary.get("provider") or "UNKNOWN"),
                    interface=f"{observation.tool_name}:{api_name}",
                    record_key=f"{observation.call_id}:{label}:{index}",
                    fetched_at=summary["latest_fetched_at"],
                    data_as_of=dataset.get("data_as_of"),
                )
            )
    return refs, complete


def _evidence_fingerprint(
    draft: FundamentalEvidenceDraft,
    evidence_scope: str,
    observations: list[FundamentalToolObservation],
) -> str:
    signatures = []
    for observation in observations:
        datasets = observation.result.get("datasets")
        dataset_signatures = []
        if isinstance(datasets, list):
            dataset_signatures = [
                {
                    "label": item.get("label"),
                    "api_name": item.get("api_name"),
                    "query_params": item.get("query_params"),
                    "data_as_of": item.get("data_as_of"),
                }
                for item in datasets
                if isinstance(item, dict)
            ]
        signatures.append(
            {
                "tool_name": observation.tool_name,
                "arguments": observation.arguments,
                "context_ref": observation.result.get("context_ref"),
                "datasets": dataset_signatures,
            }
        )
    signatures.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    payload = {
        "scope": evidence_scope,
        "draft": draft.model_dump(mode="json", exclude={"source_call_ids"}),
        "sources": signatures,
    }
    return sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _observation_supports_target(
    observation: FundamentalToolObservation,
    target: ResearchTarget,
) -> bool:
    """同时校验调用参数和 Tool 实际返回行，阻止跨股票证据污染。"""

    if (
        observation.tool_name == _IDENTITY_TOOL
        or target.type is not TargetType.STOCK
        or observation.task_id is None
        or observation.arguments.get("ts_code") != target.code
    ):
        return False

    status = observation.result.get("status")
    returned_codes = _returned_stock_codes(observation.result)
    if status == "empty":
        return not returned_codes
    return (
        bool(returned_codes)
        and returned_codes == {target.code}
        and _result_rows_match_arguments(observation)
    )


def _result_rows_match_arguments(observation: FundamentalToolObservation) -> bool:
    """验证上游行确实位于程序请求的报告期或估值窗口内。"""

    period_labels = {
        "get_financial_statements": {
            "income_statement",
            "balance_sheet",
            "cash_flow_statement",
        },
        "get_financial_quality": {
            "financial_indicators",
            "business_composition",
            "audit_opinion",
        },
        "get_earnings_and_disclosure": {
            "earnings_forecast",
            "earnings_express",
            "disclosure_schedule",
        },
        "get_dividend_and_ownership_context": {
            "top_holders",
            "top_floating_holders",
        },
    }
    expected_period = observation.arguments.get("period")
    required_period_labels = period_labels.get(observation.tool_name, set())
    start_date = _coerce_date(observation.arguments.get("start_date"))
    end_date = _coerce_date(observation.arguments.get("end_date"))

    datasets = observation.result.get("datasets")
    if not isinstance(datasets, list):
        return False
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        label = dataset.get("label")
        rows = dataset.get("rows")
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                return False
            data = row["data"]
            if label in required_period_labels:
                returned_period = data.get("end_date") or data.get("period")
                if str(returned_period or "") != str(expected_period or ""):
                    return False
            if observation.tool_name == "get_valuation_context":
                trade_date = _coerce_date(data.get("trade_date"))
                if (
                    trade_date is None
                    or start_date is None
                    or end_date is None
                    or not start_date <= trade_date <= end_date
                ):
                    return False
    return True


def _coerce_date(value: Any) -> date | None:
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


def _returned_stock_codes(result: dict[str, Any]) -> set[str]:
    """提取语义 Tool 返回行中的股票代码；市场级无代码行不参与归属证明。"""

    codes: set[str] = set()
    datasets = result.get("datasets")
    if not isinstance(datasets, list):
        return codes
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        rows = dataset.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            data = row.get("data")
            if not isinstance(data, dict):
                continue
            code = data.get("ts_code")
            if isinstance(code, str) and code:
                codes.add(code)
    return codes


def _identity_confirms_target(
    observation: FundamentalToolObservation,
    target_code: str,
) -> bool:
    """只有 ``stock_basic`` 成功且唯一命中目标代码时才允许继续个股查证。"""

    if observation.result.get("status") not in {"ok", "partial"}:
        return False
    datasets = observation.result.get("datasets")
    if not isinstance(datasets, list):
        return False
    for dataset in datasets:
        if not isinstance(dataset, dict) or dataset.get("label") != "stock_basic":
            continue
        rows = dataset.get("rows")
        if not isinstance(rows, list):
            return False
        codes = {
            data.get("ts_code")
            for row in rows
            if isinstance(row, dict)
            and isinstance((data := row.get("data")), dict)
            and isinstance(data.get("ts_code"), str)
        }
        return codes == {target_code}
    return False


def _canonical_stock_name(
    observations: Iterable[FundamentalToolObservation],
    target_code: str,
) -> str | None:
    """从已通过身份闸门的 ``stock_basic`` 行取得展示名称。"""

    for observation in observations:
        if observation.tool_name != _IDENTITY_TOOL:
            continue
        if observation.arguments.get("ts_code") != target_code:
            continue
        if not _identity_confirms_target(observation, target_code):
            continue
        for dataset in observation.result.get("datasets", []):
            if not isinstance(dataset, dict) or dataset.get("label") != "stock_basic":
                continue
            for row in dataset.get("rows", []):
                if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                    continue
                name = row["data"].get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return None


def _same_target_identity(left: ResearchTarget, right: ResearchTarget) -> bool:
    """标的身份由类型和代码决定，不因模型生成的名称文案差异而改变。"""

    return left.type is right.type and left.code == right.code


def _evidence_scope(state: FundamentalAgentState) -> str:
    request = state.get("research_request")
    return request.request_id if request is not None else state["mode"].value.lower()


def _complete_research_request(
    request: ResearchRequest | None,
    evidence: list[EvidenceRecord],
    completed_at,
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
