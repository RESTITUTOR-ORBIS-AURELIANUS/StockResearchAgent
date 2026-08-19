"""Provider 统一异常分类。"""

import re
from enum import StrEnum

from stock_research_agent.providers.models import ProviderSource


class ProviderErrorCode(StrEnum):
    PERMISSION_DENIED = "PROVIDER_PERMISSION_DENIED"
    RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    TRANSPORT_ERROR = "PROVIDER_TRANSPORT_ERROR"
    SCHEMA_ERROR = "PROVIDER_SCHEMA_ERROR"
    AUTHENTICATION_ERROR = "PROVIDER_AUTHENTICATION_ERROR"
    BUSINESS_ERROR = "PROVIDER_BUSINESS_ERROR"
    DATA_SOURCE_UNAVAILABLE = "DATA_SOURCE_UNAVAILABLE"
    UNKNOWN_API = "UNKNOWN_PROVIDER_API"


def sanitize_provider_message(message: object) -> str:
    """避免上游错误信息把 Token 或长密钥带进日志。"""

    text = str(message or "")
    text = re.sub(r"tk_live_[A-Za-z0-9_-]+", "<REDACTED_TOKEN>", text)
    text = re.sub(r"\b[A-Fa-f0-9]{40,}\b", "<REDACTED_SECRET>", text)
    return text[:500]


class ProviderError(RuntimeError):
    def __init__(
        self,
        error_code: ProviderErrorCode,
        api_name: str,
        message: object,
        *,
        provider: ProviderSource | None = None,
        provider_code: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.api_name = api_name
        self.provider = provider
        self.provider_code = provider_code
        self.safe_message = sanitize_provider_message(message)
        provider_label = provider.value if provider else "ROUTER"
        super().__init__(
            f"{error_code.value}: {api_name} via {provider_label}: {self.safe_message}"
        )


class ProviderPermissionDeniedError(ProviderError):
    pass


class ProviderRateLimitedError(ProviderError):
    pass


class ProviderTransportError(ProviderError):
    pass


class ProviderSchemaError(ProviderError):
    pass


class DataSourceUnavailableError(ProviderError):
    pass


class UnknownProviderApiError(ProviderError):
    pass
