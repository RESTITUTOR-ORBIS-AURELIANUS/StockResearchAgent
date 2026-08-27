"""Provider 支持的接口清单与统一故障转移参数。"""

from stock_research_agent.providers.errors import (
    ProviderErrorCode,
    UnknownProviderApiError,
)

SUPPORTED_APIS = frozenset(
    {
        "adj_factor",
        "balancesheet",
        "balancesheet_vip",
        "block_trade",
        "cashflow",
        "cashflow_vip",
        "cb_daily",
        "cn_cpi",
        "cn_gdp",
        "cn_m",
        "cn_pmi",
        "cn_ppi",
        "daily",
        "daily_basic",
        "disclosure_date",
        "dividend",
        "eco_cal",
        "etf_basic",
        "etf_index",
        "etf_share_size",
        "express",
        "express_vip",
        "fina_audit",
        "fina_indicator",
        "fina_indicator_vip",
        "fina_mainbz",
        "fina_mainbz_vip",
        "forecast",
        "forecast_vip",
        "fund_adj",
        "fund_basic",
        "fund_daily",
        "gz_index",
        "hibor",
        "hk_hold",
        "income",
        "income_vip",
        "index_basic",
        "index_classify",
        "index_daily",
        "index_dailybasic",
        "index_member_all",
        "index_weight",
        "libor",
        "margin",
        "margin_detail",
        "margin_secs",
        "major_news",
        "limit_list_d",
        "moneyflow_dc",
        "moneyflow_hsgt",
        "moneyflow_ind_ths",
        "moneyflow_mkt_dc",
        "moneyflow_ths",
        "monthly",
        "namechange",
        "new_share",
        "opt_basic",
        "opt_daily",
        "pledge_detail",
        "pledge_stat",
        "repurchase",
        "report_rc",
        "sf_month",
        "share_float",
        "shibor",
        "shibor_lpr",
        "shibor_quote",
        "stk_holdernumber",
        "stk_holdertrade",
        "stk_limit",
        "stock_basic",
        "stock_hsgt",
        "broker_recommend",
        "stock_st",
        "suspend_d",
        "sw_daily",
        "top10_floatholders",
        "top10_holders",
        "top_inst",
        "top_list",
        "trade_cal",
        "us_tbr",
        "us_tltr",
        "us_trltr",
        "us_trycr",
        "us_tycr",
        "weekly",
        "wz_index",
    }
)

# 主服务器每次都会优先尝试。仅当主服务器失败时，才读取或写入备用结果缓存。
BACKUP_CACHE_TTL_SECONDS = 3_600


def ensure_supported_api(api_name: str) -> None:
    """在发出网络请求前拒绝未声明的接口名。"""

    if api_name not in SUPPORTED_APIS:
        raise UnknownProviderApiError(
            ProviderErrorCode.UNKNOWN_API,
            api_name,
            "支持清单中没有这个接口",
        )


if len(SUPPORTED_APIS) != 89:
    raise RuntimeError(f"Provider 支持清单应包含 89 项，实际为 {len(SUPPORTED_APIS)} 项")
