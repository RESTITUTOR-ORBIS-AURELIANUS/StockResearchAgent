"""集中创建和关闭可复用的 HTTP 客户端与 Provider。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from stock_research_agent.config import ProviderSettings
from stock_research_agent.providers.backup import BackupTushareProvider
from stock_research_agent.providers.cache import InMemoryProviderCache, ProviderCache
from stock_research_agent.providers.primary import PrimaryRestProvider
from stock_research_agent.providers.router import RoutedMarketDataProvider


@asynccontextmanager
async def open_market_data_provider(
    settings: ProviderSettings,
    cache: ProviderCache | None = None,
) -> AsyncIterator[RoutedMarketDataProvider]:
    """在一个生命周期中复用连接池，并在退出时释放网络资源。"""

    async with httpx.AsyncClient() as client:
        primary = PrimaryRestProvider(
            client,
            str(settings.primary_base_url),
            settings.primary_api_key,
            settings.request_timeout_seconds,
        )
        backup = BackupTushareProvider(
            client,
            str(settings.backup_base_url),
            settings.backup_token,
            settings.request_timeout_seconds,
        )
        yield RoutedMarketDataProvider(
            primary,
            backup,
            cache or InMemoryProviderCache(),
            allow_paid_fallback=settings.allow_paid_fallback,
        )
