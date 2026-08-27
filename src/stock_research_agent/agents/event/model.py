"""新闻事件 Agent 的模型协议和 OpenAI-compatible 实现。"""

from typing import Literal, Protocol

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
        self._daily = build_observable_structured_output(
            chat_model,
            DailyEventAnalysis,
            method=structured_output_method,
            operation="event.analyze_daily",
            options=structured_output_options,
        )
        self._targeted = build_observable_structured_output(
            chat_model,
            TargetedEventPlan,
            method=structured_output_method,
            operation="event.plan_targeted",
            options=structured_output_options,
        )
        self._review = build_observable_structured_output(
            chat_model,
            EventReviewDecision,
            method=structured_output_method,
            operation="event.review_verification",
            options=structured_output_options,
        )

    async def analyze_daily(self, request: DailyEventInput) -> DailyEventAnalysis:
        result = await self._daily.ainvoke(
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
        result = await self._review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return EventReviewDecision.model_validate(result)
