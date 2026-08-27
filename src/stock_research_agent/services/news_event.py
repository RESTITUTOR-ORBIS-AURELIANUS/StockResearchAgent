"""新闻正文、卖方研究摘要与事件文本查询。"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    normalize_as_of,
    validate_date_range,
    validate_month,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import NEWS_EVENT_DATA_SPECS
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.models import ServiceDataset

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_NEWS_WINDOW = timedelta(days=1)
_MAX_MARKET_REPORT_WINDOW = timedelta(days=31)
_MAX_TARGET_REPORT_WINDOW = timedelta(days=366)


class NewsEventDataService(BaseDataService):
    """按冻结时间读取新闻与卖方观点，避免 Agent 使用未来数据。"""

    API_SPECS = NEWS_EVENT_DATA_SPECS

    async def get_market_news(
        self,
        start_at: datetime,
        end_at: datetime,
        *,
        source: str | None = None,
        as_of: datetime | None = None,
    ) -> ServiceDataset:
        """读取最长 24 小时的新闻窗口，可选限制新闻来源。

        ``major_news`` 的备用兼容端不接受 ``limit/offset``，因此完整性边界
        由短时间窗口和 Service 的 ``max_rows`` 共同保证。所有时间都必须带
        时区，并在请求前统一换算成上海时间。
        """

        normalized_start = _normalize_news_time(start_at, "start_at")
        normalized_end = _normalize_news_time(end_at, "end_at")
        if normalized_start > normalized_end:
            raise ServiceInputError("start_at 不能晚于 end_at")
        if normalized_end - normalized_start > _MAX_NEWS_WINDOW:
            raise ServiceInputError("单次新闻查询窗口不能超过 24 小时")

        normalized_as_of = (
            _normalize_news_time(as_of, "as_of") if as_of is not None else normalized_end
        )
        if normalized_end > normalized_as_of:
            raise ServiceInputError("新闻窗口的 end_at 不能晚于 as_of")

        params: dict[str, str] = {
            "start_date": _format_news_time(normalized_start),
            "end_date": _format_news_time(normalized_end),
        }
        if source is not None:
            normalized_source = source.strip()
            if not normalized_source:
                raise ServiceInputError("source 不能为空字符串")
            params["src"] = normalized_source

        def within_frozen_window(row: dict[str, Any]) -> bool:
            published_at = _parse_provider_news_time(row.get("pub_time"))
            if published_at is None:
                raise ServiceDataValidationError("major_news 返回了无法解析的 pub_time")
            return normalized_start <= published_at <= normalized_end

        return await self._query(
            "major_news",
            params,
            as_of=normalized_as_of,
            row_filter=within_frozen_window,
        )

    async def get_sell_side_reports(
        self,
        start_date: date,
        end_date: date,
        *,
        ts_code: str | None = None,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """读取卖方研报的结构化摘要、评级与盈利预测。

        ``report_rc`` 不包含研报全文。返回的预测、目标价和评级是
        “某机构在某日发表了该观点”的事实，不是公司未来经营结果的
        ground truth。全市场查询限制在 31 天，指定个股可回看 366 天，
        以降低不支持 offset 的兼容端静默截断风险。
        """

        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        max_window = _MAX_TARGET_REPORT_WINDOW if ts_code is not None else _MAX_MARKET_REPORT_WINDOW
        if end_date - start_date > max_window:
            scope = "指定个股" if ts_code is not None else "全市场"
            raise ServiceInputError(f"{scope}卖方研报查询窗口不能超过 {max_window.days} 天")

        normalized_code = validate_security_code(ts_code) if ts_code is not None else None
        params: dict[str, str] = {"start_date": start, "end_date": end}
        if ts_code is not None:
            params["ts_code"] = normalized_code

        def within_requested_scope(row: dict[str, Any]) -> bool:
            row_date = _parse_provider_date(row.get("report_date"))
            if row_date is None:
                raise ServiceDataValidationError("report_rc 返回了无法解析的 report_date")
            if not start_date <= row_date <= end_date:
                return False
            return normalized_code is None or str(row.get("ts_code", "")).upper() == normalized_code

        return await self._query(
            "report_rc",
            params,
            as_of=as_of,
            row_filter=within_requested_scope,
        )

    async def get_daily_sell_side_reports(
        self,
        report_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """按单一报告日读取全市场卖方研报摘要。

        兼容端对全市场 ``start_date/end_date`` 区间查询偶发失败，
        每日快照因此固定使用精确 ``report_date``。
        """

        validate_observation_end(report_date, as_of)

        def exact_report_date(row: dict[str, Any]) -> bool:
            row_date = _parse_provider_date(row.get("report_date"))
            if row_date is None:
                raise ServiceDataValidationError("report_rc 返回了无法解析的 report_date")
            return row_date == report_date

        return await self._query(
            "report_rc",
            {"report_date": validate_date_range(report_date, report_date)[0]},
            as_of=as_of,
            row_filter=exact_report_date,
        )

    async def get_broker_recommendations(
        self,
        month: str,
        *,
        ts_code: str | None = None,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """读取某月券商金股名单，可在 Service 本地精确筛选个股。

        月度金股表示“该券商将该股列入当月名单”，不表示我们
        已经验证了其收益预测。
        """

        normalized_month = validate_month(month)
        normalized_as_of = normalize_as_of(as_of)
        if normalized_as_of is not None and normalized_month > normalized_as_of.strftime("%Y%m"):
            raise ServiceInputError("券商金股 month 不能晚于 as_of 所在月")
        normalized_code = validate_security_code(ts_code) if ts_code is not None else None

        def matches_requested_scope(row: dict[str, Any]) -> bool:
            if str(row.get("month") or "").strip() != normalized_month:
                return False
            return normalized_code is None or str(row.get("ts_code", "")).upper() == normalized_code

        return await self._query(
            "broker_recommend",
            {"month": normalized_month},
            as_of=as_of,
            row_filter=matches_requested_scope,
        )


def _normalize_news_time(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ServiceInputError(f"{field_name} 必须是带时区的 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ServiceInputError(f"{field_name} 必须带时区")
    return value.astimezone(_SHANGHAI).replace(microsecond=0)


def _format_news_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_provider_news_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=_SHANGHAI)
        return value.astimezone(_SHANGHAI)
    if value is None:
        return None

    text = str(value).strip()
    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=_SHANGHAI)
        except ValueError:
            pass
    return None


def _parse_provider_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None
