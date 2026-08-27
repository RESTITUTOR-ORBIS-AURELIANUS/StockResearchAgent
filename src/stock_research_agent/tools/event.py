"""事件研究员使用的新闻、公司行动与宏观日历 Tool。"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool

from stock_research_agent.services.public_news_event import MarketNewsSource
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, execute_tool_queries
from stock_research_agent.tools.models import (
    CorporateActionInput,
    EconomicCalendarInput,
    NewsWindowInput,
    SellSideResearchContextInput,
    TargetedNewsDisclosureInput,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def build_event_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def search_market_news(
        start_at: datetime,
        end_at: datetime,
        source: str = "ALL",
    ) -> dict[str, object]:
        sources = tuple(MarketNewsSource) if source == "ALL" else (MarketNewsSource(source),)
        return await execute_tool_queries(
            tool_name="search_market_news",
            context=context,
            queries=tuple(
                (
                    f"market_news_{news_source.value.lower()}",
                    context.services.public_news_event.get_market_news(
                        news_source,
                        start_at,
                        end_at,
                        as_of=context.as_of,
                    ),
                )
                for news_source in sources
            ),
        )

    async def get_targeted_news_and_disclosures(
        ts_code: str,
        start_date: date,
        end_date: date,
        announcement_category: str = "全部",
    ) -> dict[str, object]:
        start_at = datetime.combine(start_date, time.min, tzinfo=_SHANGHAI)
        requested_end = datetime.combine(end_date, time.max, tzinfo=_SHANGHAI).replace(
            microsecond=0
        )
        end_at = min(requested_end, context.as_of)
        public_news = context.services.public_news_event
        return await execute_tool_queries(
            tool_name="get_targeted_news_and_disclosures",
            context=context,
            queries=(
                (
                    "stock_news",
                    public_news.get_stock_news(
                        ts_code,
                        start_at,
                        end_at,
                        as_of=context.as_of,
                    ),
                ),
                (
                    "stock_announcements",
                    public_news.get_stock_announcements(
                        ts_code,
                        start_date,
                        end_date,
                        category=announcement_category,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    async def get_corporate_action_events(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        ownership = context.services.ownership_event
        fundamental = context.services.fundamental_data
        return await execute_tool_queries(
            tool_name="get_corporate_action_events",
            context=context,
            queries=(
                (
                    "repurchase_events",
                    ownership.get_repurchase_events(
                        ts_code, start_date, end_date, as_of=context.as_of
                    ),
                ),
                (
                    "unlock_events",
                    ownership.get_unlock_events(ts_code, start_date, end_date, as_of=context.as_of),
                ),
                (
                    "holder_trades",
                    ownership.get_holder_trades(ts_code, start_date, end_date, as_of=context.as_of),
                ),
                (
                    "dividends",
                    fundamental.get_dividends(
                        ts_code,
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    async def get_sell_side_research_context(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        news_event = context.services.news_event
        queries = [
            (
                "sell_side_reports",
                news_event.get_sell_side_reports(
                    start_date,
                    end_date,
                    ts_code=ts_code,
                    as_of=context.as_of,
                ),
            )
        ]
        queries.extend(
            (
                f"broker_recommendations_{month}",
                news_event.get_broker_recommendations(
                    month,
                    ts_code=ts_code,
                    as_of=context.as_of,
                ),
            )
            for month in _months_between(start_date, end_date)
        )
        return await execute_tool_queries(
            tool_name="get_sell_side_research_context",
            context=context,
            queries=tuple(queries),
        )

    async def get_economic_calendar(
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        return await execute_tool_queries(
            tool_name="get_economic_calendar",
            context=context,
            queries=(
                (
                    "economic_calendar",
                    context.services.macro_data.get_economic_calendar(
                        start_date,
                        end_date,
                        as_of=context.as_of,
                    ),
                ),
            ),
        )

    return (
        create_structured_tool(
            name="search_market_news",
            description=(
                "读取冻结时间之前、最长 24 小时窗口内的公开市场快讯。默认并行查询东方财富、"
                "同花顺和财联社；单个来源失败会明确返回 partial。快讯接口只提供各站点最近 N 条，"
                "结果 complete=false 时不能声称该窗口新闻已经穷尽。"
            ),
            args_schema=NewsWindowInput,
            coroutine=search_market_news,
        ),
        create_structured_tool(
            name="get_targeted_news_and_disclosures",
            description=(
                "新闻事件分析的指定股票查证入口。同时读取该股票最近新闻和公告索引；新闻来自"
                "东方财富，公告来自东方财富公开公告页。返回原文 URL 与逐行来源，不下载或绕过"
                "任何付费正文。单次日期窗口最多 31 天。"
            ),
            args_schema=TargetedNewsDisclosureInput,
            coroutine=get_targeted_news_and_disclosures,
        ),
        create_structured_tool(
            name="get_corporate_action_events",
            description="取得个股回购、解禁、股东增减持和分红事件，用于寻找催化剂与供给风险。",
            args_schema=CorporateActionInput,
            coroutine=get_corporate_action_events,
        ),
        create_structured_tool(
            name="get_sell_side_research_context",
            description=(
                "指定股票的卖方研究查证入口。返回 report_rc 的结构化研报标题、"
                "机构、评级、目标价和预测摘要，以及查询月份内的券商金股名单。"
                "这些数据只能证明某机构发表过该观点，不能当作未来业绩或"
                "股价结果的 ground truth；report_rc 不含研报全文。"
            ),
            args_schema=SellSideResearchContextInput,
            coroutine=get_sell_side_research_context,
        ),
        create_structured_tool(
            name="get_economic_calendar",
            description="取得日期区间内的财经事件日历，用于识别宏观事件与时间上的共现关系。",
            args_schema=EconomicCalendarInput,
            coroutine=get_economic_calendar,
        ),
    )


def _months_between(start_date: date, end_date: date) -> tuple[str, ...]:
    """返回闭区间覆盖的 YYYYMM，不依赖外部日期库。"""

    year, month = start_date.year, start_date.month
    end_value = end_date.year * 12 + end_date.month
    result: list[str] = []
    while year * 12 + month <= end_value:
        result.append(f"{year:04d}{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tuple(result)
