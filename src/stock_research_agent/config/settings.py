"""从环境变量读取 Provider 配置，避免把凭据写进源码。"""

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSettings(BaseSettings):
    """两套行情服务的连接配置。"""

    model_config = SettingsConfigDict(
        env_prefix="TUSHARE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    primary_base_url: AnyHttpUrl = AnyHttpUrl("http://datahubco.com/app-api/openapi/v1/tushare")
    primary_api_key: SecretStr = Field(min_length=1)
    backup_base_url: AnyHttpUrl = AnyHttpUrl("https://quantdata888.duckdns.org")
    backup_token: SecretStr = Field(min_length=1)
    allow_paid_fallback: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
