"""最终委员会建议组装模型协议与 OpenAI-compatible 适配器。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.consensus_assembly.models import (
    ConsensusRecommendationSynthesisDraft,
    ConsensusRecommendationSynthesisInput,
)
from stock_research_agent.agents.consensus_assembly.prompts import (
    CONSENSUS_RECOMMENDATION_SYNTHESIS_PROMPT,
)
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class ConsensusRecommendationSynthesisModel(Protocol):
    async def synthesize(
        self,
        request: ConsensusRecommendationSynthesisInput,
    ) -> ConsensusRecommendationSynthesisDraft: ...


class OpenAIConsensusRecommendationSynthesisModel:
    """让模型只生成 RecommendationRecord 的顶层文字表达。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._generation = build_observable_structured_output(
            chat_model,
            ConsensusRecommendationSynthesisDraft,
            method=structured_output_method,
            operation="consensus.synthesize",
            options=structured_output_options,
        )

    async def synthesize(
        self,
        request: ConsensusRecommendationSynthesisInput,
    ) -> ConsensusRecommendationSynthesisDraft:
        result = await self._generation.ainvoke(
            [
                SystemMessage(content=CONSENSUS_RECOMMENDATION_SYNTHESIS_PROMPT),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return ConsensusRecommendationSynthesisDraft.model_validate(result)
