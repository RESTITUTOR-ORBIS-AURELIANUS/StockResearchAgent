"""解析两套上游共有的 Tushare fields/items 响应结构。"""

from datetime import UTC, datetime
from typing import Any

from stock_research_agent.providers.errors import (
    ProviderError,
    ProviderErrorCode,
    ProviderPermissionDeniedError,
    ProviderRateLimitedError,
    ProviderSchemaError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource

_RATE_LIMIT_WORDS = ("频率", "限流", "次数限制", "too many", "rate limit")
_PERMISSION_WORDS = ("无权限", "没有权限", "访问权限", "permission", "未购买", "not purchased")
_AUTH_WORDS = ("token", "api key", "apikey", "x-api-key")


def _raise_business_error(
    request: ProviderQuery,
    provider: ProviderSource,
    provider_code: int,
    message: object,
) -> None:
    normalized = str(message or "").lower()
    common = {
        "api_name": request.api_name,
        "message": message,
        "provider": provider,
        "provider_code": provider_code,
    }
    if any(word in normalized for word in _RATE_LIMIT_WORDS):
        raise ProviderRateLimitedError(ProviderErrorCode.RATE_LIMITED, **common)
    if provider_code == 40203 or any(word in normalized for word in _PERMISSION_WORDS):
        raise ProviderPermissionDeniedError(ProviderErrorCode.PERMISSION_DENIED, **common)
    if any(word in normalized for word in _AUTH_WORDS):
        raise ProviderError(ProviderErrorCode.AUTHENTICATION_ERROR, **common)
    raise ProviderError(ProviderErrorCode.BUSINESS_ERROR, **common)


def parse_provider_payload(
    request: ProviderQuery,
    provider: ProviderSource,
    payload: Any,
    response_bytes: int,
) -> ProviderResult:
    """校验业务 code、schema 和每行列数，然后转换成字典列表。"""

    if not isinstance(payload, dict):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "JSON 根节点不是对象",
            provider=provider,
        )

    code = payload.get("code")
    if not isinstance(code, int):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "响应缺少整数类型 code",
            provider=provider,
        )
    if code != 0:
        _raise_business_error(request, provider, code, payload.get("msg"))

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "成功响应缺少 data 对象",
            provider=provider,
            provider_code=code,
        )
    fields = data.get("fields")
    raw_items = data.get("items")
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "data.fields 必须是字符串数组",
            provider=provider,
            provider_code=code,
        )
    if not isinstance(raw_items, list):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "data.items 必须是数组",
            provider=provider,
            provider_code=code,
        )

    items: list[dict[str, Any]] = []
    for row_number, row in enumerate(raw_items):
        if not isinstance(row, list) or len(row) != len(fields):
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                f"data.items[{row_number}] 的列数与 fields 不一致",
                provider=provider,
                provider_code=code,
            )
        items.append(dict(zip(fields, row, strict=True)))

    raw_has_more = data.get("has_more")
    if raw_has_more is not None and not isinstance(raw_has_more, bool):
        raise ProviderSchemaError(
            ProviderErrorCode.SCHEMA_ERROR,
            request.api_name,
            "data.has_more 必须是布尔值",
            provider=provider,
            provider_code=code,
        )
    if isinstance(raw_has_more, bool):
        has_more = raw_has_more
    else:
        # 标准 Tushare DataApi 的响应经常没有 has_more。若一页正好达到
        # limit，就继续请求下一 offset；最后最多多取一个合法空页，却不会
        # 把满页误当成完整数据。
        page_limit = request.params.get("limit")
        has_more = (
            isinstance(page_limit, int)
            and not isinstance(page_limit, bool)
            and page_limit > 0
            and len(items) >= page_limit
        )

    return ProviderResult(
        api_name=request.api_name,
        provider=provider,
        fetched_at=datetime.now(UTC),
        fields=tuple(fields),
        items=items,
        provider_code=code,
        has_more=has_more,
        response_bytes=response_bytes,
    )
