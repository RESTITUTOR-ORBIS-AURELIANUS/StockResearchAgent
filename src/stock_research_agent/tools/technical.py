"""技术研究员使用的量价、基准与跨资产 Tool。"""

from datetime import date

from langchain_core.tools import BaseTool

from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, execute_stored_tool_queries
from stock_research_agent.tools.models import (
    FundMarketContextInput,
    IndexMarketContextInput,
    StockPriceContextInput,
)


def build_technical_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def get_stock_price_context(
        ts_code: str,
        start_date: date,
        end_date: date,
        frequency: str = "daily",
    ) -> dict[str, object]:
        service = context.services.equity_market_data
        calendar_service = context.services.instrument_reference
        calendar_exchange = _calendar_exchange(ts_code)
        queries = [
            (
                "price_bars",
                service.get_stock_bars(
                    ts_code,
                    start_date,
                    end_date,
                    frequency=frequency,
                    as_of=context.as_of,
                ),
            ),
            (
                "adjustment_factors",
                service.get_adjustment_factors(ts_code, start_date, end_date, as_of=context.as_of),
            ),
        ]
        if frequency == "daily":
            queries.extend(
                [
                    (
                        "daily_valuation_and_turnover",
                        service.get_daily_valuation(
                            ts_code, start_date, end_date, as_of=context.as_of
                        ),
                    ),
                    (
                        "price_limits",
                        service.get_price_limits(
                            ts_code, start_date, end_date, as_of=context.as_of
                        ),
                    ),
                    (
                        "suspensions",
                        service.get_suspensions(ts_code, start_date, end_date, as_of=context.as_of),
                    ),
                    (
                        "trade_calendar",
                        calendar_service.get_trade_calendar(
                            calendar_exchange,
                            start_date,
                            end_date,
                            as_of=context.as_of,
                        ),
                    ),
                ]
            )
        return await execute_stored_tool_queries(
            tool_name="get_stock_price_context",
            bundle_kind="stock_price_context",
            bundle_metadata={
                "ts_code": ts_code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": frequency,
                "calendar_exchange": calendar_exchange,
            },
            context=context,
            queries=queries,
        )

    async def get_index_market_context(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        service = context.services.equity_market_data
        if ts_code.endswith(".SI"):
            queries = (
                (
                    "index_price_bars",
                    service.get_sw_industry_bars(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            )
        else:
            queries = (
                (
                    "index_price_bars",
                    service.get_index_bars(ts_code, start_date, end_date, as_of=context.as_of),
                ),
                (
                    "index_daily_metrics",
                    service.get_index_daily_metrics(ts_code, end_date, as_of=context.as_of),
                ),
            )
        return await execute_stored_tool_queries(
            tool_name="get_index_market_context",
            bundle_kind="index_market_context",
            bundle_metadata={
                "ts_code": ts_code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": "daily",
            },
            context=context,
            queries=queries,
        )

    async def get_fund_market_context(
        ts_code: str,
        start_date: date,
        end_date: date,
        include_adjustment_factors: bool = True,
        include_share_history: bool = False,
    ) -> dict[str, object]:
        service = context.services.cross_asset_market_data
        queries = [
            (
                "fund_price_bars",
                service.get_fund_bars(ts_code, start_date, end_date, as_of=context.as_of),
            )
        ]
        if include_adjustment_factors:
            queries.append(
                (
                    "fund_adjustment_factors",
                    service.get_fund_adjustment_factors(
                        ts_code, start_date, end_date, as_of=context.as_of
                    ),
                )
            )
        if include_share_history:
            queries.append(
                (
                    "etf_share_history",
                    service.get_etf_share_history(
                        ts_code, start_date, end_date, as_of=context.as_of
                    ),
                )
            )
        return await execute_stored_tool_queries(
            tool_name="get_fund_market_context",
            bundle_kind="fund_market_context",
            bundle_metadata={
                "ts_code": ts_code,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "frequency": "daily",
                "include_adjustment_factors": include_adjustment_factors,
                "include_share_history": include_share_history,
            },
            context=context,
            queries=queries,
        )

    return (
        create_structured_tool(
            name="get_stock_price_context",
            description=(
                "取得个股 K 线和复权因子；daily 还会取得换手估值、涨跌停、停复牌"
                "和交易日历。"
                "完整数据保存为 context_ref，只返回少量预览；把引用交给确定性计算器。"
            ),
            args_schema=StockPriceContextInput,
            coroutine=get_stock_price_context,
        ),
        create_structured_tool(
            name="get_index_market_context",
            description=(
                "取得市场、中证或申万行业指数的区间行情；非申万指数还读取结束日指标。"
                "完整数据保存为 "
                "context_ref，可供五个确定性计算器进行市场、板块及相对强弱分析。"
            ),
            args_schema=IndexMarketContextInput,
            coroutine=get_index_market_context,
        ),
        create_structured_tool(
            name="get_fund_market_context",
            description=(
                "取得基金或 ETF 的行情，可选复权因子和份额变化。完整数据保存为 "
                "context_ref，可供五个确定性计算器进行跨资产、行业 ETF 或资金申赎验证。"
            ),
            args_schema=FundMarketContextInput,
            coroutine=get_fund_market_context,
        ),
    )


def _calendar_exchange(ts_code: str) -> str:
    """把证券代码后缀映射为 Tushare 交易日历使用的交易所代码。"""

    suffix = ts_code.rsplit(".", maxsplit=1)[-1]
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[suffix]
