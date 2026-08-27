"""A 股和指数行情 Service。"""

from datetime import date
from typing import Literal

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    matches_code,
    validate_choice,
    validate_date_range,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import EQUITY_MARKET_DATA_SPECS
from stock_research_agent.services.models import ServiceDataset

StockBarFrequency = Literal["daily", "weekly", "monthly"]


class EquityMarketDataService(BaseDataService):
    """负责股票/指数价格、估值、涨跌停和停复牌数据。"""

    API_SPECS = EQUITY_MARKET_DATA_SPECS

    async def get_daily_market_bars(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个交易日的全 A 股日线横截面。"""

        return await self._market_day_query("daily", trade_date, as_of=as_of)

    async def get_daily_market_valuation(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个交易日的全 A 股换手、量比、估值和市值横截面。"""

        return await self._market_day_query("daily_basic", trade_date, as_of=as_of)

    async def get_daily_market_limits(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._market_day_query("stk_limit", trade_date, as_of=as_of)

    async def get_daily_market_suspensions(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._market_day_query("suspend_d", trade_date, as_of=as_of)

    async def get_stock_bars(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        frequency: StockBarFrequency = "daily",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        normalized_frequency = validate_choice(
            frequency,
            frozenset({"DAILY", "WEEKLY", "MONTHLY"}),
            "frequency",
        ).lower()
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            normalized_frequency,
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
        )

    async def get_adjustment_factors(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_stock_query(
            "adj_factor", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_daily_valuation(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_stock_query(
            "daily_basic", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_price_limits(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_stock_query(
            "stk_limit", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_suspensions(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """兼容端不接收 ts_code，因此先按日期取全市场、再在本地筛选。"""

        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "suspend_d",
            {"start_date": start, "end_date": end},
            as_of=as_of,
            row_filter=matches_code(ts_code),
        )

    async def get_index_bars(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_stock_query(
            "index_daily", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_index_daily_metrics(
        self,
        ts_code: str,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "index_dailybasic",
            {
                "ts_code": validate_security_code(ts_code),
                "trade_date": format_date(trade_date),
            },
            as_of=as_of,
        )

    async def get_sw_industry_bars(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """取得一个申万行业指数的日线；主兼容端要求按指数代码查询。"""

        return await self._dated_stock_query("sw_daily", ts_code, start_date, end_date, as_of=as_of)

    async def _market_day_query(
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
            paginate=False,
        )

    async def _dated_stock_query(
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
