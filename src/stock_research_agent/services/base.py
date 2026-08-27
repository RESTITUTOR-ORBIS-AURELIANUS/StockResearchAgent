"""八个源数据 Service 共享的校验、分页和时间截止逻辑。"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.providers.models import ProviderParam, ProviderQuery, ProviderSource
from stock_research_agent.providers.protocol import MarketDataProvider
from stock_research_agent.services.catalog import ApiSpec
from stock_research_agent.services.errors import (
    ServiceApiOwnershipError,
    ServiceDataValidationError,
    ServiceInputError,
    ServicePaginationError,
)
from stock_research_agent.services.models import (
    ServiceDataset,
    ServiceItemTrace,
    ServicePageTrace,
)

RowFilter = Callable[[dict[str, Any]], bool]
AsOfValue = date | datetime | None

_SECURITY_CODE = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?$")
_REPORT_PERIOD = re.compile(r"^\d{8}$")
_MONTH = re.compile(r"^\d{6}$")
_QUARTER = re.compile(r"^\d{4}Q[1-4]$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class _SourcedRow:
    data: dict[str, Any]
    trace: ServiceItemTrace


class BaseDataService:
    """把一次业务查询安全地转换成一个完整的 ``ServiceDataset``。"""

    API_SPECS: Mapping[str, ApiSpec] = {}

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        page_size: int = 1_000,
        max_pages: int = 50,
        max_rows: int = 50_000,
    ) -> None:
        if page_size < 1:
            raise ServiceInputError("page_size 必须大于 0")
        if max_pages < 1:
            raise ServiceInputError("max_pages 必须大于 0")
        if max_rows < page_size:
            raise ServiceInputError("max_rows 不能小于 page_size")
        self._provider = provider
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_rows = max_rows

    @property
    def supported_apis(self) -> frozenset[str]:
        return frozenset(self.API_SPECS)

    async def _query(
        self,
        api_name: str,
        params: Mapping[str, ProviderParam],
        *,
        as_of: AsOfValue = None,
        row_filter: RowFilter | None = None,
        paginate: bool | None = None,
    ) -> ServiceDataset:
        spec = self._owned_spec(api_name)
        use_offset_pagination = spec.supports_offset_pagination if paginate is None else paginate
        if use_offset_pagination and not spec.supports_offset_pagination:
            raise ServiceInputError(f"{api_name} 的兼容契约不允许 offset 分页")
        normalized_as_of = normalize_as_of(as_of)
        query_params = dict(params)
        if "limit" in query_params or "offset" in query_params:
            raise ServiceInputError("limit/offset 由 Service 分页器统一管理")

        pages: list[ServicePageTrace] = []
        received_rows: list[_SourcedRow] = []
        seen_page_fingerprints: set[str] = set()
        pagination_source: tuple[ProviderSource, bool] | None = None
        returned_schema: frozenset[str] | None = None
        offset = 0

        for page_number in range(1, self._max_pages + 1):
            request_params = dict(query_params)
            if use_offset_pagination:
                request_params.update(limit=self._page_size, offset=offset)
            result = await self._provider.query(
                ProviderQuery(
                    api_name=spec.api_name,
                    params=request_params,
                    fields=spec.fields,
                )
            )
            if result.api_name != spec.api_name:
                raise ServiceDataValidationError(
                    f"请求 {spec.api_name}，Provider 却返回 {result.api_name}"
                )
            current_source = (result.provider, result.from_cache)
            if pagination_source is None:
                pagination_source = current_source
            elif current_source != pagination_source:
                raise ServicePaginationError(
                    f"{api_name} 分页期间数据源从 {pagination_source} 切换到 "
                    f"{current_source}；拒绝拼接不同快照"
                )
            if normalized_as_of is not None and not spec.historical_as_of_safe:
                fetched_date = result.fetched_at.astimezone(_SHANGHAI).date()
                if normalized_as_of < fetched_date:
                    raise ServiceDataValidationError(
                        f"{api_name} 没有可靠发布日期或历史成分快照，"
                        f"不能在 {fetched_date} 抓取后回放 as_of={normalized_as_of}"
                    )
            if result.items and spec.fields:
                missing_fields = sorted(set(spec.fields).difference(result.fields))
                if missing_fields:
                    raise ServiceDataValidationError(
                        f"{api_name} 的响应缺少 Service 必需字段：{missing_fields}"
                    )
                for row_index, item in enumerate(result.items):
                    missing_row_fields = sorted(set(spec.fields).difference(item))
                    if missing_row_fields:
                        raise ServiceDataValidationError(
                            f"{api_name} 第 {row_index} 行缺少字段：{missing_row_fields}"
                        )
            if result.items:
                current_schema = frozenset(result.fields)
                if returned_schema is None:
                    returned_schema = current_schema
                elif current_schema != returned_schema:
                    raise ServiceDataValidationError(f"{api_name} 分页期间返回 schema 发生变化")

            page_index = len(pages)

            pages.append(
                ServicePageTrace(
                    page_index=page_index,
                    provider=result.provider,
                    from_cache=result.from_cache,
                    fetched_at=result.fetched_at,
                    offset=offset,
                    item_count=len(result.items),
                    returned_fields=result.fields,
                    response_bytes=result.response_bytes,
                )
            )
            received_rows.extend(
                _SourcedRow(
                    data=dict(item),
                    trace=ServiceItemTrace(
                        page_index=page_index,
                        source_offset=offset + row_index,
                        provider=result.provider,
                        from_cache=result.from_cache,
                        fetched_at=result.fetched_at,
                    ),
                )
                for row_index, item in enumerate(result.items)
            )

            if result.items:
                page_fingerprint = _page_fingerprint(result.items)
                if page_fingerprint in seen_page_fingerprints:
                    raise ServicePaginationError(
                        f"{api_name} 在 offset={offset} 返回了重复页面，上游可能忽略 offset"
                    )
                seen_page_fingerprints.add(page_fingerprint)

            if len(received_rows) > self._max_rows:
                raise ServicePaginationError(
                    f"{api_name} 超过 max_rows={self._max_rows}，拒绝静默截断"
                )
            if not use_offset_pagination:
                if result.has_more:
                    raise ServicePaginationError(
                        f"{api_name} 不接受 limit/offset，但上游声明仍有下一页，无法保证结果完整"
                    )
                break
            if not result.has_more:
                break
            if len(received_rows) >= self._max_rows:
                raise ServicePaginationError(
                    f"{api_name} 仍有下一页但已达到 max_rows={self._max_rows}"
                )
            if not result.items:
                raise ServicePaginationError(
                    f"{api_name} 返回 has_more=true 但当前页为空，无法安全推进 offset"
                )
            if page_number == self._max_pages:
                raise ServicePaginationError(
                    f"{api_name} 超过 max_pages={self._max_pages}，拒绝静默截断"
                )
            offset += len(result.items)

        filtered_rows = received_rows
        if row_filter is not None:
            filtered_rows = [row for row in filtered_rows if row_filter(row.data)]

        filtered_rows, row_as_of_dates = _apply_as_of(
            filtered_rows,
            normalized_as_of,
            spec.as_of_fields,
            api_name,
        )
        filtered_rows = _deduplicate(filtered_rows, spec.identity_fields)

        return ServiceDataset(
            api_name=spec.api_name,
            query_params=query_params,
            requested_fields=spec.fields,
            items=[row.data for row in filtered_rows],
            item_traces=tuple(row.trace for row in filtered_rows),
            pages=tuple(pages),
            as_of=normalized_as_of,
            data_as_of=max(row_as_of_dates, default=None),
            received_item_count=len(received_rows),
            discarded_item_count=len(received_rows) - len(filtered_rows),
        )

    def _owned_spec(self, api_name: str) -> ApiSpec:
        try:
            return self.API_SPECS[api_name]
        except KeyError as exc:
            raise ServiceApiOwnershipError(f"{type(self).__name__} 不负责接口 {api_name}") from exc


def format_date(value: date) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ServiceInputError("datetime 日期参数必须带时区")
        value = value.astimezone(_SHANGHAI).date()
    if not isinstance(value, date):
        raise ServiceInputError("日期参数必须是 datetime.date")
    return value.strftime("%Y%m%d")


def validate_date_range(start_date: date, end_date: date) -> tuple[str, str]:
    start = format_date(start_date)
    end = format_date(end_date)
    if start > end:
        raise ServiceInputError("start_date 不能晚于 end_date")
    return start, end


def validate_observation_end(end_date: date, as_of: AsOfValue) -> None:
    normalized_as_of = normalize_as_of(as_of)
    normalized_end = datetime.strptime(format_date(end_date), "%Y%m%d").date()
    if normalized_as_of is not None and normalized_end > normalized_as_of:
        raise ServiceInputError("观测区间的 end_date 不能晚于 as_of")


def validate_security_code(ts_code: str) -> str:
    normalized = ts_code.strip().upper()
    if not _SECURITY_CODE.fullmatch(normalized):
        raise ServiceInputError(f"证券代码格式不合法：{ts_code!r}")
    return normalized


def validate_choice(value: str, allowed: set[str] | frozenset[str], field_name: str) -> str:
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise ServiceInputError(f"{field_name} 必须是 {sorted(allowed)} 之一")
    return normalized


def validate_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ServiceInputError(f"{field_name} 不能为空")
    return normalized


def validate_report_period(period: str) -> str:
    if not _REPORT_PERIOD.fullmatch(period):
        raise ServiceInputError("财报 period 必须使用 YYYYMMDD")
    try:
        datetime.strptime(period, "%Y%m%d")
    except ValueError as exc:
        raise ServiceInputError("财报 period 不是有效日期") from exc
    return period


def validate_month(month: str) -> str:
    if not _MONTH.fullmatch(month):
        raise ServiceInputError("月份必须使用 YYYYMM")
    try:
        datetime.strptime(month, "%Y%m")
    except ValueError as exc:
        raise ServiceInputError("月份不是有效年月") from exc
    return month


def validate_quarter(quarter: str) -> str:
    if not _QUARTER.fullmatch(quarter):
        raise ServiceInputError("季度必须使用 YYYYQ1 到 YYYYQ4")
    return quarter


def normalize_as_of(value: AsOfValue) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ServiceInputError("as_of 的 datetime 必须带时区")
        return value.astimezone(_SHANGHAI).date()
    if isinstance(value, date):
        return value
    raise ServiceInputError("as_of 必须是 date、带时区 datetime 或 None")


def matches_code(ts_code: str) -> RowFilter:
    normalized = validate_security_code(ts_code)
    return lambda row: str(row.get("ts_code", "")).upper() == normalized


def matches_date_range(
    field_name: str,
    start_date: date,
    end_date: date,
) -> RowFilter:
    """构造一个失败即关闭的本地日期过滤器。

    某些兼容接口不能在请求中表达起止日期，Service 仍要保证业务层声明的
    时间窗口真实生效。若上游行缺少或无法解析该日期字段，拒绝把它静默混入
    结果，而不是猜测它位于窗口内。
    """

    start_text, end_text = validate_date_range(start_date, end_date)
    normalized_start = datetime.strptime(start_text, "%Y%m%d").date()
    normalized_end = datetime.strptime(end_text, "%Y%m%d").date()

    def predicate(row: dict[str, Any]) -> bool:
        parsed = _parse_data_date(row.get(field_name))
        if parsed is None:
            raise ServiceDataValidationError(
                f"响应行缺少可验证的日期字段 {field_name!r}，无法执行时间窗口过滤"
            )
        return normalized_start <= parsed <= normalized_end

    return predicate


def _apply_as_of(
    rows: list[_SourcedRow],
    as_of: date | None,
    date_fields: tuple[str, ...],
    api_name: str,
) -> tuple[list[_SourcedRow], list[date]]:
    if not date_fields:
        return rows, []

    accepted: list[_SourcedRow] = []
    accepted_dates: list[date] = []
    for row in rows:
        dates = [
            parsed
            for field in date_fields
            if (parsed := _parse_data_date(row.data.get(field))) is not None
        ]
        if not dates:
            if as_of is not None:
                raise ServiceDataValidationError(
                    f"{api_name} 的响应行缺少可验证的截止日期字段 {date_fields}"
                )
            accepted.append(row)
            continue
        available_on = max(dates)
        if as_of is None or available_on <= as_of:
            accepted.append(row)
            accepted_dates.append(available_on)
    return accepted, accepted_dates


def _parse_data_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip().upper()
    for pattern in (
        "%Y%m%d",
        "%Y-%m-%d",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    if _MONTH.fullmatch(text):
        year = int(text[:4])
        month = int(text[4:])
        if not 1 <= month <= 12:
            return None
        next_month = date(year + (month == 12), month % 12 + 1, 1)
        return date.fromordinal(next_month.toordinal() - 1)
    if _QUARTER.fullmatch(text):
        year = int(text[:4])
        quarter = int(text[-1])
        end_month = quarter * 3
        next_month = date(year + (end_month == 12), end_month % 12 + 1, 1)
        return date.fromordinal(next_month.toordinal() - 1)
    return None


def _deduplicate(rows: list[_SourcedRow], identity_fields: tuple[str, ...]) -> list[_SourcedRow]:
    if not identity_fields:
        return rows
    seen: set[tuple[str, ...]] = set()
    result: list[_SourcedRow] = []
    for row in rows:
        raw_key = tuple(row.data.get(field) for field in identity_fields)
        if any(value is None or value == "" for value in raw_key):
            result.append(row)
            continue
        key = tuple(str(value) for value in raw_key)
        if key not in seen:
            result.append(row)
            seen.add(key)
    return result


def _page_fingerprint(items: list[dict[str, Any]]) -> str:
    serialized = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()
