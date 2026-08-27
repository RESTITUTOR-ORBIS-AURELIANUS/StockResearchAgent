"""四位证据研究员都可以使用的公共 Tool。"""

from datetime import date

from langchain_core.tools import BaseTool

from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, execute_tool_queries
from stock_research_agent.tools.models import StockIdentityInput, TradeCalendarInput


def build_common_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def resolve_stock_identity(
        ts_code: str,
        list_status: str = "L",
    ) -> dict[str, object]:
        services = context.services.instrument_reference
        return await execute_tool_queries(
            tool_name="resolve_stock_identity",
            context=context,
            queries=(
                (
                    "stock_basic",
                    services.get_stock_basic(
                        ts_code,
                        list_status=list_status,
                        as_of=context.as_of,
                    ),
                ),
                (
                    "name_history",
                    services.get_name_history(ts_code, as_of=context.as_of),
                ),
            ),
        )

    async def get_trade_calendar(
        exchange: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        return await execute_tool_queries(
            tool_name="get_trade_calendar",
            context=context,
            queries=(
                (
                    "trade_calendar",
                    context.services.instrument_reference.get_trade_calendar(
                        exchange,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    return (
        create_structured_tool(
            name="resolve_stock_identity",
            description=(
                "确认一只 A 股的名称、行业、市场、上市日期和曾用名。开始任何个股研究时先调用，"
                "避免把代码对应到错误公司。"
            ),
            args_schema=StockIdentityInput,
            coroutine=resolve_stock_identity,
        ),
        create_structured_tool(
            name="get_trade_calendar",
            description="查询指定交易所在日期区间内的开休市日和上一交易日。",
            args_schema=TradeCalendarInput,
            coroutine=get_trade_calendar,
        ),
    )
