"""基金、ETF、期权与可转债行情 Service。"""

from datetime import date

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    validate_date_range,
    validate_non_empty,
    validate_observation_end,
    validate_security_code,
)
from stock_research_agent.services.catalog import CROSS_ASSET_MARKET_DATA_SPECS
from stock_research_agent.services.models import ServiceDataset


class CrossAssetMarketDataService(BaseDataService):
    """为大盘环境和跨资产验证提供基金、期权、转债数据。"""

    API_SPECS = CROSS_ASSET_MARKET_DATA_SPECS

    async def get_fund_bars(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_security_query(
            "fund_daily", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_fund_adjustment_factors(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_security_query(
            "fund_adj", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_etf_share_history(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._dated_security_query(
            "etf_share_size", ts_code, start_date, end_date, as_of=as_of
        )

    async def get_option_daily(
        self,
        trade_date: date,
        *,
        exchange: str,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "opt_daily",
            {
                "trade_date": format_date(trade_date),
                "exchange": validate_non_empty(exchange, "exchange"),
            },
            as_of=as_of,
        )

    async def get_convertible_bond_daily(
        self,
        trade_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(trade_date, as_of)
        return await self._query(
            "cb_daily",
            {"trade_date": format_date(trade_date)},
            as_of=as_of,
        )

    async def _dated_security_query(
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
