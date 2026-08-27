"""投资组合经理模型协议及 OpenAI-compatible 结构化输出适配器。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.portfolio.models import (
    PortfolioRecommendationDraft,
    PortfolioRecommendationInput,
)
from stock_research_agent.agents.portfolio.prompts import portfolio_prompt_for_manager
from stock_research_agent.domain.enums import PortfolioManager
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class PortfolioManagerModel(Protocol):
    async def generate_recommendation(
        self,
        request: PortfolioRecommendationInput,
    ) -> PortfolioRecommendationDraft: ...


class OpenAIPortfolioManagerModel:
    """用同一输出 Schema 配合固定角色 Prompt 生成独立建议。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        manager: PortfolioManager,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._manager = manager
        self._prompt = portfolio_prompt_for_manager(manager.value)
        self._generation = build_observable_structured_output(
            chat_model,
            PortfolioRecommendationDraft,
            method=structured_output_method,
            operation=f"portfolio.{manager.value.lower()}.generate_recommendation",
            options=structured_output_options,
        )

    async def generate_recommendation(
        self,
        request: PortfolioRecommendationInput,
    ) -> PortfolioRecommendationDraft:
        if request.manager is not self._manager:
            raise ValueError("portfolio request was sent to the wrong manager model")
        result = await self._generation.ainvoke(
            [
                SystemMessage(content=self._prompt),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return PortfolioRecommendationDraft.model_validate(result)


class OpenAIAggressivePortfolioManagerModel(OpenAIPortfolioManagerModel):
    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        super().__init__(
            chat_model,
            manager=PortfolioManager.AGGRESSIVE,
            structured_output_method=structured_output_method,
            structured_output_options=structured_output_options,
        )


class OpenAIConservativePortfolioManagerModel(OpenAIPortfolioManagerModel):
    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        super().__init__(
            chat_model,
            manager=PortfolioManager.CONSERVATIVE,
            structured_output_method=structured_output_method,
            structured_output_options=structured_output_options,
        )
