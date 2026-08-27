"""投资组合经理交叉评分模型协议与 OpenAI-compatible 适配器。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.debate.models import (
    PortfolioCrossReviewDraft,
    PortfolioCrossReviewInput,
)
from stock_research_agent.agents.debate.prompts import cross_review_prompt_for_manager
from stock_research_agent.domain.enums import PortfolioManager
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class PortfolioCrossReviewModel(Protocol):
    async def review_recommendation(
        self,
        request: PortfolioCrossReviewInput,
    ) -> PortfolioCrossReviewDraft: ...


class OpenAIPortfolioCrossReviewModel:
    """用固定经理身份和严格 Schema 评价另一位经理的全部条目。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        reviewer: PortfolioManager,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._reviewer = reviewer
        self._prompt = cross_review_prompt_for_manager(reviewer.value)
        self._review = build_observable_structured_output(
            chat_model,
            PortfolioCrossReviewDraft,
            method=structured_output_method,
            operation=f"debate.{reviewer.value.lower()}.cross_review",
            options=structured_output_options,
        )

    async def review_recommendation(
        self,
        request: PortfolioCrossReviewInput,
    ) -> PortfolioCrossReviewDraft:
        if request.reviewer is not self._reviewer:
            raise ValueError("cross-review request was sent to the wrong manager model")
        result = await self._review.ainvoke(
            [
                SystemMessage(content=self._prompt),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return PortfolioCrossReviewDraft.model_validate(result)


class OpenAIAggressivePortfolioCrossReviewModel(OpenAIPortfolioCrossReviewModel):
    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        super().__init__(
            chat_model,
            reviewer=PortfolioManager.AGGRESSIVE,
            structured_output_method=structured_output_method,
            structured_output_options=structured_output_options,
        )


class OpenAIConservativePortfolioCrossReviewModel(OpenAIPortfolioCrossReviewModel):
    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        super().__init__(
            chat_model,
            reviewer=PortfolioManager.CONSERVATIVE,
            structured_output_method=structured_output_method,
            structured_output_options=structured_output_options,
        )
