"""Provider 接口；作用接近 Java interface。"""

from typing import Protocol

from stock_research_agent.providers.models import ProviderQuery, ProviderResult


class MarketDataProvider(Protocol):
    async def query(self, request: ProviderQuery) -> ProviderResult:
        """执行一次查询并返回统一结果。"""

        ...
