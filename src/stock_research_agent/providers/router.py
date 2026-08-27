"""统一执行“主服务器优先，失败后回退备用服务器”的查询策略。"""

import asyncio

from stock_research_agent.providers.cache import ProviderCache, provider_cache_key
from stock_research_agent.providers.errors import (
    DataSourceUnavailableError,
    ProviderError,
    ProviderErrorCode,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult
from stock_research_agent.providers.protocol import MarketDataProvider
from stock_research_agent.providers.routes import (
    BACKUP_CACHE_TTL_SECONDS,
    ensure_supported_api,
)


class RoutedMarketDataProvider:
    """研究 Agent 唯一需要依赖的行情查询入口。"""

    def __init__(
        self,
        primary: MarketDataProvider,
        backup: MarketDataProvider,
        cache: ProviderCache,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self._cache = cache
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_locks_guard = asyncio.Lock()

    async def query(self, request: ProviderQuery) -> ProviderResult:
        ensure_supported_api(request.api_name)
        return await self._query_primary(request)

    async def _query_primary(self, request: ProviderQuery) -> ProviderResult:
        try:
            return await self._primary.query(request)
        except ProviderError as primary_error:
            return await self._query_backup(request, primary_error)

    async def _query_backup(
        self,
        request: ProviderQuery,
        primary_error: ProviderError,
    ) -> ProviderResult:
        """主服务器失败后，优先复用备用缓存，否则实际请求备用服务器。"""

        key = provider_cache_key(request)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        lock = await self._lock_for(key)
        async with lock:
            cached = await self._cache.get(key)
            if cached is not None:
                return cached
            try:
                result = await self._backup.query(request)
            except ProviderError as backup_error:
                message = (
                    f"主服务器失败：{primary_error.error_code.value} "
                    f"({primary_error.safe_message})；"
                    f"备用服务器失败：{backup_error.error_code.value} "
                    f"({backup_error.safe_message})"
                )
                raise DataSourceUnavailableError(
                    ProviderErrorCode.DATA_SOURCE_UNAVAILABLE,
                    request.api_name,
                    message,
                ) from backup_error
            await self._cache.put(key, result, BACKUP_CACHE_TTL_SECONDS)
            return result

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._key_locks_guard:
            return self._key_locks.setdefault(key, asyncio.Lock())
