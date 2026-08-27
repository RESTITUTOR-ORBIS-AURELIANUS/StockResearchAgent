"""技术 Agent 的模型协议及 OpenAI-compatible 实现。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.technical.models import (
    DailyAnalysisInput,
    DailyTechnicalAnalysis,
    TargetedPlanningInput,
    TargetedTechnicalPlan,
    VerificationReviewDecision,
    VerificationReviewInput,
)
from stock_research_agent.agents.technical.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class TechnicalReasoningModel(Protocol):
    """子图只依赖该协议；单测可注入 scripted fake，不触网。"""

    async def analyze_daily(self, request: DailyAnalysisInput) -> DailyTechnicalAnalysis: ...

    async def plan_targeted(self, request: TargetedPlanningInput) -> TargetedTechnicalPlan: ...

    async def review_verification(
        self,
        request: VerificationReviewInput,
    ) -> VerificationReviewDecision: ...


class OpenAITechnicalReasoningModel:
    """使用三个独立结构化输出通道，避免自由文本再做脆弱 JSON 解析。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._daily = build_observable_structured_output(
            chat_model,
            DailyTechnicalAnalysis,
            method=structured_output_method,
            operation="technical.analyze_daily",
            options=structured_output_options,
        )
        self._targeted = build_observable_structured_output(
            chat_model,
            TargetedTechnicalPlan,
            method=structured_output_method,
            operation="technical.plan_targeted",
            options=structured_output_options,
        )
        self._review = build_observable_structured_output(
            chat_model,
            VerificationReviewDecision,
            method=structured_output_method,
            operation="technical.review_verification",
            options=structured_output_options,
        )

    async def analyze_daily(self, request: DailyAnalysisInput) -> DailyTechnicalAnalysis:
        result = await self._daily.ainvoke(
            [
                SystemMessage(content=DAILY_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return DailyTechnicalAnalysis.model_validate(result)

    async def plan_targeted(self, request: TargetedPlanningInput) -> TargetedTechnicalPlan:
        result = await self._targeted.ainvoke(
            [
                SystemMessage(content=TARGETED_PLANNING_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return TargetedTechnicalPlan.model_validate(result)

    async def review_verification(
        self,
        request: VerificationReviewInput,
    ) -> VerificationReviewDecision:
        result = await self._review.ainvoke(
            [
                SystemMessage(content=VERIFICATION_REVIEW_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return VerificationReviewDecision.model_validate(result)
