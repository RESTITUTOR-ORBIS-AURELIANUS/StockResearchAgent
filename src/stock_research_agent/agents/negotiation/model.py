"""正式协商三阶段模型协议与 OpenAI-compatible 适配器。"""

from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from stock_research_agent.agents.negotiation.models import (
    DebateScoreDraft,
    DebateScoreInput,
    ProposalRevisionDraft,
    ProposalRevisionInput,
    ReasonExchangeDraft,
    ReasonExchangeInput,
)
from stock_research_agent.agents.negotiation.prompts import (
    debate_score_prompt,
    proposal_revision_prompt,
    reason_exchange_prompt,
)
from stock_research_agent.domain.enums import PortfolioManager
from stock_research_agent.llm.structured_output import (
    StructuredOutputOptions,
    build_observable_structured_output,
)


class PortfolioNegotiationModel(Protocol):
    async def exchange_reasons(self, request: ReasonExchangeInput) -> ReasonExchangeDraft: ...

    async def revise_proposals(self, request: ProposalRevisionInput) -> ProposalRevisionDraft: ...

    async def score_revisions(self, request: DebateScoreInput) -> DebateScoreDraft: ...


class OpenAIPortfolioNegotiationModel:
    """把同一经理身份绑定到理由、修订和重评三个严格 Schema。"""

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        manager: PortfolioManager,
        structured_output_method: Literal["function_calling", "json_schema"] = "function_calling",
        structured_output_options: StructuredOutputOptions | None = None,
    ) -> None:
        self._manager = manager
        self._reason_prompt = reason_exchange_prompt(manager)
        self._revision_prompt = proposal_revision_prompt(manager)
        self._score_prompt = debate_score_prompt(manager)
        self._exchange = build_observable_structured_output(
            chat_model,
            ReasonExchangeDraft,
            method=structured_output_method,
            operation=f"negotiation.{manager.value.lower()}.exchange_reasons",
            options=structured_output_options,
        )
        self._revise = build_observable_structured_output(
            chat_model,
            ProposalRevisionDraft,
            method=structured_output_method,
            operation=f"negotiation.{manager.value.lower()}.revise_proposals",
            options=structured_output_options,
        )
        self._score = build_observable_structured_output(
            chat_model,
            DebateScoreDraft,
            method=structured_output_method,
            operation=f"negotiation.{manager.value.lower()}.score_revisions",
            options=structured_output_options,
        )

    async def exchange_reasons(self, request: ReasonExchangeInput) -> ReasonExchangeDraft:
        if request.reviewer is not self._manager:
            raise ValueError("reason-exchange request was sent to the wrong manager model")
        result = await self._exchange.ainvoke(
            [
                SystemMessage(content=self._reason_prompt),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return ReasonExchangeDraft.model_validate(result)

    async def revise_proposals(self, request: ProposalRevisionInput) -> ProposalRevisionDraft:
        if request.proposer is not self._manager:
            raise ValueError("proposal-revision request was sent to the wrong manager model")
        result = await self._revise.ainvoke(
            [
                SystemMessage(content=self._revision_prompt),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return ProposalRevisionDraft.model_validate(result)

    async def score_revisions(self, request: DebateScoreInput) -> DebateScoreDraft:
        if request.manager is not self._manager:
            raise ValueError("debate-score request was sent to the wrong manager model")
        result = await self._score.ainvoke(
            [
                SystemMessage(content=self._score_prompt),
                HumanMessage(content=request.model_dump_json(indent=2)),
            ]
        )
        return DebateScoreDraft.model_validate(result)


class OpenAIAggressivePortfolioNegotiationModel(OpenAIPortfolioNegotiationModel):
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


class OpenAIConservativePortfolioNegotiationModel(OpenAIPortfolioNegotiationModel):
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
