"""投资论点审查员的模型协议与 OpenAI-compatible 适配器。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.validator.models import (
    ThesisValidationDecision,
    ThesisValidationInput,
    ThesisValidationModelOutput,
)
from stock_research_agent.agents.validator.prompts import THESIS_VALIDATION_SYSTEM_PROMPT
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class ThesisValidationAnalystModel(Protocol):
    async def review_thesis(
        self,
        request: ThesisValidationInput,
    ) -> ThesisValidationDecision: ...


class OpenAIThesisValidationAnalystModel:
    """每次重放同一观点的完整会话，并要求 Schema 约束的二选一决策。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._review = build_observable_structured_output(
            chat_model,
            ThesisValidationModelOutput,
            method=structured_output_method,
            operation="validator.review_thesis",
            options=structured_output_options,
        )

    async def review_thesis(
        self,
        request: ThesisValidationInput,
    ) -> ThesisValidationDecision:
        result = await self._review.ainvoke(
            [
                SystemMessage(content=THESIS_VALIDATION_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return ThesisValidationModelOutput.model_validate(result).to_domain_decision()
