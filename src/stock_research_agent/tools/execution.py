"""Tool 查询执行、来源压缩、大小保护与异常转换。"""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from uuid import uuid4

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ValidationError

from stock_research_agent.providers.errors import ProviderError, ProviderErrorCode
from stock_research_agent.providers.models import ProviderSource
from stock_research_agent.research_data import ResearchDataBundle
from stock_research_agent.research_data.models import ResearchMetadataValue
from stock_research_agent.services.errors import (
    ServiceApiOwnershipError,
    ServiceDataValidationError,
    ServiceInputError,
    ServicePaginationError,
)
from stock_research_agent.services.models import ServiceDataset, ServicePageTrace
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.models import (
    ResearchToolResult,
    StoredResearchToolResult,
    StoredToolDatasetResult,
    ToolDataRow,
    ToolDatasetResult,
    ToolIssue,
    ToolIssueCode,
    ToolResultStatus,
    ToolRowSource,
    ToolSourceSummary,
)

logger = logging.getLogger(__name__)

ToolQuery = tuple[str, Awaitable[ServiceDataset]]


async def execute_tool_queries(
    *,
    tool_name: str,
    context: ResearchToolContext,
    queries: Iterable[ToolQuery],
) -> dict[str, Any]:
    """并行执行一个语义 Tool 所需的若干 Service 查询。

    已知的上游或数据错误会成为结构化 issue；部分查询成功时状态为 partial。
    未知编程错误只向日志写 traceback，给 LLM 的内容使用 correlation_id。
    """

    ordered_queries = tuple(queries)
    outcomes = await asyncio.gather(
        *(query for _, query in ordered_queries),
        return_exceptions=True,
    )

    datasets: list[ToolDatasetResult] = []
    issues: list[ToolIssue] = []
    for (label, _), outcome in zip(ordered_queries, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            issues.append(_issue_from_exception(label, outcome))
        else:
            datasets.append(_convert_dataset(label, outcome))

    total_items = sum(dataset.returned_item_count for dataset in datasets)
    if total_items > context.limits.max_items:
        return _too_large_result(
            tool_name,
            context,
            f"查询结果共有 {total_items} 行，超过 Tool 上限 {context.limits.max_items} 行",
        )

    oversized_label = _find_oversized_item(datasets, context.limits.max_item_chars)
    if oversized_label is not None:
        return _too_large_result(
            tool_name,
            context,
            f"数据集 {oversized_label} 中存在超过单行字符上限的内容",
        )

    result = _build_result(tool_name, context, datasets, issues)
    payload = result.model_dump(mode="json")
    serialized_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if serialized_chars > context.limits.max_serialized_chars:
        return _too_large_result(
            tool_name,
            context,
            (
                f"序列化结果约 {serialized_chars} 字符，超过 Tool 上限 "
                f"{context.limits.max_serialized_chars} 字符"
            ),
        )
    return payload


async def execute_stored_tool_queries(
    *,
    tool_name: str,
    bundle_kind: str,
    bundle_metadata: Mapping[str, ResearchMetadataValue],
    context: ResearchToolContext,
    queries: Iterable[ToolQuery],
) -> dict[str, Any]:
    """执行原始行情查询，把完整结果存入运行期仓库，只向 LLM 返回清单和预览。

    与 :func:`execute_tool_queries` 的关键区别是：完整 ``ServiceDataset`` 不再进入
    模型上下文，而是保存为一个 ``context_ref``。后续确定性计算器凭该引用读取
    完整数据，因此预览截断不会损害指标计算。
    """

    ordered_queries = tuple(queries)
    outcomes = await asyncio.gather(
        *(query for _, query in ordered_queries),
        return_exceptions=True,
    )

    successful_datasets: dict[str, ServiceDataset] = {}
    issues: list[ToolIssue] = []
    for (label, _), outcome in zip(ordered_queries, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            issues.append(_issue_from_exception(label, outcome))
        else:
            successful_datasets[label] = outcome

    failed_dataset_labels = tuple(
        issue.dataset_label for issue in issues if issue.dataset_label is not None
    )
    incomplete_dataset_labels = tuple(
        label for label, dataset in successful_datasets.items() if not dataset.complete
    )
    issues.extend(
        ToolIssue(
            dataset_label=label,
            code=ToolIssueCode.DATA_INTEGRITY,
            message="Service 返回的数据集未完整分页",
            retryable=True,
            suggested_action="缩小查询窗口后重新获取，形成证据时不得把当前数据当作完整样本",
        )
        for label in incomplete_dataset_labels
    )

    if not successful_datasets:
        return StoredResearchToolResult(
            tool_name=tool_name,
            status=ToolResultStatus.ERROR,
            as_of=context.as_of,
            context_ref=None,
            datasets=[],
            issues=issues,
            total_stored_items=0,
            total_preview_items=0,
            complete=False,
        ).model_dump(mode="json")

    try:
        stored_metadata = {
            **bundle_metadata,
            "successful_dataset_count": len(successful_datasets),
            "failed_dataset_count": len(failed_dataset_labels),
            "failed_dataset_labels": ",".join(failed_dataset_labels),
            "incomplete_dataset_labels": ",".join(incomplete_dataset_labels),
        }
        context_ref = await context.data_store.put(
            context.run_id,
            ResearchDataBundle(
                kind=bundle_kind,
                tool_name=tool_name,
                as_of=context.as_of,
                datasets=successful_datasets,
                metadata=stored_metadata,
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive storage boundary
        return StoredResearchToolResult(
            tool_name=tool_name,
            status=ToolResultStatus.ERROR,
            as_of=context.as_of,
            context_ref=None,
            datasets=[],
            issues=[*issues, _internal_issue("research_data_store", exc)],
            total_stored_items=0,
            total_preview_items=0,
            complete=False,
        ).model_dump(mode="json")

    datasets = [
        _convert_stored_dataset(
            label,
            dataset,
            preview_limit=context.limits.preview_items_per_dataset,
        )
        for label, dataset in successful_datasets.items()
    ]
    total_stored_items = sum(dataset.stored_item_count for dataset in datasets)
    total_preview_items = sum(dataset.preview_item_count for dataset in datasets)

    # max_items 在存储型 Tool 中限制的是“进入模型上下文的预览”，而不是仓库里的
    # 完整数据量。这样长区间行情仍可被计算器完整使用。
    if total_preview_items > context.limits.max_items:
        return _stored_too_large_result(
            tool_name,
            context,
            context_ref,
            total_stored_items,
            (
                f"预览结果共有 {total_preview_items} 行，超过 Tool 上限 "
                f"{context.limits.max_items} 行"
            ),
        )

    oversized_label = _find_oversized_stored_item(datasets, context.limits.max_item_chars)
    if oversized_label is not None:
        return _stored_too_large_result(
            tool_name,
            context,
            context_ref,
            total_stored_items,
            f"数据集 {oversized_label} 的预览中存在超过单行字符上限的内容",
        )

    if issues:
        status = ToolResultStatus.PARTIAL
    elif total_stored_items == 0:
        status = ToolResultStatus.EMPTY
    else:
        status = ToolResultStatus.OK

    result = StoredResearchToolResult(
        tool_name=tool_name,
        status=status,
        as_of=context.as_of,
        context_ref=context_ref,
        datasets=datasets,
        issues=issues,
        total_stored_items=total_stored_items,
        total_preview_items=total_preview_items,
        complete=not issues and all(dataset.complete for dataset in datasets),
    )
    payload = result.model_dump(mode="json")
    serialized_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if serialized_chars > context.limits.max_serialized_chars:
        return _stored_too_large_result(
            tool_name,
            context,
            context_ref,
            total_stored_items,
            (
                f"序列化预览约 {serialized_chars} 字符，超过 Tool 上限 "
                f"{context.limits.max_serialized_chars} 字符"
            ),
        )
    return payload


def create_structured_tool(
    *,
    name: str,
    description: str,
    args_schema: type[BaseModel],
    coroutine: Callable[..., Awaitable[dict[str, object]]],
) -> StructuredTool:
    """统一创建异步 StructuredTool，并安全格式化参数校验错误。"""

    return StructuredTool.from_function(
        coroutine=coroutine,
        name=name,
        description=description,
        args_schema=args_schema,
        infer_schema=False,
        handle_validation_error=_format_validation_error,
    )


def issue_from_exception(label: str, exc: BaseException) -> ToolIssue:
    """把 Service/Provider/未知异常转换成 Tool 的安全结构化问题。"""

    return _issue_from_exception(label, exc)


def _convert_dataset(label: str, dataset: ServiceDataset) -> ToolDatasetResult:
    rows = _convert_rows(dataset)

    return ToolDatasetResult(
        label=label,
        api_name=dataset.api_name,
        query_params=dataset.query_params,
        requested_fields=dataset.requested_fields,
        rows=rows,
        received_item_count=dataset.received_item_count,
        returned_item_count=len(rows),
        discarded_item_count=dataset.discarded_item_count,
        data_as_of=dataset.data_as_of,
        complete=dataset.complete,
        source_summary=_summarize_sources(dataset),
    )


def _convert_stored_dataset(
    label: str,
    dataset: ServiceDataset,
    *,
    preview_limit: int,
) -> StoredToolDatasetResult:
    preview_rows = _convert_rows(dataset, limit=preview_limit)
    stored_item_count = len(dataset.items)

    return StoredToolDatasetResult(
        label=label,
        api_name=dataset.api_name,
        query_params=dataset.query_params,
        requested_fields=dataset.requested_fields,
        preview_rows=preview_rows,
        received_item_count=dataset.received_item_count,
        stored_item_count=stored_item_count,
        preview_item_count=len(preview_rows),
        discarded_item_count=dataset.discarded_item_count,
        data_as_of=dataset.data_as_of,
        complete=dataset.complete,
        preview_complete=len(preview_rows) == stored_item_count,
        source_summary=_summarize_sources(dataset),
    )


def _convert_rows(
    dataset: ServiceDataset,
    *,
    limit: int | None = None,
) -> list[ToolDataRow]:
    items = dataset.items if limit is None else dataset.items[:limit]
    traces = dataset.item_traces if limit is None else dataset.item_traces[:limit]
    return [
        ToolDataRow(
            data=item,
            source=ToolRowSource(
                provider=trace.provider,
                from_cache=trace.from_cache,
                fetched_at=trace.fetched_at,
                page_index=trace.page_index,
                source_offset=trace.source_offset,
            ),
        )
        for item, trace in zip(items, traces, strict=True)
    ]


def _summarize_sources(dataset: ServiceDataset) -> tuple[ToolSourceSummary, ...]:

    grouped_pages: dict[tuple[ProviderSource, bool], list[ServicePageTrace]] = defaultdict(list)
    for page in dataset.pages:
        grouped_pages[(page.provider, page.from_cache)].append(page)

    return tuple(
        ToolSourceSummary(
            provider=provider,
            from_cache=from_cache,
            page_count=len(pages),
            item_count=sum(page.item_count for page in pages),
            response_bytes=sum(page.response_bytes for page in pages),
            latest_fetched_at=max(page.fetched_at for page in pages),
        )
        for (provider, from_cache), pages in grouped_pages.items()
    )


def _build_result(
    tool_name: str,
    context: ResearchToolContext,
    datasets: list[ToolDatasetResult],
    issues: list[ToolIssue],
) -> ResearchToolResult:
    total_items = sum(dataset.returned_item_count for dataset in datasets)
    if issues and datasets:
        status = ToolResultStatus.PARTIAL
    elif issues:
        status = ToolResultStatus.ERROR
    elif total_items == 0:
        status = ToolResultStatus.EMPTY
    else:
        status = ToolResultStatus.OK

    return ResearchToolResult(
        tool_name=tool_name,
        status=status,
        as_of=context.as_of,
        datasets=datasets,
        issues=issues,
        total_returned_items=total_items,
        complete=not issues and all(dataset.complete for dataset in datasets),
    )


def _issue_from_exception(label: str, exc: BaseException) -> ToolIssue:
    if isinstance(exc, ServiceInputError):
        return ToolIssue(
            dataset_label=label,
            code=ToolIssueCode.INVALID_ARGUMENT,
            message=str(exc),
            retryable=False,
            suggested_action="修正证券代码、日期区间或业务枚举后重试",
        )
    if isinstance(exc, (ServicePaginationError, ServiceDataValidationError)):
        return ToolIssue(
            dataset_label=label,
            code=ToolIssueCode.DATA_INTEGRITY,
            message=str(exc),
            retryable=False,
            suggested_action="缩小查询窗口，或交由程序维护者检查上游数据契约",
        )
    if isinstance(exc, ServiceApiOwnershipError):
        return _internal_issue(label, exc)
    if isinstance(exc, ProviderError):
        return _provider_issue(label, exc)
    return _internal_issue(label, exc)


def _provider_issue(label: str, exc: ProviderError) -> ToolIssue:
    capability_codes = {
        ProviderErrorCode.PERMISSION_DENIED,
        ProviderErrorCode.AUTHENTICATION_ERROR,
        ProviderErrorCode.UNKNOWN_API,
    }
    integrity_codes = {ProviderErrorCode.SCHEMA_ERROR}
    if exc.error_code in capability_codes:
        code = ToolIssueCode.CAPABILITY_UNAVAILABLE
        retryable = False
        action = "不要把它当成空数据；改用现有能力或通知程序维护者"
    elif exc.error_code in integrity_codes:
        code = ToolIssueCode.DATA_INTEGRITY
        retryable = False
        action = "不要使用该结果形成证据；通知程序维护者检查 schema"
    else:
        code = ToolIssueCode.UPSTREAM_UNAVAILABLE
        retryable = True
        action = "保留本次失败记录，由协调器稍后按预算重试"

    return ToolIssue(
        dataset_label=label,
        code=code,
        message=exc.safe_message or exc.error_code.value,
        retryable=retryable,
        suggested_action=action,
    )


def _internal_issue(label: str, exc: BaseException) -> ToolIssue:
    correlation_id = f"tool_{uuid4().hex}"
    logger.error(
        "Tool 内部错误 correlation_id=%s",
        correlation_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return ToolIssue(
        dataset_label=label,
        code=ToolIssueCode.INTERNAL_ERROR,
        message="工具执行发生内部错误",
        retryable=False,
        suggested_action="停止重复调用并把 correlation_id 交给程序维护者",
        correlation_id=correlation_id,
    )


def _too_large_result(
    tool_name: str,
    context: ResearchToolContext,
    message: str,
) -> dict[str, Any]:
    result = ResearchToolResult(
        tool_name=tool_name,
        status=ToolResultStatus.TOO_LARGE,
        as_of=context.as_of,
        datasets=[],
        issues=[
            ToolIssue(
                code=ToolIssueCode.RESULT_TOO_LARGE,
                message=message,
                retryable=True,
                suggested_action="缩短日期或新闻时间窗口，再分段调用该工具",
            )
        ],
        total_returned_items=0,
        complete=False,
    )
    return result.model_dump(mode="json")


def _stored_too_large_result(
    tool_name: str,
    context: ResearchToolContext,
    context_ref: str,
    total_stored_items: int,
    message: str,
) -> dict[str, Any]:
    """预览超限时保留完整数据引用，但不把超限预览交给模型。"""

    result = StoredResearchToolResult(
        tool_name=tool_name,
        status=ToolResultStatus.TOO_LARGE,
        as_of=context.as_of,
        context_ref=context_ref,
        datasets=[],
        issues=[
            ToolIssue(
                code=ToolIssueCode.RESULT_TOO_LARGE,
                message=message,
                retryable=False,
                suggested_action=(
                    "完整数据已经保存；直接把 context_ref 交给计算器，或缩短日期区间后重新获取预览"
                ),
            )
        ],
        total_stored_items=total_stored_items,
        total_preview_items=0,
        complete=False,
    )
    return result.model_dump(mode="json")


def _find_oversized_item(
    datasets: Iterable[ToolDatasetResult],
    max_item_chars: int,
) -> str | None:
    for dataset in datasets:
        for row in dataset.rows:
            size = len(json.dumps(row.data, ensure_ascii=False, default=str))
            if size > max_item_chars:
                return dataset.label
    return None


def _find_oversized_stored_item(
    datasets: Iterable[StoredToolDatasetResult],
    max_item_chars: int,
) -> str | None:
    for dataset in datasets:
        for row in dataset.preview_rows:
            size = len(json.dumps(row.data, ensure_ascii=False, default=str))
            if size > max_item_chars:
                return dataset.label
    return None


def _format_validation_error(exc: ValidationError) -> str:
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors(include_url=False, include_input=False)[:5]
    ]
    return json.dumps(
        {
            "status": ToolResultStatus.ERROR.value,
            "issues": [
                {
                    "code": ToolIssueCode.INVALID_ARGUMENT.value,
                    "message": "Tool 参数校验失败",
                    "retryable": False,
                    "suggested_action": "根据字段说明修正参数后重试",
                    "details": details,
                }
            ],
        },
        ensure_ascii=False,
    )
