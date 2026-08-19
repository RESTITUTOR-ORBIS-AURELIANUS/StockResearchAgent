"""不同上游协议之间共享的请求和返回值。"""

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel

ProviderParam = str | int | float | bool


class ProviderSource(StrEnum):
    PRIMARY = "PRIMARY"
    BACKUP = "BACKUP"


class ProviderQuery(DomainModel):
    """上层向行情 Provider 发出的统一查询。"""

    api_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    params: dict[str, ProviderParam] = Field(default_factory=dict)
    fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def protect_query_contract(self) -> "ProviderQuery":
        forbidden = {"token", "api_key", "apikey", "x-api-key"}
        secret_names = forbidden.intersection(name.lower() for name in self.params)
        if secret_names:
            raise ValueError("ProviderQuery.params 不能包含凭据")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("ProviderQuery.fields 不能包含重复字段")
        return self


class ProviderResult(DomainModel):
    """两个上游都要转换成的统一结果。"""

    api_name: str
    provider: ProviderSource
    from_cache: bool = False
    fetched_at: AwareDatetime
    data_as_of: date | None = None
    fields: tuple[str, ...]
    items: list[dict[str, Any]]
    provider_code: int
    has_more: bool = False
    response_bytes: int = Field(default=0, ge=0)
