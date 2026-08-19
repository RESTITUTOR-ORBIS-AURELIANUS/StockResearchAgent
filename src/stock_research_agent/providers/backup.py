"""备用 Tushare DataApi 协议适配器。"""

import httpx
from pydantic import SecretStr

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    ProviderSchemaError,
    ProviderTransportError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.providers.parsing import parse_provider_payload


class BackupTushareProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: SecretStr,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    async def query(self, request: ProviderQuery) -> ProviderResult:
        body = {
            "api_name": request.api_name,
            "token": self._token.get_secret_value(),
            "params": request.params,
            "fields": ",".join(request.fields),
        }
        try:
            response = await self._client.post(
                self._base_url,
                json=body,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "StockResearchAgent/0.1",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                type(exc).__name__,
                provider=ProviderSource.BACKUP,
            ) from exc

        if not response.is_success:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                f"HTTP {response.status_code}",
                provider=ProviderSource.BACKUP,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                "响应不是合法 JSON",
                provider=ProviderSource.BACKUP,
            ) from exc

        return parse_provider_payload(
            request,
            ProviderSource.BACKUP,
            payload,
            len(response.content),
        )
