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
        # 备用兼容端采用 Tushare DataApi 协议，但明确拒绝通用 limit/offset。
        # Service 仍可对主端使用分页；一旦回退到备用端，这里移除分页参数并
        # 一次返回备用端的完整响应。BaseDataService 接受超过 page_size 的一页，
        # 仍会执行 max_rows、字段、as_of 和去重校验。
        wire_params = {
            name: value for name, value in request.params.items() if name not in {"limit", "offset"}
        }
        wire_request = request.model_copy(update={"params": wire_params})
        body = {
            "api_name": wire_request.api_name,
            "token": self._token.get_secret_value(),
            "params": wire_request.params,
            "fields": ",".join(wire_request.fields),
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
            wire_request,
            ProviderSource.BACKUP,
            payload,
            len(response.content),
        )
