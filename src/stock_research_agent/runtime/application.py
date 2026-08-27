"""把配置、数据管线、Agent 模型与 LangGraph 装配为一个可关闭的运行时。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel

from stock_research_agent.agents.consensus_assembly import (
    ConsensusRecommendationSynthesisModel,
    OpenAIConsensusRecommendationSynthesisModel,
)
from stock_research_agent.agents.debate import (
    OpenAIAggressivePortfolioCrossReviewModel,
    OpenAIConservativePortfolioCrossReviewModel,
    PortfolioCrossReviewModel,
)
from stock_research_agent.agents.event import (
    EventReasoningModel,
    OpenAIEventReasoningModel,
    build_event_agent_graph,
)
from stock_research_agent.agents.fundamental import (
    FundamentalReasoningModel,
    OpenAIFundamentalReasoningModel,
    build_fundamental_agent_graph,
)
from stock_research_agent.agents.negotiation import (
    OpenAIAggressivePortfolioNegotiationModel,
    OpenAIConservativePortfolioNegotiationModel,
    PortfolioNegotiationModel,
)
from stock_research_agent.agents.portfolio import (
    OpenAIAggressivePortfolioManagerModel,
    OpenAIConservativePortfolioManagerModel,
    PortfolioManagerModel,
)
from stock_research_agent.agents.sentiment_flow import (
    OpenAISentimentFlowReasoningModel,
    SentimentFlowReasoningModel,
    build_sentiment_flow_agent_graph,
)
from stock_research_agent.agents.strategist import (
    LeadResearchStrategistModel,
    OpenAILeadResearchStrategistModel,
)
from stock_research_agent.agents.technical import (
    OpenAITechnicalReasoningModel,
    TechnicalReasoningModel,
    build_technical_agent_graph,
)
from stock_research_agent.agents.validator import (
    OpenAIThesisValidationAnalystModel,
    ThesisValidationAnalystModel,
)
from stock_research_agent.config import AkshareSettings, LLMSettings, ProviderSettings
from stock_research_agent.graph.builder import build_research_graph
from stock_research_agent.graph.nodes.event_research import EventAgentGraphFactory
from stock_research_agent.graph.nodes.fundamental_research import FundamentalAgentGraphFactory
from stock_research_agent.graph.nodes.sentiment_flow_research import (
    SentimentFlowAgentGraphFactory,
)
from stock_research_agent.graph.nodes.technical_research import TechnicalAgentGraphFactory
from stock_research_agent.llm.factory import build_chat_model
from stock_research_agent.llm.structured_output import StructuredOutputOptions
from stock_research_agent.providers.akshare_news import AkshareNewsProvider
from stock_research_agent.providers.backup import BackupTushareProvider
from stock_research_agent.providers.cache import InMemoryProviderCache, ProviderCache
from stock_research_agent.providers.primary import PrimaryRestProvider
from stock_research_agent.providers.router import RoutedMarketDataProvider
from stock_research_agent.research_data import (
    InMemoryResearchDataStore,
    ResearchDataStore,
    validate_run_id,
)
from stock_research_agent.services import DataServices, build_data_services
from stock_research_agent.tools import (
    AgentToolRegistry,
    ResearchToolContext,
    ToolLimits,
    build_agent_tool_registry,
)


@dataclass(frozen=True, slots=True)
class ResearchRuntimeSettings:
    """完整应用组合根的配置；凭据继续由原有 SecretStr Settings 持有。"""

    providers: ProviderSettings
    llm: LLMSettings
    akshare: AkshareSettings = field(default_factory=AkshareSettings)
    tool_limits: ToolLimits = field(default_factory=ToolLimits)
    service_page_size: int = 1_000
    service_max_pages: int = 50
    service_max_rows: int = 50_000
    graph_recursion_limit: int = 300

    def __post_init__(self) -> None:
        if self.service_page_size < 1:
            raise ValueError("service_page_size 必须大于 0")
        if self.service_max_pages < 1:
            raise ValueError("service_max_pages 必须大于 0")
        if self.service_max_rows < self.service_page_size:
            raise ValueError("service_max_rows 不能小于 service_page_size")
        if not 50 <= self.graph_recursion_limit <= 1_000:
            raise ValueError("graph_recursion_limit 必须在 50 到 1000 之间")

    @classmethod
    def from_env(cls) -> "ResearchRuntimeSettings":
        """显式读取 ``.env``；导入模块本身不会提前要求任何密钥。"""

        return cls(
            providers=ProviderSettings(),
            akshare=AkshareSettings(),
            llm=LLMSettings(),
        )


@dataclass(frozen=True, slots=True)
class RuntimeModelDependencies:
    """主图需要的所有模型端口，全部复用同一个 ChatModel。"""

    technical: TechnicalReasoningModel
    sentiment_flow: SentimentFlowReasoningModel
    fundamental: FundamentalReasoningModel
    event: EventReasoningModel
    lead_research_strategist: LeadResearchStrategistModel
    thesis_validator: ThesisValidationAnalystModel
    aggressive_portfolio_manager: PortfolioManagerModel
    conservative_portfolio_manager: PortfolioManagerModel
    aggressive_cross_review: PortfolioCrossReviewModel
    conservative_cross_review: PortfolioCrossReviewModel
    aggressive_negotiation: PortfolioNegotiationModel
    conservative_negotiation: PortfolioNegotiationModel
    consensus_assembly: ConsensusRecommendationSynthesisModel


@dataclass(frozen=True, slots=True)
class EvidenceAgentGraphFactories:
    """四个证据 Agent 的 run-scoped 子图工厂。"""

    technical: TechnicalAgentGraphFactory
    sentiment_flow: SentimentFlowAgentGraphFactory
    fundamental: FundamentalAgentGraphFactory
    event: EventAgentGraphFactory


@dataclass(frozen=True, slots=True)
class RunScopedToolResources:
    """同一 ``run_id`` 下四位证据研究员共享的 Tool 组合根。"""

    context: ResearchToolContext
    registry: AgentToolRegistry


class RunScopedToolResourceManager:
    """按运行延迟创建 ToolContext，并负责清理完整原始数据快照。"""

    def __init__(
        self,
        services: DataServices,
        data_store: ResearchDataStore,
        limits: ToolLimits,
    ) -> None:
        self._services = services
        self._data_store = data_store
        self._limits = limits
        self._resources: dict[str, RunScopedToolResources] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))

    async def resolve(self, *, run_id: str, as_of: datetime) -> RunScopedToolResources:
        """返回当前运行唯一的 ToolContext；同 ID 不允许切换截止时间。"""

        validate_run_id(run_id)
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("运行期 ToolContext 的 as_of 必须带时区")

        async with self._lock:
            if self._closed:
                raise RuntimeError("runtime 已关闭，不能再创建 ToolContext")
            existing = self._resources.get(run_id)
            if existing is not None:
                normalized_as_of = ResearchToolContext(
                    services=self._services,
                    as_of=as_of,
                    limits=self._limits,
                    run_id=run_id,
                    data_store=self._data_store,
                ).as_of
                if existing.context.as_of != normalized_as_of:
                    raise ValueError("同一 run_id 不能对应不同的 as_of")
                return existing

            context = ResearchToolContext(
                services=self._services,
                as_of=as_of,
                limits=self._limits,
                run_id=run_id,
                data_store=self._data_store,
            )
            resources = RunScopedToolResources(
                context=context,
                registry=build_agent_tool_registry(context),
            )
            self._resources[run_id] = resources
            return resources

    async def cleanup(self, run_id: str) -> int:
        """释放 Tool 对象并删除本轮保存的所有完整数据快照。"""

        validate_run_id(run_id)
        async with self._lock:
            self._resources.pop(run_id, None)
            return await self._data_store.cleanup(run_id)

    async def aclose(self) -> None:
        """幂等关闭所有仍存活的运行资源。"""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            run_ids = tuple(self._resources)
            self._resources.clear()
        for run_id in run_ids:
            await self._data_store.cleanup(run_id)


@dataclass(slots=True)
class ResearchRuntime:
    """可执行图及其全部长生命周期资源；结束使用后必须调用 ``aclose``。"""

    settings: ResearchRuntimeSettings
    http_client: httpx.AsyncClient
    market_data_provider: RoutedMarketDataProvider
    public_news_provider: AkshareNewsProvider
    services: DataServices
    data_store: ResearchDataStore
    tool_resources: RunScopedToolResourceManager
    chat_model: BaseChatModel
    models: RuntimeModelDependencies
    evidence_graph_factories: EvidenceAgentGraphFactories
    graph: Any
    _exit_stack: AsyncExitStack
    _closed: bool = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def tool_registry_for(
        self,
        *,
        run_id: str,
        as_of: datetime,
    ) -> AgentToolRegistry:
        """供诊断或受控扩展读取本轮与 Agent 相同的 Tool 白名单。"""

        resources = await self.tool_resources.resolve(run_id=run_id, as_of=as_of)
        return resources.registry

    async def cleanup_run(self, run_id: str) -> int:
        """一次图运行结果不再需要原始 ``context_ref`` 后，显式回收其数据。"""

        return await self.tool_resources.cleanup(run_id)

    async def ainvoke(
        self,
        state: Mapping[str, object],
        *,
        config: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        """执行完整研究图；结果消费完后由调用方用 ``cleanup_run`` 回收原始数据。"""

        if self._closed:
            raise RuntimeError("runtime 已关闭，不能再执行研究图")
        effective_config = dict(config or {})
        effective_config.setdefault(
            "recursion_limit",
            self.settings.graph_recursion_limit,
        )
        return await self.graph.ainvoke(state, config=effective_config)

    async def aclose(self) -> None:
        """按 Tool 数据、AKShare 线程池、共享 HTTP 连接池的顺序幂等关闭。"""

        if self._closed:
            return
        self._closed = True
        try:
            await self.tool_resources.aclose()
        finally:
            await self._exit_stack.aclose()

    async def __aenter__(self) -> "ResearchRuntime":
        if self._closed:
            raise RuntimeError("不能重新进入已经关闭的 runtime")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


async def create_research_runtime(
    settings: ResearchRuntimeSettings,
    *,
    cache: ProviderCache | None = None,
    data_store: ResearchDataStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ResearchRuntime:
    """创建完整生产装配；函数只建连接池和对象，不会主动调用任何外部 API。"""

    stack = AsyncExitStack()
    try:
        http_client = await stack.enter_async_context(httpx.AsyncClient(transport=transport))
        public_news_provider = AkshareNewsProvider(
            timeout_seconds=settings.akshare.request_timeout_seconds,
            max_workers=settings.akshare.max_workers,
        )
        stack.push_async_callback(public_news_provider.aclose)

        primary = PrimaryRestProvider(
            http_client,
            str(settings.providers.primary_base_url),
            settings.providers.primary_api_key,
            settings.providers.request_timeout_seconds,
        )
        backup = BackupTushareProvider(
            http_client,
            str(settings.providers.backup_base_url),
            settings.providers.backup_token,
            settings.providers.request_timeout_seconds,
        )
        market_data_provider = RoutedMarketDataProvider(
            primary,
            backup,
            cache if cache is not None else InMemoryProviderCache(),
        )
        services = build_data_services(
            market_data_provider,
            public_news_provider=public_news_provider,
            page_size=settings.service_page_size,
            max_pages=settings.service_max_pages,
            max_rows=settings.service_max_rows,
        )
        resolved_data_store = (
            data_store if data_store is not None else InMemoryResearchDataStore()
        )
        tool_resources = RunScopedToolResourceManager(
            services,
            resolved_data_store,
            settings.tool_limits,
        )

        chat_model = build_chat_model(
            settings.llm,
            http_async_client=http_client,
        )
        models = _build_model_dependencies(chat_model, settings.llm)
        evidence_graph_factories = _build_evidence_graph_factories(
            models=models,
            tool_resources=tool_resources,
        )
        graph = build_research_graph(
            technical_agent_graph_factory=evidence_graph_factories.technical,
            sentiment_flow_agent_graph_factory=evidence_graph_factories.sentiment_flow,
            fundamental_agent_graph_factory=evidence_graph_factories.fundamental,
            event_agent_graph_factory=evidence_graph_factories.event,
            lead_research_strategist_model=models.lead_research_strategist,
            thesis_validation_model=models.thesis_validator,
            aggressive_portfolio_manager_model=models.aggressive_portfolio_manager,
            conservative_portfolio_manager_model=models.conservative_portfolio_manager,
        )
        return ResearchRuntime(
            settings=settings,
            http_client=http_client,
            market_data_provider=market_data_provider,
            public_news_provider=public_news_provider,
            services=services,
            data_store=resolved_data_store,
            tool_resources=tool_resources,
            chat_model=chat_model,
            models=models,
            evidence_graph_factories=evidence_graph_factories,
            graph=graph,
            _exit_stack=stack,
        )
    except BaseException:
        await stack.aclose()
        raise


@asynccontextmanager
async def open_research_runtime(
    settings: ResearchRuntimeSettings,
    *,
    cache: ProviderCache | None = None,
    data_store: ResearchDataStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[ResearchRuntime]:
    """``async with`` 便利入口，异常退出也不会泄漏网络或线程池资源。"""

    runtime = await create_research_runtime(
        settings,
        cache=cache,
        data_store=data_store,
        transport=transport,
    )
    try:
        yield runtime
    finally:
        await runtime.aclose()


def _build_model_dependencies(
    chat_model: BaseChatModel,
    settings: LLMSettings,
) -> RuntimeModelDependencies:
    method = settings.structured_output_method
    options = StructuredOutputOptions(
        strict=settings.structured_output_strict,
        repair_attempts=settings.structured_output_repair_attempts,
        diagnostics_path=settings.structured_output_diagnostics_path,
        raw_max_characters=settings.structured_output_raw_max_characters,
    )
    return RuntimeModelDependencies(
        technical=OpenAITechnicalReasoningModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        sentiment_flow=OpenAISentimentFlowReasoningModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        fundamental=OpenAIFundamentalReasoningModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        event=OpenAIEventReasoningModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        lead_research_strategist=OpenAILeadResearchStrategistModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        thesis_validator=OpenAIThesisValidationAnalystModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        aggressive_portfolio_manager=OpenAIAggressivePortfolioManagerModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        conservative_portfolio_manager=OpenAIConservativePortfolioManagerModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        aggressive_cross_review=OpenAIAggressivePortfolioCrossReviewModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        conservative_cross_review=OpenAIConservativePortfolioCrossReviewModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        aggressive_negotiation=OpenAIAggressivePortfolioNegotiationModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        conservative_negotiation=OpenAIConservativePortfolioNegotiationModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
        consensus_assembly=OpenAIConsensusRecommendationSynthesisModel(
            chat_model,
            structured_output_method=method,
            structured_output_options=options,
        ),
    )


def _build_evidence_graph_factories(
    *,
    models: RuntimeModelDependencies,
    tool_resources: RunScopedToolResourceManager,
) -> EvidenceAgentGraphFactories:
    async def resources(run_id: str, as_of: datetime) -> RunScopedToolResources:
        return await tool_resources.resolve(run_id=run_id, as_of=as_of)

    async def technical(*, run_id: str, as_of: datetime):
        resolved = await resources(run_id, as_of)
        return build_technical_agent_graph(
            model=models.technical,
            tool_context=resolved.context,
            tools=resolved.registry.technical,
        )

    async def sentiment_flow(*, run_id: str, as_of: datetime):
        resolved = await resources(run_id, as_of)
        return build_sentiment_flow_agent_graph(
            model=models.sentiment_flow,
            tool_context=resolved.context,
            tools=resolved.registry.sentiment_flow,
        )

    async def fundamental(*, run_id: str, as_of: datetime):
        resolved = await resources(run_id, as_of)
        return build_fundamental_agent_graph(
            model=models.fundamental,
            tool_context=resolved.context,
            tools=resolved.registry.fundamental,
        )

    async def event(*, run_id: str, as_of: datetime):
        resolved = await resources(run_id, as_of)
        return build_event_agent_graph(
            model=models.event,
            tool_context=resolved.context,
            tools=resolved.registry.event,
        )

    return EvidenceAgentGraphFactories(
        technical=technical,
        sentiment_flow=sentiment_flow,
        fundamental=fundamental,
        event=event,
    )
