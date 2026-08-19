"""使用 HTTPX MockTransport 验证 Provider，不访问真实上游。"""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from stock_research_agent.providers.backup import BackupTushareProvider
from stock_research_agent.providers.cache import InMemoryProviderCache
from stock_research_agent.providers.errors import (
    DataSourceUnavailableError,
    ProviderRateLimitedError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderSource
from stock_research_agent.providers.primary import PrimaryRestProvider
from stock_research_agent.providers.router import RoutedMarketDataProvider
from stock_research_agent.providers.routes import (
    BACKUP_APIS,
    PRIMARY_APIS,
    PRIMARY_CACHED_TTLS,
    ROUTES,
    UNAVAILABLE_APIS,
)


def success_payload() -> dict[str, object]:
    return {
        "code": 0,
        "msg": "",
        "data": {
            "fields": ["ts_code", "trade_date", "close"],
            "items": [["000001.SZ", "20260818", 12.34]],
            "has_more": False,
            "count": 0,
        },
    }


def test_route_matrix_matches_verified_counts() -> None:
    assert len(PRIMARY_APIS) == 37
    assert len(PRIMARY_CACHED_TTLS) == 5
    assert len(BACKUP_APIS) == 33
    assert len(UNAVAILABLE_APIS) == 1
    assert len(ROUTES) == 76


def test_primary_provider_converts_rows_to_dicts() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path.endswith("/daily")
            assert request.headers["X-API-Key"] == "test-primary-key"
            assert request.url.params["fields"] == "ts_code,trade_date,close"
            return httpx.Response(200, json=success_payload())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = PrimaryRestProvider(
                client,
                "http://primary.test/tushare",
                SecretStr("test-primary-key"),
            )
            result = await provider.query(
                ProviderQuery(
                    api_name="daily",
                    params={"ts_code": "000001.SZ"},
                    fields=("ts_code", "trade_date", "close"),
                )
            )

        assert result.provider is ProviderSource.PRIMARY
        assert result.items[0]["close"] == 12.34

    asyncio.run(scenario())


def test_backup_provider_uses_data_api_protocol() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert request.method == "POST"
            assert body["api_name"] == "income"
            assert body["token"] == "test-backup-token"
            assert body["params"] == {"ts_code": "000001.SZ"}
            return httpx.Response(200, json=success_payload())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = BackupTushareProvider(
                client,
                "https://backup.test",
                SecretStr("test-backup-token"),
            )
            result = await provider.query(
                ProviderQuery(api_name="income", params={"ts_code": "000001.SZ"})
            )

        assert result.provider is ProviderSource.BACKUP

    asyncio.run(scenario())


def test_business_rate_limit_is_not_treated_as_http_success() -> None:
    async def scenario() -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 40203, "msg": "接口频率超限"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = PrimaryRestProvider(
                client,
                "http://primary.test/tushare",
                SecretStr("test-primary-key"),
            )
            with pytest.raises(ProviderRateLimitedError):
                await provider.query(ProviderQuery(api_name="daily_basic"))

    asyncio.run(scenario())


def test_router_caches_rate_limited_primary_interface() -> None:
    async def scenario() -> None:
        calls = 0

        def primary_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=success_payload())

        def backup_handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("daily_basic 不应调用备用服务器")

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(primary_handler)) as primary_client,
            httpx.AsyncClient(transport=httpx.MockTransport(backup_handler)) as backup_client,
        ):
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(primary_client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(backup_client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            request = ProviderQuery(api_name="daily_basic", params={"ts_code": "000001.SZ"})
            first, second = await asyncio.gather(router.query(request), router.query(request))

        assert calls == 1
        assert first.from_cache is False
        assert second.from_cache is True

    asyncio.run(scenario())


def test_router_sends_backup_route_directly_to_backup() -> None:
    async def scenario() -> None:
        primary_calls = 0
        backup_calls = 0

        def primary_handler(_: httpx.Request) -> httpx.Response:
            nonlocal primary_calls
            primary_calls += 1
            raise AssertionError("income 已知主服务器无权限，不应先请求主服务器")

        def backup_handler(_: httpx.Request) -> httpx.Response:
            nonlocal backup_calls
            backup_calls += 1
            return httpx.Response(200, json=success_payload())

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(primary_handler)) as primary_client,
            httpx.AsyncClient(transport=httpx.MockTransport(backup_handler)) as backup_client,
        ):
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(primary_client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(backup_client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            result = await router.query(
                ProviderQuery(api_name="income", params={"ts_code": "000001.SZ"})
            )

        assert primary_calls == 0
        assert backup_calls == 1
        assert result.provider is ProviderSource.BACKUP

    asyncio.run(scenario())


def test_router_reports_unavailable_interface_without_http_call() -> None:
    async def scenario() -> None:
        def forbidden_handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("UNAVAILABLE 接口不应产生 HTTP 请求")

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_handler)) as client:
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            with pytest.raises(DataSourceUnavailableError):
                await router.query(ProviderQuery(api_name="etf_share_size"))

    asyncio.run(scenario())
