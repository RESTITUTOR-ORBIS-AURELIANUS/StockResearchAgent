"""基本面研究员每日模式的全市场快照 Tool。"""

import json

from langchain_core.tools import BaseTool

from stock_research_agent.research_data import ResearchDataBundle
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool, issue_from_exception
from stock_research_agent.tools.models import (
    DailyFundamentalSnapshotInput,
    DailyFundamentalSnapshotToolResult,
    ToolIssue,
    ToolIssueCode,
    ToolResultStatus,
)

_TOOL_NAME = "get_daily_fundamental_snapshot"


def build_daily_fundamental_snapshot_tools(
    context: ResearchToolContext,
) -> tuple[BaseTool, ...]:
    async def get_daily_fundamental_snapshot(
        candidate_count: int = 10,
        announcement_lookback_days: int = 14,
    ) -> dict[str, object]:
        try:
            build = await context.services.daily_fundamental_snapshot.build_daily_snapshot(
                as_of=context.as_of,
                candidate_count=candidate_count,
                announcement_lookback_days=announcement_lookback_days,
            )
        except Exception as exc:
            return DailyFundamentalSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=None,
                issues=[issue_from_exception("daily_fundamental_snapshot", exc)],
                source_dataset_count=0,
                total_stored_items=0,
                complete=False,
            ).model_dump(mode="json")

        total_stored_items = sum(len(dataset.items) for dataset in build.datasets.values())
        optional_issues = [
            issue_from_exception(label, exc) for label, exc in build.optional_failures.items()
        ]
        try:
            context_ref = await context.data_store.put(
                context.run_id,
                ResearchDataBundle(
                    kind="daily_fundamental_snapshot",
                    tool_name=_TOOL_NAME,
                    as_of=context.as_of,
                    datasets=build.datasets,
                    metadata={
                        "trade_date": build.snapshot.trade_date.isoformat(),
                        "report_period": build.snapshot.report_period,
                        "comparison_period": build.snapshot.comparison_period,
                        "candidate_count": candidate_count,
                        "announcement_lookback_days": announcement_lookback_days,
                        "source_dataset_count": len(build.datasets),
                        "optional_failure_count": len(build.optional_failures),
                        "optional_failure_labels": ",".join(build.optional_failures),
                    },
                ),
            )
        except Exception as exc:
            return DailyFundamentalSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=None,
                issues=[issue_from_exception("research_data_store", exc)],
                source_dataset_count=len(build.datasets),
                total_stored_items=0,
                complete=False,
            ).model_dump(mode="json")

        result = DailyFundamentalSnapshotToolResult(
            tool_name=_TOOL_NAME,
            status=(ToolResultStatus.PARTIAL if optional_issues else ToolResultStatus.OK),
            as_of=context.as_of,
            context_ref=context_ref,
            snapshot=build.snapshot,
            issues=optional_issues,
            source_dataset_count=len(build.datasets),
            total_stored_items=total_stored_items,
            complete=not optional_issues,
        )
        payload = result.model_dump(mode="json")
        serialized_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if serialized_chars <= context.limits.max_serialized_chars:
            return payload

        return DailyFundamentalSnapshotToolResult(
            tool_name=_TOOL_NAME,
            status=ToolResultStatus.TOO_LARGE,
            as_of=context.as_of,
            context_ref=context_ref,
            snapshot=None,
            issues=[
                ToolIssue(
                    code=ToolIssueCode.RESULT_TOO_LARGE,
                    message=(
                        f"每日基本面快照约 {serialized_chars} 字符，超过 Tool 上限 "
                        f"{context.limits.max_serialized_chars} 字符"
                    ),
                    retryable=False,
                    suggested_action="完整源表已经保存；缩小候选数量后重新调用",
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
                "基本面分析每日模式的首个入口。自动选择最近完整交易日和当前披露窗口的报告期，"
                "汇总全市场估值极值、近期业绩预告/快报、同比财务质量变化，以及中国宏观和"
                "中美利率最近观测。完整源表保存在 context_ref；低估值等横截面极值不是投资结论，"
                "模型应只对少量候选继续调用个股基本面 Tool 查证。"
            ),
            args_schema=DailyFundamentalSnapshotInput,
            coroutine=get_daily_fundamental_snapshot,
        ),
    )
