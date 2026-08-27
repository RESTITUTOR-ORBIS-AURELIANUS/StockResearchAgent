"""互联互通、龙虎榜、大宗交易与融资融券 Service。"""

from datetime import date

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    matches_code,
    validate_date_range,
    validate_non_empty,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import TRADING_BEHAVIOR_SPECS
from stock_research_agent.services.models import ServiceDataset


class TradingBehaviorService(BaseDataService):
    """负责杠杆、异常交易、机构席位和北向持股行为。"""

    API_SPECS = TRADING_BEHAVIOR_SPECS

    async def get_northbound_holdings(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "hk_hold",
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )

    async def get_block_trades(
        self,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._full_market_trade_date_query(
            "block_trade", ts_code, trade_date, as_of=as_of
        )

    async def get_top_list(
        self,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._full_market_trade_date_query(
            "top_list", ts_code, trade_date, as_of=as_of
        )

    async def get_top_institutions(
        self,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._full_market_trade_date_query(
            "top_inst", ts_code, trade_date, as_of=as_of
        )

    async def get_margin_market(
        self,
        trade_date: date,
        *,
        exchange_id: str,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "margin",
            {
                "trade_date": format_date(trade_date),
                "exchange_id": validate_non_empty(exchange_id, "exchange_id"),
            },
            as_of=as_of,
        )

    async def get_margin_detail(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "margin_detail",
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )

    async def get_margin_eligibility(
        self,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "margin_secs",
            {
                "ts_code": validate_security_code(ts_code),
                "trade_date": format_date(trade_date),
            },
            as_of=as_of,
        )

    async def get_daily_stock_moneyflow_ths(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个交易日的全市场同花顺个股资金流，供确定性横截面筛选。"""

        return await self._daily_market_query("moneyflow_ths", trade_date, as_of=as_of)

    async def get_daily_stock_moneyflow_dc(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个交易日的全市场东财个股资金流，供确定性横截面筛选。"""

        return await self._daily_market_query("moneyflow_dc", trade_date, as_of=as_of)

    async def get_stock_moneyflow_ths(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得指定股票的同花顺资金流历史。"""

        return await self._stock_date_range_query(
            "moneyflow_ths", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_stock_moneyflow_dc(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得指定股票的东财资金流历史。"""

        return await self._stock_date_range_query(
            "moneyflow_dc", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_daily_industry_moneyflow_ths(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._daily_market_query("moneyflow_ind_ths", trade_date, as_of=as_of)

    async def get_daily_market_moneyflow_dc(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._daily_market_query("moneyflow_mkt_dc", trade_date, as_of=as_of)

    async def get_hsgt_moneyflow(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "moneyflow_hsgt",
            {"start_date": start, "end_date": end},
            as_of=as_of,
        )

    async def get_daily_limit_list(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._daily_market_query("limit_list_d", trade_date, as_of=as_of)

    async def _full_market_trade_date_query(
        self,
        api_name: str,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        """这些接口只按交易日取全市场，股票代码在本地筛选。"""

        validate_observation_end(trade_date, as_of)
        return await self._query(
            api_name,
            {"trade_date": format_date(trade_date)},
            as_of=as_of,
            row_filter=matches_code(ts_code),
        )

    async def _daily_market_query(
        self,
        api_name: str,
        trade_date: date,
        *,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            api_name,
            {"trade_date": format_date(trade_date)},
            as_of=as_of,
        )

    async def _stock_date_range_query(
        self,
        api_name: str,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            api_name,
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )
