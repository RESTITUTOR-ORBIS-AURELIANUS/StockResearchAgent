"""数据 Service 层对外返回的确定性结果。"""

from datetime import date
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.providers.models import ProviderParam, ProviderSource


class ServicePageTrace(DomainModel):
    """一次分页请求的来源记录，供后续 Evidence 追溯。"""

    page_index: int = Field(ge=0)
    provider: ProviderSource
    from_cache: bool
    fetched_at: AwareDatetime
    offset: int = Field(ge=0)
    item_count: int = Field(ge=0)
    returned_fields: tuple[str, ...]
    response_bytes: int = Field(ge=0)


class ServiceItemTrace(DomainModel):
    """与 ``ServiceDataset.items`` 同位置的一条行级来源记录。"""

    page_index: int = Field(ge=0)
    source_offset: int = Field(ge=0)
    provider: ProviderSource
    from_cache: bool
    fetched_at: AwareDatetime


class ServiceDataset(DomainModel):
    """一个业务方法完成分页、过滤后得到的数据集。

    ``items`` 仍保留上游原始行，不在这里提前编造成 Evidence。每一行的
    来源保存在 ``item_traces``，分页请求本身保存在 ``pages``；同一次分页
    若切换 Provider，查询层会失败关闭，不拼接不同快照。
    """

    api_name: str
    query_params: dict[str, ProviderParam]
    requested_fields: tuple[str, ...]
    items: list[dict[str, Any]]
    item_traces: tuple[ServiceItemTrace, ...]
    pages: tuple[ServicePageTrace, ...]
    as_of: date | None = None
    data_as_of: date | None = None
    received_item_count: int = Field(ge=0)
    discarded_item_count: int = Field(ge=0)
    complete: bool = True

    @model_validator(mode="after")
    def align_items_with_traces(self) -> "ServiceDataset":
        if len(self.items) != len(self.item_traces):
            raise ValueError("items 与 item_traces 必须逐行对齐")
        return self
