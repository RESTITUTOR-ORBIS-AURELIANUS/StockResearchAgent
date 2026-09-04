"""新闻事件 Agent 的模型协议和 OpenAI-compatible 实现。"""

from copy import deepcopy
from typing import Any, Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.event.models import (
    DailyEventAnalysis,
    DailyEventInput,
    EventReviewDecision,
    EventReviewInput,
    TargetedEventInput,
    TargetedEventPlan,
)
from stock_research_agent.agents.event.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class EventReasoningModel(Protocol):
    async def analyze_daily(self, request: DailyEventInput) -> DailyEventAnalysis: ...

    async def plan_targeted(self, request: TargetedEventInput) -> TargetedEventPlan: ...

    async def review_verification(
        self,
        request: EventReviewInput,
    ) -> EventReviewDecision: ...


class OpenAIEventReasoningModel:
    """复用全局已经配置好的聊天模型，只建立三个结构化输出通道。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._structured_output_method = structured_output_method
        self._structured_output_options = structured_output_options
        self._targeted = build_observable_structured_output(
            chat_model,
            TargetedEventPlan,
            method=structured_output_method,
            operation="event.plan_targeted",
            options=structured_output_options,
        )

    async def analyze_daily(self, request: DailyEventInput) -> DailyEventAnalysis:
        allowed_targets = _daily_target_codes(request)
        allowed_record_keys = _collect_record_keys(request.snapshot_result)
        daily = build_observable_structured_output(
            self._chat_model,
            DailyEventAnalysis,
            method=self._structured_output_method,
            operation="event.analyze_daily",
            options=self._structured_output_options,
            json_schema_override=(
                _event_json_schema(
                    DailyEventAnalysis,
                    allowed_call_ids=frozenset({request.snapshot_call_id}),
                    allowed_record_keys=allowed_record_keys,
                    allowed_target_codes=allowed_targets,
                )
                if self._structured_output_method == "json_schema"
                else None
            ),
            post_validate=lambda value: _validate_event_references(
                value,
                allowed_call_ids=frozenset({request.snapshot_call_id}),
                allowed_record_keys=allowed_record_keys,
                allowed_target_codes=allowed_targets,
            ),
        )
        result = await daily.ainvoke(
            [
                SystemMessage(content=DAILY_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return DailyEventAnalysis.model_validate(result)

    async def plan_targeted(self, request: TargetedEventInput) -> TargetedEventPlan:
        result = await self._targeted.ainvoke(
            [
                SystemMessage(content=TARGETED_PLANNING_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return TargetedEventPlan.model_validate(result)

    async def review_verification(self, request: EventReviewInput) -> EventReviewDecision:
        allowed_call_ids = _review_call_ids(request)
        allowed_record_keys = _collect_record_keys(
            [observation.result for observation in request.observations]
        )
        review = build_observable_structured_output(
            self._chat_model,
            EventReviewDecision,
            method=self._structured_output_method,
            operation="event.review_verification",
            options=self._structured_output_options,
            json_schema_override=(
                _event_json_schema(
                    EventReviewDecision,
                    allowed_call_ids=allowed_call_ids,
                    allowed_record_keys=allowed_record_keys,
                    allowed_target_codes=None,
                )
                if self._structured_output_method == "json_schema"
                else None
            ),
            post_validate=lambda value: _validate_event_references(
                value,
                allowed_call_ids=allowed_call_ids,
                allowed_record_keys=allowed_record_keys,
                allowed_target_codes=None,
            ),
        )
        result = await review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return EventReviewDecision.model_validate(result)


def _daily_target_codes(request: DailyEventInput) -> frozenset[str]:
    snapshot = request.snapshot_result.get("snapshot") or {}
    codes = {"A_SHARE"}
    if request.scope_target.type.value == "MARKET":
        codes.add(request.scope_target.code)
    for news in snapshot.get("market_news") or []:
        if isinstance(news, dict):
            codes.update(
                str(item["ts_code"])
                for item in news.get("related_stocks") or []
                if isinstance(item, dict) and item.get("ts_code")
            )
    for group in ("announcements", "sell_side_reports", "broker_recommendations"):
        codes.update(
            str(item["ts_code"])
            for item in snapshot.get(group) or []
            if isinstance(item, dict) and item.get("ts_code")
        )
    return frozenset(codes)


def _review_call_ids(request: EventReviewInput) -> frozenset[str]:
    return frozenset(
        observation.call_id
        for observation in request.observations
        if observation.tool_name != "resolve_stock_identity"
        and observation.result.get("status") in {"ok", "partial"}
    )


def _collect_record_keys(value: Any) -> frozenset[str]:
    keys: set[str] = set()

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            record_key = current.get("record_key")
            if isinstance(record_key, str) and record_key:
                keys.add(record_key)
            for field in ("record_keys", "supporting_record_keys"):
                values = current.get(field)
                if isinstance(values, (list, tuple)):
                    keys.update(str(item) for item in values if item)
            for nested in current.values():
                visit(nested)
        elif isinstance(current, (list, tuple)):
            for nested in current:
                visit(nested)

    visit(value)
    return frozenset(keys)


def _event_json_schema(
    output_type: type[DailyEventAnalysis] | type[EventReviewDecision],
    *,
    allowed_call_ids: frozenset[str],
    allowed_record_keys: frozenset[str],
    allowed_target_codes: frozenset[str] | None,
) -> dict[str, Any]:
    schema = deepcopy(output_type.model_json_schema())
    evidence = schema["$defs"]["EventEvidenceDraft"]["properties"]
    if allowed_call_ids:
        evidence["source_call_ids"]["items"] = {
            "type": "string",
            "enum": sorted(allowed_call_ids),
        }
    else:
        evidence["source_call_ids"]["description"] = "本轮没有可形成证据的 Tool 调用"
    if allowed_record_keys:
        evidence["source_record_keys"]["items"] = {
            "type": "string",
            "enum": sorted(allowed_record_keys),
        }
    else:
        evidence["source_record_keys"]["description"] = "本轮没有可形成证据的原始行"
    if allowed_target_codes is not None:
        schema["$defs"]["ResearchTarget"]["properties"]["code"]["enum"] = sorted(
            allowed_target_codes
        )
    return schema


def _validate_event_references(
    decision: DailyEventAnalysis | EventReviewDecision,
    *,
    allowed_call_ids: frozenset[str],
    allowed_record_keys: frozenset[str],
    allowed_target_codes: frozenset[str] | None,
) -> DailyEventAnalysis | EventReviewDecision:
    evidence = (
        decision.snapshot_evidence
        if isinstance(decision, DailyEventAnalysis)
        else decision.evidence
    )
    unknown_calls = sorted(
        {
            call_id
            for draft in evidence
            for call_id in draft.source_call_ids
            if call_id not in allowed_call_ids
        }
    )
    unknown_keys = sorted(
        {
            key
            for draft in evidence
            for key in draft.source_record_keys
            if key not in allowed_record_keys
        }
    )
    planned_requests = (
        decision.verification_requests
        if isinstance(decision, DailyEventAnalysis)
        else decision.follow_up_requests
    )
    candidate_target_codes = {draft.target.code for draft in evidence} | {
        request.target.code for request in planned_requests
    }
    unknown_targets = (
        sorted(candidate_target_codes - allowed_target_codes)
        if allowed_target_codes is not None
        else []
    )
    problems = []
    if unknown_calls:
        problems.append("不存在或不可引用的 source_call_ids=" + ",".join(unknown_calls))
    if unknown_keys:
        problems.append("不存在的 source_record_keys=" + ",".join(unknown_keys))
    if unknown_targets:
        problems.append("快照未授权的 target.code=" + ",".join(unknown_targets))
    if problems:
        raise ValueError("；".join(problems))
    return decision
