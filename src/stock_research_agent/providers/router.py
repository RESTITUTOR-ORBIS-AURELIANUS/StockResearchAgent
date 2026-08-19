"""按照路由表选择主服务器、备用服务器和缓存。"""

import asyncio

from stock_research_agent.providers.cache import ProviderCache, provider_cache_key
from stock_research_agent.providers.errors import (
    DataSourceUnavailableError,
    ProviderErrorCode,
    ProviderTransportError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult
from stock_research_agent.providers.protocol import MarketDataProvider
from stock_research_agent.providers.routes import RouteMode, get_route


class RoutedMarketDataProvider:
    """研究 Agent 唯一需要依赖的行情查询入口。"""

    def __init__(
        self,
        primary: MarketDataProvider,
        backup: MarketDataProvider,
        cache: ProviderCache,
        *,
        allow_paid_fallback: bool = False,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self._cache = cache
        self._allow_paid_fallback = allow_paid_fallback
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_locks_guard = asyncio.Lock()

    async def query(self, request: ProviderQuery) -> ProviderResult:
        route = get_route(request.api_name)
        if route.mode is RouteMode.UNAVAILABLE:
            raise DataSourceUnavailableError(
                ProviderErrorCode.DATA_SOURCE_UNAVAILABLE,
                request.api_name,
                "主备服务器均不可用",
            )
        if route.mode is RouteMode.BACKUP:
            return await self._query_cached(
                self._backup,
                request,
                route.cache_ttl_seconds,
            )
        if route.mode is RouteMode.PRIMARY_CACHED:
            return await self._query_cached(
                self._primary,
                request,
                route.cache_ttl_seconds,
            )
        return await self._query_primary(request)

    async def _query_primary(self, request: ProviderQuery) -> ProviderResult:
        try:
            return await self._primary.query(request)
        except ProviderTransportError:
            if not self._allow_paid_fallback:
                raise
            return await self._backup.query(request)

    async def _query_cached(
        self,
        provider: MarketDataProvider,
        request: ProviderQuery,
        ttl_seconds: int | None,
    ) -> ProviderResult:
        if ttl_seconds is None:
            return await provider.query(request)

        key = provider_cache_key(request)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        lock = await self._lock_for(key)
        async with lock:
            cached = await self._cache.get(key)
            if cached is not None:
                return cached
            result = await provider.query(request)
            await self._cache.put(key, result, ttl_seconds)
            return result

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._key_locks_guard:
            return self._key_locks.setdefault(key, asyncio.Lock())
