"""一次研究运行内所有 Tool 共享的不可变依赖。"""

from dataclasses import dataclass, field
from datetime import datetime
from secrets import token_hex
from zoneinfo import ZoneInfo

from stock_research_agent.research_data import (
    InMemoryResearchDataStore,
    ResearchDataStore,
    validate_run_id,
)
from stock_research_agent.services import DataServices

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """限制进入 LLM 上下文的原始数据体积。

    Service 可以为了完整性读取很多页；Tool 不能把几万行数据直接塞给模型。
    普通 Tool 仍采用“超限即明确失败”。三个行情原始 Tool 会把完整数据保存到
    ``ResearchDataStore``，只截取有明确 ``preview_*`` 标记的预览，因此不会把
    预览冒充成完整结果，也不会损害后续确定性计算。
    """

    max_items: int = 500
    max_item_chars: int = 40_000
    max_serialized_chars: int = 200_000
    preview_items_per_dataset: int = 5

    def __post_init__(self) -> None:
        if self.max_items <= 0:
            raise ValueError("max_items 必须大于 0")
        if self.max_item_chars <= 0:
            raise ValueError("max_item_chars 必须大于 0")
        if self.max_serialized_chars <= 0:
            raise ValueError("max_serialized_chars 必须大于 0")
        if self.preview_items_per_dataset <= 0:
            raise ValueError("preview_items_per_dataset 必须大于 0")


@dataclass(frozen=True, slots=True)
class ResearchToolContext:
    """Tool 的组合根。

    ``services`` 在应用或一次 LangGraph 运行开始时创建并复用；``as_of`` 和
    ``run_id`` 是本次研究冻结的系统字段，故意不出现在任何 Tool 的 LLM 参数中。
    ``data_store`` 保存完整 ServiceDataset，计算器只接收它生成的 context_ref。
    """

    services: DataServices
    as_of: datetime
    limits: ToolLimits = field(default_factory=ToolLimits)
    run_id: str = field(default_factory=lambda: f"run_tool_{token_hex(12)}")
    data_store: ResearchDataStore = field(default_factory=InMemoryResearchDataStore)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("ResearchToolContext.as_of 必须带时区")
        object.__setattr__(
            self,
            "as_of",
            self.as_of.astimezone(_SHANGHAI).replace(microsecond=0),
        )
        validate_run_id(self.run_id)
        if not isinstance(self.data_store, ResearchDataStore):
            raise TypeError("ResearchToolContext.data_store 必须实现 ResearchDataStore")
