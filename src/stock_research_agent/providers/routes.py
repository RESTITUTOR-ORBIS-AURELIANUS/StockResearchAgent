"""根据 2026-08-14 实测结果固定主备路由。"""

from dataclasses import dataclass
from enum import StrEnum

from stock_research_agent.providers.errors import ProviderErrorCode, UnknownProviderApiError


class RouteMode(StrEnum):
    PRIMARY = "PRIMARY"
    PRIMARY_CACHED = "PRIMARY_CACHED"
    BACKUP = "BACKUP"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RoutePolicy:
    mode: RouteMode
    cache_ttl_seconds: int | None = None


PRIMARY_APIS = frozenset(
    {
        "trade_cal",
        "stock_basic",
        "etf_basic",
        "opt_basic",
        "index_basic",
        "namechange",
        "new_share",
        "daily",
        "weekly",
        "monthly",
        "stk_limit",
        "index_dailybasic",
        "opt_daily",
        "hk_hold",
        "express",
        "dividend",
        "fina_audit",
        "shibor_quote",
        "shibor_lpr",
        "hibor",
        "wz_index",
        "gz_index",
        "cn_gdp",
        "cn_cpi",
        "cn_ppi",
        "sf_month",
        "cn_pmi",
        "us_tycr",
        "us_trycr",
        "us_tbr",
        "us_tltr",
        "us_trltr",
        "top10_holders",
        "top10_floatholders",
        "repurchase",
        "stk_holdernumber",
        "top_inst",
    }
)

PRIMARY_CACHED_TTLS = {
    "adj_factor": 3_600,
    "daily_basic": 3_600,
    "index_daily": 3_600,
    "share_float": 86_400,
    "shibor": 86_400,
}

BACKUP_APIS = frozenset(
    {
        "etf_index",
        "fund_basic",
        "suspend_d",
        "fund_daily",
        "fund_adj",
        "cb_daily",
        "income",
        "income_vip",
        "balancesheet",
        "balancesheet_vip",
        "cashflow",
        "cashflow_vip",
        "forecast",
        "forecast_vip",
        "express_vip",
        "fina_indicator",
        "fina_indicator_vip",
        "fina_mainbz",
        "fina_mainbz_vip",
        "disclosure_date",
        "eco_cal",
        "libor",
        "cn_m",
        "stock_st",
        "stock_hsgt",
        "pledge_stat",
        "pledge_detail",
        "block_trade",
        "stk_holdertrade",
        "top_list",
        "margin",
        "margin_detail",
        "margin_secs",
    }
)

UNAVAILABLE_APIS = frozenset({"etf_share_size"})
BACKUP_CACHE_TTL_SECONDS = 3_600


def _build_routes() -> dict[str, RoutePolicy]:
    groups = [PRIMARY_APIS, frozenset(PRIMARY_CACHED_TTLS), BACKUP_APIS, UNAVAILABLE_APIS]
    if sum(len(group) for group in groups) != len(set().union(*groups)):
        raise RuntimeError("Provider 路由表存在重复 api_name")

    routes = {api: RoutePolicy(RouteMode.PRIMARY) for api in PRIMARY_APIS}
    routes.update(
        {
            api: RoutePolicy(RouteMode.PRIMARY_CACHED, ttl)
            for api, ttl in PRIMARY_CACHED_TTLS.items()
        }
    )
    routes.update(
        {api: RoutePolicy(RouteMode.BACKUP, BACKUP_CACHE_TTL_SECONDS) for api in BACKUP_APIS}
    )
    routes.update({api: RoutePolicy(RouteMode.UNAVAILABLE) for api in UNAVAILABLE_APIS})
    if len(routes) != 76:
        raise RuntimeError(f"Provider 路由表应包含 76 项，实际为 {len(routes)} 项")
    return routes


ROUTES = _build_routes()


def get_route(api_name: str) -> RoutePolicy:
    try:
        return ROUTES[api_name]
    except KeyError as exc:
        raise UnknownProviderApiError(
            ProviderErrorCode.UNKNOWN_API,
            api_name,
            "路由表中没有这个接口",
        ) from exc
