"""基本面 Agent 的模型协议和 OpenAI-compatible 实现。"""

from typing import Literal, Protocol

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
        self._daily = build_observable_structured_output(
            chat_model,
            DailyFundamentalAnalysis,
            method=structured_output_method,
            operation="fundamental.analyze_daily",
            options=structured_output_options,
        )
        self._targeted = build_observable_structured_output(
            chat_model,
            TargetedFundamentalPlan,
            method=structured_output_method,
            operation="fundamental.plan_targeted",
            options=structured_output_options,
        )
        self._review = build_observable_structured_output(
            chat_model,
            FundamentalReviewDecision,
            method=structured_output_method,
            operation="fundamental.review_verification",
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
        result = await self._targeted.ainvoke(
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
        result = await self._review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return FundamentalReviewDecision.model_validate(result)
