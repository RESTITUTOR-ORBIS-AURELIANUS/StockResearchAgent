"""八个源数据 Service 对 89 个 Provider 接口的唯一归属。"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from stock_research_agent.providers.routes import SUPPORTED_APIS


@dataclass(frozen=True, slots=True)
class ApiSpec:
    api_name: str
    purpose: str
    fields: tuple[str, ...] = ()
    as_of_fields: tuple[str, ...] = ()
    identity_fields: tuple[str, ...] = ()
    historical_as_of_safe: bool = True
    supports_offset_pagination: bool = True


def _spec(
    api_name: str,
    purpose: str,
    fields: str = "",
    *,
    as_of_fields: tuple[str, ...] = (),
    identity_fields: tuple[str, ...] = (),
    historical_as_of_safe: bool = True,
    supports_offset_pagination: bool = True,
) -> ApiSpec:
    return ApiSpec(
        api_name=api_name,
        purpose=purpose,
        fields=tuple(field for field in fields.split(",") if field),
        as_of_fields=as_of_fields,
        identity_fields=identity_fields,
        historical_as_of_safe=historical_as_of_safe,
        supports_offset_pagination=supports_offset_pagination,
    )


INSTRUMENT_REFERENCE_SPECS = MappingProxyType(
    {
        "trade_cal": _spec(
            "trade_cal",
            "交易日历",
            "exchange,cal_date,is_open,pretrade_date",
            as_of_fields=("cal_date",),
            identity_fields=("exchange", "cal_date"),
        ),
        "stock_basic": _spec(
            "stock_basic",
            "股票身份与上市信息",
            "ts_code,name,industry,market,list_date",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "etf_basic": _spec(
            "etf_basic",
            "ETF 基础信息",
            "ts_code,csname,list_date",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "etf_index": _spec(
            "etf_index",
            "ETF 基准指数目录",
            "ts_code,name",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "opt_basic": _spec(
            "opt_basic",
            "期权合约目录",
            "ts_code,name",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "fund_basic": _spec(
            "fund_basic",
            "公募基金目录",
            "ts_code,name",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "index_basic": _spec(
            "index_basic",
            "指数目录",
            "ts_code,name",
            identity_fields=("ts_code",),
            historical_as_of_safe=False,
        ),
        "index_classify": _spec(
            "index_classify",
            "申万行业分类目录",
            "index_code,industry_name,level,industry_code,is_pub,parent_code,src",
            identity_fields=("index_code",),
            historical_as_of_safe=False,
            supports_offset_pagination=False,
        ),
        "index_member_all": _spec(
            "index_member_all",
            "申万行业完整成分",
            "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new",
            identity_fields=("l1_code", "l2_code", "l3_code", "ts_code", "is_new"),
            historical_as_of_safe=False,
            supports_offset_pagination=False,
        ),
        "index_weight": _spec(
            "index_weight",
            "指数成分与权重",
            "index_code,con_code,trade_date,weight",
            as_of_fields=("trade_date",),
            identity_fields=("index_code", "con_code", "trade_date"),
            supports_offset_pagination=False,
        ),
        "namechange": _spec(
            "namechange",
            "股票曾用名历史",
            "ts_code,name,start_date,end_date,change_reason",
            as_of_fields=("start_date",),
            identity_fields=("ts_code", "name", "start_date"),
        ),
        "new_share": _spec(
            "new_share",
            "IPO 新股发行记录",
            "ts_code,name,ipo_date,issue_date",
            as_of_fields=("ipo_date",),
            identity_fields=("ts_code", "ipo_date"),
        ),
        "stock_st": _spec(
            "stock_st",
            "ST 股票名单",
            "ts_code,name,trade_date,type,type_name",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date", "type"),
        ),
        "stock_hsgt": _spec(
            "stock_hsgt",
            "沪深港通股票名单",
            "ts_code,name,type",
            identity_fields=("ts_code", "type"),
            historical_as_of_safe=False,
        ),
    }
)


EQUITY_MARKET_DATA_SPECS = MappingProxyType(
    {
        name: _spec(
            name,
            purpose,
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        )
        for name, purpose in (
            ("daily", "股票日线行情"),
            ("weekly", "股票周线行情"),
            ("monthly", "股票月线行情"),
            ("index_daily", "指数日线行情"),
        )
    }
    | {
        "adj_factor": _spec(
            "adj_factor",
            "股票复权因子",
            "ts_code,trade_date,adj_factor",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "daily_basic": _spec(
            "daily_basic",
            "股票估值与交易指标",
            "ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,dv_ttm,total_mv,circ_mv",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "stk_limit": _spec(
            "stk_limit",
            "每日涨跌停价格",
            "ts_code,trade_date,pre_close,up_limit,down_limit",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "suspend_d": _spec(
            "suspend_d",
            "每日停复牌记录",
            "ts_code,trade_date,suspend_timing,suspend_type",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date", "suspend_timing"),
        ),
        "index_dailybasic": _spec(
            "index_dailybasic",
            "指数每日指标",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "sw_daily": _spec(
            "sw_daily",
            "申万行业指数日线",
            "ts_code,trade_date,name,open,low,high,close,change,pct_change,vol,amount,pe,pb,float_mv,total_mv",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
    }
)


CROSS_ASSET_MARKET_DATA_SPECS = MappingProxyType(
    {
        "fund_daily": _spec(
            "fund_daily",
            "基金和 ETF 日线行情",
            "ts_code,trade_date,open,high,low,close,vol,amount",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "fund_adj": _spec(
            "fund_adj",
            "基金复权因子",
            "ts_code,trade_date,adj_factor",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "etf_share_size": _spec(
            "etf_share_size",
            "ETF 份额规模",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "opt_daily": _spec(
            "opt_daily",
            "期权日线行情",
            "ts_code,trade_date,pre_settle,pre_close,open,high,low,close,settle,vol,amount,oi",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
        "cb_daily": _spec(
            "cb_daily",
            "可转债日线行情",
            "ts_code,trade_date,pre_close,open,high,low,close,change,pct_chg,vol,amount",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date"),
        ),
    }
)


FUNDAMENTAL_DATA_SPECS = MappingProxyType(
    {
        "income": _spec(
            "income",
            "利润表",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_revenue,n_income_attr_p,basic_eps,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "income_vip": _spec(
            "income_vip",
            "全市场利润表批量同步",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_revenue,n_income_attr_p,basic_eps,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "balancesheet": _spec(
            "balancesheet",
            "资产负债表",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_assets,total_liab,total_hldr_eqy_exc_min_int,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "balancesheet_vip": _spec(
            "balancesheet_vip",
            "全市场资产负债表批量同步",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_assets,total_liab,total_hldr_eqy_exc_min_int,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "cashflow": _spec(
            "cashflow",
            "现金流量表",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "cashflow_vip": _spec(
            "cashflow_vip",
            "全市场现金流量表批量同步",
            "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,update_flag",
            as_of_fields=("ann_date", "f_ann_date"),
        ),
        "forecast": _spec(
            "forecast",
            "业绩预告",
            "ts_code,ann_date,end_date,type,p_change_min,p_change_max",
            as_of_fields=("ann_date",),
        ),
        "forecast_vip": _spec(
            "forecast_vip",
            "全市场业绩预告批量同步",
            "ts_code,ann_date,end_date,type,p_change_min,p_change_max",
            as_of_fields=("ann_date",),
        ),
        "express": _spec(
            "express",
            "业绩快报",
            "ts_code,ann_date,end_date,revenue",
            as_of_fields=("ann_date",),
        ),
        "express_vip": _spec(
            "express_vip",
            "全市场业绩快报批量同步",
            "ts_code,ann_date,end_date,revenue",
            as_of_fields=("ann_date",),
        ),
        "dividend": _spec(
            "dividend",
            "分红送股记录",
            "ts_code,end_date,ann_date,div_proc,cash_div_tax,record_date,ex_date",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "end_date", "div_proc"),
        ),
        "fina_indicator": _spec(
            "fina_indicator",
            "财务指标",
            "ts_code,ann_date,end_date,roe,roa,grossprofit_margin,debt_to_assets,ocf_to_or,update_flag",
            as_of_fields=("ann_date",),
        ),
        "fina_indicator_vip": _spec(
            "fina_indicator_vip",
            "全市场财务指标批量同步",
            "ts_code,ann_date,end_date,roe,roa,grossprofit_margin,debt_to_assets,ocf_to_or,update_flag",
            as_of_fields=("ann_date",),
        ),
        "fina_audit": _spec(
            "fina_audit",
            "财务审计意见",
            "ts_code,ann_date,end_date,audit_result,audit_agency",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "end_date"),
        ),
        "fina_mainbz": _spec(
            "fina_mainbz",
            "主营业务构成",
            "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost",
            as_of_fields=("end_date",),
            identity_fields=("ts_code", "end_date", "bz_item"),
            historical_as_of_safe=False,
        ),
        "fina_mainbz_vip": _spec(
            "fina_mainbz_vip",
            "全市场主营业务构成批量同步",
            "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost",
            as_of_fields=("end_date",),
            identity_fields=("ts_code", "end_date", "bz_item"),
            historical_as_of_safe=False,
        ),
        "disclosure_date": _spec(
            "disclosure_date",
            "财报披露计划",
            "ts_code,ann_date,end_date,pre_date,actual_date,modify_date",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "end_date", "ann_date"),
        ),
    }
)


MACRO_DATA_SPECS = MappingProxyType(
    {
        "eco_cal": _spec("eco_cal", "财经日历", as_of_fields=("date", "trade_date")),
        "shibor": _spec("shibor", "Shibor 利率", as_of_fields=("date",)),
        "shibor_quote": _spec("shibor_quote", "Shibor 报价", as_of_fields=("date",)),
        "shibor_lpr": _spec("shibor_lpr", "LPR 贷款市场报价利率", as_of_fields=("date",)),
        "libor": _spec("libor", "Libor 利率", as_of_fields=("date",)),
        "hibor": _spec("hibor", "Hibor 利率", as_of_fields=("date",)),
        "wz_index": _spec("wz_index", "温州民间借贷利率", as_of_fields=("date",)),
        "gz_index": _spec("gz_index", "广州民间借贷利率", as_of_fields=("date",)),
        "cn_gdp": _spec(
            "cn_gdp",
            "中国 GDP",
            as_of_fields=("quarter",),
            historical_as_of_safe=False,
        ),
        "cn_cpi": _spec(
            "cn_cpi",
            "中国 CPI",
            as_of_fields=("month",),
            historical_as_of_safe=False,
        ),
        "cn_ppi": _spec(
            "cn_ppi",
            "中国 PPI",
            as_of_fields=("month",),
            historical_as_of_safe=False,
        ),
        "cn_m": _spec(
            "cn_m",
            "中国货币供应量",
            as_of_fields=("month",),
            historical_as_of_safe=False,
        ),
        "sf_month": _spec(
            "sf_month",
            "中国社会融资规模",
            as_of_fields=("month",),
            historical_as_of_safe=False,
        ),
        "cn_pmi": _spec(
            "cn_pmi",
            "中国 PMI",
            as_of_fields=("month",),
            historical_as_of_safe=False,
        ),
        "us_tycr": _spec("us_tycr", "美国国债收益率曲线", as_of_fields=("date",)),
        "us_trycr": _spec("us_trycr", "美国国债实际收益率曲线", as_of_fields=("date",)),
        "us_tbr": _spec("us_tbr", "美国短期国债利率", as_of_fields=("date",)),
        "us_tltr": _spec("us_tltr", "美国长期国债利率", as_of_fields=("date",)),
        "us_trltr": _spec("us_trltr", "美国实际长期利率", as_of_fields=("date",)),
    }
)


OWNERSHIP_EVENT_SPECS = MappingProxyType(
    {
        "top10_holders": _spec(
            "top10_holders",
            "前十大股东",
            "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,hold_change",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "end_date", "holder_name"),
        ),
        "top10_floatholders": _spec(
            "top10_floatholders",
            "前十大流通股东",
            "ts_code,ann_date,end_date,holder_name,hold_amount,hold_float_ratio,hold_change",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "end_date", "holder_name"),
        ),
        "pledge_stat": _spec(
            "pledge_stat",
            "股权质押统计",
            "ts_code,end_date,pledge_count,total_share,pledge_ratio",
            as_of_fields=("end_date",),
            identity_fields=("ts_code", "end_date"),
            historical_as_of_safe=False,
        ),
        "pledge_detail": _spec(
            "pledge_detail",
            "股权质押明细",
            as_of_fields=("ann_date",),
        ),
        "repurchase": _spec(
            "repurchase",
            "股票回购事件",
            "ts_code,ann_date,proc,vol,amount,high_limit,low_limit",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "proc", "vol", "amount"),
        ),
        "share_float": _spec(
            "share_float",
            "限售股解禁计划",
            "ts_code,ann_date,float_date,float_share,float_ratio,holder_name",
            as_of_fields=("ann_date",),
            identity_fields=(
                "ts_code",
                "ann_date",
                "float_date",
                "holder_name",
                "float_share",
            ),
        ),
        "stk_holdernumber": _spec(
            "stk_holdernumber",
            "股东人数",
            "ts_code,ann_date,end_date,holder_num",
            as_of_fields=("ann_date",),
            identity_fields=("ts_code", "ann_date", "end_date"),
        ),
        "stk_holdertrade": _spec(
            "stk_holdertrade",
            "股东增减持",
            "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,avg_price",
            as_of_fields=("ann_date",),
            identity_fields=(
                "ts_code",
                "ann_date",
                "holder_name",
                "in_de",
                "change_vol",
                "avg_price",
            ),
        ),
    }
)


TRADING_BEHAVIOR_SPECS = MappingProxyType(
    {
        "hk_hold": _spec(
            "hk_hold",
            "沪深港股通持股明细",
            "ts_code,trade_date,name,vol,ratio,exchange",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date", "exchange"),
        ),
        "block_trade": _spec(
            "block_trade",
            "大宗交易",
            "ts_code,trade_date,price,vol,amount,buyer,seller",
            as_of_fields=("trade_date",),
        ),
        "top_list": _spec(
            "top_list",
            "龙虎榜每日明细",
            "trade_date,ts_code,name,pct_change,l_buy,l_sell,net_amount,reason",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date", "reason"),
        ),
        "top_inst": _spec(
            "top_inst",
            "龙虎榜机构席位",
            "trade_date,ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy",
            as_of_fields=("trade_date",),
            identity_fields=("ts_code", "trade_date", "exalter"),
        ),
        "margin": _spec(
            "margin",
            "融资融券市场汇总",
            "trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rzrqye",
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "exchange_id"),
        ),
        "margin_detail": _spec(
            "margin_detail",
            "个股融资融券明细",
            "trade_date,ts_code,rzye,rqye,rzmre,rzche,rqmcl,rzrqye",
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code"),
        ),
        "margin_secs": _spec(
            "margin_secs",
            "融资融券标的名单",
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code"),
        ),
        "moneyflow_ths": _spec(
            "moneyflow_ths",
            "同花顺个股资金流向",
            (
                "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,"
                "buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,"
                "buy_sm_amount,buy_sm_amount_rate"
            ),
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code"),
        ),
        "moneyflow_dc": _spec(
            "moneyflow_dc",
            "东财个股资金流向",
            (
                "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,"
                "buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
                "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate"
            ),
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code"),
        ),
        "moneyflow_ind_ths": _spec(
            "moneyflow_ind_ths",
            "同花顺行业资金流向",
            (
                "trade_date,ts_code,industry,lead_stock,close,pct_change,company_num,"
                "pct_change_stock,close_price,net_buy_amount,net_sell_amount,net_amount"
            ),
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code"),
        ),
        "moneyflow_mkt_dc": _spec(
            "moneyflow_mkt_dc",
            "东财大盘资金流向",
            (
                "trade_date,close_sh,pct_change_sh,close_sz,pct_change_sz,net_amount,"
                "net_amount_rate,buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,"
                "buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,buy_sm_amount,"
                "buy_sm_amount_rate"
            ),
            as_of_fields=("trade_date",),
            identity_fields=("trade_date",),
        ),
        "moneyflow_hsgt": _spec(
            "moneyflow_hsgt",
            "沪深港通每日资金流向",
            "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money",
            as_of_fields=("trade_date",),
            identity_fields=("trade_date",),
        ),
        "limit_list_d": _spec(
            "limit_list_d",
            "涨跌停和炸板数据",
            (
                "trade_date,ts_code,industry,name,close,pct_chg,amount,limit_amount,"
                "float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,"
                "open_times,up_stat,limit_times,limit"
            ),
            as_of_fields=("trade_date",),
            identity_fields=("trade_date", "ts_code", "limit"),
        ),
    }
)


NEWS_EVENT_DATA_SPECS = MappingProxyType(
    {
        "major_news": _spec(
            "major_news",
            "新闻通讯正文",
            "title,content,pub_time,src",
            as_of_fields=("pub_time",),
            identity_fields=("src", "pub_time", "title"),
            supports_offset_pagination=False,
        ),
        "report_rc": _spec(
            "report_rc",
            "卖方研报盈利预测与评级摘要",
            (
                "ts_code,name,report_date,report_title,report_type,classify,org_name,"
                "author_name,quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,"
                "rating,max_price,min_price,imp_dg,create_time"
            ),
            as_of_fields=("report_date",),
            identity_fields=(
                "ts_code",
                "report_date",
                "org_name",
                "author_name",
                "report_title",
                "quarter",
            ),
            supports_offset_pagination=False,
        ),
        "broker_recommend": _spec(
            "broker_recommend",
            "券商月度金股名单",
            "month,broker,ts_code,name",
            identity_fields=("month", "broker", "ts_code"),
            # 只有月份而没有精确发布日期，禁止用月末抓取结果回放月初状态。
            historical_as_of_safe=False,
            supports_offset_pagination=False,
        ),
    }
)


SERVICE_API_GROUPS: Final = MappingProxyType(
    {
        "InstrumentReferenceService": INSTRUMENT_REFERENCE_SPECS,
        "EquityMarketDataService": EQUITY_MARKET_DATA_SPECS,
        "CrossAssetMarketDataService": CROSS_ASSET_MARKET_DATA_SPECS,
        "FundamentalDataService": FUNDAMENTAL_DATA_SPECS,
        "MacroDataService": MACRO_DATA_SPECS,
        "OwnershipEventService": OWNERSHIP_EVENT_SPECS,
        "TradingBehaviorService": TRADING_BEHAVIOR_SPECS,
        "NewsEventDataService": NEWS_EVENT_DATA_SPECS,
    }
)


_all_declared = [api_name for group in SERVICE_API_GROUPS.values() for api_name in group]
if len(_all_declared) != len(set(_all_declared)):
    raise RuntimeError("数据 Service 的接口分组存在重复")
if set(_all_declared) != set(SUPPORTED_APIS):
    missing = sorted(SUPPORTED_APIS.difference(_all_declared))
    extra = sorted(set(_all_declared).difference(SUPPORTED_APIS))
    raise RuntimeError(f"数据 Service 分组与 Provider 清单不一致：missing={missing}, extra={extra}")
