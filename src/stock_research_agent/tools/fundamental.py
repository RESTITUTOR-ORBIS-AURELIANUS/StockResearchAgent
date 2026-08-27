"""基本面研究员使用的财务、经营、所有权与宏观 Tool。"""

from datetime import date

from langchain_core.tools import BaseTool

from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, execute_tool_queries
from stock_research_agent.tools.models import (
    ChinaMacroContextInput,
    DividendOwnershipInput,
    FinancialQualityInput,
    InterestRateContextInput,
    StockCodeInput,
    StockDateRangeToolInput,
    StockPeriodInput,
)

_MONTHLY_MACRO_SERIES = ("cn_cpi", "cn_ppi", "cn_m", "sf_month", "cn_pmi")


def build_fundamental_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def get_financial_statements(
        ts_code: str,
        period: str,
    ) -> dict[str, object]:
        service = context.services.fundamental_data
        return await execute_tool_queries(
            tool_name="get_financial_statements",
            context=context,
            queries=(
                (
                    "income_statement",
                    service.get_income_statement(ts_code, period, as_of=context.as_of),
                ),
                (
                    "balance_sheet",
                    service.get_balance_sheet(ts_code, period, as_of=context.as_of),
                ),
                (
                    "cash_flow_statement",
                    service.get_cash_flow_statement(ts_code, period, as_of=context.as_of),
                ),
            ),
        )

    async def get_financial_quality(
        ts_code: str,
        period: str,
        composition_type: str = "P",
    ) -> dict[str, object]:
        service = context.services.fundamental_data
        return await execute_tool_queries(
            tool_name="get_financial_quality",
            context=context,
            queries=(
                (
                    "financial_indicators",
                    service.get_financial_indicators(ts_code, period, as_of=context.as_of),
                ),
                (
                    "business_composition",
                    service.get_business_composition(
                        ts_code,
                        period,
                        composition_type=composition_type,
                        as_of=context.as_of,
                    ),
                ),
                (
                    "audit_opinion",
                    service.get_audit_opinion(ts_code, period, as_of=context.as_of),
                ),
            ),
        )

    async def get_earnings_and_disclosure(
        ts_code: str,
        period: str,
    ) -> dict[str, object]:
        service = context.services.fundamental_data
        return await execute_tool_queries(
            tool_name="get_earnings_and_disclosure",
            context=context,
            queries=(
                (
                    "earnings_forecast",
                    service.get_earnings_forecast(ts_code, period, as_of=context.as_of),
                ),
                (
                    "earnings_express",
                    service.get_earnings_express(ts_code, period, as_of=context.as_of),
                ),
                (
                    "disclosure_schedule",
                    service.get_disclosure_schedule(ts_code, period, as_of=context.as_of),
                ),
            ),
        )

    async def get_dividend_and_ownership_context(
        ts_code: str,
        period: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        fundamental = context.services.fundamental_data
        ownership = context.services.ownership_event
        return await execute_tool_queries(
            tool_name="get_dividend_and_ownership_context",
            context=context,
            queries=(
                (
                    "dividends",
                    fundamental.get_dividends(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
                (
                    "top_holders",
                    ownership.get_top_holders(ts_code, period, as_of=context.as_of),
                ),
                (
                    "top_floating_holders",
                    ownership.get_top_holders(ts_code, period, floating=True, as_of=context.as_of),
                ),
                (
                    "holder_counts",
                    ownership.get_holder_counts(ts_code, start_date, end_date, as_of=context.as_of),
                ),
            ),
        )

    async def get_pledge_risk_context(ts_code: str) -> dict[str, object]:
        ownership = context.services.ownership_event
        return await execute_tool_queries(
            tool_name="get_pledge_risk_context",
            context=context,
            queries=(
                (
                    "pledge_statistics",
                    ownership.get_pledge_statistics(ts_code, as_of=context.as_of),
                ),
                (
                    "pledge_details",
                    ownership.get_pledge_details(ts_code, as_of=context.as_of),
                ),
            ),
        )

    async def get_valuation_context(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        return await execute_tool_queries(
            tool_name="get_valuation_context",
            context=context,
            queries=(
                (
                    "daily_valuation",
                    context.services.equity_market_data.get_daily_valuation(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    async def get_china_macro_context(
        start_month: str,
        end_month: str,
        start_quarter: str,
        end_quarter: str,
    ) -> dict[str, object]:
        service = context.services.macro_data
        queries = [
            (
                "gdp",
                service.get_gdp(
                    start_quarter,
                    end_quarter,
                    as_of=context.as_of,
                ),
            )
        ]
        queries.extend(
            (
                series,
                service.get_monthly_indicator(
                    series,
                    start_month,
                    end_month,
                    as_of=context.as_of,
                ),
            )
            for series in sorted(_MONTHLY_MACRO_SERIES)
        )
        return await execute_tool_queries(
            tool_name="get_china_macro_context",
            context=context,
            queries=queries,
        )

    async def get_interest_rate_context(
        series: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        return await execute_tool_queries(
            tool_name="get_interest_rate_context",
            context=context,
            queries=(
                (
                    "interest_rate_series",
                    context.services.macro_data.get_rate_range(
                        series,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    return (
        create_structured_tool(
            name="get_financial_statements",
            description="取得同一股票同一报告期的利润表、资产负债表和现金流量表。",
            args_schema=StockPeriodInput,
            coroutine=get_financial_statements,
        ),
        create_structured_tool(
            name="get_financial_quality",
            description="取得财务指标、主营构成和审计意见，用于评价盈利质量与经营结构。",
            args_schema=FinancialQualityInput,
            coroutine=get_financial_quality,
        ),
        create_structured_tool(
            name="get_earnings_and_disclosure",
            description="取得指定报告期的业绩预告、业绩快报和披露日程；合法空表表示公司没有该事件。",
            args_schema=StockPeriodInput,
            coroutine=get_earnings_and_disclosure,
        ),
        create_structured_tool(
            name="get_dividend_and_ownership_context",
            description="取得分红、前十大股东、流通股东和股东人数变化。",
            args_schema=DividendOwnershipInput,
            coroutine=get_dividend_and_ownership_context,
        ),
        create_structured_tool(
            name="get_pledge_risk_context",
            description="取得个股股权质押统计和质押明细，用于判断控制权与强平风险。",
            args_schema=StockCodeInput,
            coroutine=get_pledge_risk_context,
        ),
        create_structured_tool(
            name="get_valuation_context",
            description="取得日期区间内的 PE、PB、股息率、换手率和市值等每日估值数据。",
            args_schema=StockDateRangeToolInput,
            coroutine=get_valuation_context,
        ),
        create_structured_tool(
            name="get_china_macro_context",
            description="取得 GDP 以及 CPI、PPI、货币供应、社融和 PMI 的一组中国宏观数据。",
            args_schema=ChinaMacroContextInput,
            coroutine=get_china_macro_context,
        ),
        create_structured_tool(
            name="get_interest_rate_context",
            description="查询一条境内或境外利率序列，用于验证融资环境和贴现率变化。",
            args_schema=InterestRateContextInput,
            coroutine=get_interest_rate_context,
        ),
    )
