"""情绪资金 Agent 的模型协议和 OpenAI-compatible 实现。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.sentiment_flow.models import (
    DailySentimentFlowAnalysis,
    DailySentimentFlowInput,
    SentimentFlowReviewDecision,
    SentimentFlowReviewInput,
    TargetedSentimentFlowInput,
    TargetedSentimentFlowPlan,
)
from stock_research_agent.agents.sentiment_flow.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class SentimentFlowReasoningModel(Protocol):
    """子图依赖的最小模型协议，单测可以注入 scripted fake。"""

    async def analyze_daily(
        self,
        request: DailySentimentFlowInput,
    ) -> DailySentimentFlowAnalysis: ...

    async def plan_targeted(
        self,
        request: TargetedSentimentFlowInput,
    ) -> TargetedSentimentFlowPlan: ...

    async def review_verification(
        self,
        request: SentimentFlowReviewInput,
    ) -> SentimentFlowReviewDecision: ...


class OpenAISentimentFlowReasoningModel:
    """通过三个结构化输出通道调用兼容 OpenAI 的聊天模型。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._daily = build_observable_structured_output(
            chat_model,
            DailySentimentFlowAnalysis,
            method=structured_output_method,
            operation="sentiment_flow.analyze_daily",
            options=structured_output_options,
        )
        self._targeted = build_observable_structured_output(
            chat_model,
            TargetedSentimentFlowPlan,
            method=structured_output_method,
            operation="sentiment_flow.plan_targeted",
            options=structured_output_options,
        )
        self._review = build_observable_structured_output(
            chat_model,
            SentimentFlowReviewDecision,
            method=structured_output_method,
            operation="sentiment_flow.review_verification",
            options=structured_output_options,
        )

    async def analyze_daily(
        self,
        request: DailySentimentFlowInput,
    ) -> DailySentimentFlowAnalysis:
        result = await self._daily.ainvoke(
            [
                SystemMessage(content=DAILY_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return DailySentimentFlowAnalysis.model_validate(result)

    async def plan_targeted(
        self,
        request: TargetedSentimentFlowInput,
    ) -> TargetedSentimentFlowPlan:
        result = await self._targeted.ainvoke(
            [
                SystemMessage(content=TARGETED_PLANNING_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return TargetedSentimentFlowPlan.model_validate(result)

    async def review_verification(
        self,
        request: SentimentFlowReviewInput,
    ) -> SentimentFlowReviewDecision:
        result = await self._review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return SentimentFlowReviewDecision.model_validate(result)
