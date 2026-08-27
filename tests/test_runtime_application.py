"""生产运行时组合根测试；只构造对象，不访问真实行情或大模型接口。"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

from stock_research_agent.agents.consensus_assembly import (
    OpenAIConsensusRecommendationSynthesisModel,
)
from stock_research_agent.agents.debate import (
    OpenAIAggressivePortfolioCrossReviewModel,
    OpenAIConservativePortfolioCrossReviewModel,
)
from stock_research_agent.agents.event import OpenAIEventReasoningModel
from stock_research_agent.agents.fundamental import OpenAIFundamentalReasoningModel
from stock_research_agent.agents.negotiation import (
    OpenAIAggressivePortfolioNegotiationModel,
    OpenAIConservativePortfolioNegotiationModel,
)
from stock_research_agent.agents.portfolio import (
    OpenAIAggressivePortfolioManagerModel,
    OpenAIConservativePortfolioManagerModel,
)
from stock_research_agent.agents.sentiment_flow import OpenAISentimentFlowReasoningModel
from stock_research_agent.agents.strategist import OpenAILeadResearchStrategistModel
from stock_research_agent.agents.technical import OpenAITechnicalReasoningModel
from stock_research_agent.agents.validator import OpenAIThesisValidationAnalystModel
from stock_research_agent.config import AkshareSettings, LLMSettings, ProviderSettings
from stock_research_agent.research_data import InMemoryResearchDataStore
from stock_research_agent.runtime import (
    ResearchRuntimeSettings,
    create_research_runtime,
    open_research_runtime,
)

AS_OF = datetime(2026, 8, 26, 15, 0, tzinfo=timezone(timedelta(hours=8)))
RUN_ID = "run_20260826_150000_A_SHARE_aaaaaaaa"


class RecordingDataStore(InMemoryResearchDataStore):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_calls: list[str] = []

    async def cleanup(self, run_id: str) -> int:
        self.cleanup_calls.append(run_id)
        return await super().cleanup(run_id)


def runtime_settings() -> ResearchRuntimeSettings:
    return ResearchRuntimeSettings(
        providers=ProviderSettings(
            primary_base_url="http://primary.invalid/tushare",
            primary_api_key=SecretStr("test-primary"),
            backup_base_url="https://backup.invalid",
            backup_token=SecretStr("test-backup"),
        ),
        akshare=AkshareSettings(request_timeout_seconds=1, max_workers=1),
        llm=LLMSettings(
            base_url="https://llm.invalid/v1",
            api_key=SecretStr("test-llm"),
            model="test-structured-model",
            structured_output_method="function_calling",
        ),
        service_page_size=20,
        service_max_pages=2,
        service_max_rows=40,
    )


def no_network_transport() -> httpx.MockTransport:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("运行时对象装配不应发起网络请求")

    return httpx.MockTransport(handler)


def test_runtime_settings_reject_invalid_pagination_boundaries() -> None:
    base = runtime_settings()
    with pytest.raises(ValueError, match="service_max_rows"):
        ResearchRuntimeSettings(
            providers=base.providers,
            llm=base.llm,
            akshare=base.akshare,
            service_page_size=100,
            service_max_rows=99,
        )


def test_runtime_settings_reject_too_small_graph_recursion_limit() -> None:
    base = runtime_settings()
    with pytest.raises(ValueError, match="graph_recursion_limit"):
        ResearchRuntimeSettings(
            providers=base.providers,
            llm=base.llm,
            akshare=base.akshare,
            graph_recursion_limit=25,
        )


def test_llm_settings_use_long_timeout_for_large_structured_prompts() -> None:
    settings = LLMSettings(
        base_url="https://llm.invalid/v1",
        api_key=SecretStr("test-llm"),
        model="test-structured-model",
    )

    assert settings.request_timeout_seconds == 1_200.0


def test_runtime_ainvoke_applies_safe_default_and_preserves_explicit_override() -> None:
    class RecordingGraph:
        def __init__(self) -> None:
            self.configs: list[dict] = []

        async def ainvoke(self, state, *, config):
            self.configs.append(config)
            return state

    async def scenario() -> None:
        runtime = await create_research_runtime(
            runtime_settings(),
            transport=no_network_transport(),
        )
        recording_graph = RecordingGraph()
        runtime.graph = recording_graph
        try:
            await runtime.ainvoke({"value": "default"})
            await runtime.ainvoke(
                {"value": "override"},
                config={"recursion_limit": 500, "tags": ["test"]},
            )
        finally:
            await runtime.aclose()

        assert recording_graph.configs == [
            {"recursion_limit": 300},
            {"recursion_limit": 500, "tags": ["test"]},
        ]

    asyncio.run(scenario())


def test_runtime_assembles_v1_no_debate_graph_without_io() -> None:
    async def scenario() -> None:
        runtime = await create_research_runtime(
            runtime_settings(),
            transport=no_network_transport(),
        )
        try:
            assert runtime.chat_model.http_async_client is runtime.http_client
            assert runtime.services.equity_market_data._provider is (
                runtime.market_data_provider
            )
            assert isinstance(runtime.models.technical, OpenAITechnicalReasoningModel)
            assert isinstance(
                runtime.models.sentiment_flow,
                OpenAISentimentFlowReasoningModel,
            )
            assert isinstance(runtime.models.fundamental, OpenAIFundamentalReasoningModel)
            assert isinstance(runtime.models.event, OpenAIEventReasoningModel)
            assert isinstance(
                runtime.models.lead_research_strategist,
                OpenAILeadResearchStrategistModel,
            )
            assert isinstance(
                runtime.models.thesis_validator,
                OpenAIThesisValidationAnalystModel,
            )
            assert isinstance(
                runtime.models.aggressive_portfolio_manager,
                OpenAIAggressivePortfolioManagerModel,
            )
            assert isinstance(
                runtime.models.conservative_portfolio_manager,
                OpenAIConservativePortfolioManagerModel,
            )
            assert isinstance(
                runtime.models.aggressive_cross_review,
                OpenAIAggressivePortfolioCrossReviewModel,
            )
            assert isinstance(
                runtime.models.conservative_cross_review,
                OpenAIConservativePortfolioCrossReviewModel,
            )
            assert isinstance(
                runtime.models.aggressive_negotiation,
                OpenAIAggressivePortfolioNegotiationModel,
            )
            assert isinstance(
                runtime.models.conservative_negotiation,
                OpenAIConservativePortfolioNegotiationModel,
            )
            assert isinstance(
                runtime.models.consensus_assembly,
                OpenAIConsensusRecommendationSynthesisModel,
            )

            graph_nodes = set(runtime.graph.get_graph().nodes)
            assert {
                "initial_technical_evidence",
                "initial_sentiment_flow_evidence",
                "initial_fundamental_evidence",
                "initial_event_evidence",
                "generate_candidate_theses",
                "review_active_thesis",
                "generate_aggressive_recommendation",
                "generate_conservative_recommendation",
                "finalize_independent_recommendations",
                "compose_report",
            } <= graph_nodes
            assert {
                "normalize_proposals",
                "aggressive_cross_review",
                "conservative_cross_review",
                "validate_conflict_scores",
                "exchange_negotiation_reasons",
                "assemble_consensus_recommendation",
            }.isdisjoint(graph_nodes)
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_four_graph_factories_share_one_run_scoped_tool_registry() -> None:
    async def scenario() -> None:
        runtime = await create_research_runtime(
            runtime_settings(),
            transport=no_network_transport(),
        )
        try:
            first = await runtime.tool_resources.resolve(run_id=RUN_ID, as_of=AS_OF)
            second = await runtime.tool_resources.resolve(run_id=RUN_ID, as_of=AS_OF)
            assert second is first
            assert await runtime.tool_registry_for(run_id=RUN_ID, as_of=AS_OF) is first.registry

            factories = runtime.evidence_graph_factories
            graphs = [
                await factories.technical(run_id=RUN_ID, as_of=AS_OF),
                await factories.sentiment_flow(run_id=RUN_ID, as_of=AS_OF),
                await factories.fundamental(run_id=RUN_ID, as_of=AS_OF),
                await factories.event(run_id=RUN_ID, as_of=AS_OF),
            ]
            assert all(callable(getattr(graph, "ainvoke", None)) for graph in graphs)
            assert runtime.tool_resources.active_run_ids == (RUN_ID,)

            with pytest.raises(ValueError, match="同一 run_id"):
                await runtime.tool_resources.resolve(
                    run_id=RUN_ID,
                    as_of=AS_OF + timedelta(days=1),
                )
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_cleanup_and_close_are_explicit_idempotent_lifecycle_operations() -> None:
    async def scenario() -> None:
        data_store = RecordingDataStore()
        runtime = await create_research_runtime(
            runtime_settings(),
            data_store=data_store,
            transport=no_network_transport(),
        )
        await runtime.tool_resources.resolve(run_id=RUN_ID, as_of=AS_OF)

        assert await runtime.cleanup_run(RUN_ID) == 0
        assert runtime.tool_resources.active_run_ids == ()
        assert data_store.cleanup_calls == [RUN_ID]
        await runtime.aclose()
        await runtime.aclose()

        assert runtime.is_closed is True
        assert runtime.http_client.is_closed is True
        with pytest.raises(RuntimeError, match="runtime 已关闭"):
            await runtime.tool_resources.resolve(run_id=RUN_ID, as_of=AS_OF)

    asyncio.run(scenario())


def test_async_context_manager_closes_runtime_on_exception() -> None:
    async def scenario() -> None:
        captured = None
        with pytest.raises(RuntimeError, match="test failure"):
            async with open_research_runtime(
                runtime_settings(),
                transport=no_network_transport(),
            ) as runtime:
                captured = runtime
                raise RuntimeError("test failure")

        assert captured is not None
        assert captured.is_closed is True
        assert captured.http_client.is_closed is True

    asyncio.run(scenario())
