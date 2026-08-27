"""从环境变量读取外部服务配置，避免把凭据写进源码。"""

from pathlib import Path
from typing import Literal

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
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)


class AkshareSettings(BaseSettings):
    """AKShare 公开新闻/公告接口的阻塞调用边界。"""

    model_config = SettingsConfigDict(
        env_prefix="AKSHARE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_workers: int = Field(default=2, ge=1, le=8)


class LLMSettings(BaseSettings):
    """OpenAI-compatible 大模型连接配置；与行情 Provider 凭据完全分离。"""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: AnyHttpUrl
    api_key: SecretStr = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(default=1_200.0, gt=0, le=1_800)
    max_retries: int = Field(default=2, ge=0, le=10)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    structured_output_method: Literal["function_calling", "json_schema"] = "function_calling"
    structured_output_strict: bool = True
    structured_output_repair_attempts: int = Field(default=1, ge=0, le=3)
    structured_output_diagnostics_path: Path = Path(
        ".artifacts/llm-structured-output.jsonl"
    )
    structured_output_raw_max_characters: int = Field(
        default=20_000,
        ge=1_000,
        le=200_000,
    )
