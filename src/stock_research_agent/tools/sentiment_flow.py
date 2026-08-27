"""情绪与资金研究员使用的持仓、杠杆与异常交易 Tool。"""

from datetime import date

from langchain_core.tools import BaseTool

from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, execute_tool_queries
from stock_research_agent.tools.models import (
    CapitalFlowContextInput,
    StockDateRangeToolInput,
    UnusualTradingInput,
)


def build_sentiment_flow_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def get_capital_flow_context(
        ts_code: str,
        start_date: date,
        end_date: date,
        exchange_id: str,
    ) -> dict[str, object]:
        service = context.services.trading_behavior
        return await execute_tool_queries(
            tool_name="get_capital_flow_context",
            context=context,
            queries=(
                (
                    "northbound_holdings",
                    service.get_northbound_holdings(
                        ts_code, start_date, end_date, as_of=context.as_of
                    ),
                ),
                (
                    "stock_margin_detail",
                    service.get_margin_detail(ts_code, start_date, end_date, as_of=context.as_of),
                ),
                (
                    "margin_eligibility",
                    service.get_margin_eligibility(ts_code, end_date, as_of=context.as_of),
                ),
                (
                    "market_margin_summary",
                    service.get_margin_market(
                        end_date,
                        exchange_id=exchange_id,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    async def get_unusual_trading_activity(
        ts_code: str,
        trade_date: date,
    ) -> dict[str, object]:
        service = context.services.trading_behavior
        return await execute_tool_queries(
            tool_name="get_unusual_trading_activity",
            context=context,
            queries=(
                (
                    "block_trades",
                    service.get_block_trades(ts_code, trade_date, as_of=context.as_of),
                ),
                (
                    "top_list",
                    service.get_top_list(ts_code, trade_date, as_of=context.as_of),
                ),
                (
                    "top_institutions",
                    service.get_top_institutions(ts_code, trade_date, as_of=context.as_of),
                ),
            ),
        )

    async def get_stock_active_money_flow_context(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        service = context.services.trading_behavior
        return await execute_tool_queries(
            tool_name="get_stock_active_money_flow_context",
            context=context,
            queries=(
                (
                    "moneyflow_ths",
                    service.get_stock_moneyflow_ths(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
                (
                    "moneyflow_dc",
                    service.get_stock_moneyflow_dc(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    return (
        create_structured_tool(
            name="get_capital_flow_context",
            description=(
                "取得北向持股、个股融资融券、融资融券资格和市场融资融券汇总，"
                "用于观察外资与杠杆资金行为。"
            ),
            args_schema=CapitalFlowContextInput,
            coroutine=get_capital_flow_context,
        ),
        create_structured_tool(
            name="get_unusual_trading_activity",
            description="取得某日个股的大宗交易、龙虎榜明细和机构席位；空结果表示当日没有对应记录。",
            args_schema=UnusualTradingInput,
            coroutine=get_unusual_trading_activity,
        ),
        create_structured_tool(
            name="get_stock_active_money_flow_context",
            description=(
                "同时读取 THS 与东财口径的指定股票主动资金流历史。比较方向、持续性、"
                "大中小单结构和价格背离；两个口径不可相加。"
            ),
            args_schema=StockDateRangeToolInput,
            coroutine=get_stock_active_money_flow_context,
        ),
    )
