"""首席研究策略师的模型协议和 OpenAI-compatible 实现。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.strategist.models import (
    CandidateThesisGeneration,
    LeadStrategistInput,
)
from stock_research_agent.agents.strategist.prompts import CANDIDATE_THESIS_SYSTEM_PROMPT
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class LeadResearchStrategistModel(Protocol):
    async def generate_candidates(
        self,
        request: LeadStrategistInput,
    ) -> CandidateThesisGeneration: ...


class OpenAILeadResearchStrategistModel:
    """复用全局 ChatModel，并以结构化输出约束候选观点。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._generation = build_observable_structured_output(
            chat_model,
            CandidateThesisGeneration,
            method=structured_output_method,
            operation="strategist.generate_candidates",
            options=structured_output_options,
        )

    async def generate_candidates(
        self,
        request: LeadStrategistInput,
    ) -> CandidateThesisGeneration:
        result = await self._generation.ainvoke(
            [
                SystemMessage(content=CANDIDATE_THESIS_SYSTEM_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return CandidateThesisGeneration.model_validate(result)
