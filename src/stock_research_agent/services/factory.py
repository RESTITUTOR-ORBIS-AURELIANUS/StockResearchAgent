"""源数据 Service 与每日聚合 Service 的组合根。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from stock_research_agent.config import AkshareSettings, ProviderSettings
from stock_research_agent.providers.cache import ProviderCache
from stock_research_agent.providers.errors import DataSourceUnavailableError, ProviderErrorCode
from stock_research_agent.providers.factory import (
    open_akshare_news_provider,
    open_market_data_provider,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult
from stock_research_agent.providers.protocol import MarketDataProvider
from stock_research_agent.services.cross_asset_market_data import CrossAssetMarketDataService
from stock_research_agent.services.daily_event_snapshot import DailyEventSnapshotService
from stock_research_agent.services.daily_fundamental_snapshot import (
    DailyFundamentalSnapshotService,
)
from stock_research_agent.services.daily_sentiment_flow_snapshot import (
    DailySentimentFlowSnapshotService,
)
from stock_research_agent.services.daily_technical_snapshot import DailyTechnicalSnapshotService
from stock_research_agent.services.equity_market_data import EquityMarketDataService
from stock_research_agent.services.fundamental_data import FundamentalDataService
from stock_research_agent.services.instrument_reference import InstrumentReferenceService
from stock_research_agent.services.macro_data import MacroDataService
from stock_research_agent.services.news_event import NewsEventDataService
from stock_research_agent.services.ownership_event import OwnershipEventService
from stock_research_agent.services.public_news_event import PublicNewsEventService
from stock_research_agent.services.trading_behavior import TradingBehaviorService


@dataclass(frozen=True, slots=True)
class DataServices:
    """一次运行内共享 Tushare 路由与 AKShare 新闻 Provider 的 Service 组合根。"""

    instrument_reference: InstrumentReferenceService
    equity_market_data: EquityMarketDataService
    cross_asset_market_data: CrossAssetMarketDataService
    fundamental_data: FundamentalDataService
    macro_data: MacroDataService
    ownership_event: OwnershipEventService
    trading_behavior: TradingBehaviorService
    public_news_event: PublicNewsEventService
    news_event: NewsEventDataService
    daily_technical_snapshot: DailyTechnicalSnapshotService
    daily_sentiment_flow_snapshot: DailySentimentFlowSnapshotService
    daily_fundamental_snapshot: DailyFundamentalSnapshotService
    daily_event_snapshot: DailyEventSnapshotService


def build_data_services(
    provider: MarketDataProvider,
    *,
    public_news_provider: MarketDataProvider | None = None,
    page_size: int = 1_000,
    max_pages: int = 50,
    max_rows: int = 50_000,
) -> DataServices:
    """分别注入结构化市场 Provider 与公开新闻 Provider，再组合每日快照。"""

    options = {
        "page_size": page_size,
        "max_pages": max_pages,
        "max_rows": max_rows,
    }
    instrument_reference = InstrumentReferenceService(provider, **options)
    equity_market_data = EquityMarketDataService(provider, **options)
    fundamental_data = FundamentalDataService(provider, **options)
    macro_data = MacroDataService(provider, **options)
    trading_behavior = TradingBehaviorService(provider, **options)
    public_news_event = PublicNewsEventService(
        public_news_provider or _UnavailablePublicNewsProvider(),
        **options,
    )
    news_event = NewsEventDataService(provider, **options)
    daily_technical_snapshot = DailyTechnicalSnapshotService(
        instrument_reference,
        equity_market_data,
    )
    return DataServices(
        instrument_reference=instrument_reference,
        equity_market_data=equity_market_data,
        cross_asset_market_data=CrossAssetMarketDataService(provider, **options),
        fundamental_data=fundamental_data,
        macro_data=macro_data,
        ownership_event=OwnershipEventService(provider, **options),
        trading_behavior=trading_behavior,
        public_news_event=public_news_event,
        news_event=news_event,
        daily_technical_snapshot=daily_technical_snapshot,
        daily_sentiment_flow_snapshot=DailySentimentFlowSnapshotService(
            daily_technical_snapshot,
            trading_behavior,
        ),
        daily_fundamental_snapshot=DailyFundamentalSnapshotService(
            instrument_reference,
            equity_market_data,
            fundamental_data,
            macro_data,
        ),
        daily_event_snapshot=DailyEventSnapshotService(
            public_news_event,
            news_event,
            instrument_reference,
        ),
    )


@asynccontextmanager
async def open_data_services(
    settings: ProviderSettings,
    *,
    akshare_settings: AkshareSettings | None = None,
    cache: ProviderCache | None = None,
    page_size: int = 1_000,
    max_pages: int = 50,
    max_rows: int = 50_000,
) -> AsyncIterator[DataServices]:
    """把 HTTP 连接池和所有 Service 绑定在同一个安全生命周期内。"""

    async with open_market_data_provider(settings, cache) as provider:
        async with open_akshare_news_provider(
            akshare_settings or AkshareSettings()
        ) as public_news_provider:
            yield build_data_services(
                provider,
                public_news_provider=public_news_provider,
                page_size=page_size,
                max_pages=max_pages,
                max_rows=max_rows,
            )


class _UnavailablePublicNewsProvider:
    """测试或手工组装时未注入 AKShare Provider 的明确失败边界。"""

    async def query(self, request: ProviderQuery) -> ProviderResult:
        raise DataSourceUnavailableError(
            ProviderErrorCode.DATA_SOURCE_UNAVAILABLE,
            request.api_name,
            "尚未向 build_data_services 注入 public_news_provider",
        )
