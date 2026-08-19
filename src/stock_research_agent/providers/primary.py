"""主 REST 行情服务器适配器。"""

import httpx
from pydantic import SecretStr

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    ProviderSchemaError,
    ProviderTransportError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource
from stock_research_agent.providers.parsing import parse_provider_payload


class PrimaryRestProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: SecretStr,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def query(self, request: ProviderQuery) -> ProviderResult:
        endpoint = request.api_name.replace("_", "-")
        params = dict(request.params)
        if request.fields:
            params["fields"] = ",".join(request.fields)

        try:
            response = await self._client.get(
                f"{self._base_url}/{endpoint}",
                params=params,
                headers={
                    "X-API-Key": self._api_key.get_secret_value(),
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
                provider=ProviderSource.PRIMARY,
            ) from exc

        if not response.is_success:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                f"HTTP {response.status_code}",
                provider=ProviderSource.PRIMARY,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                "响应不是合法 JSON",
                provider=ProviderSource.PRIMARY,
            ) from exc

        return parse_provider_payload(
            request,
            ProviderSource.PRIMARY,
            payload,
            len(response.content),
        )
