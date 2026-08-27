# StockResearchAgent Data Service 调用速查

> 代码位置：`src/stock_research_agent/services/`

本文是“怎么调用”的速查表；分页、主备回退、字段白名单和 `as_of` 规则的设计说明见
[`data-services-v1.md`](data-services-v1.md)。

## 1. 统一调用规则

所有公开方法都是异步方法：

```python
dataset = await services.equity_market_data.get_stock_bars(
    "000001.SZ",
    date(2026, 8, 1),
    date(2026, 8, 20),
    frequency="daily",
    as_of=frozen_as_of,
)
```

统一规则如下：

- `ts_code` 使用 `000001.SZ`、`600519.SH` 这样的 Tushare 代码；
- `date` 参数使用 Python `datetime.date`，不要传裸字符串；
- `period` 使用 `YYYYMMDD`，例如 `20251231`；
- `month` 使用 `YYYYMM`，`quarter` 使用 `YYYYQ1`～`YYYYQ4`；
- 除新闻外，`as_of` 可以是 `date` 或带时区 `datetime`；无时区 `datetime` 会被拒绝；
- 新闻的 `start_at/end_at/as_of` 必须是带时区 `datetime`；
- 每个方法返回一个 `ServiceDataset`，且只装一个 Provider API 的数据；
- `code=0` 且 `items=[]` 是合法空表，与异常不同；
- Tool 层会固定注入 `as_of`，LLM 不会直接调用下表或填写 `as_of`；
- 三个行情原始 Tool 以及四个每日快照 Tool 会把相关 Service 返回的完整 `ServiceDataset` 存入 run-scoped
  `ResearchDataStore`，技术计算 Tool 通过 `context_ref` 读取；详见
  [`tool-call-reference.md`](tool-call-reference.md)。

正确的生命周期：

```python
from stock_research_agent.services import open_data_services

async with open_data_services(provider_settings) as services:
    # 在一次应用/研究运行中复用 services
    ...
```

## 2. InstrumentReferenceService

入口：`services.instrument_reference`

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_trade_calendar(exchange, start_date, end_date, *, as_of=None)` | 交易日历 | `trade_cal` |
| `get_stock_basic(ts_code, *, list_status="L", as_of=None)` | 股票身份；`list_status=L/D/P` | `stock_basic` |
| `get_all_stocks(*, list_status="L", as_of=None)` | 全市场股票目录；供确定性聚合使用 | `stock_basic` |
| `get_etf_basic(ts_code, *, as_of=None)` | ETF 身份 | `etf_basic` |
| `get_etf_index(ts_code, *, as_of=None)` | ETF 基准指数 | `etf_index` |
| `get_option_contracts(exchange, *, as_of=None)` | 期权合约目录 | `opt_basic` |
| `get_funds(*, market="E", status="L", as_of=None)` | 公募基金目录；`status=L/D/I` | `fund_basic` |
| `get_indices(market, *, as_of=None)` | 指数目录 | `index_basic` |
| `get_industry_classifications(*, src="SW2021", level=None, as_of=None)` | 申万行业分类；`level=L1/L2/L3` 在本地筛选 | `index_classify` |
| `get_industry_members(l1_code, *, is_new="Y", as_of=None)` | 一个申万一级行业的完整成分 | `index_member_all` |
| `get_index_weights(index_code, start_date, end_date, *, as_of=None)` | 指数成分权重 | `index_weight` |
| `get_name_history(ts_code, *, as_of=None)` | 股票曾用名 | `namechange` |
| `get_new_shares(start_date, end_date, *, as_of=None)` | IPO 新股 | `new_share` |
| `get_st_list(trade_date, *, as_of=None)` | 指定日 ST 名单 | `stock_st` |
| `get_hsgt_stock(ts_code, *, as_of=None)` | 沪深港通标的身份 | `stock_hsgt` |

`get_option_contracts/get_funds/get_indices` 可能返回大型目录，通常由确定性程序缩小范围后使用，
不直接注册为 LLM Tool。

## 3. EquityMarketDataService

入口：`services.equity_market_data`

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_stock_bars(ts_code, start_date, end_date, *, frequency="daily", as_of=None)` | 股票 K 线；`daily/weekly/monthly` | 同名频率接口 |
| `get_daily_market_bars(trade_date, *, as_of=None)` | 单日全 A 股行情横截面 | `daily` |
| `get_daily_market_valuation(trade_date, *, as_of=None)` | 单日全 A 股换手/估值横截面 | `daily_basic` |
| `get_daily_market_limits(trade_date, *, as_of=None)` | 单日全 A 股涨跌停价 | `stk_limit` |
| `get_daily_market_suspensions(trade_date, *, as_of=None)` | 单日全 A 股停复牌记录 | `suspend_d` |
| `get_adjustment_factors(ts_code, start_date, end_date, *, as_of=None)` | 复权因子 | `adj_factor` |
| `get_daily_valuation(ts_code, start_date, end_date, *, as_of=None)` | 换手、量比、估值、市值 | `daily_basic` |
| `get_price_limits(ts_code, start_date, end_date, *, as_of=None)` | 涨跌停价格 | `stk_limit` |
| `get_suspensions(ts_code, start_date, end_date, *, as_of=None)` | 停复牌记录 | `suspend_d` |
| `get_index_bars(ts_code, start_date, end_date, *, as_of=None)` | 指数日线 | `index_daily` |
| `get_index_daily_metrics(ts_code, trade_date, *, as_of=None)` | 指数单日指标 | `index_dailybasic` |
| `get_sw_industry_bars(ts_code, start_date, end_date, *, as_of=None)` | 单个申万行业指数日线 | `sw_daily` |

这里返回原始行情；已经实现的 `analytics/technical` 和五个技术计算 Tool 会通过当前 run 的
`context_ref` 确定性计算均线、MACD、RSI、ATR 和相对强弱，不让 LLM 心算。

## 4. CrossAssetMarketDataService

入口：`services.cross_asset_market_data`

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_fund_bars(ts_code, start_date, end_date, *, as_of=None)` | 基金/ETF 日线 | `fund_daily` |
| `get_fund_adjustment_factors(ts_code, start_date, end_date, *, as_of=None)` | 基金复权因子 | `fund_adj` |
| `get_etf_share_history(ts_code, start_date, end_date, *, as_of=None)` | ETF 份额变化 | `etf_share_size` |
| `get_option_daily(trade_date, *, exchange, as_of=None)` | 单日全市场期权行情 | `opt_daily` |
| `get_convertible_bond_daily(trade_date, *, as_of=None)` | 单日全市场转债行情 | `cb_daily` |

后两个方法是全市场结果，不直接注册为 LLM Tool。

## 5. FundamentalDataService

入口：`services.fundamental_data`。下列 `period` 都是 `YYYYMMDD`。

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_income_statement(ts_code, period, *, as_of=None)` | 单股利润表 | `income` |
| `get_income_batch(period, *, as_of=None)` | 全市场利润表 | `income_vip` |
| `get_balance_sheet(ts_code, period, *, as_of=None)` | 单股资产负债表 | `balancesheet` |
| `get_balance_sheet_batch(period, *, as_of=None)` | 全市场资产负债表 | `balancesheet_vip` |
| `get_cash_flow_statement(ts_code, period, *, as_of=None)` | 单股现金流量表 | `cashflow` |
| `get_cash_flow_batch(period, *, as_of=None)` | 全市场现金流量表 | `cashflow_vip` |
| `get_earnings_forecast(ts_code, period, *, as_of=None)` | 业绩预告 | `forecast` |
| `get_earnings_forecast_batch(period, *, as_of=None)` | 全市场业绩预告 | `forecast_vip` |
| `get_earnings_express(ts_code, period, *, as_of=None)` | 业绩快报 | `express` |
| `get_earnings_express_batch(period, *, as_of=None)` | 全市场业绩快报 | `express_vip` |
| `get_dividends(ts_code, start_date=None, end_date=None, *, as_of=None)` | 分红送股；同时给出起止日期时按 `ann_date` 公告日闭区间本地过滤 | `dividend` |
| `get_financial_indicators(ts_code, period, *, as_of=None)` | 财务指标 | `fina_indicator` |
| `get_financial_indicators_batch(period, *, as_of=None)` | 全市场财务指标 | `fina_indicator_vip` |
| `get_audit_opinion(ts_code, period, *, as_of=None)` | 审计意见 | `fina_audit` |
| `get_business_composition(ts_code, period, *, composition_type="P", as_of=None)` | 主营构成；`P/D/I` | `fina_mainbz` |
| `get_business_composition_batch(period, *, composition_type="P", as_of=None)` | 全市场主营构成 | `fina_mainbz_vip` |
| `get_disclosure_schedule(ts_code, period, *, as_of=None)` | 财报披露计划 | `disclosure_date` |

七个带 `_batch` 的方法只供 scheduler/admin 离线同步，不注册为 LLM Tool。

## 6. MacroDataService

入口：`services.macro_data`

| 方法调用 | 内容 |
|---|---|
| `get_economic_calendar(start_date, end_date, *, as_of=None)` | 财经日历 |
| `get_rate_range(series, start_date, end_date, *, as_of=None)` | 区间利率序列 |
| `get_rate_snapshot(series, observation_date, *, as_of=None)` | 单日利率快照 |
| `get_gdp(start_quarter, end_quarter, *, as_of=None)` | GDP 季度数据 |
| `get_monthly_indicator(series, start_month, end_month, *, as_of=None)` | 中国月度宏观指标 |

`get_rate_range` 的 `series`：

```text
shibor, shibor_lpr, wz_index, gz_index,
us_tycr, us_trycr, us_tbr, us_tltr, us_trltr
```

`get_rate_snapshot` 的 `series`：`shibor_quote, libor, hibor`。

`get_monthly_indicator` 的 `series`：`cn_cpi, cn_ppi, cn_m, sf_month, cn_pmi`。

## 7. OwnershipEventService

入口：`services.ownership_event`

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_top_holders(ts_code, period, *, floating=False, as_of=None)` | 前十大股东；`floating=True` 为流通股东 | `top10_holders` / `top10_floatholders` |
| `get_pledge_statistics(ts_code, *, end_date=None, as_of=None)` | 股权质押统计 | `pledge_stat` |
| `get_pledge_details(ts_code, *, as_of=None)` | 股权质押明细 | `pledge_detail` |
| `get_repurchase_events(ts_code, start_date, end_date, *, as_of=None)` | 股票回购 | `repurchase` |
| `get_unlock_events(ts_code, start_date, end_date, *, as_of=None)` | 限售股解禁计划 | `share_float` |
| `get_holder_counts(ts_code, start_date, end_date, *, as_of=None)` | 股东人数变化 | `stk_holdernumber` |

`get_unlock_events` 的日期是计划解禁日，可以晚于 `as_of`；Service 仍会按公告日阻止未来信息。

## 8. TradingBehaviorService

入口：`services.trading_behavior`

| 方法调用 | 内容 | Provider API |
|---|---|---|
| `get_northbound_holdings(ts_code, start_date, end_date, *, as_of=None)` | 北向持股 | `hk_hold` |
| `get_block_trades(ts_code, trade_date, *, as_of=None)` | 大宗交易 | `block_trade` |
| `get_top_list(ts_code, trade_date, *, as_of=None)` | 龙虎榜每日明细 | `top_list` |
| `get_top_institutions(ts_code, trade_date, *, as_of=None)` | 龙虎榜机构席位 | `top_inst` |
| `get_margin_market(trade_date, *, exchange_id, as_of=None)` | 市场融资融券汇总 | `margin` |
| `get_margin_detail(ts_code, start_date, end_date, *, as_of=None)` | 个股融资融券明细 | `margin_detail` |
| `get_margin_eligibility(ts_code, trade_date, *, as_of=None)` | 融资融券标的身份 | `margin_secs` |
| `get_daily_stock_moneyflow_ths(trade_date, *, as_of=None)` | THS 全市场个股资金流横截面 | `moneyflow_ths` |
| `get_daily_stock_moneyflow_dc(trade_date, *, as_of=None)` | DC 全市场个股资金流横截面 | `moneyflow_dc` |
| `get_stock_moneyflow_ths(ts_code, start_date, end_date, *, as_of=None)` | 单股 THS 资金流区间 | `moneyflow_ths` |
| `get_stock_moneyflow_dc(ts_code, start_date, end_date, *, as_of=None)` | 单股 DC 资金流区间 | `moneyflow_dc` |
| `get_daily_industry_moneyflow_ths(trade_date, *, as_of=None)` | THS 行业资金流 | `moneyflow_ind_ths` |
| `get_daily_market_moneyflow_dc(trade_date, *, as_of=None)` | DC 大盘资金流 | `moneyflow_mkt_dc` |
| `get_hsgt_moneyflow(start_date, end_date, *, as_of=None)` | 沪深港通资金流区间 | `moneyflow_hsgt` |
| `get_daily_limit_list(trade_date, *, as_of=None)` | 当日涨跌停明细 | `limit_list_d` |

THS 与 DC 是两套不同供应商口径。Service 保留各自原始字段和来源，不做相加或强行归一化。

## 9. NewsEventDataService

入口：`services.news_event`

```python
get_market_news(
    start_at: datetime,
    end_at: datetime,
    *,
    source: str | None = None,
    as_of: datetime | None = None,
) -> ServiceDataset
```

对应旧 Tushare `major_news`。所有时间必须带时区，单次窗口不超过 24 小时，`end_at <= as_of`。
该接口不能按 `ts_code` 筛选；公司级新闻研究必须先取得公司名称和曾用名，再做实体匹配。
当前 Event Tool 不再调用这个方法，它只作为兼容代码保留。

```python
get_sell_side_reports(
    start_date: date,
    end_date: date,
    *,
    ts_code: str | None = None,
    as_of: date | datetime | None = None,
) -> ServiceDataset
```

调用 `report_rc`。全市场窗口最多 31 天，指定股票窗口最多 366 天；返回研报标题、机构、作者、
评级、目标价和盈利预测摘要，不含研报全文。评级和预测只表示机构观点。

```python
get_daily_sell_side_reports(
    report_date: date,
    *,
    as_of: date | datetime | None = None,
) -> ServiceDataset
```

同样调用 `report_rc`，但固定使用精确 `report_date`。每日事件快照逐日调用，避免兼容端全市场
区间查询不稳定。

```python
get_broker_recommendations(
    month: str,
    *,
    ts_code: str | None = None,
    as_of: date | datetime | None = None,
) -> ServiceDataset
```

调用 `broker_recommend`；`month` 使用 `YYYYMM`。指定股票时 Service 在本地精确过滤。返回名单只
证明券商当月推荐过该股票，不证明收益或推荐理由。由于上游只有月份而没有精确发布日期，
该数据被视为 current-only：不允许把月末抓取结果回放成月初已经可见的数据。

## 10. PublicNewsEventService

入口：`services.public_news_event`

```python
get_market_news(
    source: MarketNewsSource,
    start_at: datetime,
    end_at: datetime,
    *,
    as_of: datetime,
) -> ServiceDataset
```

`source` 为 `EASTMONEY`、`THS` 或 `CLS`，分别调用 `stock_info_global_em`、
`stock_info_global_ths`、`stock_info_global_cls`。窗口最长 24 小时，所有时间必须带时区。
这些接口只返回最近若干条，因此即使请求成功，`complete` 也固定为 `false`。

```python
get_stock_news(
    ts_code: str,
    start_at: datetime,
    end_at: datetime,
    *,
    as_of: datetime,
) -> ServiceDataset
```

调用 `stock_news_em`。`ts_code` 使用 `000001.SZ` 格式，Service 内部转换为 AKShare 的六位代码；
单次窗口最多 31 天，结果同样不声明为完整历史集合。

```python
get_daily_announcements(
    announcement_date: date,
    *,
    category: AnnouncementCategory = AnnouncementCategory.ALL,
    as_of: date | datetime,
) -> ServiceDataset
```

调用 `stock_notice_report`。公告分类可选：全部、重大事项、财务报告、融资公告、风险提示、
资产重组、信息变更、持股变动。

```python
get_stock_announcements(
    ts_code: str,
    start_date: date,
    end_date: date,
    *,
    category: AnnouncementCategory = AnnouncementCategory.ALL,
    as_of: date | datetime,
) -> ServiceDataset
```

调用 `stock_individual_notice_report`，单次最长 366 天。两个公告方法只返回索引、类型和原文 URL，
不下载公告正文。

## 11. DailyTechnicalSnapshotService

入口：`services.daily_technical_snapshot`

```python
build = await services.daily_technical_snapshot.build_daily_snapshot(
    as_of=frozen_as_of,
    candidate_count=10,
)

snapshot = build.snapshot  # 市场宽度、行业宽度、指数、五类候选
source_datasets = build.datasets  # 完整 ServiceDataset 映射，供 Tool 存储和追溯
optional_failures = build.optional_failures  # 可选申万行业日线的失败项
```

该方法不接收交易日：它根据系统注入的 `as_of` 自动选择最近完整交易日。行业标准固定为
`SW2021/L1`，市场指数固定为上证指数、深证成指、创业板指，权重基准固定为沪深 300、
中证 500、中证 1000。LLM 不参与这些路由和数据范围选择。

市场横截面、行业分类/成分、三大指数和三套基准权重是必要输入，失败时整个聚合失败。
`sw_daily` 是可选增强；单个行业查询失败时会进入 `optional_failures`，该行业仍通过成分股日线
计算上涨、下跌、平盘和领涨/领跌股票。调用方必须把这些失败披露为 `partial`，不能解释成空表。

## 12. DailySentimentFlowSnapshotService

入口：`services.daily_sentiment_flow_snapshot`

```python
build = await services.daily_sentiment_flow_snapshot.build_daily_snapshot(
    as_of=frozen_as_of,
    candidate_count=10,
)

snapshot = build.snapshot
source_datasets = build.datasets
optional_failures = build.optional_failures
```

该方法自动复用 `DailyTechnicalSnapshotService` 的交易日和市场背景，再组合 8 类资金/情绪
查询。返回的语义快照包含市场资金、行业资金、THS/DC 个股资金候选和涨跌停候选；完整源表
仍保留为 `ServiceDataset`。单项可选源失败记录在 `optional_failures`，所有资金/情绪源都没有
有效数据时失败关闭。

## 13. DailyFundamentalSnapshotService

入口：`services.daily_fundamental_snapshot`

```python
build = await services.daily_fundamental_snapshot.build_daily_snapshot(
    as_of=frozen_as_of,
    candidate_count=10,
    announcement_lookback_days=14,
)

snapshot = build.snapshot
source_datasets = build.datasets
optional_failures = build.optional_failures
```

该方法自动选择最近完整交易日、当前披露窗口报告期及上年同期，组合估值、业绩预告/快报、
财务指标、股票目录和宏观利率源表。除个股极值候选外，还会按 `stock_basic.industry` 确定性
生成行业估值、财务质量和近期披露活动候选；该分类不是申万行业。`candidate_count` 限制每个
候选分组为 3～20 条；公告回看窗口限制为 7～60 天。完整批量数据不直接放入 LLM 上下文，
而由 Tool 存入 `ResearchDataStore`。

## 14. DailyEventSnapshotService

入口：`services.daily_event_snapshot`

```python
build = await services.daily_event_snapshot.build_daily_snapshot(
    as_of=frozen_as_of,
    candidate_count=10,
    news_lookback_hours=24,
    announcement_lookback_days=3,
    research_lookback_days=7,
)

snapshot = build.snapshot
source_datasets = build.datasets
optional_failures = build.optional_failures
```

参数边界：`candidate_count` 为 3～20，新闻回看 1～24 小时，公告回看 1～7 个自然日，研报回看
1～14 天。Service 并行查询三路市场快讯、逐日公告索引、逐日卖方研报摘要、当月券商荐股和
当前上市股票目录，确定性筛选有限新闻/公告/研报/荐股候选；股票目录只用于精确连接新闻中出现
的上市公司全名，不推断概念股；
完整标准化源表仍放在 `build.datasets`。任一来源失败进入 `optional_failures`，最近快讯永远不会
被标记为可完整历史回放。

## 15. ServiceDataset 怎样读取

```python
for row, trace in zip(dataset.items, dataset.item_traces, strict=True):
    print(row)  # 上游标准化原始行
    print(trace.provider)  # PRIMARY/BACKUP，或具体 AKSHARE_* 公开来源
    print(trace.from_cache)  # 是否来自备用缓存
    print(trace.fetched_at)  # 抓取时间
```

不要只保留 `items` 而丢掉 `item_traces`。后续生成 `EvidenceRecord` 时，两者需要一起进入证据溯源链。
