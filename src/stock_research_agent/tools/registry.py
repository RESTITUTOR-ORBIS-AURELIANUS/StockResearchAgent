"""按证据研究员职责建立显式 Tool 白名单。"""

from dataclasses import dataclass
from enum import StrEnum

from langchain_core.tools import BaseTool

from stock_research_agent.tools.common import build_common_tools
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.daily_event_snapshot import build_daily_event_snapshot_tools
from stock_research_agent.tools.daily_fundamental_snapshot import (
    build_daily_fundamental_snapshot_tools,
)
from stock_research_agent.tools.daily_sentiment_flow_snapshot import (
    build_daily_sentiment_flow_snapshot_tools,
)
from stock_research_agent.tools.daily_technical_snapshot import (
    build_daily_technical_snapshot_tools,
)
from stock_research_agent.tools.event import build_event_tools
from stock_research_agent.tools.fundamental import build_fundamental_tools
from stock_research_agent.tools.sentiment_flow import build_sentiment_flow_tools
from stock_research_agent.tools.technical import build_technical_tools
from stock_research_agent.tools.technical_calculators import (
    build_technical_calculator_tools,
)


class EvidenceAgentRole(StrEnum):
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    EVENT = "event"
    SENTIMENT_FLOW = "sentiment_flow"


@dataclass(frozen=True, slots=True)
class AgentToolRegistry:
    """四位证据研究员各自能看到的 Tool 白名单。"""

    technical: tuple[BaseTool, ...]
    fundamental: tuple[BaseTool, ...]
    event: tuple[BaseTool, ...]
    sentiment_flow: tuple[BaseTool, ...]

    def for_role(self, role: EvidenceAgentRole) -> tuple[BaseTool, ...]:
        if role is EvidenceAgentRole.TECHNICAL:
            return self.technical
        if role is EvidenceAgentRole.FUNDAMENTAL:
            return self.fundamental
        if role is EvidenceAgentRole.EVENT:
            return self.event
        if role is EvidenceAgentRole.SENTIMENT_FLOW:
            return self.sentiment_flow
        raise ValueError(f"未知证据研究员角色：{role}")

    @property
    def all_tools(self) -> tuple[BaseTool, ...]:
        """返回去重后的全部 Tool，主要供诊断和测试使用。"""

        by_name: dict[str, BaseTool] = {}
        for tool in (
            *self.technical,
            *self.fundamental,
            *self.event,
            *self.sentiment_flow,
        ):
            by_name.setdefault(tool.name, tool)
        return tuple(by_name.values())


def build_agent_tool_registry(context: ResearchToolContext) -> AgentToolRegistry:
    """创建四个小型工具集合；同一个 Tool 实例可安全共享给多个角色。"""

    common = build_common_tools(context)
    technical = build_technical_tools(context)
    daily_technical_snapshot = build_daily_technical_snapshot_tools(context)
    daily_sentiment_flow_snapshot = build_daily_sentiment_flow_snapshot_tools(context)
    daily_fundamental_snapshot = build_daily_fundamental_snapshot_tools(context)
    daily_event_snapshot = build_daily_event_snapshot_tools(context)
    technical_calculators = build_technical_calculator_tools(context)
    fundamental = build_fundamental_tools(context)
    event = build_event_tools(context)
    sentiment_flow = build_sentiment_flow_tools(context)

    built_tools = (
        *common,
        *technical,
        *daily_technical_snapshot,
        *daily_sentiment_flow_snapshot,
        *daily_fundamental_snapshot,
        *daily_event_snapshot,
        *technical_calculators,
        *fundamental,
        *event,
        *sentiment_flow,
    )
    by_name: dict[str, BaseTool] = {}
    for tool in built_tools:
        if tool.name in by_name:
            raise RuntimeError(f"Tool 名称重复：{tool.name}")
        by_name[tool.name] = tool

    return AgentToolRegistry(
        technical=(*common, *daily_technical_snapshot, *technical, *technical_calculators),
        fundamental=(*common, *daily_fundamental_snapshot, *fundamental),
        event=(
            *common,
            *daily_event_snapshot,
            *event,
            by_name["get_earnings_and_disclosure"],
        ),
        sentiment_flow=(
            *common,
            *daily_sentiment_flow_snapshot,
            *sentiment_flow,
            by_name["get_fund_market_context"],
            by_name["search_market_news"],
        ),
    )
