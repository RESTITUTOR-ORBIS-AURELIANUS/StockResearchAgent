"""技术分析研究员每日模式的全市场快照 Tool。"""

import json

from langchain_core.tools import BaseTool

from stock_research_agent.research_data import ResearchDataBundle
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import (
    create_structured_tool,
    issue_from_exception,
)
from stock_research_agent.tools.models import (
    DailyTechnicalSnapshotInput,
    DailyTechnicalSnapshotToolResult,
    ToolIssue,
    ToolIssueCode,
    ToolResultStatus,
)

_TOOL_NAME = "get_daily_technical_market_snapshot"


def build_daily_technical_snapshot_tools(
    context: ResearchToolContext,
) -> tuple[BaseTool, ...]:
    async def get_daily_technical_market_snapshot(
        candidate_count: int = 10,
    ) -> dict[str, object]:
        try:
            build = await context.services.daily_technical_snapshot.build_daily_snapshot(
                as_of=context.as_of,
                candidate_count=candidate_count,
            )
        except Exception as exc:
            return DailyTechnicalSnapshotToolResult(
                tool_name=_TOOL_NAME,
                status=ToolResultStatus.ERROR,
                as_of=context.as_of,
                context_ref=None,
                snapshot=None,
                issues=[issue_from_exception("daily_technical_snapshot", exc)],
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
                    kind="daily_technical_market_snapshot",
                    tool_name=_TOOL_NAME,
                    as_of=context.as_of,
                    datasets=build.datasets,
                    metadata={
                        "trade_date": build.snapshot.trade_date.isoformat(),
                        "candidate_count": candidate_count,
                        "industry_standard": build.snapshot.industry_standard,
                        "industry_level": build.snapshot.industry_level,
                        "source_dataset_count": len(build.datasets),
                        "optional_failure_count": len(build.optional_failures),
                        "optional_failure_labels": ",".join(build.optional_failures),
                    },
                ),
            )
        except Exception as exc:
            return DailyTechnicalSnapshotToolResult(
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

        result = DailyTechnicalSnapshotToolResult(
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

        return DailyTechnicalSnapshotToolResult(
            tool_name=_TOOL_NAME,
            status=ToolResultStatus.TOO_LARGE,
            as_of=context.as_of,
            context_ref=context_ref,
            snapshot=None,
            issues=[
                ToolIssue(
                    code=ToolIssueCode.RESULT_TOO_LARGE,
                    message=(
                        f"每日技术快照约 {serialized_chars} 字符，超过 Tool 上限 "
                        f"{context.limits.max_serialized_chars} 字符"
                    ),
                    retryable=False,
                    suggested_action=(
                        "完整源表已经保存；提高受控输出上限或缩小每组候选数量后重新调用"
                    ),
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
                "技术分析每日模式的首个入口。自动选择 as_of 时点最近的完整交易日，"
                "汇总全 A 股涨跌宽度、涨跌停/停牌、主要市场指数、申万一级行业宽度与"
                "行业指数表现，并按涨幅、跌幅、成交额、换手率和量比挑出后续查证候选。"
                "完整原始源表保存在 context_ref；不要用它替代指定个股的详细技术查证。"
            ),
            args_schema=DailyTechnicalSnapshotInput,
            coroutine=get_daily_technical_market_snapshot,
        ),
    )
