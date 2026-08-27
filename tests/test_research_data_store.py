"""一次研究运行内完整数据仓库的隔离与快照测试。"""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_research_agent.providers.models import ProviderSource
from stock_research_agent.research_data import (
    InMemoryResearchDataStore,
    InvalidContextReferenceError,
    InvalidResearchRunIdError,
    ResearchDataBundle,
    ResearchDataNotFoundError,
    ResearchDataScopeError,
)
from stock_research_agent.services.models import (
    ServiceDataset,
    ServiceItemTrace,
    ServicePageTrace,
)
from stock_research_agent.tools.context import ToolLimits

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 15, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 1, tzinfo=SHANGHAI)
RUN_A = "run_20260820_150000_000001_SZ_aaaaaaaa"
RUN_B = "run_20260820_150000_600000_SH_bbbbbbbb"


def price_dataset(*, close: float = 12.34) -> ServiceDataset:
    fields = ("ts_code", "trade_date", "close")
    return ServiceDataset(
        api_name="daily",
        query_params={"ts_code": "000001.SZ"},
        requested_fields=fields,
        items=[
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260820",
                "close": close,
            }
        ],
        item_traces=(
            ServiceItemTrace(
                page_index=0,
                source_offset=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
            ),
        ),
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=1,
                returned_fields=fields,
                response_bytes=128,
            ),
        ),
        as_of=AS_OF.date(),
        data_as_of=date(2026, 8, 20),
        received_item_count=1,
        discarded_item_count=0,
    )


def stock_bundle(dataset: ServiceDataset | None = None) -> ResearchDataBundle:
    return ResearchDataBundle(
        kind="stock_price_context",
        tool_name="get_stock_price_context",
        as_of=AS_OF,
        datasets={"price_bars": dataset or price_dataset()},
        metadata={
            "ts_code": "000001.SZ",
            "frequency": "daily",
            "start_date": "2026-08-01",
            "end_date": "2026-08-20",
        },
    )


def test_bundle_freezes_top_level_fields_and_copies_inputs() -> None:
    original = price_dataset()
    bundle = stock_bundle(original)
    original.items[0]["close"] = 99.0

    assert bundle.datasets["price_bars"].items[0]["close"] == 12.34
    with pytest.raises(ValidationError):
        bundle.kind = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        bundle.datasets["another"] = price_dataset()  # type: ignore[index]
    with pytest.raises(TypeError):
        bundle.metadata["ts_code"] = "600000.SH"  # type: ignore[index]


def test_store_returns_unpredictable_run_scoped_independent_snapshots() -> None:
    async def scenario() -> None:
        store = InMemoryResearchDataStore()
        context_ref = await store.put(RUN_A, stock_bundle())
        another_ref = await store.put(RUN_A, stock_bundle())

        assert context_ref.startswith("ctx_")
        assert len(context_ref) == 36
        assert context_ref != another_ref

        first_read = await store.get(RUN_A, context_ref)
        first_read.datasets["price_bars"].items[0]["close"] = 999.0
        second_read = await store.get(RUN_A, context_ref)
        assert second_read.datasets["price_bars"].items[0]["close"] == 12.34

        with pytest.raises(ResearchDataScopeError):
            await store.get(RUN_B, context_ref)

    asyncio.run(scenario())


def test_cleanup_is_run_scoped_and_idempotent() -> None:
    async def scenario() -> None:
        store = InMemoryResearchDataStore()
        ref_a1 = await store.put(RUN_A, stock_bundle())
        await store.put(RUN_A, stock_bundle())
        ref_b = await store.put(RUN_B, stock_bundle())

        assert await store.cleanup(RUN_A) == 2
        assert await store.cleanup(RUN_A) == 0
        with pytest.raises(ResearchDataNotFoundError):
            await store.get(RUN_A, ref_a1)
        assert (await store.get(RUN_B, ref_b)).tool_name == "get_stock_price_context"

    asyncio.run(scenario())


def test_store_rejects_invalid_run_ids_and_references() -> None:
    async def scenario() -> None:
        store = InMemoryResearchDataStore()
        with pytest.raises(InvalidResearchRunIdError):
            await store.put("not-a-run", stock_bundle())
        with pytest.raises(InvalidContextReferenceError):
            await store.get(RUN_A, "ctx_guessable")
        with pytest.raises(ResearchDataNotFoundError):
            await store.get(RUN_A, f"ctx_{'a' * 32}")

    asyncio.run(scenario())


def test_preview_limit_must_be_positive() -> None:
    assert ToolLimits().preview_items_per_dataset == 5
    with pytest.raises(ValueError):
        ToolLimits(preview_items_per_dataset=0)
