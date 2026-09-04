"""基本面 Agent 的模型协议和 OpenAI-compatible 实现。"""

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Any, Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.fundamental.models import (
    DailyFundamentalAnalysis,
    DailyFundamentalInput,
    FundamentalReviewDecision,
    FundamentalReviewInput,
    TargetedFundamentalInput,
    TargetedFundamentalPlan,
)
from stock_research_agent.agents.fundamental.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class FundamentalReasoningModel(Protocol):
    """子图依赖的最小模型协议，单测可以注入 scripted fake。"""

    async def analyze_daily(
        self,
        request: DailyFundamentalInput,
    ) -> DailyFundamentalAnalysis: ...

    async def plan_targeted(
        self,
        request: TargetedFundamentalInput,
    ) -> TargetedFundamentalPlan: ...

    async def review_verification(
        self,
        request: FundamentalReviewInput,
    ) -> FundamentalReviewDecision: ...


class OpenAIFundamentalReasoningModel:
    """通过三个结构化输出通道调用兼容 OpenAI 的聊天模型。"""

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
        self._daily = build_observable_structured_output(
            chat_model,
            DailyFundamentalAnalysis,
            method=structured_output_method,
            operation="fundamental.analyze_daily",
            options=structured_output_options,
        )

    async def analyze_daily(
        self,
        request: DailyFundamentalInput,
    ) -> DailyFundamentalAnalysis:
        result = await self._daily.ainvoke(
            [
                SystemMessage(content=DAILY_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return DailyFundamentalAnalysis.model_validate(result)

    async def plan_targeted(
        self,
        request: TargetedFundamentalInput,
    ) -> TargetedFundamentalPlan:
        targeted = build_observable_structured_output(
            self._chat_model,
            TargetedFundamentalPlan,
            method=self._structured_output_method,
            operation="fundamental.plan_targeted",
            options=self._structured_output_options,
            pre_validate=lambda value: _normalize_targeted_periods(value, request),
        )
        result = await targeted.ainvoke(
            [
                SystemMessage(content=TARGETED_PLANNING_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return TargetedFundamentalPlan.model_validate(result)

    async def review_verification(
        self,
        request: FundamentalReviewInput,
    ) -> FundamentalReviewDecision:
        allowed_call_ids = _citable_review_call_ids(request)
        review = build_observable_structured_output(
            self._chat_model,
            FundamentalReviewDecision,
            method=self._structured_output_method,
            operation="fundamental.review_verification",
            options=self._structured_output_options,
            json_schema_override=(
                _review_json_schema(allowed_call_ids)
                if self._structured_output_method == "json_schema"
                else None
            ),
            post_validate=lambda value: _validate_review_call_ids(
                value,
                allowed_call_ids,
            ),
            pre_validate=lambda value: _normalize_review_periods(value, request),
        )
        result = await review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return FundamentalReviewDecision.model_validate(result)


def _citable_review_call_ids(request: FundamentalReviewInput) -> frozenset[str]:
    return frozenset(
        observation.call_id
        for observation in request.observations
        if observation.tool_name != "resolve_stock_identity"
        and observation.result.get("status") in {"ok", "partial", "empty"}
    )


def _review_json_schema(allowed_call_ids: frozenset[str]) -> dict[str, object]:
    schema = deepcopy(FundamentalReviewDecision.model_json_schema())
    source_ids = schema["$defs"]["FundamentalEvidenceDraft"]["properties"]["source_call_ids"]
    if allowed_call_ids:
        source_ids["items"] = {
            "type": "string",
            "enum": sorted(allowed_call_ids),
        }
        source_ids["description"] = (
            "只能从本轮真实、可引用的 Tool 调用编号中选择；不得改写或自行构造编号"
        )
    else:
        source_ids["description"] = "本轮没有可形成基本面证据的 Tool 调用"
    return schema


def _validate_review_call_ids(
    decision: FundamentalReviewDecision,
    allowed_call_ids: frozenset[str],
) -> FundamentalReviewDecision:
    unknown = sorted(
        {
            call_id
            for evidence in decision.evidence
            for call_id in evidence.source_call_ids
            if call_id not in allowed_call_ids
        }
    )
    if unknown:
        raise ValueError("source_call_ids 包含本轮不存在或不可引用的调用：" + ", ".join(unknown))
    return decision


_PERIOD_CHECK_VALUES = {
    "FINANCIAL_STATEMENTS",
    "FINANCIAL_QUALITY",
    "EARNINGS_DISCLOSURE",
    "DIVIDEND_OWNERSHIP",
}
_COMPARABLE_CHECK_VALUES = {
    "FINANCIAL_STATEMENTS",
    "FINANCIAL_QUALITY",
    "EARNINGS_DISCLOSURE",
}


def _normalize_targeted_periods(
    payload: Any,
    request: TargetedFundamentalInput,
) -> Any:
    cutoff = min(request.as_of.date(), request.research_request.time_range.end)
    return _normalize_request_periods(
        payload,
        field_name="verification_requests",
        cutoff=cutoff,
        periods_by_target={},
    )


def _normalize_review_periods(
    payload: Any,
    request: FundamentalReviewInput,
) -> Any:
    periods_by_target = {
        task.target.code: task.report_period
        for task in request.tasks
        if task.report_period is not None
    }
    return _normalize_request_periods(
        payload,
        field_name="follow_up_requests",
        cutoff=request.as_of.date(),
        periods_by_target=periods_by_target,
    )


def _normalize_request_periods(
    payload: Any,
    *,
    field_name: str,
    cutoff: date,
    periods_by_target: Mapping[str, str],
) -> Any:
    """在业务 Schema 校验前，把缺失或越界报告期稳定落到已披露季度末。"""

    if not isinstance(payload, Mapping):
        return payload
    normalized = deepcopy(dict(payload))
    raw_requests = normalized.get(field_name)
    if not isinstance(raw_requests, (list, tuple)):
        return normalized

    normalized_requests: list[Any] = []
    default_period = _latest_quarter_end(cutoff)
    for raw_request in raw_requests:
        if not isinstance(raw_request, Mapping):
            normalized_requests.append(raw_request)
            continue
        item = dict(raw_request)
        checks = {str(value) for value in item.get("checks") or ()}
        if not checks.intersection(_PERIOD_CHECK_VALUES):
            normalized_requests.append(item)
            continue

        target = item.get("target")
        target_code = str(target.get("code") or "") if isinstance(target, Mapping) else ""
        inherited = periods_by_target.get(target_code)
        report_period = _valid_quarter_period(item.get("report_period"), cutoff=cutoff)
        item["report_period"] = report_period or inherited or default_period

        if item.get("comparison_period") is not None:
            comparison = _valid_quarter_period(
                item.get("comparison_period"),
                cutoff=cutoff,
            )
            expected = _previous_year_period(item["report_period"])
            if (
                not checks.intersection(_COMPARABLE_CHECK_VALUES)
                or comparison != expected
            ):
                item["comparison_period"] = (
                    expected if checks.intersection(_COMPARABLE_CHECK_VALUES) else None
                )
        normalized_requests.append(item)
    normalized[field_name] = normalized_requests
    return normalized


def _valid_quarter_period(value: Any, *, cutoff: date) -> str | None:
    text = str(value or "")
    if len(text) != 8 or not text.isdigit() or text[4:] not in {"0331", "0630", "0930", "1231"}:
        return None
    try:
        parsed = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None
    return text if parsed <= cutoff else None


def _latest_quarter_end(cutoff: date) -> str:
    candidates = (
        date(cutoff.year, 3, 31),
        date(cutoff.year, 6, 30),
        date(cutoff.year, 9, 30),
        date(cutoff.year, 12, 31),
    )
    latest = next((candidate for candidate in reversed(candidates) if candidate <= cutoff), None)
    if latest is None:
        latest = date(cutoff.year - 1, 12, 31)
    return latest.strftime("%Y%m%d")


def _previous_year_period(report_period: str) -> str:
    return f"{int(report_period[:4]) - 1:04d}{report_period[4:]}"
