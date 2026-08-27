"""离线验证 AKShare 新闻 Provider；所有上游函数均由 DataFrame 假实现注入。"""

import asyncio
import threading

import pandas as pd
import pytest

from stock_research_agent.providers.akshare_news import AkshareNewsProvider
from stock_research_agent.providers.errors import (
    ProviderSchemaError,
    ProviderTransportError,
    UnknownProviderApiError,
)
from stock_research_agent.providers.models import ProviderQuery, ProviderSource


def test_eastmoney_market_news_normalizes_chinese_columns() -> None:
    async def scenario() -> None:
        provider = AkshareNewsProvider(
            functions={
                "stock_info_global_em": lambda: pd.DataFrame(
                    [
                        {
                            "标题": "沪深两市收盘",
                            "摘要": pd.NA,
                            "发布时间": pd.Timestamp("2026-08-24 15:05:06"),
                            "链接": "https://finance.eastmoney.com/a/1.html",
                        }
                    ]
                )
            }
        )
        try:
            result = await provider.query(ProviderQuery(api_name="stock_info_global_em"))
        finally:
            await provider.aclose()

        assert result.provider is ProviderSource.AKSHARE_EASTMONEY
        assert result.fields == (
            "record_key",
            "title",
            "content",
            "published_at",
            "source_name",
            "source_url",
            "source_kind",
            "citable",
        )
        assert result.data_as_of.isoformat() == "2026-08-24"
        assert result.items == [
            {
                "record_key": result.items[0]["record_key"],
                "title": "沪深两市收盘",
                "content": None,
                "published_at": "2026-08-24 15:05:06",
                "source_name": "东方财富",
                "source_url": "https://finance.eastmoney.com/a/1.html",
                "source_kind": "market_news",
                "citable": True,
            }
        ]
        assert result.items[0]["record_key"].startswith("ak_")

    asyncio.run(scenario())


def test_ths_market_news_normalizes_chinese_columns() -> None:
    async def scenario() -> None:
        provider = AkshareNewsProvider(
            functions={
                "stock_info_global_ths": lambda: pd.DataFrame(
                    [
                        {
                            "标题": "行业资金出现异动",
                            "内容": "半导体板块成交活跃。",
                            "发布时间": "2026/08/24 14:31:00",
                            "链接": "https://news.10jqka.com.cn/1/",
                        }
                    ]
                )
            }
        )
        try:
            result = await provider.query(ProviderQuery(api_name="stock_info_global_ths"))
        finally:
            await provider.aclose()

        row = result.items[0]
        assert result.provider is ProviderSource.AKSHARE_THS
        assert row["title"] == "行业资金出现异动"
        assert row["content"] == "半导体板块成交活跃。"
        assert row["published_at"] == "2026-08-24 14:31:00"
        assert row["source_name"] == "同花顺"
        assert row["source_url"] == "https://news.10jqka.com.cn/1/"
        assert row["citable"] is True

    asyncio.run(scenario())


def test_cls_market_news_combines_date_and_time_and_marks_row_non_citable() -> None:
    async def scenario() -> None:
        observed_params: dict[str, object] = {}

        def cls_feed(**params: object) -> pd.DataFrame:
            observed_params.update(params)
            return pd.DataFrame(
                [
                    {
                        "标题": "政策快讯",
                        "内容": "有关部门发布新政策。",
                        "发布日期": "20260824",
                        "发布时间": "13:20",
                    },
                    {
                        "标题": pd.NA,
                        "内容": pd.NA,
                        "发布日期": pd.NA,
                        "发布时间": pd.NA,
                    },
                ]
            )

        provider = AkshareNewsProvider(functions={"stock_info_global_cls": cls_feed})
        try:
            result = await provider.query(
                ProviderQuery(api_name="stock_info_global_cls", params={"symbol": "全部"})
            )
        finally:
            await provider.aclose()

        assert observed_params == {"symbol": "全部"}
        assert result.provider is ProviderSource.AKSHARE_CLS
        assert result.items[0]["published_at"] == "2026-08-24 13:20:00"
        assert result.items[0]["source_name"] == "财联社"
        assert result.items[0]["source_url"] is None
        assert result.items[0]["citable"] is False
        assert len(result.items) == 1

    asyncio.run(scenario())


def test_stock_news_normalizes_identity_source_and_keywords() -> None:
    async def scenario() -> None:
        observed_params: dict[str, object] = {}

        def stock_news(**params: object) -> pd.DataFrame:
            observed_params.update(params)
            return pd.DataFrame(
                [
                    {
                        "关键词": "银行,年报",
                        "新闻标题": "平安银行披露经营数据",
                        "新闻内容": "公司披露了最新经营情况。",
                        "发布时间": "2026-08-24T12:00:00",
                        "文章来源": "证券时报",
                        "新闻链接": "https://example.test/stock-news/1",
                    }
                ]
            )

        provider = AkshareNewsProvider(functions={"stock_news_em": stock_news})
        try:
            result = await provider.query(
                ProviderQuery(api_name="stock_news_em", params={"symbol": "000001"})
            )
        finally:
            await provider.aclose()

        row = result.items[0]
        assert observed_params == {"symbol": "000001"}
        assert result.provider is ProviderSource.AKSHARE_EASTMONEY
        assert row["ts_code"] == "000001.SZ"
        assert row["title"] == "平安银行披露经营数据"
        assert row["published_at"] == "2026-08-24 12:00:00"
        assert row["source_name"] == "证券时报"
        assert row["keywords"] == "银行,年报"
        assert row["source_kind"] == "stock_news"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("api_name", "params", "expected_params", "raw_code", "expected_ts_code"),
    [
        (
            "stock_notice_report",
            {"symbol": "全部", "date": "20260824"},
            {"symbol": "全部", "date": "20260824"},
            "000001",
            "000001.SZ",
        ),
        (
            "stock_individual_notice_report",
            {
                "security": "600519",
                "symbol": "全部",
                "begin_date": "20260801",
                "end_date": "20260824",
            },
            {
                "security": "600519",
                "symbol": "全部",
                "begin_date": "20260801",
                "end_date": "20260824",
            },
            "600519",
            "600519.SH",
        ),
    ],
)
def test_notice_interfaces_normalize_chinese_columns_and_provider_source(
    api_name: str,
    params: dict[str, str],
    expected_params: dict[str, str],
    raw_code: str,
    expected_ts_code: str,
) -> None:
    async def scenario() -> None:
        observed_params: dict[str, object] = {}

        def notices(**call_params: object) -> pd.DataFrame:
            observed_params.update(call_params)
            return pd.DataFrame(
                [
                    {
                        "代码": raw_code,
                        "名称": "测试公司",
                        "公告标题": "关于重大事项的公告",
                        "公告类型": "重大事项",
                        "公告日期": pd.Timestamp("2026-08-24"),
                        "网址": "https://data.eastmoney.com/notices/1.html",
                    }
                ]
            )

        provider = AkshareNewsProvider(functions={api_name: notices})
        try:
            result = await provider.query(ProviderQuery(api_name=api_name, params=params))
        finally:
            await provider.aclose()

        row = result.items[0]
        assert observed_params == expected_params
        assert result.provider is ProviderSource.AKSHARE_EASTMONEY
        assert result.data_as_of.isoformat() == "2026-08-24"
        assert row["security_code"] == raw_code
        assert row["ts_code"] == expected_ts_code
        assert row["stock_name"] == "测试公司"
        assert row["title"] == "关于重大事项的公告"
        assert row["announcement_type"] == "重大事项"
        assert row["announcement_date"] == "2026-08-24"
        assert row["source_kind"] == "announcement"
        assert row["citable"] is True

    asyncio.run(scenario())


def test_daily_notice_preserves_and_tolerates_non_a_share_security_codes() -> None:
    async def scenario() -> None:
        provider = AkshareNewsProvider(
            functions={
                "stock_notice_report": lambda **params: pd.DataFrame(
                    [
                        {
                            "代码": "A24037",
                            "名称": "非 A 股证券",
                            "公告标题": "其他证券公告",
                            "公告类型": "其他",
                            "公告日期": "2026-08-24",
                            "网址": "https://example.test/non-a-share",
                        }
                    ]
                )
            }
        )
        try:
            result = await provider.query(
                ProviderQuery(
                    api_name="stock_notice_report",
                    params={"symbol": "全部", "date": "20260824"},
                )
            )
        finally:
            await provider.aclose()

        assert result.items[0]["security_code"] == "A24037"
        assert result.items[0]["ts_code"] is None

    asyncio.run(scenario())


def test_unknown_api_is_rejected_before_any_function_call() -> None:
    async def scenario() -> None:
        called = False

        def forbidden_function() -> pd.DataFrame:
            nonlocal called
            called = True
            return pd.DataFrame()

        provider = AkshareNewsProvider(functions={"not_declared": forbidden_function})
        try:
            with pytest.raises(UnknownProviderApiError):
                await provider.query(ProviderQuery(api_name="not_declared"))
        finally:
            await provider.aclose()
        assert called is False

    asyncio.run(scenario())


def test_missing_dataframe_column_becomes_schema_error() -> None:
    async def scenario() -> None:
        provider = AkshareNewsProvider(
            functions={
                "stock_info_global_em": lambda: pd.DataFrame(
                    [{"标题": "字段不完整", "摘要": "正文", "发布时间": "2026-08-24 10:00:00"}]
                )
            }
        )
        try:
            with pytest.raises(ProviderSchemaError, match="链接") as captured:
                await provider.query(ProviderQuery(api_name="stock_info_global_em"))
        finally:
            await provider.aclose()

        assert captured.value.provider is ProviderSource.AKSHARE_EASTMONEY

    asyncio.run(scenario())


def test_injected_function_exception_becomes_transport_error() -> None:
    async def scenario() -> None:
        def failing_function() -> pd.DataFrame:
            raise RuntimeError("上游页面结构异常")

        provider = AkshareNewsProvider(functions={"stock_info_global_ths": failing_function})
        try:
            with pytest.raises(ProviderTransportError) as captured:
                await provider.query(ProviderQuery(api_name="stock_info_global_ths"))
        finally:
            await provider.aclose()

        assert captured.value.provider is ProviderSource.AKSHARE_THS
        assert captured.value.safe_message == "RuntimeError"

    asyncio.run(scenario())


def test_slow_injected_function_becomes_transport_timeout() -> None:
    async def scenario() -> None:
        release_worker = threading.Event()

        def slow_function() -> pd.DataFrame:
            release_worker.wait(timeout=1)
            return pd.DataFrame(
                [
                    {
                        "标题": "迟到的新闻",
                        "摘要": "正文",
                        "发布时间": "2026-08-24 10:00:00",
                        "链接": "https://example.test/slow",
                    }
                ]
            )

        provider = AkshareNewsProvider(
            timeout_seconds=0.01,
            max_workers=1,
            functions={"stock_info_global_em": slow_function},
        )
        try:
            with pytest.raises(ProviderTransportError, match="超过") as captured:
                await provider.query(ProviderQuery(api_name="stock_info_global_em"))
            assert captured.value.provider is ProviderSource.AKSHARE_EASTMONEY
        finally:
            release_worker.set()
            await asyncio.sleep(0.02)
            await provider.aclose()

    asyncio.run(scenario())
