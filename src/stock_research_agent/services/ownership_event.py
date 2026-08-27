"""股东、质押、回购、解禁与增减持 Service。"""

from datetime import date

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    format_date,
    matches_code,
    matches_date_range,
    validate_date_range,
    validate_observation_end,
    validate_report_period,
    validate_security_code,
)
from stock_research_agent.services.catalog import OWNERSHIP_EVENT_SPECS
from stock_research_agent.services.models import ServiceDataset


class OwnershipEventService(BaseDataService):
    """负责所有权结构、股权质押以及股本供给变化事件。"""

    API_SPECS = OWNERSHIP_EVENT_SPECS

    async def get_top_holders(
        self,
        ts_code: str,
        period: str,
        *,
        floating: bool = False,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        api_name = "top10_floatholders" if floating else "top10_holders"
        return await self._query(
            api_name,
            {
                "ts_code": validate_security_code(ts_code),
                "period": validate_report_period(period),
            },
            as_of=as_of,
        )

    async def get_pledge_statistics(
        self,
        ts_code: str,
        *,
        end_date: date | None = None,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        params: dict[str, str] = {"ts_code": validate_security_code(ts_code)}
        if end_date is not None:
            params["end_date"] = format_date(end_date)
        return await self._query("pledge_stat", params, as_of=as_of)

    async def get_pledge_details(
        self,
        ts_code: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "pledge_detail",
            {"ts_code": validate_security_code(ts_code)},
            as_of=as_of,
        )

    async def get_repurchase_events(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        return await self._full_market_event_query(
            "repurchase",
            ts_code,
            start_date,
            end_date,
            date_field="ann_date",
            as_of=as_of,
        )

    async def get_unlock_events(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """日期表示计划解禁日范围；可以晚于 as_of，但公告日不能晚于 as_of。"""

        return await self._stock_event_query(
            "share_float",
            ts_code,
            start_date,
            end_date,
            date_field="float_date",
            as_of=as_of,
        )

    async def get_holder_counts(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        start, end = validate_date_range(start_date, end_date)
        code_filter = matches_code(ts_code)
        date_filter = matches_date_range("ann_date", start_date, end_date)
        return await self._query(
            "stk_holdernumber",
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
            row_filter=lambda row: code_filter(row) and date_filter(row),
        )

    async def get_holder_trades(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        validate_observation_end(end_date, as_of)
        return await self._stock_event_query(
            "stk_holdertrade",
            ts_code,
            start_date,
            end_date,
            date_field="ann_date",
            as_of=as_of,
        )

    async def _full_market_event_query(
        self,
        api_name: str,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        date_field: str,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        """兼容端不接收 ts_code 的事件接口：按日期取全市场后本地筛选。"""

        start, end = validate_date_range(start_date, end_date)
        code_filter = matches_code(ts_code)
        date_filter = matches_date_range(date_field, start_date, end_date)
        return await self._query(
            api_name,
            {"start_date": start, "end_date": end},
            as_of=as_of,
            row_filter=lambda row: code_filter(row) and date_filter(row),
        )

    async def _stock_event_query(
        self,
        api_name: str,
        ts_code: str,
        start_date: date,
        end_date: date,
        *,
        date_field: str,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        start, end = validate_date_range(start_date, end_date)
        code_filter = matches_code(ts_code)
        date_filter = matches_date_range(date_field, start_date, end_date)
        return await self._query(
            api_name,
            {
                "ts_code": validate_security_code(ts_code),
                "start_date": start,
                "end_date": end,
            },
            as_of=as_of,
            # 即使兼容端忽略 ts_code，本地仍做一次确定性保护。
            row_filter=lambda row: code_filter(row) and date_filter(row),
        )
