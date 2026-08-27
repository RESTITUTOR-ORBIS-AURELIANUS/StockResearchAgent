"""面向新闻事件节点的 AKShare 新闻与公告 Service。"""

from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    matches_date_range,
    normalize_as_of,
    validate_date_range,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import ApiSpec
from stock_research_agent.services.errors import ServiceDataValidationError, ServiceInputError
from stock_research_agent.services.models import ServiceDataset

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_NEWS_WINDOW = timedelta(days=1)
_MAX_TARGETED_NEWS_WINDOW = timedelta(days=31)
_MAX_TARGETED_NOTICE_WINDOW = timedelta(days=366)


class MarketNewsSource(StrEnum):
    EASTMONEY = "EASTMONEY"
    THS = "THS"
    CLS = "CLS"


class AnnouncementCategory(StrEnum):
    ALL = "全部"
    MATERIAL_EVENT = "重大事项"
    FINANCIAL_REPORT = "财务报告"
    FINANCING = "融资公告"
    RISK_WARNING = "风险提示"
    RESTRUCTURING = "资产重组"
    INFORMATION_CHANGE = "信息变更"
    HOLDING_CHANGE = "持股变动"


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

PUBLIC_NEWS_EVENT_SPECS = MappingProxyType(
    {
        name: ApiSpec(
            api_name=name,
            purpose=purpose,
            fields=_NEWS_FIELDS,
            as_of_fields=("published_at",),
            identity_fields=("record_key",),
            historical_as_of_safe=False,
            supports_offset_pagination=False,
        )
        for name, purpose in {
            "stock_info_global_em": "东方财富全市场财经快讯",
            "stock_info_global_ths": "同花顺全市场财经快讯",
            "stock_info_global_cls": "财联社全市场财经快讯",
        }.items()
    }
    | {
        "stock_news_em": ApiSpec(
            api_name="stock_news_em",
            purpose="东方财富指定股票新闻",
            fields=_STOCK_NEWS_FIELDS,
            as_of_fields=("published_at",),
            identity_fields=("record_key",),
            historical_as_of_safe=False,
            supports_offset_pagination=False,
        ),
        "stock_notice_report": ApiSpec(
            api_name="stock_notice_report",
            purpose="东方财富每日全市场公告索引",
            fields=_NOTICE_FIELDS,
            as_of_fields=("announcement_date",),
            identity_fields=("record_key",),
            supports_offset_pagination=False,
        ),
        "stock_individual_notice_report": ApiSpec(
            api_name="stock_individual_notice_report",
            purpose="东方财富指定股票公告索引",
            fields=_NOTICE_FIELDS,
            as_of_fields=("announcement_date",),
            identity_fields=("record_key",),
            supports_offset_pagination=False,
        ),
    }
)

_SOURCE_APIS = {
    MarketNewsSource.EASTMONEY: "stock_info_global_em",
    MarketNewsSource.THS: "stock_info_global_ths",
    MarketNewsSource.CLS: "stock_info_global_cls",
}


class PublicNewsEventService(BaseDataService):
    """把 AKShare 最近快讯和可回放公告转换成冻结的 ServiceDataset。"""

    API_SPECS = PUBLIC_NEWS_EVENT_SPECS

    async def get_market_news(
        self,
        source: MarketNewsSource,
        start_at: datetime,
        end_at: datetime,
        *,
        as_of: datetime,
    ) -> ServiceDataset:
        start, end, cutoff = _validate_news_window(start_at, end_at, as_of)
        try:
            normalized_source = MarketNewsSource(source)
        except ValueError as exc:
            raise ServiceInputError(f"未知市场新闻来源：{source}") from exc
        api_name = _SOURCE_APIS[normalized_source]
        params = {"symbol": "全部"} if normalized_source is MarketNewsSource.CLS else {}

        def in_window(row: dict[str, Any]) -> bool:
            published_at = _parse_news_time(row.get("published_at"))
            if published_at is None:
                raise ServiceDataValidationError(f"{api_name} 返回了无法解析的 published_at")
            return start <= published_at <= end

        dataset = await self._query(
            api_name,
            params,
            as_of=cutoff,
            row_filter=in_window,
        )
        # 三个公开快讯接口只返回“最近 N 条”，不能把成功请求误报成完整历史窗口。
        return dataset.model_copy(update={"complete": False})

    async def get_stock_news(
        self,
        ts_code: str,
        start_at: datetime,
        end_at: datetime,
        *,
        as_of: datetime,
    ) -> ServiceDataset:
        normalized_code = validate_security_code(ts_code)
        start, end, cutoff = _validate_news_window(
            start_at,
            end_at,
            as_of,
            max_window=_MAX_TARGETED_NEWS_WINDOW,
            window_label="31 天",
        )

        def in_window(row: dict[str, Any]) -> bool:
            published_at = _parse_news_time(row.get("published_at"))
            if published_at is None:
                raise ServiceDataValidationError("stock_news_em 返回了无法解析的 published_at")
            return row.get("ts_code") == normalized_code and start <= published_at <= end

        dataset = await self._query(
            "stock_news_em",
            {"symbol": _akshare_security_code(normalized_code)},
            as_of=cutoff,
            row_filter=in_window,
        )
        return dataset.model_copy(update={"complete": False})

    async def get_daily_announcements(
        self,
        announcement_date: date,
        *,
        category: AnnouncementCategory = AnnouncementCategory.ALL,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        normalized_as_of = normalize_as_of(as_of)
        if normalized_as_of is None:
            raise ServiceInputError("公告查询必须提供 as_of")
        if announcement_date > normalized_as_of:
            raise ServiceInputError("公告日期不能晚于 as_of")
        normalized_category = _announcement_category(category)
        date_filter = matches_date_range(
            "announcement_date",
            announcement_date,
            announcement_date,
        )
        return await self._query(
            "stock_notice_report",
            {
                "symbol": normalized_category.value,
                "date": format_date(announcement_date),
            },
            as_of=as_of,
            row_filter=lambda row: bool(row.get("ts_code")) and date_filter(row),
        )

    async def get_stock_announcements(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        category: AnnouncementCategory = AnnouncementCategory.ALL,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        normalized_code = validate_security_code(ts_code)
        start, end = validate_date_range(start_date, end_date)
        validate_observation_end(end_date, as_of)
        if end_date - start_date > _MAX_TARGETED_NOTICE_WINDOW:
            raise ServiceInputError("单次个股公告查询不能超过 366 天")
        normalized_category = _announcement_category(category)
        date_filter = matches_date_range("announcement_date", start_date, end_date)
        return await self._query(
            "stock_individual_notice_report",
            {
                "security": _akshare_security_code(normalized_code),
                "symbol": normalized_category.value,
                "begin_date": start,
                "end_date": end,
            },
            as_of=as_of,
            row_filter=lambda row: row.get("ts_code") == normalized_code and date_filter(row),
        )


def _validate_news_window(
    start_at: datetime,
    end_at: datetime,
    as_of: datetime,
    *,
    max_window: timedelta = _MAX_NEWS_WINDOW,
    window_label: str = "24 小时",
) -> tuple[datetime, datetime, datetime]:
    start = _normalize_time(start_at, "start_at")
    end = _normalize_time(end_at, "end_at")
    cutoff = _normalize_time(as_of, "as_of")
    if start > end:
        raise ServiceInputError("start_at 不能晚于 end_at")
    if end - start > max_window:
        raise ServiceInputError(f"单次新闻查询窗口不能超过 {window_label}")
    if end > cutoff:
        raise ServiceInputError("新闻窗口的 end_at 不能晚于 as_of")
    return start, end, cutoff


def _normalize_time(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServiceInputError(f"{field_name} 必须是带时区的 datetime")
    return value.astimezone(_SHANGHAI).replace(microsecond=0)


def _parse_news_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=_SHANGHAI)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(_SHANGHAI)
        )
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=_SHANGHAI)
        except ValueError:
            pass
    return None


def _announcement_category(value: AnnouncementCategory) -> AnnouncementCategory:
    try:
        return AnnouncementCategory(value)
    except ValueError as exc:
        raise ServiceInputError(f"未知公告分类：{value}") from exc


def _akshare_security_code(ts_code: str) -> str:
    return ts_code.split(".", maxsplit=1)[0]
