"""只在一次研究运行内存活的完整原始数据快照。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import AwareDatetime, ConfigDict, Field, field_serializer, field_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.services.models import ServiceDataset

DatasetLabel = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=100,
        description="Tool 内稳定的数据集标签，例如 price_bars",
    ),
]
type ResearchMetadataValue = str | int | float | bool | None


class ResearchDataBundle(DomainModel):
    """一个原始数据 Tool 产生的复合数据包。

    ``datasets`` 保存完整 ``ServiceDataset``，不会为了 LLM 上下文而截断。LLM
    只会拿到仓库返回的 ``context_ref`` 和清单；确定性计算器再凭引用读取本对象。

    模型本身和两层 Mapping 均不可写。仓库还会把它序列化为独立快照，因此调用方
    即使修改从 ``get`` 得到的嵌套 ServiceDataset，也不会污染仓库中的原始版本。
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    kind: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=100,
        description="数据包类别，例如 stock_price_context",
    )
    tool_name: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=100,
        description="创建该数据包的原始数据 Tool 名称",
    )
    as_of: AwareDatetime = Field(description="本次研究冻结的截止时间")
    datasets: Mapping[DatasetLabel, ServiceDataset]
    metadata: Mapping[str, ResearchMetadataValue] = Field(default_factory=dict)

    @field_validator("datasets", mode="after")
    @classmethod
    def freeze_datasets(
        cls,
        value: Mapping[str, ServiceDataset],
    ) -> Mapping[str, ServiceDataset]:
        if not value:
            raise ValueError("ResearchDataBundle.datasets 不能为空")
        snapshots = {label: dataset.model_copy(deep=True) for label, dataset in value.items()}
        return MappingProxyType(snapshots)

    @field_validator("metadata", mode="after")
    @classmethod
    def freeze_metadata(
        cls,
        value: Mapping[str, ResearchMetadataValue],
    ) -> Mapping[str, ResearchMetadataValue]:
        return MappingProxyType(dict(value))

    @field_serializer("datasets")
    def serialize_datasets(
        self,
        value: Mapping[str, ServiceDataset],
    ) -> dict[str, ServiceDataset]:
        return dict(value)

    @field_serializer("metadata")
    def serialize_metadata(
        self,
        value: Mapping[str, ResearchMetadataValue],
    ) -> dict[str, ResearchMetadataValue]:
        return dict(value)
