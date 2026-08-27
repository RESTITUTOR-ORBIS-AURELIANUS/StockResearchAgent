"""证券身份、目录和交易日历 Service。"""

from datetime import date
from typing import Literal

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    matches_code,
    validate_choice,
    validate_date_range,
    validate_non_empty,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import INSTRUMENT_REFERENCE_SPECS
from stock_research_agent.services.models import ServiceDataset


class InstrumentReferenceService(BaseDataService):
    """负责“这是什么证券、在哪交易、某天是否开市”等参考数据。"""

    API_SPECS = INSTRUMENT_REFERENCE_SPECS

    async def get_trade_calendar(
        self,
        exchange: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "trade_cal",
            {
                "exchange": validate_non_empty(exchange, "exchange"),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )

    async def get_stock_basic(
        self,
        ts_code: str,
        *,
        list_status: Literal["L", "D", "P"] = "L",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "stock_basic",
            {
                "ts_code": validate_security_code(ts_code),
                "list_status": validate_choice(
                    list_status, frozenset({"L", "D", "P"}), "list_status"
                ),
            },
            as_of=as_of,
        )

    async def get_all_stocks(
        self,
        *,
        list_status: Literal["L", "D", "P"] = "L",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得指定上市状态的完整股票目录，供确定性横截面聚合使用。"""

        return await self._query(
            "stock_basic",
            {
                "list_status": validate_choice(
                    list_status, frozenset({"L", "D", "P"}), "list_status"
                )
            },
            as_of=as_of,
        )

    async def get_etf_basic(self, ts_code: str, *, as_of: AsOfValue = None) -> ServiceDataset:
        return await self._query(
            "etf_basic", {"ts_code": validate_security_code(ts_code)}, as_of=as_of
        )

    async def get_etf_index(self, ts_code: str, *, as_of: AsOfValue = None) -> ServiceDataset:
        return await self._query(
            "etf_index", {"ts_code": validate_security_code(ts_code)}, as_of=as_of
        )

    async def get_option_contracts(
        self, exchange: str, *, as_of: AsOfValue = None
    ) -> ServiceDataset:
        # 当前兼容端只验证过 exchange；不要擅自传 list_status。
        return await self._query(
            "opt_basic", {"exchange": validate_non_empty(exchange, "exchange")}, as_of=as_of
        )

    async def get_funds(
        self,
        *,
        market: str = "E",
        status: Literal["L", "D", "I"] = "L",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "fund_basic",
            {
                "market": validate_non_empty(market, "market"),
                "status": validate_choice(status, frozenset({"L", "D", "I"}), "status"),
            },
            as_of=as_of,
        )

    async def get_indices(self, market: str, *, as_of: AsOfValue = None) -> ServiceDataset:
        return await self._query(
            "index_basic", {"market": validate_non_empty(market, "market")}, as_of=as_of
        )

    async def get_industry_classifications(
        self,
        *,
        src: str = "SW2021",
        level: Literal["L1", "L2", "L3"] | None = None,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得申万行业目录；兼容端不传 level，统一在本地筛选。"""

        normalized_level = (
            validate_choice(level, frozenset({"L1", "L2", "L3"}), "level")
            if level is not None
            else None
        )
        return await self._query(
            "index_classify",
            {"src": validate_non_empty(src, "src")},
            as_of=as_of,
            row_filter=(
                (lambda row: str(row.get("level", "")).upper() == normalized_level)
                if normalized_level is not None
                else None
            ),
        )

    async def get_industry_members(
        self,
        l1_code: str,
        *,
        is_new: Literal["Y", "N"] = "Y",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个申万一级行业下的完整成分，避免上游 2000 行截断。"""

        return await self._query(
            "index_member_all",
            {
                "l1_code": validate_security_code(l1_code),
                "is_new": validate_choice(is_new, frozenset({"Y", "N"}), "is_new"),
            },
            as_of=as_of,
        )

    async def get_index_weights(
        self,
        index_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得指数在一个窄日期窗口内公布的成分权重。"""

        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "index_weight",
            {
                "index_code": validate_security_code(index_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )

    async def get_name_history(
        self,
        ts_code: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "namechange",
            {"ts_code": validate_security_code(ts_code)},
            as_of=as_of,
        )

    async def get_new_shares(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "new_share",
            {"start_date": start, "end_date": end},
            as_of=as_of,
        )

    async def get_st_list(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "stock_st",
            {"trade_date": format_date(trade_date)},
            as_of=as_of,
        )

    async def get_hsgt_stock(self, ts_code: str, *, as_of: AsOfValue = None) -> ServiceDataset:
        # 当前兼容端只验证过 ts_code；不要擅自传 is_new。
        return await self._query(
            "stock_hsgt",
            {"ts_code": validate_security_code(ts_code)},
            as_of=as_of,
            row_filter=matches_code(ts_code),
        )
