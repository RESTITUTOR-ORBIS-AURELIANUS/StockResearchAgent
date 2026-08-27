# StockResearchAgent 数据 Service：v1 接口与职责

> 文档状态：已实现的 v1 数据查询层
> 更新日期：2026-08-24
> 代码位置：`src/stock_research_agent/services/`

## 1. 为什么 Provider 上面还需要 Service

`Provider` 只负责“怎样访问一套上游接口”：发送 HTTP、解析返回值、主服务器失败时回退备用服务器。它返回的 `ProviderResult` 是**一次接口、一次分页请求**的原始结果。

`Service` 负责“业务代码怎样正确地取数据”：

- 给调用方提供有业务含义的方法，不让 Agent 记忆 89 个接口名和字段名；
- 将 `date`、报告期、证券代码等参数转成上游格式并提前校验；
- 固定字段白名单，避免模型自由拼接字段；
- 统一处理 `limit/offset` 分页，不把截断的数据冒充完整结果；
- 针对兼容端的特殊参数规则做适配；
- 对存在可靠可用日期的数据按 `as_of` 截止；对只有“当前快照”的数据拒绝伪装成历史数据；
- 为每一页及每一条保留实际 Provider、缓存状态、抓取时间和响应位置；
- 对全市场接口在本地按股票代码筛选；
- 对有稳定主键的响应做确定性去重。

当前分层如下：

```text
LangGraph / Tool（数据 Tool + 技术计算 Tool 已实现）
             ↓ 只表达“需要什么研究数据”
四个 Daily Snapshot Service（技术、情绪资金、基本面、新闻事件）
             ↓ 组合接口、生成有界候选快照
九个源 Data Service（本文，已实现）
             ↓ ProviderQuery
      ┌──────┴──────────────────────┐
RoutedMarketDataProvider      AkshareNewsProvider
             ↓                         ↓
主 REST ──失败──> 备用链      六个公开新闻/公告函数
```

九个源 Service **都只依赖 `MarketDataProvider` 接口**：前八个注入 Tushare 主备 Router，
`PublicNewsEventService` 单独注入 `AkshareNewsProvider`。四个聚合 Service 只组合确定性源 Service，
其中情绪资金快照还复用技术每日快照作为市场背景。
它们都不知道 API Key、Token、HTTP 地址和主备选择规则，也不会各自建立 HTTP 连接。

## 2. 统一返回值

九个源 Service 的公开方法返回 `ServiceDataset`：

| 字段 | 含义 |
|---|---|
| `api_name` | 本次使用的单个上游接口 |
| `query_params` | 业务查询参数，不包含内部的 `limit/offset` |
| `requested_fields` | Service 固定请求的字段；空元组表示当前兼容 schema 尚未固定 |
| `items` | 完成分页、截止时间过滤、本地筛选和去重后的原始行 |
| `item_traces` | 与 `items` 逐行对齐的来源、缓存状态、抓取时间、页号和原始 offset |
| `pages` | 每一页的 Provider 来源、缓存状态、抓取时间、offset、行数和字段 |
| `as_of` | 本次研究允许使用数据的截止日期 |
| `data_as_of` | 从最终保留行的可用日期字段推导出的最新日期；不是未经校验的 Provider 声明值 |
| `received_item_count` | 过滤前从上游收到的总行数 |
| `discarded_item_count` | 因股票筛选、时间截止或去重被丢弃的行数 |
| `complete` | 是否能声明已完整覆盖请求范围；Tushare 分页查询正常读完时为 `true`，只返回最近 N 条的 AKShare 快讯/个股新闻固定为 `false` |

`items` 仍然是原始数据，不是 `EvidenceRecord`。一个 `ServiceDataset` 仍只对应一个接口；行情原始
Tool 会把多个完整 `ServiceDataset` 组成 run-scoped 数据包，技术计算 Tool 再按 `context_ref`
读取和对齐它们。

## 3. 九个源 Service 与两类 Provider 的归属

前八个源 Service 仍唯一覆盖 Tushare-compatible 白名单中的 89 项接口；新增的
`PublicNewsEventService` 只覆盖 `AkshareNewsProvider` 暴露的六个公开新闻/公告接口，
不经过主备 Tushare 路由。

八组数量为 `14 + 10 + 5 + 17 + 19 + 8 + 13 + 3 = 89`。代码在启动导入时检查“无遗漏、无重复”，一旦与 Provider 的 `SUPPORTED_APIS` 漂移就立即报错。

### 3.1 `InstrumentReferenceService`：14 项

负责证券身份、目录、上市状态和交易日历。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `trade_cal` | 交易日历 | `get_trade_calendar()` |
| `stock_basic` | 股票身份、行业、市场、上市日 | `get_stock_basic()` |
| `etf_basic` | ETF 基础信息 | `get_etf_basic()` |
| `etf_index` | ETF 基准指数目录 | `get_etf_index()` |
| `opt_basic` | 期权合约目录 | `get_option_contracts()` |
| `fund_basic` | 公募基金目录 | `get_funds()` |
| `index_basic` | 指数目录 | `get_indices()` |
| `index_classify` | 申万行业分类目录 | `get_industry_classifications()` |
| `index_member_all` | 申万行业完整成分 | `get_industry_members()` |
| `index_weight` | 指数成分权重 | `get_index_weights()` |
| `namechange` | 股票曾用名历史 | `get_name_history()` |
| `new_share` | IPO 新股记录 | `get_new_shares()` |
| `stock_st` | ST 股票名单 | `get_st_list()` |
| `stock_hsgt` | 沪深港通股票名单 | `get_hsgt_stock()` |

### 3.2 `EquityMarketDataService`：10 项

负责 A 股和指数的价格、估值、涨跌停与停牌状态。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `daily` | 股票日线行情 | `get_stock_bars(frequency="daily")` |
| `weekly` | 股票周线行情 | `get_stock_bars(frequency="weekly")` |
| `monthly` | 股票月线行情 | `get_stock_bars(frequency="monthly")` |
| `adj_factor` | 股票复权因子 | `get_adjustment_factors()` |
| `daily_basic` | 换手、量比、估值和市值 | `get_daily_valuation()` |
| `stk_limit` | 每日涨跌停价格 | `get_price_limits()` |
| `suspend_d` | 停复牌记录 | `get_suspensions()` |
| `index_daily` | 指数日线行情 | `get_index_bars()` |
| `index_dailybasic` | 指数每日指标 | `get_index_daily_metrics()` |
| `sw_daily` | 申万行业指数日线 | `get_sw_industry_bars()` |

这一层只取可复现的原始量价和估值数据；均线、MACD、RSI、波动率、相对强弱等指标已由
独立的确定性 Analytics 层计算，并通过 5 个按需调用的 Tool 使用，不交给 LLM 心算。

### 3.3 `CrossAssetMarketDataService`：5 项

负责基金、ETF、期权与可转债，用于大盘环境和跨资产证实。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `fund_daily` | 基金/ETF 日线行情 | `get_fund_bars()` |
| `fund_adj` | 基金复权因子 | `get_fund_adjustment_factors()` |
| `etf_share_size` | ETF 份额规模 | `get_etf_share_history()` |
| `opt_daily` | 期权日线 | `get_option_daily()` |
| `cb_daily` | 可转债日线 | `get_convertible_bond_daily()` |

任何接口若主、备都失败，Service 都会保留 Provider 异常，不会把权限、限流或网络失败伪造
成合法空数据。

### 3.4 `FundamentalDataService`：17 项

负责三大财务报表、财务质量、业绩事件、分红、主营构成与披露日程。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `income` | 单股利润表 | `get_income_statement()` |
| `income_vip` | 全市场利润表批量同步 | `get_income_batch()` |
| `balancesheet` | 单股资产负债表 | `get_balance_sheet()` |
| `balancesheet_vip` | 全市场资产负债表批量同步 | `get_balance_sheet_batch()` |
| `cashflow` | 单股现金流量表 | `get_cash_flow_statement()` |
| `cashflow_vip` | 全市场现金流量表批量同步 | `get_cash_flow_batch()` |
| `forecast` | 单股业绩预告 | `get_earnings_forecast()` |
| `forecast_vip` | 全市场业绩预告同步 | `get_earnings_forecast_batch()` |
| `express` | 单股业绩快报（全市场取回后筛选） | `get_earnings_express()` |
| `express_vip` | 全市场业绩快报同步 | `get_earnings_express_batch()` |
| `dividend` | 分红送股 | `get_dividends()` |
| `fina_indicator` | 单股财务指标 | `get_financial_indicators()` |
| `fina_indicator_vip` | 全市场财务指标同步 | `get_financial_indicators_batch()` |
| `fina_audit` | 审计意见 | `get_audit_opinion()` |
| `fina_mainbz` | 单股主营业务构成 | `get_business_composition()` |
| `fina_mainbz_vip` | 全市场主营构成同步 | `get_business_composition_batch()` |
| `disclosure_date` | 财报披露计划 | `get_disclosure_schedule()` |

带 `_batch` 的方法可能读取全市场多页数据，供定时同步任务使用，不应直接暴露为 LLM Tool。单股研究优先使用非 VIP 方法。

### 3.5 `MacroDataService`：19 项

负责财经日历、国内外利率和中国宏观指标。

| 接口组 | Provider 接口 | Service 方法 |
|---|---|---|
| 日历 | `eco_cal` | `get_economic_calendar()` |
| 区间利率 | `shibor`, `shibor_lpr`, `wz_index`, `gz_index`, `us_tycr`, `us_trycr`, `us_tbr`, `us_tltr`, `us_trltr` | `get_rate_range()` |
| 单日利率 | `shibor_quote`, `libor`, `hibor` | `get_rate_snapshot()` |
| 季度宏观 | `cn_gdp` | `get_gdp()` |
| 月度宏观 | `cn_cpi`, `cn_ppi`, `cn_m`, `sf_month`, `cn_pmi` | `get_monthly_indicator()` |

宏观兼容接口的列名差异较大，因此 v1 先保留完整返回字段；Snapshot 层确认 schema 后再建立稳定的指标模型。

### 3.6 `OwnershipEventService`：8 项

负责所有权结构、质押风险、回购、解禁、股东人数与增减持。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `top10_holders` | 前十大股东 | `get_top_holders(floating=False)` |
| `top10_floatholders` | 前十大流通股东 | `get_top_holders(floating=True)` |
| `pledge_stat` | 股权质押统计 | `get_pledge_statistics()` |
| `pledge_detail` | 股权质押明细 | `get_pledge_details()` |
| `repurchase` | 股票回购 | `get_repurchase_events()` |
| `share_float` | 限售股解禁计划 | `get_unlock_events()` |
| `stk_holdernumber` | 股东人数 | `get_holder_counts()` |
| `stk_holdertrade` | 股东增减持 | `get_holder_trades()` |

`share_float` 的计划解禁日可以晚于 `as_of`，因为“今天已公告的未来解禁”本身就是今天可用的证据；Service 按公告日而不是解禁日执行截止检查。

### 3.7 `TradingBehaviorService`：13 项

负责北向持股、异常交易、龙虎榜、融资融券和特色资金流。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `hk_hold` | 沪深港股通持股明细 | `get_northbound_holdings()` |
| `block_trade` | 大宗交易 | `get_block_trades()` |
| `top_list` | 龙虎榜每日明细 | `get_top_list()` |
| `top_inst` | 龙虎榜机构席位 | `get_top_institutions()` |
| `margin` | 市场融资融券汇总 | `get_margin_market()` |
| `margin_detail` | 个股融资融券明细 | `get_margin_detail()` |
| `margin_secs` | 融资融券标的 | `get_margin_eligibility()` |
| `moneyflow_ths` | 同花顺个股资金流，全市场日截面/单股区间 | `get_daily_stock_moneyflow_ths()` / `get_stock_moneyflow_ths()` |
| `moneyflow_dc` | 东财个股资金流，全市场日截面/单股区间 | `get_daily_stock_moneyflow_dc()` / `get_stock_moneyflow_dc()` |
| `moneyflow_ind_ths` | 同花顺行业资金流 | `get_daily_industry_moneyflow_ths()` |
| `moneyflow_mkt_dc` | 东财大盘资金流 | `get_daily_market_moneyflow_dc()` |
| `moneyflow_hsgt` | 沪深港通资金流 | `get_hsgt_moneyflow()` |
| `limit_list_d` | 涨跌停明细 | `get_daily_limit_list()` |

### 3.8 `NewsEventDataService`：3 项

该 Service 保留旧 Tushare `major_news` 兼容查询，并提供卖方研报摘要与月度券商荐股。
新闻事件 Tool 不再调用 `major_news`，公开新闻与公告走独立 AKShare 链；每日事件快照和定向卖方
查证则使用 `report_rc` 与 `broker_recommend`。该 Service 与结构化公司事件分开，因为新闻具有
带时区发布时间，卖方研究又具有“观点记录而非经营事实”的专门语义。

| Provider 接口 | 业务内容 | Service 方法 |
|---|---|---|
| `major_news` | 新闻标题、正文、发布时间与来源 | `get_market_news()` |
| `report_rc` | 卖方研报标题、机构、评级、目标价和盈利预测摘要 | `get_sell_side_reports()`、`get_daily_sell_side_reports()` |
| `broker_recommend` | 某月券商金股名单 | `get_broker_recommendations()` |

所有 `datetime` 参数必须带时区，进入 Service 后统一换算成 `Asia/Shanghai`。单次窗口最长
24 小时，`end_at` 不能晚于 `as_of`；返回行还会按 `pub_time` 再做一次本地精确过滤。
`major_news` 不能按股票代码检索，不能通过文本模糊匹配冒充公司公告。`report_rc` 不含研报全文；
评级、目标价与盈利预测只证明机构发表过观点。`broker_recommend` 只证明某券商把股票列入当月名单，
不证明后续收益。它只有月份而没有精确发布日期，因此按 current-only 处理：晚抓的月度名单不得
回放到该月更早的历史时点。

### 3.9 `PublicNewsEventService`：6 项

入口为 `services.public_news_event`。它由独立的 `AkshareNewsProvider` 提供数据，负责公开快讯、
个股新闻和公告索引：

| AKShare 函数 | 业务内容 | Service 方法 |
|---|---|---|
| `stock_info_global_em` | 东方财富最近全市场快讯 | `get_market_news(EASTMONEY, ...)` |
| `stock_info_global_ths` | 同花顺最近全市场快讯 | `get_market_news(THS, ...)` |
| `stock_info_global_cls` | 财联社最近全市场快讯 | `get_market_news(CLS, ...)` |
| `stock_news_em` | 指定股票新闻 | `get_stock_news()` |
| `stock_notice_report` | 指定日期全市场公告索引 | `get_daily_announcements()` |
| `stock_individual_notice_report` | 指定股票公告索引 | `get_stock_announcements()` |

公开快讯与个股新闻在 Service 中按 `Asia/Shanghai` 的冻结窗口过滤。三个快讯接口和
`stock_news_em` 只返回网站最近若干条，因此相应 `ServiceDataset.complete=false`，不能把空结果
解释为窗口内确定没有新闻。公告接口返回索引和 URL，不下载公告正文。

## 4. 兼容端的五个重要适配

这些规则已固化在 Service 中，调用方不需要记忆：

1. `opt_basic` 当前只发送 `exchange`，不发送未经兼容验证的 `list_status`；
2. `stock_hsgt` 当前只发送 `ts_code`，不发送 `is_new`；
3. `suspend_d` 只按日期查询全市场，再在本地按 `ts_code` 筛选；
4. `repurchase`、`block_trade`、`top_list`、`top_inst` 使用兼容端已验证的全市场日期参数，再在本地按股票筛选；`share_float`、`stk_holdertrade` 会把 `ts_code` 发给上游，同时仍进行本地校验，避免无谓拉取全市场数据；
5. `major_news`、`report_rc` 与 `broker_recommend` 的兼容端不使用 `limit/offset`；Service 分别通过
   最长 24 小时新闻窗口、全市场逐日研报查询/指定个股有界日期窗口和单月荐股查询约束体积。

上述第 5 条中 `major_news` 只描述旧兼容 Service；Event Tool 的公开新闻路径改走 AKShare，卖方
研究路径仍会调用 `report_rc` 与 `broker_recommend`。
六个 AKShare 接口同样不使用 offset 分页，每次调用一个同步函数并由 Service 做本地过滤。

本地筛选前的行数保存在 `received_item_count`，筛选后的行放在 `items`，因此不会把“上游空数据”和“目标股票没有该事件”混为一谈。

## 5. 分页、截止时间与错误规则

### 5.1 分页

- 默认 `page_size=1000`、`max_pages=50`、`max_rows=50000`；
- Service 串行发送 `limit/offset`，直到 `has_more=false`；
- `major_news`、`report_rc` 与 `broker_recommend` 不使用 offset 分页：前者只请求一次最长 24 小时
  窗口；全市场研报按单个 `report_date` 逐日读取，指定个股研报窗口最多 366 天；荐股按单月读取。
  它们仍受 `max_rows` 约束；若上游声明 `has_more=true`，Service 会失败关闭，因为无法安全继续；
- `has_more=true` 但返回空页会抛出 `ServicePaginationError`；
- 达到页数/行数上限会抛错，不返回半截数据；
- 每一页及每一条都保存来源；同一次分页若从主源切到备源（或从实时切到缓存），Service 会抛错，拒绝把两个排序或时点可能不同的快照按同一个 offset 拼接。

自动化测试覆盖正常多页合并、缺少 `has_more` 时按页长推断，以及跨 Provider 时失败关闭。主、备 Provider 是否继续兼容 `limit/offset` 属于运行时外部条件，不在本文固化某次在线探测结果。

### 5.2 `as_of`

公开方法接受 `date` 或带时区的 `datetime`。带时区时间先换算到 `Asia/Shanghai` 日期；无时区 `datetime` 会被拒绝。

- 行情使用 `trade_date`；
- 财报、业绩、分红、回购等优先使用 `ann_date`；
- 宏观观测值可以使用 `date`、`month` 或 `quarter`，但月份/季度本身不是发布日期；
- 如果指定了 `as_of`，但响应行没有可验证日期，Service 会失败关闭，而不是放行未知时点数据。

`stock_basic` 等当前名单、`fina_mainbz`、质押统计以及月度/季度宏观值缺少可靠的历史成分、首次发布日期或修订可见时间。若在抓取日之后要求更早的 `as_of`，Service 会拒绝这类查询。后续 Historical Snapshot 必须保存首次抓取快照，并补充官方发布日期；当前不能把它们宣称为无穿越回测数据。

### 5.3 错误

- 参数错误：`ServiceInputError`；
- 分页无法完整结束：`ServicePaginationError`；
- 上游成功响应不满足数据契约：`ServiceDataValidationError`；
- Provider 传输、权限、限流或双源失败：原样向上传递 `ProviderError`。

`code=0` 且 `items=[]` 是合法空数据。Service 不会因为空表自动换源，也不会把 Provider 失败伪装成空表。

## 6. 正确的创建和调用方式

```python
from datetime import date

from stock_research_agent.config import ProviderSettings
from stock_research_agent.services import open_data_services


async def load_daily_data():
    settings = ProviderSettings()

    # 一个应用生命周期或一次研究运行只打开一次连接池和组合根。
    async with open_data_services(settings) as services:
        daily = await services.equity_market_data.get_stock_bars(
            "000001.SZ",
            date(2026, 8, 1),
            date(2026, 8, 18),
            as_of=date(2026, 8, 18),
        )

        return daily
```

不要在每个 Service、每个 Tool 或每个 LangGraph 节点里重新调用 `open_data_services()`；否则会反复创建连接池并失去统一生命周期。需要单元测试或依赖注入时，才使用 `build_data_services(fake_provider)`。

## 7. 四个每日聚合 Service

### 7.1 每日技术快照

`DailyTechnicalSnapshotService.build_daily_snapshot(as_of=..., candidate_count=10)` 不直接拥有
Provider API，而是组合 `InstrumentReferenceService` 与 `EquityMarketDataService`。它会：

- 用交易日历选择 `as_of` 之前最近的完整交易日；交易日 15:00 前调用会回退到前一交易日；
- 读取上市股票、全市场日线/估值/涨跌停/停牌/ST；
- 读取上证、深证成指、创业板指，并组合申万 2021 一级行业目录、成分和行业日线；
- 读取沪深 300、中证 500、中证 1000 最近一期权重；
- 确定性计算市场宽度、行业宽度和涨幅/跌幅/成交额/换手率/量比五组候选；
- 返回 `DailyTechnicalSnapshotBuild`，其中 `snapshot` 给 Tool 使用，`datasets` 保留完整溯源。

它不生成 `EvidenceRecord`，也不替代指定个股的区间技术分析与五个计算器 Tool。

### 7.2 每日情绪与资金快照

`DailySentimentFlowSnapshotService.build_daily_snapshot(as_of=..., candidate_count=10)` 先复用
每日技术快照的交易日、指数、行业和异常个股背景，再并发读取：

- `moneyflow_ths` 与 `moneyflow_dc` 的全市场个股资金流横截面；
- `moneyflow_ind_ths` 行业资金流和 `moneyflow_mkt_dc` 大盘资金流；
- 最近 14 日 `moneyflow_hsgt`；
- 当日 `limit_list_d`；
- 上交所、深交所 `margin` 汇总。

它确定性生成行业净流入/净流出、THS/DC 个股净流入/净流出、涨停/跌停等候选分组，同时把
完整源表交给 Tool 存入 `ResearchDataStore`。任何单项特色数据失败都会进入
`optional_failures`，其余数据仍可形成 `partial` 快照；若所有情绪资金源都失败，则聚合失败。
THS 与 DC 是两套不同口径，只能交叉验证方向和持续性，不能把二者数值相加。

### 7.3 每日基本面快照

`DailyFundamentalSnapshotService.build_daily_snapshot(as_of=..., candidate_count=10,
announcement_lookback_days=14)` 先由交易日历选择最近完整交易日，并按披露节奏选择报告期及其
上年同期，然后并发组合：

- `daily_basic` 的全市场估值横截面；
- `forecast_vip` 与 `express_vip` 的近期业绩事件；
- 当期和上年同期 `fina_indicator_vip`，用于确定性计算 ROE 变化；
- GDP、CPI、PPI、货币供应、社融、PMI、Shibor、LPR 和美国国债收益率曲线。

完整批量源表保留在 `DailyFundamentalSnapshotBuild.datasets`，快照输出估值极值、近期业绩
候选、财务质量候选、按 `stock_basic.industry` 聚合的行业横截面，以及每个宏观/利率序列最近
两期观测。行业聚合保留有效样本数、中位数、近期披露活动率和代表股；它不是申万行业口径。
股票目录补充名称与行业。除交易日历外的
单项失败进入 `optional_failures`；只要仍有分析数据就返回部分快照，所有分析来源均无数据时
失败关闭。宏观月度/季度数据目前不具备无修订历史回放保证。

### 7.4 每日新闻事件快照

`DailyEventSnapshotService.build_daily_snapshot()` 以冻结 `as_of` 为终点，并行读取东方财富、
同花顺、财联社最近快讯，最近 1～7 个自然日的全市场公告索引，最近 1～14 天逐日卖方研报摘要、
冻结月份的券商金股名单和当前上市股票目录。它按标题规范化去重新闻，优先保留带原文 URL 且
可以精确映射当前上市公司全名的来源；公告按风险提示、重组、业绩、资本动作和融资关键词排序；
研报按报告日、评级与预测字段排序；荐股按同月提及该股的券商数量排序。

`candidate_count` 分别限制新闻、公告、研报和荐股候选各保留 3～20 条；新闻回看限制 1～24 小时，
卖方研报默认回看 7 天。
完整标准化源表放在 `DailyEventSnapshotBuild.datasets`，任一来源失败进入 `optional_failures`；
`coverage.recent_feed_is_complete_history` 固定为 `false`，明确最近快讯不是完整历史新闻库。

每日快照只用 `stock_basic` 当前上市公司完整名称做精确实体连接，不生成概念、供应链、合作方或
受益股映射。卖方研报和荐股候选明确携带“不是结果 ground truth”的语义，防止模型把机构观点
写成公司经营结果。

## 8. 当前没有放进源 Service 的内容

当前 Provider 清单共有 89 项。白名单之外的特色接口，例如 `stk_factor`、`cyq_perf`、
`moneyflow`、`stk_surv`、`limit_list_ths`、热榜、`news` 和 `cctv_news`，
**不属于当前实现**，调用会在发出 HTTP 前被 `UNKNOWN_PROVIDER_API` 拒绝。

2026-08-24 已完成 AKShare `1.18.94` 的新闻事件数据源评估，并已将以下六项接入
`AkshareNewsProvider → PublicNewsEventService`：

- `stock_info_global_em`、`stock_info_global_ths`、`stock_info_global_cls`：最近的全市场财经快讯；
- `stock_news_em`：按股票代码或关键词查询个股新闻；
- `stock_notice_report`、`stock_individual_notice_report`：全市场每日公告和指定个股公告；
- 研报接口 `stock_research_report_em` 本轮未接入。

这六项不属于 89 项 Tushare 白名单，也不经过主备 Tushare Router。详细调用方式、标准化字段和
限制见 [`akshare-news-data-sources.md`](akshare-news-data-sources.md)。Event 的
`get_daily_event_snapshot`、`search_market_news` 和 `get_targeted_news_and_disclosures` 已全部改用
这条公开数据链，不再调用 `major_news`。最近快讯仍无法替代长期持久化的每日新闻档案。

已完成的本轮增量包括：

- 独立 `AkshareNewsProvider` 与 `PublicNewsEventService`；
- `DailyEventSnapshotService` 和公开新闻、公告、卖方研究 Event Tool；

尚未完成或仍属后续范围的内容包括：

- 机构调研 `stk_surv` 仍待验证后接入；已接入的 `report_rc` 与 `broker_recommend` 明确区分观点记录和 ground truth；
- 继续扩展 `TradingBehaviorService`：仅在字段、分页和筛选契约验证后增加热榜或其他特色资金接口；
- 已实现独立的 Technical Analytics 层，基于 run-scoped 行情数据包本地计算指标，不依赖供应商特色因子；
- 当前已实现四个面向 Agent 的高层 Tool 集合，共 25 个数据 Tool 和 5 个技术计算 Tool；
  详见 [`agent-tools-v1.md`](agent-tools-v1.md)。

不要为了“凑齐四位研究员”绕过接口白名单，也不要让 LLM 直接调用任意 `api_name`。

## 9. 验证方式

- 八组源接口归属在导入时自动校验为 89 项且无重复；
- 单元测试覆盖分页、逐行来源、分页期间来源切换失败关闭、`as_of` 过滤、历史回放限制、
  字段契约、本地筛选、异常透传和统一工厂；
- 执行 `uv run pytest tests/test_services.py` 验证 Service 契约；
- 执行 `uv run pytest` 做全量确定性回归。

这些测试证明数据查询层的本地契约，不证明真实上游权限永久可用，也不代表完整决策 LangGraph
或端到端投研流程已经通过验证。四位已实现证据 Agent 另由各自子图、结构化输出和证据装配测试覆盖。
