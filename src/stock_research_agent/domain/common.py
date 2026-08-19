"""多个领域对象都会复用的值对象。"""

import re
from datetime import date

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import TargetType

_A_SHARE_STOCK_CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class ResearchTarget(DomainModel):
    """本次研究所针对的市场、板块或股票。"""

    type: TargetType
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_stock_code(self) -> "ResearchTarget":
        if self.type is TargetType.STOCK and not _A_SHARE_STOCK_CODE.fullmatch(self.code):
            raise ValueError("A 股股票代码必须类似 000001.SZ、600000.SH 或 430047.BJ")
        return self


class SourceReference(DomainModel):
    """一条证据对应的原始数据位置。"""

    provider: str = Field(min_length=1, max_length=100)
    interface: str = Field(min_length=1, max_length=100)
    record_key: str = Field(min_length=1, max_length=255)
    published_at: AwareDatetime
    url: str | None = None


class TimeRange(DomainModel):
    """定向查证请求允许搜索的日期范围。"""

    start: date
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> "TimeRange":
        if self.end < self.start:
            raise ValueError("time_range.end 不能早于 time_range.start")
        return self
