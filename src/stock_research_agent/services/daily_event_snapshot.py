"""新闻事件研究员每日模式使用的确定性候选快照。"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.services.errors import ServiceInputError
from stock_research_agent.services.instrument_reference import InstrumentReferenceService
from stock_research_agent.services.models import ServiceDataset
from stock_research_agent.services.news_event import NewsEventDataService
from stock_research_agent.services.public_news_event import (
    AnnouncementCategory,
    MarketNewsSource,
    PublicNewsEventService,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

_ANNOUNCEMENT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("风险提示", ("风险提示", "退市", "立案", "处罚", "诉讼", "冻结", "违约")),
    ("资产重组", ("资产重组", "重大资产", "收购", "出售资产", "控制权变更")),
    ("业绩披露", ("业绩预告", "业绩快报", "年度报告", "半年度报告", "季度报告")),
    ("资本动作", ("回购", "增持", "减持", "解禁", "分红", "权益分派")),
    ("融资事项", ("定增", "增发", "可转债", "融资", "募资")),
)


class EventRelatedStock(DomainModel):
    """新闻文本中精确出现的当前上市公司名称及其代码。"""

    ts_code: str
    stock_name: str
    matched_name: str
    supporting_record_keys: tuple[str, ...] = ()


class EventNewsCandidate(DomainModel):
    """从最近快讯中确定性筛出的有限候选，不等同于最终 Evidence。"""

    title: str
    summary: str
    published_at: AwareDatetime
    source_names: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_dataset_labels: tuple[str, ...]
    record_keys: tuple[str, ...]
    citable: bool
    related_stocks: tuple[EventRelatedStock, ...] = ()


class EventAnnouncementCandidate(DomainModel):
    ts_code: str
    stock_name: str | None = None
    title: str
    announcement_type: str
    announcement_date: date
    source_url: str | None = None
    source_dataset_label: str
    record_key: str
    citable: bool
    selection_signals: tuple[str, ...]


class EventSellSideReportCandidate(DomainModel):
    """卖方研报结构化摘要。它是“机构发表过观点”的事实。"""

    ts_code: str
    stock_name: str | None = None
    report_date: date
    report_title: str
    report_type: str | None = None
    classify: str | None = None
    org_name: str
    author_name: str | None = None
    rating: str | None = None
    target_price_min: Any = None
    target_price_max: Any = None
    forecast_points: tuple[dict[str, Any], ...] = ()
    source_dataset_labels: tuple[str, ...]
    supporting_record_keys: tuple[str, ...]
    citable: bool
    full_text_available: bool = False
    prediction_is_ground_truth: bool = False


class EventBrokerRecommendationCandidate(DomainModel):
    """同一月份、同一股票的券商金股名单聚合。"""

    ts_code: str
    stock_name: str | None = None
    month: str
    brokers: tuple[str, ...]
    broker_count: int = Field(ge=1)
    source_dataset_labels: tuple[str, ...]
    supporting_record_keys: tuple[str, ...]
    citable: bool
    recommendation_is_outcome_ground_truth: bool = False


class EventSnapshotCoverage(DomainModel):
    configured_market_source_count: int = Field(ge=0)
    successful_market_source_count: int = Field(ge=0)
    successful_announcement_day_count: int = Field(ge=0)
    source_dataset_count: int = Field(ge=0)
    raw_market_news_count: int = Field(ge=0)
    deduplicated_market_news_count: int = Field(ge=0)
    selected_market_news_count: int = Field(ge=0)
    raw_announcement_count: int = Field(ge=0)
    selected_announcement_count: int = Field(ge=0)
    stock_catalog_available: bool
    mapped_market_news_count: int = Field(ge=0)
    configured_sell_side_report_day_count: int = Field(ge=0)
    successful_sell_side_report_day_count: int = Field(ge=0)
    raw_sell_side_report_count: int = Field(ge=0)
    selected_sell_side_report_count: int = Field(ge=0)
    broker_recommendation_available: bool
    raw_broker_recommendation_count: int = Field(ge=0)
    selected_broker_recommendation_count: int = Field(ge=0)
    optional_failure_count: int = Field(ge=0)
    recent_feed_is_complete_history: bool = False


class DailyEventSnapshot(DomainModel):
    """给新闻事件 Agent 直接阅读、但尚未转换为 Evidence 的候选快照。"""

    as_of: AwareDatetime
    news_window_start: AwareDatetime
    news_window_end: AwareDatetime
    announcement_start_date: date
    announcement_end_date: date
    sell_side_report_start_date: date
    sell_side_report_end_date: date
    broker_recommendation_month: str
    market_news: tuple[EventNewsCandidate, ...]
    announcements: tuple[EventAnnouncementCandidate, ...]
    sell_side_reports: tuple[EventSellSideReportCandidate, ...]
    broker_recommendations: tuple[EventBrokerRecommendationCandidate, ...]
    coverage: EventSnapshotCoverage


@dataclass(frozen=True, slots=True)
class DailyEventSnapshotBuild:
    snapshot: DailyEventSnapshot
    datasets: Mapping[str, ServiceDataset]
    optional_failures: Mapping[str, BaseException]


type _DatasetQuery = tuple[str, Callable[[], Awaitable[ServiceDataset]]]


class DailyEventSnapshotService:
    """并行抓取三路快讯和近期公告，再只输出少量可查证候选。"""

    def __init__(
        self,
        public_news_event: PublicNewsEventService,
        news_event: NewsEventDataService,
        instrument_reference: InstrumentReferenceService,
        *,
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1:
            raise ServiceInputError("max_concurrency 必须大于 0")
        self._public_news_event = public_news_event
        self._news_event = news_event
        self._instrument_reference = instrument_reference
        self._max_concurrency = max_concurrency

    async def build_daily_snapshot(
        self,
        *,
        as_of: datetime,
        candidate_count: int = 10,
        news_lookback_hours: int = 24,
        announcement_lookback_days: int = 3,
        research_lookback_days: int = 7,
    ) -> DailyEventSnapshotBuild:
        cutoff = _normalize_as_of(as_of)
        if not 3 <= candidate_count <= 20:
            raise ServiceInputError("candidate_count 必须在 3 到 20 之间")
        if not 1 <= news_lookback_hours <= 24:
            raise ServiceInputError("news_lookback_hours 必须在 1 到 24 之间")
        if not 1 <= announcement_lookback_days <= 7:
            raise ServiceInputError("announcement_lookback_days 必须在 1 到 7 之间")
        if not 1 <= research_lookback_days <= 14:
            raise ServiceInputError("research_lookback_days 必须在 1 到 14 之间")

        news_start = cutoff - timedelta(hours=news_lookback_hours)
        announcement_start = cutoff.date() - timedelta(days=announcement_lookback_days - 1)
        research_start = cutoff.date() - timedelta(days=research_lookback_days - 1)
        broker_month = cutoff.strftime("%Y%m")
        queries: list[_DatasetQuery] = [
            (
                f"market_news_{source.value.lower()}",
                lambda source=source: self._public_news_event.get_market_news(
                    source,
                    news_start,
                    cutoff,
                    as_of=cutoff,
                ),
            )
            for source in MarketNewsSource
        ]
        queries.append(
            (
                "stock_catalog",
                lambda: self._instrument_reference.get_all_stocks(as_of=cutoff),
            )
        )
        for offset in range(announcement_lookback_days):
            announcement_date = announcement_start + timedelta(days=offset)
            queries.append(
                (
                    f"announcements_{announcement_date:%Y%m%d}",
                    lambda announcement_date=announcement_date: (
                        self._public_news_event.get_daily_announcements(
                            announcement_date,
                            category=AnnouncementCategory.ALL,
                            as_of=cutoff,
                        )
                    ),
                )
            )
        for offset in range(research_lookback_days):
            report_date = research_start + timedelta(days=offset)
            queries.append(
                (
                    f"sell_side_reports_{report_date:%Y%m%d}",
                    lambda report_date=report_date: self._news_event.get_daily_sell_side_reports(
                        report_date,
                        as_of=cutoff,
                    ),
                )
            )
        queries.append(
            (
                f"broker_recommendations_{broker_month}",
                lambda: self._news_event.get_broker_recommendations(
                    broker_month,
                    as_of=cutoff,
                ),
            )
        )

        datasets, optional_failures = await self._run_queries(tuple(queries))
        stock_name_index = _build_stock_name_index(datasets.get("stock_catalog"))
        market_news, deduplicated_news_count, raw_news_count = _select_market_news(
            datasets,
            candidate_count,
            stock_name_index,
        )
        announcements, raw_announcement_count = _select_announcements(
            datasets,
            candidate_count,
        )
        sell_side_reports, raw_sell_side_report_count = _select_sell_side_reports(
            datasets,
            candidate_count,
        )
        broker_recommendations, raw_broker_recommendation_count = _select_broker_recommendations(
            datasets, candidate_count
        )
        successful_market_sources = sum(label.startswith("market_news_") for label in datasets)
        successful_announcement_days = sum(label.startswith("announcements_") for label in datasets)
        successful_report_days = sum(label.startswith("sell_side_reports_") for label in datasets)

        snapshot = DailyEventSnapshot(
            as_of=cutoff,
            news_window_start=news_start,
            news_window_end=cutoff,
            announcement_start_date=announcement_start,
            announcement_end_date=cutoff.date(),
            sell_side_report_start_date=research_start,
            sell_side_report_end_date=cutoff.date(),
            broker_recommendation_month=broker_month,
            market_news=market_news,
            announcements=announcements,
            sell_side_reports=sell_side_reports,
            broker_recommendations=broker_recommendations,
            coverage=EventSnapshotCoverage(
                configured_market_source_count=len(MarketNewsSource),
                successful_market_source_count=successful_market_sources,
                successful_announcement_day_count=successful_announcement_days,
                source_dataset_count=len(datasets),
                raw_market_news_count=raw_news_count,
                deduplicated_market_news_count=deduplicated_news_count,
                selected_market_news_count=len(market_news),
                raw_announcement_count=raw_announcement_count,
                selected_announcement_count=len(announcements),
                stock_catalog_available="stock_catalog" in datasets,
                mapped_market_news_count=sum(bool(item.related_stocks) for item in market_news),
                configured_sell_side_report_day_count=research_lookback_days,
                successful_sell_side_report_day_count=successful_report_days,
                raw_sell_side_report_count=raw_sell_side_report_count,
                selected_sell_side_report_count=len(sell_side_reports),
                broker_recommendation_available=(
                    f"broker_recommendations_{broker_month}" in datasets
                ),
                raw_broker_recommendation_count=raw_broker_recommendation_count,
                selected_broker_recommendation_count=len(broker_recommendations),
                optional_failure_count=len(optional_failures),
                recent_feed_is_complete_history=False,
            ),
        )
        return DailyEventSnapshotBuild(
            snapshot=snapshot,
            datasets=datasets,
            optional_failures=optional_failures,
        )

    async def _run_queries(
        self,
        queries: tuple[_DatasetQuery, ...],
    ) -> tuple[dict[str, ServiceDataset], dict[str, BaseException]]:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run(query: _DatasetQuery) -> tuple[str, ServiceDataset | BaseException]:
            label, factory = query
            async with semaphore:
                try:
                    return label, await factory()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    return label, exc

        outcomes = await asyncio.gather(*(run(query) for query in queries))
        datasets: dict[str, ServiceDataset] = {}
        failures: dict[str, BaseException] = {}
        for label, outcome in outcomes:
            if isinstance(outcome, BaseException):
                failures[label] = outcome
            else:
                datasets[label] = outcome
        return datasets, failures


def _select_market_news(
    datasets: Mapping[str, ServiceDataset],
    candidate_count: int,
    stock_name_index: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[EventNewsCandidate, ...], int, int]:
    rows: list[tuple[datetime, str, dict[str, Any]]] = []
    for label, dataset in datasets.items():
        if not label.startswith("market_news_"):
            continue
        for row in dataset.items:
            published_at = _parse_published_at(row.get("published_at"))
            if published_at is not None:
                rows.append((published_at, label, row))
    rows.sort(key=lambda item: (bool(item[2].get("citable")), item[0]), reverse=True)

    grouped: dict[str, dict[str, Any]] = {}
    for published_at, label, row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        summary = _summarize(row.get("content"))
        title_key = _normalize_title(title)
        group = grouped.get(title_key)
        if group is None:
            group = {
                "title": title,
                "summary": summary,
                "published_at": published_at,
                "source_names": [],
                "source_urls": [],
                "source_dataset_labels": [],
                "record_keys": [],
                "source_texts": [],
            }
            grouped[title_key] = group
        group["source_names"].append(str(row.get("source_name") or "未知来源"))
        source_url = str(row.get("source_url") or "").strip()
        if source_url:
            group["source_urls"].append(source_url)
        group["source_dataset_labels"].append(label)
        record_key = str(row.get("record_key") or "")
        group["record_keys"].append(record_key)
        group["source_texts"].append((title, summary, record_key))
        if published_at > group["published_at"]:
            group["published_at"] = published_at
        if len(summary) > len(group["summary"]):
            group["summary"] = summary

    for group in grouped.values():
        group["related_stocks"] = _match_related_stocks_from_rows(
            group["source_texts"],
            stock_name_index,
        )

    ordered = sorted(
        grouped.values(),
        key=lambda group: (
            bool(group["source_urls"]),
            bool(group["related_stocks"]),
            len(group["source_dataset_labels"]),
            group["published_at"],
        ),
        reverse=True,
    )
    selected = tuple(
        EventNewsCandidate(
            title=group["title"],
            summary=group["summary"],
            published_at=group["published_at"],
            source_names=_unique(group["source_names"]),
            source_urls=_unique(group["source_urls"]),
            source_dataset_labels=_unique(group["source_dataset_labels"]),
            record_keys=_unique(key for key in group["record_keys"] if key),
            citable=bool(group["source_urls"]),
            related_stocks=group["related_stocks"],
        )
        for group in ordered[:candidate_count]
    )
    return selected, len(grouped), len(rows)


def _select_announcements(
    datasets: Mapping[str, ServiceDataset],
    candidate_count: int,
) -> tuple[tuple[EventAnnouncementCandidate, ...], int]:
    candidates: list[tuple[int, date, str, dict[str, Any], tuple[str, ...]]] = []
    seen: set[str] = set()
    raw_count = 0
    for label, dataset in datasets.items():
        if not label.startswith("announcements_"):
            continue
        raw_count += len(dataset.items)
        for row in dataset.items:
            record_key = str(row.get("record_key") or "")
            if record_key and record_key in seen:
                continue
            if record_key:
                seen.add(record_key)
            announcement_date = _parse_announcement_date(row.get("announcement_date"))
            if announcement_date is None:
                continue
            signals = _announcement_signals(row)
            score = len(signals)
            candidates.append((score, announcement_date, label, row, signals))

    candidates.sort(
        key=lambda item: (item[0], item[1], str(item[3].get("ts_code") or "")),
        reverse=True,
    )
    selected = tuple(
        EventAnnouncementCandidate(
            ts_code=str(row.get("ts_code") or ""),
            stock_name=(str(row["stock_name"]) if row.get("stock_name") else None),
            title=str(row.get("title") or ""),
            announcement_type=str(row.get("announcement_type") or "未分类"),
            announcement_date=announcement_date,
            source_url=(str(row["source_url"]) if row.get("source_url") else None),
            source_dataset_label=label,
            record_key=str(row.get("record_key") or ""),
            citable=bool(row.get("citable")),
            selection_signals=signals or ("最新公告",),
        )
        for _, announcement_date, label, row, signals in candidates[:candidate_count]
    )
    return selected, raw_count


def _select_sell_side_reports(
    datasets: Mapping[str, ServiceDataset],
    candidate_count: int,
) -> tuple[tuple[EventSellSideReportCandidate, ...], int]:
    """把同一份研报的多个预测年度行聚合成一个有界候选。"""

    groups: dict[tuple[str, date, str, str, str], dict[str, Any]] = {}
    raw_count = 0
    for label, dataset in datasets.items():
        if not label.startswith("sell_side_reports_"):
            continue
        raw_count += len(dataset.items)
        for row in dataset.items:
            ts_code = str(row.get("ts_code") or "").strip().upper()
            report_date = _parse_data_date(row.get("report_date"))
            title = str(row.get("report_title") or "").strip()
            organization = str(row.get("org_name") or "").strip()
            author_name = str(row.get("author_name") or "").strip()
            record_key = event_report_record_key(row)
            if (
                not ts_code
                or report_date is None
                or not title
                or not organization
                or record_key is None
            ):
                continue
            key = (ts_code, report_date, organization, author_name, title)
            group = groups.setdefault(
                key,
                {
                    "row": row,
                    "labels": [],
                    "forecast_points": [],
                    "supporting_record_keys": [],
                },
            )
            group["labels"].append(label)
            group["supporting_record_keys"].append(record_key)
            forecast = {
                field: row.get(field)
                for field in (
                    "quarter",
                    "op_rt",
                    "op_pr",
                    "tp",
                    "np",
                    "eps",
                    "pe",
                    "rd",
                    "roe",
                    "ev_ebitda",
                    "imp_dg",
                )
                if row.get(field) not in (None, "")
            }
            forecast["source_record_key"] = record_key
            if forecast and forecast not in group["forecast_points"]:
                group["forecast_points"].append(forecast)

    ordered = sorted(
        groups.items(),
        key=lambda item: (
            item[0][1],
            bool(item[1]["row"].get("rating")),
            len(item[1]["forecast_points"]),
            item[0][0],
        ),
        reverse=True,
    )
    selected: list[EventSellSideReportCandidate] = []
    for (ts_code, report_date, organization, author_name, title), group in ordered[
        :candidate_count
    ]:
        row = group["row"]
        selected.append(
            EventSellSideReportCandidate(
                ts_code=ts_code,
                stock_name=(str(row["name"]) if row.get("name") else None),
                report_date=report_date,
                report_title=title,
                report_type=(str(row["report_type"]) if row.get("report_type") else None),
                classify=(str(row["classify"]) if row.get("classify") else None),
                org_name=organization,
                author_name=author_name or None,
                rating=(str(row["rating"]) if row.get("rating") else None),
                target_price_min=row.get("min_price"),
                target_price_max=row.get("max_price"),
                forecast_points=tuple(group["forecast_points"][:8]),
                source_dataset_labels=_unique(group["labels"]),
                supporting_record_keys=_unique(group["supporting_record_keys"]),
                citable=True,
            )
        )
    return tuple(selected), raw_count


def _select_broker_recommendations(
    datasets: Mapping[str, ServiceDataset],
    candidate_count: int,
) -> tuple[tuple[EventBrokerRecommendationCandidate, ...], int]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    raw_count = 0
    for label, dataset in datasets.items():
        if not label.startswith("broker_recommendations_"):
            continue
        raw_count += len(dataset.items)
        for row in dataset.items:
            ts_code = str(row.get("ts_code") or "").strip().upper()
            month = str(row.get("month") or "").strip()
            broker = str(row.get("broker") or "").strip()
            record_key = event_broker_recommendation_record_key(row)
            if not ts_code or not month or not broker or record_key is None:
                continue
            group = groups.setdefault(
                (month, ts_code),
                {
                    "row": row,
                    "brokers": [],
                    "labels": [],
                    "supporting_record_keys": [],
                },
            )
            group["brokers"].append(broker)
            group["labels"].append(label)
            group["supporting_record_keys"].append(record_key)

    ordered = sorted(
        groups.items(),
        key=lambda item: (len(set(item[1]["brokers"])), item[0][0], item[0][1]),
        reverse=True,
    )
    selected: list[EventBrokerRecommendationCandidate] = []
    for (month, ts_code), group in ordered[:candidate_count]:
        row = group["row"]
        brokers = _unique(group["brokers"])
        selected.append(
            EventBrokerRecommendationCandidate(
                ts_code=ts_code,
                stock_name=(str(row["name"]) if row.get("name") else None),
                month=month,
                brokers=brokers,
                broker_count=len(brokers),
                source_dataset_labels=_unique(group["labels"]),
                supporting_record_keys=_unique(group["supporting_record_keys"]),
                citable=True,
            )
        )
    return tuple(selected), raw_count


def _build_stock_name_index(
    dataset: ServiceDataset | None,
) -> dict[str, tuple[str, ...]]:
    """仅收录 stock_basic 当前上市目录的完整名称，不生成概念别名。"""

    if dataset is None:
        return {}
    codes_by_name: dict[str, list[str]] = {}
    for row in dataset.items:
        stock_name = str(row.get("name") or "").strip()
        ts_code = str(row.get("ts_code") or "").strip().upper()
        if len(stock_name) < 2 or not ts_code:
            continue
        codes_by_name.setdefault(stock_name, []).append(ts_code)
    unique_index: dict[str, tuple[str, ...]] = {}
    for name, codes in codes_by_name.items():
        unique_codes = _unique(codes)
        # 同名对应多个代码时无法仅靠新闻公司名确定身份，宁可不映射。
        if len(unique_codes) == 1:
            unique_index[name] = unique_codes
    return unique_index


def _match_related_stocks(
    text: str,
    stock_name_index: Mapping[str, tuple[str, ...]],
    *,
    supporting_record_keys: tuple[str, ...] = (),
) -> tuple[EventRelatedStock, ...]:
    """只在新闻原文精确包含上市公司全名时建立身份关联。"""

    haystack = _WHITESPACE.sub("", text).casefold()
    matched_names: list[str] = []
    result: list[EventRelatedStock] = []
    for stock_name in sorted(stock_name_index, key=len, reverse=True):
        normalized_name = _WHITESPACE.sub("", stock_name).casefold()
        if not normalized_name or normalized_name not in haystack:
            continue
        # 保守处理“短公司名是长公司名子串”的歧义，不重复联想。
        if any(normalized_name in matched.casefold() for matched in matched_names):
            continue
        matched_names.append(stock_name)
        result.extend(
            EventRelatedStock(
                ts_code=ts_code,
                stock_name=stock_name,
                matched_name=stock_name,
                supporting_record_keys=supporting_record_keys,
            )
            for ts_code in stock_name_index[stock_name]
        )
    return tuple(result)


def _match_related_stocks_from_rows(
    rows: list[tuple[str, str, str]],
    stock_name_index: Mapping[str, tuple[str, ...]],
) -> tuple[EventRelatedStock, ...]:
    """在标题和摘要中匹配，并保留真正出现该公司名的逐行 key。"""

    by_stock: dict[tuple[str, str, str], list[str]] = {}
    for title, summary, record_key in rows:
        related = _match_related_stocks(
            f"{title} {summary}",
            stock_name_index,
            supporting_record_keys=((record_key,) if record_key else ()),
        )
        for item in related:
            by_stock.setdefault(
                (item.ts_code, item.stock_name, item.matched_name),
                [],
            ).extend(item.supporting_record_keys)
    return tuple(
        EventRelatedStock(
            ts_code=ts_code,
            stock_name=stock_name,
            matched_name=matched_name,
            supporting_record_keys=_unique(record_keys),
        )
        for (ts_code, stock_name, matched_name), record_keys in by_stock.items()
    )


def _derived_record_key(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{sha256(raw).hexdigest()}"


def event_report_record_key(row: Mapping[str, Any]) -> str | None:
    """按研报原始行完整身份生成稳定键，预测期不同即为不同来源行。"""

    ts_code = str(row.get("ts_code") or "").strip().upper()
    report_date = _parse_data_date(row.get("report_date"))
    organization = str(row.get("org_name") or "").strip()
    author_name = str(row.get("author_name") or "").strip()
    title = str(row.get("report_title") or "").strip()
    quarter = str(row.get("quarter") or "").strip()
    if not ts_code or report_date is None or not organization or not title:
        return None
    return _derived_record_key(
        "report_rc",
        ts_code,
        report_date.isoformat(),
        organization,
        author_name,
        title,
        quarter,
    )


def event_broker_recommendation_record_key(row: Mapping[str, Any]) -> str | None:
    """按月份、券商和股票完整身份生成月度荐股原始行稳定键。"""

    month = str(row.get("month") or "").strip()
    broker = str(row.get("broker") or "").strip()
    ts_code = str(row.get("ts_code") or "").strip().upper()
    if not month or not broker or not ts_code:
        return None
    return _derived_record_key("broker_recommend", month, broker, ts_code)


def _normalize_as_of(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServiceInputError("每日事件快照的 as_of 必须是带时区的 datetime")
    return value.astimezone(_SHANGHAI).replace(microsecond=0)


def _parse_published_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=_SHANGHAI)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(_SHANGHAI)
        )
    if value is None:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SHANGHAI)
    except ValueError:
        return None


def _parse_announcement_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    return _parse_data_date(value)


def _parse_data_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _normalize_title(value: str) -> str:
    compact = _WHITESPACE.sub("", value).lower()
    return _PUNCTUATION.sub("", compact)


def _summarize(value: Any, limit: int = 500) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _announcement_signals(row: Mapping[str, Any]) -> tuple[str, ...]:
    haystack = f"{row.get('announcement_type') or ''} {row.get('title') or ''}"
    return tuple(
        label
        for label, keywords in _ANNOUNCEMENT_SIGNALS
        if any(keyword in haystack for keyword in keywords)
    )


def _unique(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))
