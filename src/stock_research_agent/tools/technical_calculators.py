"""只接受运行期数据引用的五个确定性技术计算器 Tool。"""

import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from stock_research_agent.analytics.technical import (
    MomentumInput,
    RelativeStrengthInput,
    ReturnAndTrendInput,
    RiskAndTradabilityInput,
    TechnicalAnalyticsError,
    TechnicalInstrumentKind,
    TechnicalSeriesInput,
    VolumeAndLiquidityInput,
    calculate_momentum,
    calculate_relative_strength,
    calculate_return_and_trend,
    calculate_risk_and_tradability,
    calculate_volume_and_liquidity,
)
from stock_research_agent.research_data import ResearchDataBundle, ResearchDataStoreError
from stock_research_agent.tools.context import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool
from stock_research_agent.tools.models import (
    MomentumCalculatorInput,
    RelativeStrengthCalculatorInput,
    ReturnAndTrendCalculatorInput,
    RiskAndTradabilityCalculatorInput,
    TechnicalCalculationSubject,
    TechnicalCalculationToolResult,
    ToolIssue,
    ToolIssueCode,
    ToolResultStatus,
    VolumeAndLiquidityCalculatorInput,
)

logger = logging.getLogger(__name__)

_PRICE_LABEL_BY_KIND = {
    "stock_price_context": "price_bars",
    "index_market_context": "index_price_bars",
    "fund_market_context": "fund_price_bars",
}
_ADJUSTMENT_LABEL_BY_KIND = {
    "stock_price_context": "adjustment_factors",
    "fund_market_context": "fund_adjustment_factors",
}
_INSTRUMENT_KIND_BY_BUNDLE = {
    "stock_price_context": TechnicalInstrumentKind.STOCK,
    "index_market_context": TechnicalInstrumentKind.INDEX,
    "fund_market_context": TechnicalInstrumentKind.FUND,
}


class _CalculatorContractError(ValueError):
    """引用存在，但数据包类型或组合不满足当前计算器契约。"""


class _CalculatorSourceDataError(ValueError):
    """引用合法，但上游失败使计算所需的数据集不存在。"""


def build_technical_calculator_tools(context: ResearchToolContext) -> tuple[BaseTool, ...]:
    """创建五个 Tool；LLM 只能提供引用和受控计算参数。"""

    async def return_and_trend(
        context_ref: str,
        windows: tuple[int, ...] = (5, 20, 60),
    ) -> dict[str, object]:
        return await _run_single_bundle_calculator(
            tool_name="calculate_return_and_trend",
            context=context,
            context_ref=context_ref,
            calculator=lambda bundle: calculate_return_and_trend(
                ReturnAndTrendInput(series=_series_from_bundle(bundle), windows=windows)
            ),
        )

    async def momentum(
        context_ref: str,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        roc_periods: tuple[int, ...] = (5, 20),
    ) -> dict[str, object]:
        return await _run_single_bundle_calculator(
            tool_name="calculate_momentum",
            context=context,
            context_ref=context_ref,
            calculator=lambda bundle: calculate_momentum(
                MomentumInput(
                    series=_series_from_bundle(bundle),
                    rsi_period=rsi_period,
                    macd_fast=macd_fast,
                    macd_slow=macd_slow,
                    macd_signal=macd_signal,
                    roc_windows=roc_periods,
                )
            ),
        )

    async def risk_and_tradability(
        context_ref: str,
        volatility_window: int = 20,
        atr_period: int = 14,
    ) -> dict[str, object]:
        return await _run_single_bundle_calculator(
            tool_name="calculate_risk_and_tradability",
            context=context,
            context_ref=context_ref,
            calculator=lambda bundle: calculate_risk_and_tradability(
                _risk_input_from_bundle(bundle, volatility_window, atr_period)
            ),
            extra_relevant_labels=frozenset({"price_limits", "suspensions", "trade_calendar"}),
        )

    async def volume_and_liquidity(
        context_ref: str,
        windows: tuple[int, ...] = (5, 20),
    ) -> dict[str, object]:
        return await _run_single_bundle_calculator(
            tool_name="calculate_volume_and_liquidity",
            context=context,
            context_ref=context_ref,
            calculator=lambda bundle: calculate_volume_and_liquidity(
                _volume_input_from_bundle(bundle, windows)
            ),
            extra_relevant_labels=frozenset({"daily_valuation_and_turnover"}),
        )

    async def relative_strength(
        target_context_ref: str,
        benchmark_context_ref: str,
        windows: tuple[int, ...] = (20, 60),
    ) -> dict[str, object]:
        refs = (target_context_ref, benchmark_context_ref)
        try:
            target = await context.data_store.get(context.run_id, target_context_ref)
            benchmark = await context.data_store.get(context.run_id, benchmark_context_ref)
            _validate_relative_bundles(target, benchmark)
            result = calculate_relative_strength(
                RelativeStrengthInput(
                    target=_series_from_bundle(target),
                    benchmark=_series_from_bundle(benchmark),
                    windows=windows,
                )
            )
        except ResearchDataStoreError:
            return _reference_error_result(
                "calculate_relative_strength",
                context,
                refs,
            )
        except _CalculatorContractError as exc:
            return _contract_error_result(
                "calculate_relative_strength",
                context,
                refs,
                str(exc),
            )
        except _CalculatorSourceDataError as exc:
            return _source_data_error_result(
                "calculate_relative_strength",
                context,
                refs,
                str(exc),
            )
        except TechnicalAnalyticsError as exc:
            return _analytics_error_result(
                "calculate_relative_strength",
                context,
                refs,
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive safety boundary
            return _internal_error_result(
                "calculate_relative_strength",
                context,
                refs,
                exc,
            )

        source_issues = _source_bundle_issues((target, benchmark))
        calculation_issues = _calculation_tool_issues(result)
        return _success_result(
            "calculate_relative_strength",
            context,
            refs,
            result,
            [*source_issues, *calculation_issues],
            source_bundles=(
                (target_context_ref, target),
                (benchmark_context_ref, benchmark),
            ),
        )

    return (
        create_structured_tool(
            name="calculate_return_and_trend",
            description=(
                "读取行情 context_ref 的完整数据，确定性计算区间/分期收益、均线、"
                "均线斜率、交叉和突破。不要传原始行情行。"
            ),
            args_schema=ReturnAndTrendCalculatorInput,
            coroutine=return_and_trend,
        ),
        create_structured_tool(
            name="calculate_momentum",
            description=(
                "读取行情 context_ref，确定性计算 RSI、MACD、ROC 和价格-RSI 背离。"
                "参数不足时会明确返回 insufficient_history。"
            ),
            args_schema=MomentumCalculatorInput,
            coroutine=momentum,
        ),
        create_structured_tool(
            name="calculate_risk_and_tradability",
            description=(
                "读取股票、市场/行业指数或基金/ETF 行情 context_ref，计算波动率、"
                "下行波动、ATR、回撤和跳空；涨跌停、停牌等可交易性指标只对个股适用。"
            ),
            args_schema=RiskAndTradabilityCalculatorInput,
            coroutine=risk_and_tradability,
        ),
        create_structured_tool(
            name="calculate_volume_and_liquidity",
            description=(
                "读取股票、市场/行业指数或基金/ETF 行情 context_ref，计算成交量/"
                "成交额均值、相对成交量、OBV、Amihud 非流动性和量价组合计数；"
                "daily_basic 换手率只对个股适用。"
            ),
            args_schema=VolumeAndLiquidityCalculatorInput,
            coroutine=volume_and_liquidity,
        ),
        create_structured_tool(
            name="calculate_relative_strength",
            description=(
                "读取目标与基准两个 context_ref，按共同交易日计算超额收益、相关性、"
                "Beta 和上下行相对表现；两个引用必须属于当前研究运行。"
            ),
            args_schema=RelativeStrengthCalculatorInput,
            coroutine=relative_strength,
        ),
    )


async def _run_single_bundle_calculator(
    *,
    tool_name: str,
    context: ResearchToolContext,
    context_ref: str,
    calculator: Callable[[ResearchDataBundle], BaseModel],
    extra_relevant_labels: frozenset[str] = frozenset(),
) -> dict[str, object]:
    refs = (context_ref,)
    try:
        bundle = await context.data_store.get(context.run_id, context_ref)
        result = calculator(bundle)
    except ResearchDataStoreError:
        return _reference_error_result(tool_name, context, refs)
    except _CalculatorContractError as exc:
        return _contract_error_result(tool_name, context, refs, str(exc))
    except _CalculatorSourceDataError as exc:
        return _source_data_error_result(tool_name, context, refs, str(exc))
    except TechnicalAnalyticsError as exc:
        return _analytics_error_result(tool_name, context, refs, exc)
    except Exception as exc:  # pragma: no cover - defensive safety boundary
        return _internal_error_result(tool_name, context, refs, exc)

    source_issues = _source_bundle_issues(
        (bundle,),
        extra_relevant_labels=extra_relevant_labels,
    )
    calculation_issues = _calculation_tool_issues(result)
    return _success_result(
        tool_name,
        context,
        refs,
        result,
        [*source_issues, *calculation_issues],
        source_bundles=((context_ref, bundle),),
    )


def _series_from_bundle(bundle: ResearchDataBundle) -> TechnicalSeriesInput:
    price_label = _PRICE_LABEL_BY_KIND.get(bundle.kind)
    if price_label is None:
        raise _CalculatorContractError(f"数据包类型 {bundle.kind} 不是技术计算器支持的行情 context")
    price_rows = _required_dataset_rows(bundle, price_label)
    adjustment_label = _ADJUSTMENT_LABEL_BY_KIND.get(bundle.kind)
    adjustment_required = _adjustment_required(bundle)
    if adjustment_required and adjustment_label not in bundle.datasets:
        raise _CalculatorSourceDataError(f"源数据包缺少必需数据集 {adjustment_label}")
    adjustment_rows = (
        _dataset_rows(bundle, adjustment_label) if adjustment_label is not None else []
    )
    return TechnicalSeriesInput(
        price_rows=price_rows,
        adjustment_rows=adjustment_rows,
        adjustment_mode="forward" if adjustment_required else "raw",
        require_adjustment=adjustment_required,
    )


def _required_dataset_rows(bundle: ResearchDataBundle, label: str) -> list[dict[str, Any]]:
    dataset = bundle.datasets.get(label)
    if dataset is None:
        raise _CalculatorSourceDataError(f"源数据包缺少必需数据集 {label}")
    return dataset.items


def _dataset_rows(bundle: ResearchDataBundle, label: str | None) -> list[dict[str, Any]]:
    if label is None:
        return []
    dataset = bundle.datasets.get(label)
    return dataset.items if dataset is not None else []


def _adjustment_required(bundle: ResearchDataBundle) -> bool:
    if bundle.kind == "stock_price_context":
        return True
    if bundle.kind == "fund_market_context":
        return bool(bundle.metadata.get("include_adjustment_factors", True))
    return False


def _risk_input_from_bundle(
    bundle: ResearchDataBundle,
    volatility_window: int,
    atr_period: int,
) -> RiskAndTradabilityInput:
    _require_daily_bundle(bundle, "风险与可交易性")
    return RiskAndTradabilityInput(
        series=_series_from_bundle(bundle),
        instrument_kind=_instrument_kind(bundle),
        price_limit_rows=_dataset_rows(bundle, "price_limits"),
        suspension_rows=_dataset_rows(bundle, "suspensions"),
        calendar_rows=_dataset_rows(bundle, "trade_calendar"),
        volatility_window=volatility_window,
        atr_period=atr_period,
    )


def _volume_input_from_bundle(
    bundle: ResearchDataBundle,
    windows: tuple[int, ...],
) -> VolumeAndLiquidityInput:
    _require_daily_bundle(bundle, "量能与流动性")
    return VolumeAndLiquidityInput(
        series=_series_from_bundle(bundle),
        instrument_kind=_instrument_kind(bundle),
        valuation_rows=_dataset_rows(bundle, "daily_valuation_and_turnover"),
        windows=windows,
    )


def _instrument_kind(bundle: ResearchDataBundle) -> TechnicalInstrumentKind:
    try:
        return _INSTRUMENT_KIND_BY_BUNDLE[bundle.kind]
    except KeyError as exc:
        raise _CalculatorContractError(
            f"数据包类型 {bundle.kind} 不是技术计算器支持的行情 context"
        ) from exc


def _require_daily_bundle(bundle: ResearchDataBundle, calculator_label: str) -> None:
    if bundle.metadata.get("frequency") != "daily":
        raise _CalculatorContractError(
            f"{calculator_label}计算器第一版只支持 daily 行情；周线/月线不能混用日频附属数据"
        )


def _validate_relative_bundles(
    target: ResearchDataBundle,
    benchmark: ResearchDataBundle,
) -> None:
    if target.kind not in _PRICE_LABEL_BY_KIND or benchmark.kind not in _PRICE_LABEL_BY_KIND:
        raise _CalculatorContractError("目标和基准都必须是行情 context_ref")
    if target.as_of != benchmark.as_of:
        raise _CalculatorContractError("目标和基准的 as_of 不一致，不能比较不同信息截面")
    if target.metadata.get("frequency") != benchmark.metadata.get("frequency"):
        raise _CalculatorContractError("目标和基准的数据频率不一致")
    target_code = target.metadata.get("ts_code")
    benchmark_code = benchmark.metadata.get("ts_code")
    if target_code is not None and target.kind == benchmark.kind and target_code == benchmark_code:
        raise _CalculatorContractError("目标和基准不能是同一证券")


def _source_bundle_issues(
    bundles: tuple[ResearchDataBundle, ...],
    *,
    extra_relevant_labels: frozenset[str] = frozenset(),
) -> list[ToolIssue]:
    issues: list[ToolIssue] = []
    for bundle in bundles:
        relevant_labels = _core_series_labels(bundle) | extra_relevant_labels
        failed_labels = {
            label
            for label in str(bundle.metadata.get("failed_dataset_labels", "")).split(",")
            if label
        }
        relevant_failed_labels = failed_labels & relevant_labels
        incomplete_labels = [
            label
            for label, dataset in bundle.datasets.items()
            if label in relevant_labels and not dataset.complete
        ]
        if relevant_failed_labels or incomplete_labels:
            details = []
            if relevant_failed_labels:
                details.append(f"失败数据集：{','.join(sorted(relevant_failed_labels))}")
            if incomplete_labels:
                details.append(f"未完整数据集：{','.join(incomplete_labels)}")
            issues.append(
                ToolIssue(
                    dataset_label=bundle.kind,
                    code=ToolIssueCode.DATA_INTEGRITY,
                    message="源行情数据包不完整" + (f"（{'；'.join(details)}）" if details else ""),
                    retryable=True,
                    suggested_action="重新获取该行情 context；形成证据时必须披露本次结果不完整",
                )
            )
    return issues


def _core_series_labels(bundle: ResearchDataBundle) -> frozenset[str]:
    price_label = _PRICE_LABEL_BY_KIND.get(bundle.kind)
    labels = {price_label} if price_label is not None else set()
    adjustment_label = _ADJUSTMENT_LABEL_BY_KIND.get(bundle.kind)
    if adjustment_label is not None and _adjustment_required(bundle):
        labels.add(adjustment_label)
    return frozenset(labels)


def _calculation_tool_issues(result: BaseModel) -> list[ToolIssue]:
    """把“计算成功但部分指标不可得”提升到 Tool 外层，避免误报 complete。"""

    payload = result.model_dump(mode="json")
    unavailable_count = _count_incomplete_metrics(payload)
    metadata_messages = _collect_calculation_metadata_issues(payload)
    if unavailable_count == 0 and not metadata_messages:
        return []

    details = []
    if unavailable_count:
        details.append(f"{unavailable_count} 个请求指标因历史不足或输入缺失不可得")
    if metadata_messages:
        details.append("；".join(metadata_messages[:3]))
    return [
        ToolIssue(
            dataset_label="calculation",
            code=ToolIssueCode.CALCULATION_INCOMPLETE,
            message="；".join(details),
            retryable=False,
            suggested_action="扩大行情时间窗口，或补齐计算器指出的可选源数据后重新获取 context",
        )
    ]


def _count_incomplete_metrics(value: Any) -> int:
    if isinstance(value, dict):
        own_status = value.get("status") in {"insufficient_history", "missing_input"}
        unavailable_direction = value.get("direction") == "unavailable"
        return int(own_status or unavailable_direction) + sum(
            _count_incomplete_metrics(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_count_incomplete_metrics(item) for item in value)
    return 0


def _collect_calculation_metadata_issues(payload: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for key in ("metadata", "target_metadata", "benchmark_metadata"):
        metadata = payload.get(key)
        if not isinstance(metadata, dict):
            continue
        for issue in metadata.get("issues", []):
            if isinstance(issue, dict) and issue.get("message"):
                messages.append(str(issue["message"]))
    return messages


def _success_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    result: BaseModel,
    issues: list[ToolIssue],
    *,
    source_bundles: tuple[tuple[str, ResearchDataBundle], ...],
) -> dict[str, object]:
    return TechnicalCalculationToolResult(
        tool_name=tool_name,
        status=ToolResultStatus.PARTIAL if issues else ToolResultStatus.OK,
        as_of=context.as_of,
        source_context_refs=refs,
        source_subjects=tuple(
            _calculation_subject(context_ref, bundle) for context_ref, bundle in source_bundles
        ),
        calculation=result.model_dump(mode="json"),
        issues=issues,
        complete=not issues,
    ).model_dump(mode="json")


def _calculation_subject(
    context_ref: str,
    bundle: ResearchDataBundle,
) -> TechnicalCalculationSubject:
    ts_code = bundle.metadata.get("ts_code")
    frequency = bundle.metadata.get("frequency")
    if not isinstance(ts_code, str) or not isinstance(frequency, str):
        raise _CalculatorContractError("行情 context 缺少 ts_code 或 frequency 标的元数据")
    return TechnicalCalculationSubject(
        context_ref=context_ref,
        bundle_kind=bundle.kind,
        ts_code=ts_code,
        frequency=frequency,
    )


def _reference_error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
) -> dict[str, object]:
    return _error_result(
        tool_name,
        context,
        refs,
        ToolIssue(
            code=ToolIssueCode.INVALID_ARGUMENT,
            message="context_ref 在当前研究运行中不可用",
            retryable=False,
            suggested_action="使用当前运行中原始行情 Tool 最新返回的 context_ref",
        ),
    )


def _contract_error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    message: str,
) -> dict[str, object]:
    return _error_result(
        tool_name,
        context,
        refs,
        ToolIssue(
            code=ToolIssueCode.INVALID_ARGUMENT,
            message=message,
            retryable=False,
            suggested_action="改用该计算器支持的行情 context_ref，或修正目标/基准组合",
        ),
    )


def _source_data_error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    message: str,
) -> dict[str, object]:
    return _error_result(
        tool_name,
        context,
        refs,
        ToolIssue(
            code=ToolIssueCode.DATA_INTEGRITY,
            message=message,
            retryable=True,
            suggested_action="重新调用原始行情 Tool；关键数据集恢复后再运行计算器",
        ),
    )


def _analytics_error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    exc: TechnicalAnalyticsError,
) -> dict[str, object]:
    return _error_result(
        tool_name,
        context,
        refs,
        ToolIssue(
            code=ToolIssueCode.DATA_INTEGRITY,
            message=f"{exc.code.value}: {exc.message}",
            retryable=False,
            suggested_action="检查源数据完整性、复权因子和日期覆盖后重新获取行情 context",
        ),
    )


def _internal_error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    exc: Exception,
) -> dict[str, object]:
    correlation_id = f"tool_{uuid4().hex}"
    logger.error(
        "技术计算器内部错误 correlation_id=%s",
        correlation_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return _error_result(
        tool_name,
        context,
        refs,
        ToolIssue(
            code=ToolIssueCode.INTERNAL_ERROR,
            message="技术计算器执行发生内部错误",
            retryable=False,
            suggested_action="停止重复调用并把 correlation_id 交给程序维护者",
            correlation_id=correlation_id,
        ),
    )


def _error_result(
    tool_name: str,
    context: ResearchToolContext,
    refs: tuple[str, ...],
    issue: ToolIssue,
) -> dict[str, object]:
    return TechnicalCalculationToolResult(
        tool_name=tool_name,
        status=ToolResultStatus.ERROR,
        as_of=context.as_of,
        source_context_refs=refs,
        calculation=None,
        issues=[issue],
        complete=False,
    ).model_dump(mode="json")
