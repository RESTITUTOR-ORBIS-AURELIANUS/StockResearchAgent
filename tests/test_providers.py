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
    UnknownProviderApiError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderSource
from stock_research_agent.providers.primary import PrimaryRestProvider
from stock_research_agent.providers.router import RoutedMarketDataProvider
from stock_research_agent.providers.routes import SUPPORTED_APIS


def success_payload(items: list[list[object]] | None = None) -> dict[str, object]:
    return {
        "code": 0,
        "msg": "",
        "data": {
            "fields": ["ts_code", "trade_date", "close"],
            "items": items if items is not None else [["000001.SZ", "20260818", 12.34]],
            "has_more": False,
            "count": 0,
        },
    }


def test_supported_api_catalog_contains_all_declared_interfaces() -> None:
    assert len(SUPPORTED_APIS) == 89
    assert "daily" in SUPPORTED_APIS
    assert "etf_share_size" in SUPPORTED_APIS
    assert "major_news" in SUPPORTED_APIS
    assert "moneyflow_ind_ths" in SUPPORTED_APIS
    assert "moneyflow_hsgt" in SUPPORTED_APIS
    assert "report_rc" in SUPPORTED_APIS
    assert "broker_recommend" in SUPPORTED_APIS


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


def test_backup_provider_removes_unsupported_pagination_parameters() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["params"] == {"list_status": "L"}
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "fields": ["ts_code"],
                        "items": [["000001.SZ"], ["600000.SH"]],
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = BackupTushareProvider(
                client,
                "https://backup.test",
                SecretStr("test-backup-token"),
            )
            result = await provider.query(
                ProviderQuery(
                    api_name="stock_basic",
                    params={"list_status": "L", "limit": 2, "offset": 0},
                )
            )

        assert len(result.items) == 2
        assert result.has_more is False

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


def test_router_prefers_primary_for_every_supported_interface() -> None:
    async def scenario() -> None:
        primary_calls = 0

        def primary_handler(_: httpx.Request) -> httpx.Response:
            nonlocal primary_calls
            primary_calls += 1
            return httpx.Response(200, json=success_payload())

        def backup_handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("主服务器成功时不应调用备用服务器")

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

        assert primary_calls == 1
        assert result.provider is ProviderSource.PRIMARY

    asyncio.run(scenario())


def test_router_falls_back_when_primary_returns_business_error() -> None:
    async def scenario() -> None:
        primary_calls = 0
        backup_calls = 0

        def primary_handler(_: httpx.Request) -> httpx.Response:
            nonlocal primary_calls
            primary_calls += 1
            return httpx.Response(200, json={"code": 40203, "msg": "没有接口访问权限"})

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
            result = await router.query(ProviderQuery(api_name="balancesheet"))

        assert primary_calls == 1
        assert backup_calls == 1
        assert result.provider is ProviderSource.BACKUP
        assert result.from_cache is False

    asyncio.run(scenario())


def test_router_falls_back_when_primary_transport_fails() -> None:
    async def scenario() -> None:
        def primary_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("primary unavailable", request=request)

        def backup_handler(_: httpx.Request) -> httpx.Response:
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
            result = await router.query(ProviderQuery(api_name="daily"))

        assert result.provider is ProviderSource.BACKUP

    asyncio.run(scenario())


def test_router_retries_primary_before_using_cached_backup() -> None:
    async def scenario() -> None:
        primary_calls = 0
        backup_calls = 0

        def primary_handler(_: httpx.Request) -> httpx.Response:
            nonlocal primary_calls
            primary_calls += 1
            return httpx.Response(200, json={"code": 40203, "msg": "接口频率超限"})

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
            request = ProviderQuery(api_name="daily_basic", params={"ts_code": "000001.SZ"})
            first = await router.query(request)
            second = await router.query(request)

        assert primary_calls == 2
        assert backup_calls == 1
        assert first.from_cache is False
        assert second.from_cache is True

    asyncio.run(scenario())


def test_successful_empty_primary_response_does_not_fall_back() -> None:
    async def scenario() -> None:
        def primary_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=success_payload(items=[]))

        def backup_handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("合法空数据不应触发回退")

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(primary_handler)) as primary_client,
            httpx.AsyncClient(transport=httpx.MockTransport(backup_handler)) as backup_client,
        ):
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(primary_client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(backup_client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            result = await router.query(ProviderQuery(api_name="suspend_d"))

        assert result.provider is ProviderSource.PRIMARY
        assert result.items == []

    asyncio.run(scenario())


def test_router_reports_aggregate_error_when_both_providers_fail() -> None:
    async def scenario() -> None:
        def primary_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 40203, "msg": "接口频率超限"})

        def backup_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 40203, "msg": "没有接口访问权限"})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(primary_handler)) as primary_client,
            httpx.AsyncClient(transport=httpx.MockTransport(backup_handler)) as backup_client,
        ):
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(primary_client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(backup_client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            with pytest.raises(DataSourceUnavailableError) as captured:
                await router.query(ProviderQuery(api_name="etf_share_size"))

        message = str(captured.value)
        assert "PROVIDER_RATE_LIMITED" in message
        assert "PROVIDER_PERMISSION_DENIED" in message

    asyncio.run(scenario())


def test_router_rejects_unknown_interface_without_http_call() -> None:
    async def scenario() -> None:
        def forbidden_handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("未知接口不应产生 HTTP 请求")

        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_handler)) as client:
            router = RoutedMarketDataProvider(
                PrimaryRestProvider(client, "http://primary.test", SecretStr("primary")),
                BackupTushareProvider(client, "https://backup.test", SecretStr("backup")),
                InMemoryProviderCache(),
            )
            with pytest.raises(UnknownProviderApiError):
                await router.query(ProviderQuery(api_name="not_declared"))

    asyncio.run(scenario())
