"""宏观经济、利率与财经日历 Service。"""

from datetime import date
from typing import Literal, get_args

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    validate_date_range,
    validate_month,
    validate_observation_end,
    validate_quarter,
)
from stock_research_agent.services.catalog import MACRO_DATA_SPECS
from stock_research_agent.services.errors import ServiceInputError
from stock_research_agent.services.models import ServiceDataset

RateRangeSeries = Literal[
    "shibor",
    "shibor_lpr",
    "wz_index",
    "gz_index",
    "us_tycr",
    "us_trycr",
    "us_tbr",
    "us_tltr",
    "us_trltr",
]
RateSnapshotSeries = Literal["shibor_quote", "libor", "hibor"]
MonthlyMacroSeries = Literal["cn_cpi", "cn_ppi", "cn_m", "sf_month", "cn_pmi"]

_RATE_RANGE_SERIES = frozenset(get_args(RateRangeSeries))
_RATE_SNAPSHOT_SERIES = frozenset(get_args(RateSnapshotSeries))
_MONTHLY_MACRO_SERIES = frozenset(get_args(MonthlyMacroSeries))


class MacroDataService(BaseDataService):
    """负责宏观日历、国内外利率和中国月度/季度宏观指标。"""

    API_SPECS = MACRO_DATA_SPECS

    async def get_economic_calendar(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            "eco_cal",
            {"start_date": start, "end_date": end},
            as_of=as_of,
        )

    async def get_rate_range(
        self,
        series: RateRangeSeries,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        if series not in _RATE_RANGE_SERIES:
            raise ServiceInputError(f"{series} 不是区间利率序列")
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        return await self._query(
            series,
            {"start_date": start, "end_date": end},
            as_of=as_of,
        )

    async def get_rate_snapshot(
        self,
        series: RateSnapshotSeries,
        observation_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        if series not in _RATE_SNAPSHOT_SERIES:
            raise ServiceInputError(f"{series} 不是单日利率序列")
        validate_observation_end(observation_date, as_of)
        return await self._query(
            series,
            {"date": format_date(observation_date)},
            as_of=as_of,
        )

    async def get_gdp(
        self,
        start_quarter: str,
        end_quarter: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        start = validate_quarter(start_quarter)
        end = validate_quarter(end_quarter)
        if start > end:
            raise ServiceInputError("start_quarter 不能晚于 end_quarter")
        return await self._query(
            "cn_gdp",
            {"start_q": start, "end_q": end},
            as_of=as_of,
        )

    async def get_monthly_indicator(
        self,
        series: MonthlyMacroSeries,
        start_month: str,
        end_month: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        if series not in _MONTHLY_MACRO_SERIES:
            raise ServiceInputError(f"{series} 不是月度宏观序列")
        start = validate_month(start_month)
        end = validate_month(end_month)
        if start > end:
            raise ServiceInputError("start_month 不能晚于 end_month")
        return await self._query(
            series,
            {"start_m": start, "end_m": end},
            as_of=as_of,
        )
