"""Provider 缓存接口与第一版进程内实现。"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Protocol

from stock_research_agent.providers.models import ProviderQuery, ProviderResult


def provider_cache_key(request: ProviderQuery) -> str:
    """同一个逻辑请求无论字典顺序如何，都得到相同缓存键。"""

    normalized = {
        "api_name": request.api_name,
        "params": request.params,
        "fields": sorted(request.fields),
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "provider:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProviderCache(Protocol):
    async def get(self, key: str) -> ProviderResult | None: ...

    async def put(self, key: str, value: ProviderResult, ttl_seconds: int) -> None: ...


@dataclass
class _CacheEntry:
    value: ProviderResult
    expires_at: float


class InMemoryProviderCache:
    """只在当前 Python 进程有效；生产环境后续替换为持久化实现。"""

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> ProviderResult | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return None
            return entry.value.model_copy(update={"from_cache": True})

    async def put(self, key: str, value: ProviderResult, ttl_seconds: int) -> None:
        async with self._lock:
            self._entries[key] = _CacheEntry(
                value=value.model_copy(update={"from_cache": False}),
                expires_at=time.monotonic() + ttl_seconds,
            )
