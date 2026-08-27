"""公司财务报表、业绩事件与披露 Service。"""

from datetime import date, datetime
from typing import Literal

from stock_research_agent.services.base import (
    AsOfValue,
    BaseDataService,
    matches_code,
    matches_date_range,
    validate_choice,
    validate_observation_end,
    validate_report_period,
    validate_security_code,
)
from stock_research_agent.services.catalog import FUNDAMENTAL_DATA_SPECS
from stock_research_agent.services.errors import ServiceInputError
from stock_research_agent.services.models import ServiceDataset

BusinessCompositionType = Literal["P", "D", "I"]


class FundamentalDataService(BaseDataService):
    """负责财务报表、财务指标、业绩事件、分红与披露日程。"""

    API_SPECS = FUNDAMENTAL_DATA_SPECS

    async def get_income_statement(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("income", ts_code, period, as_of=as_of)

    async def get_income_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("income_vip", period, as_of=as_of)

    async def get_balance_sheet(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("balancesheet", ts_code, period, as_of=as_of)

    async def get_balance_sheet_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("balancesheet_vip", period, as_of=as_of)

    async def get_cash_flow_statement(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("cashflow", ts_code, period, as_of=as_of)

    async def get_cash_flow_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("cashflow_vip", period, as_of=as_of)

    async def get_earnings_forecast(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("forecast", ts_code, period, as_of=as_of)

    async def get_earnings_forecast_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("forecast_vip", period, as_of=as_of)

    async def get_earnings_express(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        """兼容端的 express 按报告期返回全市场，随后在本地筛选股票。"""

        normalized_code = validate_security_code(ts_code)
        normalized_period = validate_report_period(period)
        period_date = datetime.strptime(normalized_period, "%Y%m%d").date()
        code_filter = matches_code(normalized_code)
        period_filter = matches_date_range("end_date", period_date, period_date)
        return await self._query(
            "express",
            {"period": normalized_period},
            as_of=as_of,
            row_filter=lambda row: code_filter(row) and period_filter(row),
        )

    async def get_earnings_express_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("express_vip", period, as_of=as_of)

    async def get_dividends(
        self,
        ts_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        if (start_date is None) != (end_date is None):
            raise ServiceInputError("start_date 和 end_date 必须同时提供")

        row_filter = None
        if start_date is not None and end_date is not None:
            validate_observation_end(end_date, as_of)
            row_filter = matches_date_range("ann_date", start_date, end_date)

        return await self._query(
            "dividend",
            {"ts_code": validate_security_code(ts_code)},
            as_of=as_of,
            row_filter=row_filter,
        )

    async def get_financial_indicators(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("fina_indicator", ts_code, period, as_of=as_of)

    async def get_financial_indicators_batch(
        self,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._period_batch_query("fina_indicator_vip", period, as_of=as_of)

    async def get_audit_opinion(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._single_stock_period_query("fina_audit", ts_code, period, as_of=as_of)

    async def get_business_composition(
        self,
        ts_code: str,
        period: str,
        *,
        composition_type: BusinessCompositionType = "P",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "fina_mainbz",
            {
                "ts_code": validate_security_code(ts_code),
                "period": validate_report_period(period),
                "type": validate_choice(
                    composition_type, frozenset({"P", "D", "I"}), "composition_type"
                ),
            },
            as_of=as_of,
        )

    async def get_business_composition_batch(
        self,
        period: str,
        *,
        composition_type: BusinessCompositionType = "P",
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        return await self._query(
            "fina_mainbz_vip",
            {
                "period": validate_report_period(period),
                "type": validate_choice(
                    composition_type, frozenset({"P", "D", "I"}), "composition_type"
                ),
            },
            as_of=as_of,
        )

    async def get_disclosure_schedule(
        self,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue = None,
    ) -> ServiceDataset:
        normalized_code = validate_security_code(ts_code)
        normalized_period = validate_report_period(period)
        period_date = datetime.strptime(normalized_period, "%Y%m%d").date()
        code_filter = matches_code(normalized_code)
        period_filter = matches_date_range("end_date", period_date, period_date)
        return await self._query(
            "disclosure_date",
            {
                "ts_code": normalized_code,
                "end_date": normalized_period,
            },
            as_of=as_of,
            row_filter=lambda row: code_filter(row) and period_filter(row),
        )

    async def _single_stock_period_query(
        self,
        api_name: str,
        ts_code: str,
        period: str,
        *,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        normalized_code = validate_security_code(ts_code)
        normalized_period = validate_report_period(period)
        period_date = datetime.strptime(normalized_period, "%Y%m%d").date()
        code_filter = matches_code(normalized_code)
        period_filter = matches_date_range("end_date", period_date, period_date)
        return await self._query(
            api_name,
            {
                "ts_code": normalized_code,
                "period": normalized_period,
            },
            as_of=as_of,
            row_filter=lambda row: code_filter(row) and period_filter(row),
        )

    async def _period_batch_query(
        self,
        api_name: str,
        period: str,
        *,
        as_of: AsOfValue,
    ) -> ServiceDataset:
        return await self._query(
            api_name,
            {"period": validate_report_period(period)},
            as_of=as_of,
        )
