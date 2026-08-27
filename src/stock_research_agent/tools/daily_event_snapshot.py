"""新闻事件研究员每日模式的公开新闻与公告快照 Tool。"""

import json

from langchain_core.tools import BaseTool

from stock_research_agent.research_data import ResearchDataBundle
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, issue_from_exception
from stock_research_agent.tools.models import (
    DailyEventSnapshotInput,
    DailyEventSnapshotToolResult,
    ToolIssue,
    ToolIssueCode,
    ToolResultStatus,
)

_TOOL_NAME = "get_daily_event_snapshot"


def build_daily_event_snapshot_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    async def get_daily_event_snapshot(
        candidate_count: int = 10,
        news_lookback_hours: int = 24,
        announcement_lookback_days: int = 3,
        research_lookback_days: int = 7,
    ) -> dict[str, object]:
        try:
            build = await context.services.daily_event_snapshot.build_daily_snapshot(
                as_of=context.as_of,
                candidate_count=candidate_count,
                news_lookback_hours=news_lookback_hours,
                announcement_lookback_days=announcement_lookback_days,
                research_lookback_days=research_lookback_days,
            )
        except Exception as exc:
            return DailyEventSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=None,
                issues=[issue_from_exception("daily_event_snapshot", exc)],
                source_dataset_count=0,
                total_stored_items=0,
                complete=False,
            ).model_dump(mode="json")

        optional_issues = [
            issue_from_exception(label, exc) for label, exc in build.optional_failures.items()
        ]
        total_stored_items = sum(len(dataset.items) for dataset in build.datasets.values())
        if not build.datasets:
            return DailyEventSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=build.snapshot,
                issues=optional_issues
                or [
                    ToolIssue(
                        code=ToolIssueCode.UPSTREAM_UNAVAILABLE,
                        message="新闻、公告、卖方研究与股票目录均未返回可保存的数据集",
                        retryable=True,
                        suggested_action="保留失败记录并在稍后按预算重试",
                    )
                ],
                source_dataset_count=0,
                total_stored_items=0,
                complete=False,
            ).model_dump(mode="json")

        try:
            context_ref = await context.data_store.put(
                context.run_id,
                ResearchDataBundle(
                    kind="daily_event_snapshot",
                    tool_name=_TOOL_NAME,
                    as_of=context.as_of,
                    datasets=build.datasets,
                    metadata={
                        "candidate_count": candidate_count,
                        "news_lookback_hours": news_lookback_hours,
                        "announcement_lookback_days": announcement_lookback_days,
                        "research_lookback_days": research_lookback_days,
                        "source_dataset_count": len(build.datasets),
                        "optional_failure_count": len(build.optional_failures),
                        "optional_failure_labels": ",".join(build.optional_failures),
                        "recent_feed_is_complete_history": False,
                    },
                ),
            )
        except Exception as exc:
            return DailyEventSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=None,
                issues=[*optional_issues, issue_from_exception("research_data_store", exc)],
                source_dataset_count=len(build.datasets),
                total_stored_items=0,
                complete=False,
            ).model_dump(mode="json")

        selected_count = (
            len(build.snapshot.market_news)
            + len(build.snapshot.announcements)
            + len(build.snapshot.sell_side_reports)
            + len(build.snapshot.broker_recommendations)
        )
        if optional_issues:
            status = ToolResultStatus.PARTIAL
        elif selected_count == 0:
            status = ToolResultStatus.EMPTY
        else:
            status = ToolResultStatus.OK
        result = DailyEventSnapshotToolResult(
            tool_name=_TOOL_NAME,
            status=status,
            as_of=context.as_of,
            context_ref=context_ref,
            snapshot=build.snapshot,
            issues=optional_issues,
            source_dataset_count=len(build.datasets),
            total_stored_items=total_stored_items,
            complete=(
                not optional_issues and all(dataset.complete for dataset in build.datasets.values())
            ),
        )
        payload = result.model_dump(mode="json")
        serialized_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if serialized_chars <= context.limits.max_serialized_chars:
            return payload

        return DailyEventSnapshotToolResult(
            tool_name=_TOOL_NAME,
            status=ToolResultStatus.TOO_LARGE,
            as_of=context.as_of,
            context_ref=context_ref,
            snapshot=None,
            issues=[
                ToolIssue(
                    code=ToolIssueCode.RESULT_TOO_LARGE,
                    message=(
                        f"每日事件快照约 {serialized_chars} 字符，超过 Tool 上限 "
                        f"{context.limits.max_serialized_chars} 字符"
                    ),
                    retryable=False,
                    suggested_action="完整源表已经保存；减小 candidate_count 后重新调用",
                )
            ],
            source_dataset_count=len(build.datasets),
            total_stored_items=total_stored_items,
            complete=False,
        ).model_dump(mode="json")

    return (
        create_structured_tool(
            name=_TOOL_NAME,
            description=(
                "新闻事件分析每日模式的首个入口。并行抓取东方财富、同花顺、财联社最近快讯，"
                "近期全市场公告索引、按日卖方研报结构化摘要和当月券商金股；完整源表"
                "保存到 context_ref，只返回确定性去重后的少量候选。新闻只有精确出现"
                "stock_basic 上市公司全名才会写入 related_stocks，不做供应链或概念联想。"
                "公开快讯只是各站点最近 N 条，coverage 明确标记它不是"
                "可任意历史回放的完整新闻库。"
            ),
            args_schema=DailyEventSnapshotInput,
            coroutine=get_daily_event_snapshot,
        ),
    )
