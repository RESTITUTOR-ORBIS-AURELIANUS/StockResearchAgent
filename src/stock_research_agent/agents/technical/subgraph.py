"""TechnicalResearchAnalyst 的每日模式与查证模式 LangGraph 子图。"""

import asyncio
import json
import re
from datetime import timedelta
from hashlib import sha256
from typing import Any, Literal

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.technical.model import TechnicalReasoningModel
from stock_research_agent.agents.technical.models import (
    DailyAnalysisInput,
    TargetedPlanningInput,
    TechnicalAgentLimits,
    TechnicalAgentRunSummary,
    TechnicalBenchmark,
    TechnicalEvidenceDraft,
    TechnicalInstrumentKind,
    TechnicalMeasurement,
    TechnicalResearchMode,
    TechnicalToolObservation,
    TechnicalVerificationRequestDraft,
    TechnicalVerificationTask,
    VerificationReviewInput,
)
from stock_research_agent.agents.technical.state import TechnicalAgentState
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
from stock_research_agent.research_data import ResearchDataBundle, ResearchDataStoreError
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

_AGENT_NAME = "TechnicalResearchAnalyst"
_SNAPSHOT_TOOL = "get_daily_technical_market_snapshot"
_RAW_TOOL_BY_KIND = {
    TechnicalInstrumentKind.STOCK: "get_stock_price_context",
    TechnicalInstrumentKind.INDEX: "get_index_market_context",
    TechnicalInstrumentKind.FUND: "get_fund_market_context",
}
_CALCULATOR_BY_MEASUREMENT = {
    TechnicalMeasurement.RETURN_TREND: "calculate_return_and_trend",
    TechnicalMeasurement.MOMENTUM: "calculate_momentum",
    TechnicalMeasurement.RISK_TRADABILITY: "calculate_risk_and_tradability",
    TechnicalMeasurement.VOLUME_LIQUIDITY: "calculate_volume_and_liquidity",
}
_FIXED_BENCHMARK_CODES = {"000300.SH", "000905.SH", "000852.SH"}
_ABSTRACT_A_SHARE_CODE = "A_SHARE"
_A_SHARE_PROXY_TARGETS = (
    ResearchTarget(type=TargetType.MARKET, code="000300.SH", name="沪深300"),
    ResearchTarget(type=TargetType.MARKET, code="000905.SH", name="中证500"),
    ResearchTarget(type=TargetType.MARKET, code="000852.SH", name="中证1000"),
)
_CONTEXT_REUSABLE_STATUSES = {"ok", "partial", "too_large"}
_EVIDENCE_CITABLE_STATUSES = {"ok", "partial"}


def build_technical_agent_graph(
    *,
    model: TechnicalReasoningModel,
    tool_context: ResearchToolContext,
    limits: TechnicalAgentLimits | None = None,
    tools: tuple[BaseTool, ...] | None = None,
):
    """构建一个可独立测试、以后可作为主图节点嵌入的技术研究子图。"""

    configured_limits = limits or TechnicalAgentLimits()
    technical_tools = tools or build_agent_tool_registry(tool_context).technical
    tools_by_name = {tool.name: tool for tool in technical_tools}
    _require_tools(tools_by_name)

    async def prepare(state: TechnicalAgentState) -> TechnicalAgentState:
        run_id = state.get("run_id")
        target = state.get("target")
        as_of = state.get("as_of")
        if not run_id or target is None or as_of is None:
            raise ValueError("技术 Agent 必须提供 run_id、target 和 as_of")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("技术 Agent 的 as_of 必须包含时区")
        if run_id != tool_context.run_id or as_of != tool_context.as_of:
            raise ValueError("技术 Agent 输入必须与 ResearchToolContext 的 run_id/as_of 一致")

        mode = TechnicalResearchMode(state.get("mode", TechnicalResearchMode.DAILY))
        research_request = state.get("research_request")
        if mode is TechnicalResearchMode.VERIFICATION:
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

    async def acquire_daily_snapshot(state: TechnicalAgentState) -> TechnicalAgentState:
        arguments = {"candidate_count": configured_limits.daily_candidate_count}
        result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], arguments)
        observations = [
            TechnicalToolObservation(
                call_id="tc_daily_snapshot_1",
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
            retry_arguments = {"candidate_count": 3}
            result = await _invoke_tool(tools_by_name[_SNAPSHOT_TOOL], retry_arguments)
            observations.append(
                TechnicalToolObservation(
                    call_id="tc_daily_snapshot_2",
                    tool_name=_SNAPSHOT_TOOL,
                    arguments=retry_arguments,
                    result=result,
                )
            )
            call_count += 1

        budget_exhausted = retry_needed and not retry_allowed

        errors = []
        if result.get("status") not in {"ok", "partial"} or result.get("snapshot") is None:
            errors.append(f"每日技术快照不可用：status={result.get('status', 'unknown')}")
        return {
            "snapshot_result": result,
            "observations": observations,
            "tool_call_count": call_count,
            "budget_exhausted": budget_exhausted,
            "errors": errors,
        }

    async def analyze_daily(state: TechnicalAgentState) -> TechnicalAgentState:
        snapshot_result = state["snapshot_result"]
        snapshot_observation = state["observations"][-1]
        try:
            decision = await model.analyze_daily(
                DailyAnalysisInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    snapshot_call_id=snapshot_observation.call_id,
                    snapshot_result=snapshot_result,
                )
            )
        except Exception as exc:
            return {"errors": [f"每日快照结构化分析失败：{describe_exception(exc)}"]}

        allowed_target_types, allowed_instruments = _daily_snapshot_permissions(
            snapshot_result,
            state["target"],
        )
        drafts, draft_errors = _filter_drafts(
            decision.snapshot_evidence,
            allowed_call_ids={snapshot_observation.call_id},
            allowed_target_types=allowed_target_types,
        )
        normalized_requests = _resolve_abstract_market_requests(
            decision.verification_requests
        )
        tasks, fingerprints, task_errors = _build_tasks(
            normalized_requests,
            origin="DAILY",
            round_number=1,
            allowed_instruments=allowed_instruments,
            existing_fingerprints=set(),
            limit=configured_limits.max_requests_per_round,
        )
        return {
            "evidence_drafts": drafts,
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": [*draft_errors, *task_errors],
        }

    async def plan_targeted(state: TechnicalAgentState) -> TechnicalAgentState:
        research_request = state["research_request"]
        try:
            plan = await model.plan_targeted(
                TargetedPlanningInput(
                    run_id=state["run_id"],
                    scope_target=state["target"],
                    as_of=state["as_of"],
                    research_request=research_request,
                )
            )
        except Exception as exc:
            return {"errors": [f"定向技术查证计划生成失败：{describe_exception(exc)}"]}

        normalized_requests = _resolve_abstract_market_requests(
            plan.verification_requests
        )
        allowed_instruments = {
            research_request.target.code: {
                (research_request.target.type, kind)
                for kind in _kinds_for_external_target(research_request.target)
            },
            **{
                code: {(TargetType.MARKET, TechnicalInstrumentKind.INDEX)}
                for code in _FIXED_BENCHMARK_CODES
            },
        }
        tasks, fingerprints, task_errors = _build_tasks(
            normalized_requests,
            origin="RESEARCH_REQUEST",
            round_number=1,
            allowed_instruments=allowed_instruments,
            existing_fingerprints=set(),
            limit=configured_limits.max_requests_per_round,
            required_primary_target=research_request.target,
        )
        return {
            "pending_tasks": tasks,
            "seen_request_fingerprints": fingerprints,
            "errors": task_errors,
        }

    async def execute_verification(state: TechnicalAgentState) -> TechnicalAgentState:
        round_number = state["verification_round"] + 1
        requested_tasks = state["pending_tasks"][: configured_limits.max_requests_per_round]
        previous_observations = list(state["observations"])
        remaining_budget = max(
            configured_limits.max_total_tool_calls - state["tool_call_count"],
            0,
        )
        tasks: list[TechnicalVerificationTask] = []
        reservation_errors: list[str] = []
        skipped_task_ids = [
            task.task_id
            for task in state["pending_tasks"][configured_limits.max_requests_per_round :]
        ]
        reservable_calls = remaining_budget
        for task in requested_tasks:
            estimated_calls = 1 + len(task.measurements)
            if TechnicalMeasurement.RELATIVE_STRENGTH in task.measurements:
                estimated_calls += 1
            if estimated_calls > reservable_calls:
                reservation_errors.append(f"技术查证 Tool 调用预算不足，未执行：{task.target.code}")
                skipped_task_ids.append(task.task_id)
                continue
            tasks.append(task)
            reservable_calls -= estimated_calls

        new_observations: list[TechnicalToolObservation] = []
        errors: list[str] = list(reservation_errors)
        actual_invocation_count = 0
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
            actual_invocation_count += invocation_count
            errors.extend(task_errors)

        return {
            "observations": [*previous_observations, *new_observations],
            "pending_tasks": tasks,
            "tool_call_count": state["tool_call_count"] + actual_invocation_count,
            "verification_round": round_number,
            "budget_exhausted": state["budget_exhausted"] or bool(skipped_task_ids),
            "skipped_task_ids": [*state["skipped_task_ids"], *skipped_task_ids],
            "errors": [*state["errors"], *errors],
        }

    async def review_verification(state: TechnicalAgentState) -> TechnicalAgentState:
        if not state["pending_tasks"]:
            return {"pending_tasks": []}
        try:
            decision = await model.review_verification(
                VerificationReviewInput(
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
                    f"技术查证结果结构化审阅失败：{describe_exception(exc)}",
                ],
            }

        available_call_ids = {
            observation.call_id
            for observation in state["observations"]
            if observation.result.get("status") in _EVIDENCE_CITABLE_STATUSES
        }
        drafts, draft_errors = _filter_drafts(
            decision.evidence,
            allowed_call_ids=available_call_ids,
            allowed_target_types=None,
        )
        next_tasks: list[TechnicalVerificationTask] = []
        next_fingerprints: list[str] = []
        task_errors: list[str] = []
        if (
            not state["budget_exhausted"]
            and state["verification_round"] < configured_limits.max_verification_rounds
        ):
            allowed_follow_up_instruments = _follow_up_permissions(state["pending_tasks"])
            next_tasks, next_fingerprints, task_errors = _build_tasks(
                decision.follow_up_requests,
                origin="FOLLOW_UP",
                round_number=state["verification_round"] + 1,
                allowed_instruments=allowed_follow_up_instruments,
                existing_fingerprints=set(state["seen_request_fingerprints"]),
                limit=configured_limits.max_requests_per_round,
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

    async def finalize(state: TechnicalAgentState) -> TechnicalAgentState:
        observations = {item.call_id: item for item in state["observations"]}
        evidence_records: list[EvidenceRecord] = []
        rejected = 0
        seen_evidence: set[tuple[str, str, str]] = set()
        final_errors = list(state["errors"])
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
            "run_summary": TechnicalAgentRunSummary(
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

    builder = StateGraph(TechnicalAgentState)
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
            TechnicalResearchMode.DAILY.value: "acquire_daily_snapshot",
            TechnicalResearchMode.VERIFICATION.value: "plan_targeted",
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


def _route_after_snapshot(state: TechnicalAgentState) -> Literal["analyze", "finalize"]:
    result = state.get("snapshot_result") or {}
    return "analyze" if result.get("status") in {"ok", "partial"} else "finalize"


def _route_after_plan(state: TechnicalAgentState) -> Literal["verify", "finalize"]:
    return "verify" if state.get("pending_tasks") else "finalize"


def _require_tools(tools: dict[str, BaseTool]) -> None:
    required = {
        _SNAPSHOT_TOOL,
        *_RAW_TOOL_BY_KIND.values(),
        *_CALCULATOR_BY_MEASUREMENT.values(),
        "calculate_relative_strength",
    }
    missing = sorted(required - tools.keys())
    if missing:
        raise ValueError(f"技术 Agent 缺少 Tool：{', '.join(missing)}")


def _validate_external_request(
    request: ResearchRequest | None,
    run_id: str,
    as_of,
) -> None:
    if request is None:
        raise ValueError("VERIFICATION 模式必须提供 research_request")
    if request.run_id != run_id:
        raise ValueError("ResearchRequest.run_id 与当前运行不一致")
    if request.assigned_domain is not EvidenceDomain.TECHNICAL:
        raise ValueError("技术 Agent 只能执行 assigned_domain=TECHNICAL 的请求")
    if request.status not in {ResearchRequestStatus.PENDING, ResearchRequestStatus.RUNNING}:
        raise ValueError("只能执行 PENDING 或 RUNNING 的 ResearchRequest")
    if request.time_range.end > as_of.date():
        raise ValueError("ResearchRequest 时间范围不能晚于 as_of")


def _daily_snapshot_permissions(
    result: dict[str, Any],
    scope_target: ResearchTarget,
) -> tuple[
    dict[str, set[TargetType]],
    dict[str, set[tuple[TargetType, TechnicalInstrumentKind]]],
]:
    snapshot = result.get("snapshot") or {}
    target_types: dict[str, set[TargetType]] = {scope_target.code: {scope_target.type}}
    instruments: dict[str, set[tuple[TargetType, TechnicalInstrumentKind]]] = {}

    def authorize(code: str, target_type: TargetType, kind: TechnicalInstrumentKind) -> None:
        target_types.setdefault(code, set()).add(target_type)
        instruments.setdefault(code, set()).add((target_type, kind))

    for code in _FIXED_BENCHMARK_CODES:
        authorize(code, TargetType.MARKET, TechnicalInstrumentKind.INDEX)
    for item in snapshot.get("market_indices", []):
        if item.get("ts_code"):
            authorize(
                str(item["ts_code"]),
                TargetType.MARKET,
                TechnicalInstrumentKind.INDEX,
            )
    for item in snapshot.get("industries", []):
        if item.get("index_code"):
            authorize(
                str(item["index_code"]),
                TargetType.SECTOR,
                TechnicalInstrumentKind.INDEX,
            )
    for rows in (snapshot.get("candidates") or {}).values():
        if isinstance(rows, list):
            for item in rows:
                if item.get("ts_code"):
                    authorize(
                        str(item["ts_code"]),
                        TargetType.STOCK,
                        TechnicalInstrumentKind.STOCK,
                    )
    return target_types, instruments


def _filter_drafts(
    drafts: tuple[TechnicalEvidenceDraft, ...],
    *,
    allowed_call_ids: set[str],
    allowed_target_types: dict[str, set[TargetType]] | None,
) -> tuple[list[TechnicalEvidenceDraft], list[str]]:
    accepted: list[TechnicalEvidenceDraft] = []
    errors: list[str] = []
    for draft in drafts:
        if not set(draft.source_call_ids).issubset(allowed_call_ids):
            errors.append(f"证据草稿引用了不存在或失败的调用：{draft.title}")
            continue
        if allowed_target_types is not None:
            target_types = allowed_target_types.get(draft.target.code, set())
            if draft.target.type not in target_types:
                errors.append(f"证据草稿使用了快照外标的或错误类型：{draft.target.code}")
                continue
        accepted.append(draft)
    return accepted, errors


def _build_tasks(
    requests: tuple[TechnicalVerificationRequestDraft, ...],
    *,
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"],
    round_number: int,
    allowed_instruments: dict[
        str,
        set[tuple[TargetType, TechnicalInstrumentKind]],
    ]
    | None,
    existing_fingerprints: set[str],
    limit: int,
    required_primary_target: ResearchTarget | None = None,
) -> tuple[list[TechnicalVerificationTask], list[str], list[str]]:
    tasks: list[TechnicalVerificationTask] = []
    fingerprints: list[str] = []
    errors: list[str] = []
    for request in requests[:limit]:
        if allowed_instruments is not None:
            target_permission = (request.target.type, request.instrument_kind)
            if target_permission not in allowed_instruments.get(request.target.code, set()):
                errors.append(f"拒绝未授权标的或错误行情类型：{request.target.code}")
                continue
            if request.benchmark:
                benchmark_permission = (
                    request.benchmark.target.type,
                    TechnicalInstrumentKind(request.benchmark.instrument_kind),
                )
                if benchmark_permission not in allowed_instruments.get(
                    request.benchmark.target.code,
                    set(),
                ):
                    errors.append(f"拒绝未授权基准或错误行情类型：{request.benchmark.target.code}")
                    continue
        fingerprint = _request_fingerprint(request)
        if fingerprint in existing_fingerprints or fingerprint in fingerprints:
            errors.append(f"拒绝重复技术查证：{request.target.code}")
            continue
        task_id = f"tv_r{round_number}_{len(tasks) + 1}_{fingerprint[:8]}"
        tasks.append(
            TechnicalVerificationTask(
                **request.model_dump(),
                task_id=task_id,
                origin=origin,
            )
        )
        fingerprints.append(fingerprint)

    if required_primary_target is not None and not any(
        _task_covers_primary_target(task, required_primary_target) for task in tasks
    ):
        errors.append("定向查证计划没有研究 ResearchRequest.target，已拒绝该计划")
        return [], [], errors
    return tasks, fingerprints, errors


def _resolve_abstract_market_requests(
    requests: tuple[TechnicalVerificationRequestDraft, ...],
) -> tuple[TechnicalVerificationRequestDraft, ...]:
    """把不可执行的 A_SHARE 符号稳定映射为真实、可查询的宽基指数代理。"""

    resolved: list[TechnicalVerificationRequestDraft] = []
    abstract_position = 0
    for request in requests:
        target = request.target
        benchmark = request.benchmark
        annotations: list[str] = []
        if (
            target.type is TargetType.MARKET
            and target.code == _ABSTRACT_A_SHARE_CODE
            and request.instrument_kind is TechnicalInstrumentKind.INDEX
        ):
            target = _A_SHARE_PROXY_TARGETS[
                abstract_position % len(_A_SHARE_PROXY_TARGETS)
            ]
            abstract_position += 1
            annotations.append(f"A_SHARE 抽象市场目标映射为 {target.code}")
        if (
            benchmark is not None
            and benchmark.target.type is TargetType.MARKET
            and benchmark.target.code == _ABSTRACT_A_SHARE_CODE
        ):
            benchmark_target = next(
                candidate
                for candidate in _A_SHARE_PROXY_TARGETS
                if candidate.code != target.code
            )
            benchmark = TechnicalBenchmark(
                target=benchmark_target,
                instrument_kind=TechnicalInstrumentKind.INDEX,
            )
            annotations.append(f"A_SHARE 抽象基准映射为 {benchmark_target.code}")
        if not annotations:
            resolved.append(request)
            continue
        resolved.append(
            TechnicalVerificationRequestDraft.model_validate(
                {
                    **request.model_dump(mode="python"),
                    "target": target,
                    "benchmark": benchmark,
                    "reason": f"{request.reason}；{'；'.join(annotations)}。",
                }
            )
        )
    return tuple(resolved)


def _task_covers_primary_target(
    task: TechnicalVerificationTask,
    required: ResearchTarget,
) -> bool:
    if (
        required.type is TargetType.MARKET
        and required.code == _ABSTRACT_A_SHARE_CODE
    ):
        return (
            task.target.type is TargetType.MARKET
            and task.target.code in _FIXED_BENCHMARK_CODES
            and task.instrument_kind is TechnicalInstrumentKind.INDEX
        )
    return task.target == required


def _kinds_for_external_target(target: ResearchTarget) -> set[TechnicalInstrumentKind]:
    if target.type is TargetType.STOCK:
        return {TechnicalInstrumentKind.STOCK}
    if target.code.endswith(".SI") or target.code.endswith(".CSI"):
        return {TechnicalInstrumentKind.INDEX}
    return {TechnicalInstrumentKind.INDEX, TechnicalInstrumentKind.FUND}


def _follow_up_permissions(
    tasks: list[TechnicalVerificationTask],
) -> dict[str, set[tuple[TargetType, TechnicalInstrumentKind]]]:
    permissions: dict[str, set[tuple[TargetType, TechnicalInstrumentKind]]] = {
        code: {(TargetType.MARKET, TechnicalInstrumentKind.INDEX)}
        for code in _FIXED_BENCHMARK_CODES
    }
    for task in tasks:
        permissions.setdefault(task.target.code, set()).add(
            (task.target.type, task.instrument_kind)
        )
        if task.benchmark is not None:
            permissions.setdefault(task.benchmark.target.code, set()).add(
                (
                    task.benchmark.target.type,
                    TechnicalInstrumentKind(task.benchmark.instrument_kind),
                )
            )
    return permissions


async def _execute_task(
    *,
    task: TechnicalVerificationTask,
    position: int,
    round_number: int,
    state: TechnicalAgentState,
    tools_by_name: dict[str, BaseTool],
    prior_observations: list[TechnicalToolObservation],
) -> tuple[list[TechnicalToolObservation], list[str], int]:
    observations: list[TechnicalToolObservation] = []
    errors: list[str] = []
    invocation_count = 0
    end_date = state["as_of"].date()
    start_date = end_date - timedelta(days=task.lookback_days)
    research_request = state.get("research_request")
    if research_request is not None:
        end_date = min(end_date, research_request.time_range.end)
        start_date = max(start_date, research_request.time_range.start)

    target_args = _raw_context_arguments(
        task.instrument_kind, task.target.code, start_date, end_date
    )
    target_tool = _RAW_TOOL_BY_KIND[task.instrument_kind]
    target_observation = _find_observation(prior_observations, target_tool, target_args)
    if target_observation is None:
        target_observation = await _call_observation(
            tools_by_name[target_tool],
            call_id=f"tc_r{round_number}_{position}_target",
            task_id=task.task_id,
            arguments=target_args,
        )
        observations.append(target_observation)
        invocation_count += 1
    target_ref = target_observation.result.get("context_ref")
    if not isinstance(target_ref, str):
        errors.append(f"{task.target.code} 未取得可计算的 context_ref")
        return observations, errors, invocation_count

    benchmark_ref: str | None = None
    if TechnicalMeasurement.RELATIVE_STRENGTH in task.measurements:
        benchmark = task.benchmark
        if benchmark is None:
            errors.append(f"{task.target.code} 相对强弱缺少 benchmark")
        else:
            benchmark_args = _raw_context_arguments(
                TechnicalInstrumentKind(benchmark.instrument_kind),
                benchmark.target.code,
                start_date,
                end_date,
            )
            benchmark_tool = _RAW_TOOL_BY_KIND[TechnicalInstrumentKind(benchmark.instrument_kind)]
            benchmark_observation = _find_observation(
                [*prior_observations, *observations],
                benchmark_tool,
                benchmark_args,
            )
            if benchmark_observation is None:
                benchmark_observation = await _call_observation(
                    tools_by_name[benchmark_tool],
                    call_id=f"tc_r{round_number}_{position}_benchmark",
                    task_id=task.task_id,
                    arguments=benchmark_args,
                )
                observations.append(benchmark_observation)
                invocation_count += 1
            value = benchmark_observation.result.get("context_ref")
            benchmark_ref = value if isinstance(value, str) else None

    for measurement in task.measurements:
        if measurement is TechnicalMeasurement.RELATIVE_STRENGTH:
            if benchmark_ref is None:
                continue
            tool_name = "calculate_relative_strength"
            arguments = {
                "target_context_ref": target_ref,
                "benchmark_context_ref": benchmark_ref,
            }
        else:
            tool_name = _CALCULATOR_BY_MEASUREMENT[measurement]
            arguments = {"context_ref": target_ref}
        if _find_observation([*prior_observations, *observations], tool_name, arguments):
            continue
        observations.append(
            await _call_observation(
                tools_by_name[tool_name],
                call_id=f"tc_r{round_number}_{position}_{measurement.value.lower()}",
                task_id=task.task_id,
                arguments=arguments,
            )
        )
        invocation_count += 1
    return observations, errors, invocation_count


def _raw_context_arguments(kind, code, start_date, end_date) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "ts_code": code,
        "start_date": start_date,
        "end_date": end_date,
    }
    if kind is TechnicalInstrumentKind.STOCK:
        arguments["frequency"] = "daily"
    elif kind is TechnicalInstrumentKind.FUND:
        arguments.update(
            include_adjustment_factors=True,
            include_share_history=False,
        )
    return arguments


async def _call_observation(
    tool: BaseTool,
    *,
    call_id: str,
    task_id: str,
    arguments: dict[str, Any],
) -> TechnicalToolObservation:
    return TechnicalToolObservation(
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


def _find_observation(
    observations: list[TechnicalToolObservation],
    tool_name: str,
    arguments: dict[str, Any],
) -> TechnicalToolObservation | None:
    fingerprint = _tool_fingerprint(tool_name, arguments)
    return next(
        (
            item
            for item in observations
            if _tool_fingerprint(item.tool_name, item.arguments) == fingerprint
            and item.result.get("status") in _CONTEXT_REUSABLE_STATUSES
        ),
        None,
    )


def _request_fingerprint(request: TechnicalVerificationRequestDraft) -> str:
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
    draft: TechnicalEvidenceDraft,
    *,
    run_id: str,
    as_of,
    scope_target: ResearchTarget,
    evidence_scope: str,
    observations: dict[str, TechnicalToolObservation],
    tool_context: ResearchToolContext,
) -> EvidenceRecord | None:
    referenced = [observations.get(call_id) for call_id in draft.source_call_ids]
    if any(item is None for item in referenced):
        return None
    typed_observations = [item for item in referenced if item is not None]
    if any(
        item.result.get("status") not in _EVIDENCE_CITABLE_STATUSES for item in typed_observations
    ):
        return None

    source_refs: list[SourceReference] = []
    raw_refs: list[str] = []
    bundles_by_ref: dict[str, ResearchDataBundle] = {}
    all_complete = True
    for observation in typed_observations:
        all_complete = all_complete and observation.result.get("status") == "ok"
        refs = _context_refs(observation.result)
        if not refs:
            return None
        supported_target_types = _result_subject_types(observation.result)
        has_explicit_subject = bool(supported_target_types)
        if observation.tool_name == _SNAPSHOT_TOOL:
            snapshot_target_types, _ = _daily_snapshot_permissions(
                observation.result,
                scope_target,
            )
            for code, target_types in snapshot_target_types.items():
                supported_target_types.setdefault(code, set()).update(target_types)
        for context_ref in refs:
            bundle = bundles_by_ref.get(context_ref)
            if bundle is None:
                try:
                    bundle = await tool_context.data_store.get(run_id, context_ref)
                except ResearchDataStoreError:
                    return None
                bundles_by_ref[context_ref] = bundle
            bundle_code = bundle.metadata.get("ts_code")
            if not has_explicit_subject and isinstance(bundle_code, str):
                supported_target_types.setdefault(bundle_code, set()).update(
                    _target_types_for_bundle(bundle.kind, bundle_code)
                )
            if context_ref in raw_refs:
                continue
            raw_refs.append(context_ref)
            pages = [page for dataset in bundle.datasets.values() for page in dataset.pages]
            providers = sorted({page.provider.value for page in pages}) or ["UNKNOWN"]
            fetched_at = max((page.fetched_at for page in pages), default=as_of)
            data_as_of = max(
                (
                    dataset.data_as_of
                    for dataset in bundle.datasets.values()
                    if dataset.data_as_of is not None
                ),
                default=None,
            )
            all_complete = all_complete and all(
                dataset.complete for dataset in bundle.datasets.values()
            )
            source_refs.append(
                SourceReference(
                    provider="+".join(providers),
                    interface=observation.tool_name,
                    record_key=context_ref,
                    fetched_at=fetched_at,
                    data_as_of=data_as_of,
                )
            )
        if draft.target.type not in supported_target_types.get(draft.target.code, set()):
            return None
    if not source_refs:
        return None

    description = draft.description
    if draft.limitations:
        description = f"{description}\n限制：{'；'.join(draft.limitations)}"
    evidence_suffix = re.sub(r"[^A-Za-z0-9_]", "_", run_id.removeprefix("run_"))
    scope_suffix = re.sub(r"[^A-Za-z0-9_]", "_", evidence_scope)
    content_fingerprint = _evidence_fingerprint(
        draft,
        evidence_scope=evidence_scope,
        observations=typed_observations,
        bundles_by_ref=bundles_by_ref,
    )
    return EvidenceRecord(
        evidence_id=f"ev_{evidence_suffix}_{scope_suffix}_{content_fingerprint[:16]}",
        run_id=run_id,
        target=draft.target,
        domain=EvidenceDomain.TECHNICAL,
        as_of=as_of,
        title=draft.title,
        description=description,
        source_refs=source_refs,
        verification_status=(
            VerificationStatus.VERIFIED if all_complete else VerificationStatus.UNVERIFIED
        ),
        tags=list(draft.tags),
        raw_payload_ref=raw_refs[0],
        collected_by=_AGENT_NAME,
        created_at=as_of,
    )


def _context_refs(result: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    context_ref = result.get("context_ref")
    if isinstance(context_ref, str):
        refs.append(context_ref)
    source_context_refs = result.get("source_context_refs")
    if isinstance(source_context_refs, (list, tuple)):
        refs.extend(str(item) for item in source_context_refs if isinstance(item, str))
    return refs


def _result_subject_types(result: dict[str, Any]) -> dict[str, set[TargetType]]:
    subjects = result.get("source_subjects")
    if not isinstance(subjects, (list, tuple)) or not subjects:
        return {}
    subject = subjects[0]
    if not isinstance(subject, dict) or not isinstance(subject.get("ts_code"), str):
        return {}
    code = str(subject["ts_code"])
    return {code: _target_types_for_bundle(str(subject.get("bundle_kind", "")), code)}


def _target_types_for_bundle(bundle_kind: str, code: str) -> set[TargetType]:
    if bundle_kind == "stock_price_context":
        return {TargetType.STOCK}
    if bundle_kind == "index_market_context":
        return (
            {TargetType.SECTOR}
            if code.endswith(".SI")
            else {
                TargetType.MARKET,
                TargetType.SECTOR,
            }
        )
    if bundle_kind == "fund_market_context":
        return {TargetType.MARKET, TargetType.SECTOR}
    return set()


def _evidence_fingerprint(
    draft: TechnicalEvidenceDraft,
    *,
    evidence_scope: str,
    observations: list[TechnicalToolObservation],
    bundles_by_ref: dict[str, ResearchDataBundle],
) -> str:
    source_signatures = []
    for observation in observations:
        for context_ref in _context_refs(observation.result):
            bundle = bundles_by_ref[context_ref]
            source_signatures.append(
                {
                    "tool_name": observation.tool_name,
                    "bundle_kind": bundle.kind,
                    "ts_code": bundle.metadata.get("ts_code"),
                    "datasets": sorted(
                        (
                            label,
                            dataset.api_name,
                            dataset.query_params,
                            dataset.data_as_of,
                        )
                        for label, dataset in bundle.datasets.items()
                    ),
                }
            )
    source_signatures.sort(
        key=lambda item: json.dumps(item, sort_keys=True, default=str),
    )
    payload = {
        "scope": evidence_scope,
        "draft": draft.model_dump(mode="json", exclude={"source_call_ids"}),
        "sources": source_signatures,
    }
    return sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _evidence_scope(state: TechnicalAgentState) -> str:
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
