"""AKShare 新闻与公告接口的受控异步适配器。"""

import asyncio
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    ProviderSchemaError,
    ProviderTransportError,
    UnknownProviderApiError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderResult, ProviderSource

_SHANGHAI = ZoneInfo("Asia/Shanghai")
type AkshareFunction = Callable[..., Any]
type RowNormalizer = Callable[[Mapping[str, Any], ProviderQuery], dict[str, Any] | None]


class _AkshareSchemaError(ValueError):
    """线程内发现 AKShare DataFrame 字段漂移。"""


@dataclass(frozen=True, slots=True)
class _AkshareApiSpec:
    function_name: str
    provider: ProviderSource
    required_params: frozenset[str]
    optional_params: frozenset[str]
    required_columns: frozenset[str]
    output_fields: tuple[str, ...]
    normalize_row: RowNormalizer


_NEWS_FIELDS = (
    "record_key",
    "title",
    "content",
    "published_at",
    "source_name",
    "source_url",
    "source_kind",
    "citable",
)
_STOCK_NEWS_FIELDS = (*_NEWS_FIELDS, "ts_code", "keywords")
_NOTICE_FIELDS = (
    "record_key",
    "security_code",
    "ts_code",
    "stock_name",
    "title",
    "announcement_type",
    "announcement_date",
    "source_url",
    "source_kind",
    "citable",
)


AKSHARE_NEWS_API_SPECS: Mapping[str, _AkshareApiSpec] = MappingProxyType(
    {
        "stock_info_global_em": _AkshareApiSpec(
            function_name="stock_info_global_em",
            provider=ProviderSource.AKSHARE_EASTMONEY,
            required_params=frozenset(),
            optional_params=frozenset(),
            required_columns=frozenset({"标题", "摘要", "发布时间", "链接"}),
            output_fields=_NEWS_FIELDS,
            normalize_row=lambda row, request: _normalize_market_news(
                row,
                title_column="标题",
                content_column="摘要",
                published_column="发布时间",
                url_column="链接",
                source_name="东方财富",
            ),
        ),
        "stock_info_global_ths": _AkshareApiSpec(
            function_name="stock_info_global_ths",
            provider=ProviderSource.AKSHARE_THS,
            required_params=frozenset(),
            optional_params=frozenset(),
            required_columns=frozenset({"标题", "内容", "发布时间", "链接"}),
            output_fields=_NEWS_FIELDS,
            normalize_row=lambda row, request: _normalize_market_news(
                row,
                title_column="标题",
                content_column="内容",
                published_column="发布时间",
                url_column="链接",
                source_name="同花顺",
            ),
        ),
        "stock_info_global_cls": _AkshareApiSpec(
            function_name="stock_info_global_cls",
            provider=ProviderSource.AKSHARE_CLS,
            required_params=frozenset(),
            optional_params=frozenset({"symbol"}),
            required_columns=frozenset({"标题", "内容", "发布日期", "发布时间"}),
            output_fields=_NEWS_FIELDS,
            normalize_row=lambda row, request: _normalize_cls_news(row),
        ),
        "stock_news_em": _AkshareApiSpec(
            function_name="stock_news_em",
            provider=ProviderSource.AKSHARE_EASTMONEY,
            required_params=frozenset({"symbol"}),
            optional_params=frozenset(),
            required_columns=frozenset(
                {"关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"}
            ),
            output_fields=_STOCK_NEWS_FIELDS,
            normalize_row=lambda row, request: _normalize_stock_news(row, request),
        ),
        "stock_notice_report": _AkshareApiSpec(
            function_name="stock_notice_report",
            provider=ProviderSource.AKSHARE_EASTMONEY,
            required_params=frozenset({"symbol", "date"}),
            optional_params=frozenset(),
            required_columns=frozenset(
                {"代码", "名称", "公告标题", "公告类型", "公告日期", "网址"}
            ),
            output_fields=_NOTICE_FIELDS,
            normalize_row=lambda row, request: _normalize_notice(row),
        ),
        "stock_individual_notice_report": _AkshareApiSpec(
            function_name="stock_individual_notice_report",
            provider=ProviderSource.AKSHARE_EASTMONEY,
            required_params=frozenset({"security", "symbol", "begin_date", "end_date"}),
            optional_params=frozenset(),
            required_columns=frozenset(
                {"代码", "名称", "公告标题", "公告类型", "公告日期", "网址"}
            ),
            output_fields=_NOTICE_FIELDS,
            normalize_row=lambda row, request: _normalize_notice(row),
        ),
    }
)


class AkshareNewsProvider:
    """把同步 AKShare 函数转换成项目统一的异步 Provider 协议。

    超时无法强制终止 Python 线程，因此执行槽只会在真实工作完成时释放；这能避免
    连续超时后无限创建后台线程。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_workers: int = 2,
        functions: Mapping[str, AkshareFunction] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        self._timeout_seconds = timeout_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="akshare-news",
        )
        self._slots = asyncio.Semaphore(max_workers)
        self._functions = dict(functions) if functions is not None else _load_functions()
        self._closed = False

    async def query(self, request: ProviderQuery) -> ProviderResult:
        spec = AKSHARE_NEWS_API_SPECS.get(request.api_name)
        if spec is None:
            raise UnknownProviderApiError(
                ProviderErrorCode.UNKNOWN_API,
                request.api_name,
                "AKShare 新闻支持清单中没有这个接口",
            )
        self._validate_request(request, spec)
        if self._closed:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "AKShare Provider 已关闭",
                provider=spec.provider,
            )

        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self._timeout_seconds)
        except TimeoutError as exc:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                "等待 AKShare 执行槽超时",
                provider=spec.provider,
            ) from exc

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, self._invoke_sync, request, spec)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(self._release_slot)

        try:
            items = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                f"AKShare 调用超过 {self._timeout_seconds:g} 秒",
                provider=spec.provider,
            ) from exc
        except asyncio.CancelledError:
            raise
        except _AkshareSchemaError as exc:
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                str(exc),
                provider=spec.provider,
            ) from exc
        except Exception as exc:
            raise ProviderTransportError(
                ProviderErrorCode.TRANSPORT_ERROR,
                request.api_name,
                type(exc).__name__,
                provider=spec.provider,
            ) from exc

        fetched_at = datetime.now(_SHANGHAI).replace(microsecond=0)
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), default=str)
        return ProviderResult(
            api_name=request.api_name,
            provider=spec.provider,
            fetched_at=fetched_at,
            data_as_of=_latest_data_date(items),
            fields=spec.output_fields,
            items=items,
            provider_code=0,
            has_more=False,
            response_bytes=len(payload.encode("utf-8")),
        )

    async def aclose(self) -> None:
        """拒绝新调用并取消尚未开始的排队任务。"""

        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _release_slot(self, completed: asyncio.Future[list[dict[str, Any]]]) -> None:
        """真实线程结束后释放槽，并消费超时后无人等待的异常。"""

        self._slots.release()
        if completed.cancelled():
            return
        # 正常 await 仍可再次取得同一异常；这里避免超时返回后出现未取异常警告。
        completed.exception()

    def _validate_request(self, request: ProviderQuery, spec: _AkshareApiSpec) -> None:
        parameter_names = frozenset(request.params)
        missing = spec.required_params.difference(parameter_names)
        unexpected = parameter_names.difference(spec.required_params | spec.optional_params)
        if missing or unexpected:
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                f"AKShare 参数不匹配：missing={sorted(missing)}, unexpected={sorted(unexpected)}",
                provider=spec.provider,
            )
        unknown_fields = set(request.fields).difference(spec.output_fields)
        if unknown_fields:
            raise ProviderSchemaError(
                ProviderErrorCode.SCHEMA_ERROR,
                request.api_name,
                f"请求了未标准化字段：{sorted(unknown_fields)}",
                provider=spec.provider,
            )

    def _invoke_sync(
        self,
        request: ProviderQuery,
        spec: _AkshareApiSpec,
    ) -> list[dict[str, Any]]:
        try:
            function = self._functions[spec.function_name]
        except KeyError as exc:
            raise _AkshareSchemaError(f"没有装配 AKShare 函数 {spec.function_name}") from exc
        frame = function(**dict(request.params))
        if not hasattr(frame, "columns") or not callable(getattr(frame, "to_dict", None)):
            raise _AkshareSchemaError("AKShare 返回值不是 pandas.DataFrame")
        columns = {str(column) for column in frame.columns}
        missing_columns = spec.required_columns.difference(columns)
        if missing_columns:
            raise _AkshareSchemaError(f"AKShare 响应缺少字段：{sorted(missing_columns)}")
        try:
            raw_rows = frame.to_dict(orient="records")
            normalized_rows: list[dict[str, Any]] = []
            for row in raw_rows:
                normalized = spec.normalize_row(row, request)
                if normalized is not None:
                    normalized_rows.append(normalized)
            return normalized_rows
        except _AkshareSchemaError:
            raise
        except Exception as exc:
            raise _AkshareSchemaError(f"AKShare 字段标准化失败：{type(exc).__name__}") from exc


def _load_functions() -> dict[str, AkshareFunction]:
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 只在部署依赖缺失时触发
        raise RuntimeError("缺少 akshare 依赖，请先执行 uv sync") from exc
    return {name: getattr(ak, spec.function_name) for name, spec in AKSHARE_NEWS_API_SPECS.items()}


def _normalize_market_news(
    row: Mapping[str, Any],
    *,
    title_column: str,
    content_column: str,
    published_column: str,
    url_column: str,
    source_name: str,
) -> dict[str, Any]:
    title = _required_text(row.get(title_column), title_column)
    published_at = _datetime_text(row.get(published_column), published_column)
    source_url = _optional_text(row.get(url_column))
    result = {
        "title": title,
        "content": _optional_text(row.get(content_column)),
        "published_at": published_at,
        "source_name": source_name,
        "source_url": source_url,
        "source_kind": "market_news",
        "citable": bool(source_url),
    }
    return {"record_key": _record_key(result), **result}


def _normalize_cls_news(row: Mapping[str, Any]) -> dict[str, Any] | None:
    # 财联社流偶尔夹带全空占位行；它既不可展示也不可引用，安全跳过。
    title = _optional_text(row.get("标题"))
    if title is None:
        return None
    published_at = _combine_date_time(row.get("发布日期"), row.get("发布时间"))
    result = {
        "title": title,
        "content": _optional_text(row.get("内容")),
        "published_at": published_at,
        "source_name": "财联社",
        "source_url": None,
        "source_kind": "market_news",
        "citable": False,
    }
    return {"record_key": _record_key(result), **result}


def _normalize_stock_news(
    row: Mapping[str, Any],
    request: ProviderQuery,
) -> dict[str, Any]:
    ts_code = _to_ts_code(request.params["symbol"])
    source_url = _optional_text(row.get("新闻链接"))
    result = {
        "title": _required_text(row.get("新闻标题"), "新闻标题"),
        "content": _optional_text(row.get("新闻内容")),
        "published_at": _datetime_text(row.get("发布时间"), "发布时间"),
        "source_name": _optional_text(row.get("文章来源")) or "东方财富",
        "source_url": source_url,
        "source_kind": "stock_news",
        "citable": bool(source_url),
        "ts_code": ts_code,
        "keywords": _optional_text(row.get("关键词")),
    }
    return {"record_key": _record_key(result), **result}


def _normalize_notice(row: Mapping[str, Any]) -> dict[str, Any]:
    source_url = _optional_text(row.get("网址"))
    security_code = _required_text(row.get("代码"), "代码").upper()
    result = {
        "security_code": security_code,
        "ts_code": _try_to_ts_code(security_code),
        "stock_name": _optional_text(row.get("名称")),
        "title": _required_text(row.get("公告标题"), "公告标题"),
        "announcement_type": _optional_text(row.get("公告类型")) or "未分类",
        "announcement_date": _date_text(row.get("公告日期"), "公告日期"),
        "source_url": source_url,
        "source_kind": "announcement",
        "citable": bool(source_url),
    }
    return {"record_key": _record_key(result), **result}


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        import pandas as pd

        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
        if type(missing).__name__ == "bool_" and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _optional_text(value: Any) -> str | None:
    cleaned = _clean_scalar(value)
    if cleaned is None:
        return None
    text = str(cleaned).strip()
    return text or None


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise _AkshareSchemaError(f"AKShare 行字段 {field_name} 为空")
    return text


def _datetime_text(value: Any, field_name: str) -> str:
    cleaned = _clean_scalar(value)
    if isinstance(cleaned, datetime):
        normalized = (
            cleaned.replace(tzinfo=_SHANGHAI)
            if cleaned.tzinfo is None or cleaned.utcoffset() is None
            else cleaned.astimezone(_SHANGHAI)
        )
        return normalized.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(cleaned, date):
        return datetime.combine(cleaned, time.min).strftime("%Y-%m-%d %H:%M:%S")
    text = _required_text(cleaned, field_name)
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    raise _AkshareSchemaError(f"AKShare 行字段 {field_name} 不是可识别时间：{text[:40]}")


def _date_text(value: Any, field_name: str) -> str:
    cleaned = _clean_scalar(value)
    if isinstance(cleaned, datetime):
        return cleaned.date().isoformat()
    if isinstance(cleaned, date):
        return cleaned.isoformat()
    text = _required_text(cleaned, field_name)
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    raise _AkshareSchemaError(f"AKShare 行字段 {field_name} 不是可识别日期：{text[:40]}")


def _combine_date_time(date_value: Any, time_value: Any) -> str:
    cleaned_date = _clean_scalar(date_value)
    cleaned_time = _clean_scalar(time_value)
    if isinstance(cleaned_date, datetime):
        base_date = cleaned_date.date()
    elif isinstance(cleaned_date, date):
        base_date = cleaned_date
    else:
        date_text = _required_text(cleaned_date, "发布日期")
        base_date = None
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                base_date = datetime.strptime(date_text, pattern).date()
                break
            except ValueError:
                pass
        if base_date is None:
            raise _AkshareSchemaError("财联社发布日期无法解析")

    if isinstance(cleaned_time, datetime):
        base_time = cleaned_time.time()
    elif isinstance(cleaned_time, time):
        base_time = cleaned_time
    else:
        time_text = _required_text(cleaned_time, "发布时间")
        base_time = None
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                base_time = datetime.strptime(time_text, pattern).time()
                break
            except ValueError:
                pass
        if base_time is None:
            raise _AkshareSchemaError("财联社发布时间无法解析")
    return datetime.combine(base_date, base_time).strftime("%Y-%m-%d %H:%M:%S")


def _to_ts_code(value: Any) -> str:
    raw = _required_text(value, "证券代码").upper()
    if "." in raw:
        code, exchange = raw.split(".", maxsplit=1)
        if len(code) == 6 and exchange in {"SH", "SZ", "BJ"}:
            return f"{code}.{exchange}"
        raise _AkshareSchemaError(f"证券代码无法标准化：{raw}")
    code = raw.zfill(6)
    if len(code) != 6 or not code.isdigit():
        raise _AkshareSchemaError(f"证券代码无法标准化：{raw}")
    if code.startswith(("4", "8", "92")):
        exchange = "BJ"
    elif code.startswith(("5", "6", "9")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"{code}.{exchange}"


def _try_to_ts_code(value: Any) -> str | None:
    try:
        return _to_ts_code(value)
    except _AkshareSchemaError:
        return None


def _record_key(row: Mapping[str, Any]) -> str:
    url = _optional_text(row.get("source_url"))
    if url:
        material = f"url|{url}"
    else:
        material = "|".join(
            str(row.get(field) or "")
            for field in ("source_kind", "ts_code", "published_at", "announcement_date", "title")
        )
    return f"ak_{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _latest_data_date(items: list[dict[str, Any]]) -> date | None:
    dates: list[date] = []
    for item in items:
        for field in ("published_at", "announcement_date"):
            value = item.get(field)
            if not value:
                continue
            try:
                dates.append(datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
            except ValueError:
                pass
    return max(dates, default=None)
