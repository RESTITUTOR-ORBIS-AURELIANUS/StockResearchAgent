"""行情数据 Provider 公共入口。"""

from stock_research_agent.providers.akshare_news import AkshareNewsProvider
from stock_research_agent.providers.backup import BackupTushareProvider
from stock_research_agent.providers.cache import InMemoryProviderCache
from stock_research_agent.providers.factory import (
    open_akshare_news_provider,
    open_market_data_provider,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.providers.primary import PrimaryRestProvider
from stock_research_agent.providers.router import RoutedMarketDataProvider

__all__ = [
    "AkshareNewsProvider",
    "BackupTushareProvider",
    "InMemoryProviderCache",
    "PrimaryRestProvider",
    "ProviderQuery",
    "ProviderResult",
    "ProviderSource",
    "RoutedMarketDataProvider",
    "open_akshare_news_provider",
    "open_market_data_provider",
]
