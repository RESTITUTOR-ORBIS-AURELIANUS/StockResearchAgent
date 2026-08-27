# StockResearchAgent Tool 调用规则与字段速查

> 适用版本：Tool v1.5
> 更新日期：2026-08-24
> 代码来源：`src/stock_research_agent/tools/`

本文是维护已实现的技术、情绪资金、基本面、新闻事件四位证据 Agent、system prompt 和数据调用
子图时使用的正式调用手册。
Service 的 Python 调用方式见 [`data-service-call-reference.md`](data-service-call-reference.md)，
Tool 层设计原理见 [`agent-tools-v1.md`](agent-tools-v1.md)，完整分层见
[`data-pipeline-reference.md`](data-pipeline-reference.md)。

本文中的“Agent 调用/填写 Tool”是业务层面的简写。当前四位证据 Agent 由模型输出结构化测量/查证计划，
再由 LangGraph 程序映射和执行 Tool；并非让模型直接生成任意 Tool 调用。

## 1. Agent 调用 Tool 的统一规则

### 1.1 Agent 只能填写业务字段

允许出现在 Tool 参数中的只有证券代码、日期、报告期、频率和少量业务选项。

以下字段永远不能由 Agent 填写：

```text
as_of
run_id
api_name
provider
fields
limit
offset
token / api_key
raw rows / datasets
```

- `as_of` 在一次研究运行开始时冻结，由 `ResearchToolContext` 注入；
- `run_id` 和 `ResearchDataStore` 由当前研究运行管理；
- Provider 固定采用主服务器优先、失败后回退备用服务器；
- API 名称、字段白名单和分页由 Tool/Service 决定；
- 计算器只接受原始 Tool 返回的 `context_ref` 和受控参数，不接受 LLM 复制的行情数组；
- 输入模型继承 `DomainModel`，多传任何未声明字段都会校验失败，不会被静默忽略。

### 1.2 通用字段格式

| 字段 | JSON 类型 | 格式与示例 |
|---|---|---|
| `ts_code` | string | 六位代码加市场后缀：`000001.SZ`、`600519.SH`、`430047.BJ` |
| `start_date` | string | ISO 日期，`YYYY-MM-DD`，区间包含该日 |
| `end_date` | string | ISO 日期，`YYYY-MM-DD`，区间包含该日 |
| `trade_date` | string | 单个交易日，`YYYY-MM-DD` |
| `period` | string | 财务报告期，`YYYYMMDD`，例如 `20251231` |
| `start_month/end_month` | string | 月份，`YYYYMM`，例如 `202601` |
| `start_quarter/end_quarter` | string | 季度，`YYYYQ1`～`YYYYQ4` |
| `start_at/end_at` | string | 必须包含时区的 RFC 3339 时间，例如 `2026-08-20T14:00:00+08:00` |

所有区间都要求起点不晚于终点。日线、宏观和事件数据的结束时间通常不能晚于冻结的
`as_of`；“截至当前已经公告的未来解禁计划”是一个特殊的合法例外，由 Service 按公告日过滤。

### 1.3 推荐调用顺序

个股研究推荐遵守以下顺序：

1. 先调用 `resolve_stock_identity`，确认代码、公司名称、行业和曾用名；
2. 需要判断开市日时调用 `get_trade_calendar`；
3. 每日技术研究先调用 `get_daily_technical_market_snapshot`；指定标的查证再调用行情 Tool；
4. 每日基本面研究先调用 `get_daily_fundamental_snapshot`；再对候选调用个股基本面 Tool；
5. 每日新闻事件研究先调用 `get_daily_event_snapshot`；市场/板块补查使用
   `search_market_news`，指定股票查证使用 `get_targeted_news_and_disclosures` 或
   `get_sell_side_research_context`；
6. 根据研究问题选择调用一个或多个确定性计算器，只传递引用与受控参数；
7. 检查返回的 `status`、`issues`、`data_as_of` 和来源；
8. 只有成功数据和确定性计算结果才能被证据分析 Agent 转成 `EvidenceRecord`。

Tool 返回的是受控原始数据，不是证据、观点或投资建议。

## 2. 角色 Tool 白名单

### 2.1 TechnicalResearchAnalyst：11 个

```text
resolve_stock_identity
get_trade_calendar
get_daily_technical_market_snapshot
get_stock_price_context
get_index_market_context
get_fund_market_context
calculate_return_and_trend
calculate_momentum
calculate_risk_and_tradability
calculate_volume_and_liquidity
calculate_relative_strength
```

### 2.2 FundamentalResearchAnalyst：11 个

```text
resolve_stock_identity
get_trade_calendar
get_daily_fundamental_snapshot
get_financial_statements
get_financial_quality
get_earnings_and_disclosure
get_dividend_and_ownership_context
get_pledge_risk_context
get_valuation_context
get_china_macro_context
get_interest_rate_context
```

### 2.3 EventDrivenResearchAnalyst：9 个

```text
resolve_stock_identity
get_trade_calendar
get_daily_event_snapshot
search_market_news
get_targeted_news_and_disclosures
get_sell_side_research_context
get_corporate_action_events
get_economic_calendar
get_earnings_and_disclosure
```

### 2.4 SentimentAndFlowAnalyst：8 个

```text
resolve_stock_identity
get_trade_calendar
get_daily_sentiment_flow_snapshot
get_stock_active_money_flow_context
get_capital_flow_context
get_unusual_trading_activity
get_fund_market_context
search_market_news
```

首席策略师、观点审查员和激进/保守投资经理不直接拥有这些原始数据 Tool。
它们需要补充证据时，应生成 `ResearchRequest`，再由协调器交给对应证据研究员。

## 3. 公共 Tool

### 3.1 `resolve_stock_identity`

用途：确认股票身份和曾用名，避免证券代码或公司实体识别错误。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `ts_code` | 是 | string | — | A 股代码 |
| `list_status` | 否 | enum | `L` | `L` 上市、`D` 退市、`P` 暂停上市 |

内部数据集：

| `label` | Provider API | 内容 |
|---|---|---|
| `stock_basic` | `stock_basic` | 名称、行业、市场、上市日期等 |
| `name_history` | `namechange` | 曾用名和更名日期 |

示例：

```json
{"ts_code":"000001.SZ","list_status":"L"}
```

调用规则：任何个股研究的第一步都应调用。研究退市股票时必须显式使用 `D`，否则合法空表
不代表股票不存在。

### 3.2 `get_trade_calendar`

用途：确认日期是否开市，并取得上一交易日。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `exchange` | 是 | string，1～20 字符 | 例如 `SSE` |
| `start_date` | 是 | date | 起始日期 |
| `end_date` | 是 | date | 结束日期 |

内部数据集：`trade_calendar` → `trade_cal`。

```json
{"exchange":"SSE","start_date":"2026-08-01","end_date":"2026-08-20"}
```

## 4. 技术分析 Tool

### 4.1 `get_daily_technical_market_snapshot`

用途：技术研究员每日模式的首个入口。它自动选择 `as_of` 之前最近的完整交易日，一次返回
可直接阅读的全市场横截面，而不是让模型自己拼接数千行表格。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `candidate_count` | 否 | integer | `10` | 每个候选组最多保留 3～20 只股票 |

```json
{"candidate_count":10}
```

日期、申万标准、行业层级、市场指数和权重基准均由程序固定。交易日 15:00 前调用时会选前一个
完整交易日。返回的 `snapshot` 包含 `coverage`、`market_breadth`、`market_indices`、
`industries`、`candidates` 和 `benchmarks`：

- 市场宽度包括上涨/下跌/平盘、涨跌停、停牌、总成交额、中位涨跌幅和涨跌家数比；
- 行业部分是申万 2021 一级行业的成分宽度、行业指数涨跌和领涨/领跌股票；
- 候选按涨幅、跌幅、成交额、换手率、量比分成五组，供后续指定目标查证；
- 基准部分保留沪深 300、中证 500、中证 1000 最近一期权重摘要。

完整的交易日历、股票目录、全市场横截面、行业目录/成分/日线和指数权重会保存到
`context_ref`。这个引用用于追溯，不应直接交给现有五个单标的技术计算器；选出候选后，继续用
`get_stock_price_context` 或 `get_index_market_context` 获取指定标的区间行情。

`sw_daily` 只是行业指数表现的可选增强。若某些申万一级行业的该接口不可用，Tool 仍会用
`index_member_all + daily` 计算行业成分股宽度，并返回 `status="partial"`：受影响行业的
`index_pct_change` 等指数日线字段为空，具体缺失项列在 `issues` 中。这不等于该行业没有行情，
也不影响市场宽度、行业成分宽度和候选股票的生成。

### 4.2 `get_stock_price_context`

用途：取得一只股票的量价、复权、估值/换手、涨跌停、停复牌和交易日历上下文。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `ts_code` | 是 | string | — | 股票代码 |
| `start_date` | 是 | date | — | 查询起点 |
| `end_date` | 是 | date | — | 查询终点 |
| `frequency` | 否 | enum | `daily` | `daily`、`weekly` 或 `monthly` |

内部数据集：

| `label` | Provider API | 内容 |
|---|---|---|
| `price_bars` | `daily/weekly/monthly` | 所选频率的 OHLCV；所有频率都读取 |
| `adjustment_factors` | `adj_factor` | 日频复权因子；所有频率都读取以复权价格序列 |
| `daily_valuation_and_turnover` | `daily_basic` | 日频换手、量比、PE/PB、市值等；仅 `daily` |
| `price_limits` | `stk_limit` | 日频涨跌停价格；仅 `daily` |
| `suspensions` | `suspend_d` | 停复牌记录；仅 `daily` |
| `trade_calendar` | `trade_cal` | 代码所属交易所的开闭市日历；仅 `daily` |

```json
{
  "ts_code":"000001.SZ",
  "start_date":"2026-05-01",
  "end_date":"2026-08-20",
  "frequency":"daily"
}
```

因此 `daily` 会得到六个数据集；`weekly/monthly` 只得到 `price_bars` 和
`adjustment_factors`，不会把日频估值、涨跌停、停牌或日历混进周/月线上下文。风险/可交易性
和量能/流动性计算器明确拒绝周/月线引用。完整
`ServiceDataset` 会保存到当前 run 的 `ResearchDataStore`，Tool 只返回 `context_ref`、
数据清单和每个数据集默认 5 行预览。Agent 不应自己心算 MA、MACD、RSI、ATR，
也不应把预览行复制给计算器；应把 `context_ref` 交给 4.5～4.9 的确定性计算 Tool。

### 4.3 `get_index_market_context`

用途：取得市场指数、中证行业指数或申万行业指数行情，用于判断市场/板块环境，并作为五个确定性技术
计算器的目标或基准。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 市场、中证或申万指数代码，例如 `000300.SH`、`000013.CSI`、`801780.SI` |
| `start_date` | 是 | date | 起始日期 |
| `end_date` | 是 | date | 结束日期，同时用于查询单日指数指标 |

内部数据集：

- 普通市场/中证指数：`index_price_bars` → `index_daily`，并读取
  `index_daily_metrics` → `index_dailybasic`；
- 申万 `.SI`：`index_price_bars` → `sw_daily`，不虚构不存在的普通指数日指标表。

```json
{"ts_code":"000013.CSI","start_date":"2026-05-01","end_date":"2026-08-20"}
```

如果 `end_date` 不是交易日，单日指标可能为空。应先查询交易日历或使用最后一个交易日。
该 Tool 同样返回当前 run 内有效的 `context_ref`，既可交给四个单目标计算器，也可作为
`calculate_relative_strength` 的目标或基准。

当前契约支持 `.SH/.SZ/.BJ/.CSI/.SI`。`.SI` 由程序自动改用 `sw_daily`，Agent 不需要也
不允许自己选择底层接口。

### 4.4 `get_fund_market_context`

用途：用行业 ETF、宽基 ETF 或基金数据做板块/市场技术分析、跨资产比较和资金申赎验证。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `ts_code` | 是 | string | — | 基金/ETF 代码 |
| `start_date` | 是 | date | — | 起始日期 |
| `end_date` | 是 | date | — | 结束日期 |
| `include_adjustment_factors` | 否 | boolean | `true` | 是否读取基金复权因子 |
| `include_share_history` | 否 | boolean | `false` | 是否读取 ETF 份额变化 |

内部数据集：

- `fund_price_bars` → `fund_daily`；
- `fund_adjustment_factors` → `fund_adj`，仅在选项为 `true` 时查询；
- `etf_share_history` → `etf_share_size`，仅在选项为 `true` 时查询。

```json
{
  "ts_code":"510300.SH",
  "start_date":"2026-05-01",
  "end_date":"2026-08-20",
  "include_adjustment_factors":true,
  "include_share_history":true
}
```

ETF 份额接口在部分上游可能无权限，此时结果可能是 `partial`，不能把缺失份额数据解释为
“份额没有变化”。行情 `context_ref` 可交给全部五个确定性技术计算器；是否请求份额历史
不影响价格、动量、风险和核心量价计算。

以上三个行情 Tool 采用存储型返回契约，而不是其他原始 Tool 的完整 `rows`
契约。它们的 `datasets[]` 包含：

```text
label / api_name / query_params / requested_fields
preview_rows[]
received_item_count / stored_item_count / preview_item_count / discarded_item_count
data_as_of / complete
preview_complete / preview_strategy = provider_order_head
source_summary[]
```

`preview_complete=false` 只表示 Agent 看到的是预览，不表示 Store 中的完整数据被截断。

### 4.5 `calculate_return_and_trend`

用途：从行情引用中确定性计算区间收益、多窗口收益、简单/指数移动平均、均线斜率、
价格相对均线位置、区间高低点、均线交叉和突破事实。

| 字段 | 必填 | 类型 | 默认值 | 限制 |
|---|---|---|---|---|
| `context_ref` | 是 | string | — | 当前 run 内由三个行情原始 Tool 之一返回 |
| `windows` | 否 | integer tuple | `[5,20,60]` | 1～8 个不重复值，每个 2～500；单位是当前数据频率的“期” |

```json
{"context_ref":"ctx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","windows":[5,20,60]}
```

成功时 `calculation` 是 `ReturnAndTrendResult`：

```text
metadata
latest_close / interval_return_ratio
period_returns[]
simple_moving_averages[] / exponential_moving_averages[]
moving_average_slopes[] / close_to_moving_average_ratios[]
range_high / range_low
distance_to_range_high_ratio / distance_to_range_low_ratio
crossovers[] / breakouts[]
```

其中窗口指标统一包含 `window / value / status / sample_size / reason`；
`crossovers[]` 给出快慢窗口、方向、日期和原因，`breakouts[]` 给出窗口、方向、参考价、
日期和原因。

### 4.6 `calculate_momentum`

用途：确定性计算 RSI、MACD、ROC 和动量背离条件。

| 字段 | 必填 | 类型 | 默认值 | 限制 |
|---|---|---|---|---|
| `context_ref` | 是 | string | — | 当前 run 内的行情引用 |
| `rsi_period` | 否 | integer | `14` | 2～100 |
| `macd_fast` | 否 | integer | `12` | 2～100，必须小于 `macd_slow` |
| `macd_slow` | 否 | integer | `26` | 3～200 |
| `macd_signal` | 否 | integer | `9` | 2～100 |
| `roc_periods` | 否 | integer tuple | `[5,20]` | 1～8 个不重复值，每个 1～500 |

成功时 `calculation` 是 `MomentumResult`，包含 `metadata`、`rsi`、`macd`、
`rate_of_change[]` 和 `divergence`。`macd` 显式返回快/慢/信号周期、MACD、信号线、
柱值、状态、样本数和不可用原因；背离返回 `direction / lookback / reason`。

### 4.7 `calculate_risk_and_tradability`

用途：计算年化波动率、下行波动率、ATR、最大/当前回撤、跳空，并在原始数据具备时
统计涨跌停触及与收盘、停牌和可交易性。

| 字段 | 必填 | 类型 | 默认值 | 限制 |
|---|---|---|---|---|
| `context_ref` | 是 | string | — | 当前 run 内的行情引用 |
| `volatility_window` | 否 | integer | `20` | 2～500 |
| `atr_period` | 否 | integer | `14` | 2～200 |

成功时 `calculation` 是 `RiskAndTradabilityResult`，包含：

```text
metadata
annualized_volatility / annualized_downside_volatility / average_true_range
maximum_drawdown_ratio / current_drawdown_ratio
largest_up_gap_ratio / largest_down_gap_ratio
tradability
```

`tradability` 记录预期/实得开市日、缺失交易日、停牌日、无法解释的缺失日，以及涨跌停
触及和收盘次数。个股数据包中的涨跌停、停牌或交易日历若获取失败，外层会以
`partial`/`DATA_INTEGRITY` 披露相关源数据不完整，不能把“缺输入”解释成“没有风险”。

第一版该计算器接受日频 `stock_price_context`、`index_market_context` 和
`fund_market_context`。三类目标都计算通用的价格风险；只有个股会额外读取
`price_limits`、`suspensions` 和 `trade_calendar` 并生成可交易性统计。指数/基金的
`tradability.status` 为 `not_applicable`，而不是伪造为 0 或误报为输入缺失。周/月线仍返回
`INVALID_ARGUMENT`，不会把日频附属数据错误地混入计算。

### 4.8 `calculate_volume_and_liquidity`

用途：计算成交量/成交额移动平均、相对成交量、换手率、OBV、Amihud 非流动性代理和
量价组合计数。

| 字段 | 必填 | 类型 | 默认值 | 限制 |
|---|---|---|---|---|
| `context_ref` | 是 | string | — | 当前 run 内的行情引用 |
| `windows` | 否 | integer tuple | `[5,20]` | 1～8 个不重复值，每个 2～500 |

成功时 `calculation` 是 `VolumeAndLiquidityResult`，包含 `metadata`、最新成交量/成交额、
成交量与成交额移动平均、相对成交量、最新换手率、换手率均值和分位、供应商量比、OBV、
Amihud 非流动性，以及四种量价同向/背离组合的 `regime_counts`。

第一版该计算器接受个股、市场/中证/申万行业指数和基金/ETF 的日频行情引用。三类目标都计算
成交量/成交额均值、相对成交量、OBV、Amihud 和量价组合；个股再使用 `daily_basic`
计算换手率和上游量比。指数/基金的这些个股专属字段会标为 `not_applicable`，仅有“不适用”
不会把外层状态降为 `partial`。相对量和量价状态适合在同一标的自身历史中解释；OBV 等
绝对量纲指标不应直接跨股票、指数和 ETF 比大小。周/月线引用仍返回 `INVALID_ARGUMENT`。

### 4.9 `calculate_relative_strength`

用途：在目标与基准的共同交易日上计算全区间/多窗口超额收益、滚动相关性、Beta、上涨/下跌时的
平均超额收益和相对新高事实。

| 字段 | 必填 | 类型 | 默认值 | 限制 |
|---|---|---|---|---|
| `target_context_ref` | 是 | string | — | 目标股票、指数或基金/ETF 的行情引用 |
| `benchmark_context_ref` | 是 | string | — | 基准股票、指数或基金/ETF 的行情引用；不能与目标是同类同代码 |
| `windows` | 否 | integer tuple | `[20,60]` | 1～8 个不重复值，每个 2～500 |

成功时 `calculation` 是 `RelativeStrengthResult`，包含目标/基准各自的 `metadata`、
共同日期对齐摘要 `alignment`、全区间目标/基准/超额收益、各窗口 `period_metrics[]`、
滚动相关系数、滚动 Beta、上涨/下跌样本平均超额收益，以及
`target_new_high_without_benchmark`。`alignment` 会显式报告共同样本数、仅目标/仅基准的
样本数和共同起止日期。

五个计算器的共同安全规则：

- `context_ref` 只在创建它的 `run_id` 内有效；
- Tool schema 不接受 `raw rows`、`run_id`、`as_of`、`provider`、`api_name` 或任意数据集；
- 完整行情由计算器直接从 Store 读取，不经过 LLM 转述；
- 输出是确定性测量，不是涨跌预测或投资建议。

多目标复用矩阵：

| 计算器 | 个股 | 市场/中证/申万行业指数 | 基金/ETF | 标的特有限制 |
|---|---|---|---|---|
| 收益与趋势 | 支持 | 支持 | 支持 | 无 |
| 动量 | 支持 | 支持 | 支持 | 无 |
| 风险与可交易性 | 支持 | 支持 | 支持 | 指数/基金只计算价格风险，可交易性为 `not_applicable` |
| 量能与流动性 | 支持 | 支持 | 支持 | 指数/基金不计算个股 `daily_basic` 换手字段 |
| 相对强弱 | 支持任意两类组合 | 支持任意两类组合 | 支持任意两类组合 | `as_of` 与频率必须一致 |

这张矩阵描述的是**单标的时间序列**复用。全市场涨跌家数、上涨占比、收益分位数等“市场
宽度”，以及板块内部成分参与度，依赖同一交易日的横截面股票快照/板块成分表，不能从一条
指数 K 线推导。第一版不增加名字正确但输入错误的计算器；待加入 `DailyMarketSnapshot` 和
可靠板块成分数据后，再单独实现 `calculate_market_breadth` 与
`calculate_sector_participation`。

共同的 `metadata` 包含 `observation_count / start_date / end_date / adjustment_mode /
adjustment_applied / issues[]`。标量指标通常包含 `value / status / sample_size / reason`；
`status` 可能是 `available`、`insufficient_history`、`missing_input` 或 `not_applicable`。
因此 `value=null` 必须结合状态和原因阅读，不能直接当作数值 0。

计算器外层实际使用 `ok / partial / error`：所有请求指标与相关源数据完整时为 `ok`；短历史、
缺可选输入、相关源数据不完整时仍保留可用的 `calculation`，同时返回 `partial`、
`complete=false` 和 `CALCULATION_INCOMPLETE` 或 `DATA_INTEGRITY`；引用/组合非法或必需数据
缺失时为 `error` 且 `calculation=null`。基金原始 Tool 若显式设置
`include_adjustment_factors=false`，计算器会按 `adjustment_mode="raw"` 合法计算；这不等于
错误地宣称已经复权。

## 5. 基本面分析 Tool

### 5.1 `get_daily_fundamental_snapshot`

用途：基本面研究员每日模式的首个入口。程序自动选择最近完整交易日和当前通常已进入披露
窗口的报告期，把市场、行业和代表性个股数据压缩为可阅读候选；完整批量源表进入 `context_ref`。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `candidate_count` | 否 | integer | `10` | 每组最多保留 3～20 只股票 |
| `announcement_lookback_days` | 否 | integer | `14` | 业绩预告/快报回看 7～60 个自然日 |

```json
{"candidate_count":10,"announcement_lookback_days":14}
```

`snapshot` 的五部分是：

- `valuations`：正 PE 最低/最高、PB 最高、股息率最高；这些只是横截面极值，不等于低估或高估结论；
- `earnings_events`：近期预告中盈利变动中点最强/最弱，以及最近业绩快报；
- `financial_quality`：ROE、ROE 同比变化、资产负债率、经营现金流/营业收入等候选；
- `sector_fundamentals`：按 `stock_basic.industry` 聚合的行业估值、财务质量和近期披露活动候选，保留有效样本数和代表股；该口径不是申万行业；
- `macro_and_rates`：GDP、CPI、PPI、货币供应、社融、PMI、Shibor、LPR、美国国债收益率曲线的最近两期原始观测，不混合量纲计算总分。

股票目录用于把代码映射为名称和 `industry`，行业聚合只是已取全市场数据的确定性重组，并未
新增一套外部行业行情来源。任一批量或宏观接口失败时，成功部分仍
可返回 `partial`，具体缺口进入 `issues`；所有分析来源均无有效数据时失败关闭。Agent 应从候选中
挑少量目标，再调用以下个股 Tool 查证，不能把快照候选直接升级为投资建议。

### 5.2 `get_financial_statements`

用途：一次取得同一报告期的三大财务报表。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `period` | 是 | `YYYYMMDD` | 财务报告期 |

内部数据集：`income_statement` → `income`、`balance_sheet` → `balancesheet`、
`cash_flow_statement` → `cashflow`。

```json
{"ts_code":"600519.SH","period":"20251231"}
```

### 5.3 `get_financial_quality`

用途：结合财务指标、主营构成和审计意见判断盈利质量与经营结构。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `ts_code` | 是 | string | — | 股票代码 |
| `period` | 是 | `YYYYMMDD` | — | 报告期 |
| `composition_type` | 否 | enum | `P` | `P` 产品、`D` 地区、`I` 行业 |

内部数据集：`financial_indicators` → `fina_indicator`、`business_composition` →
`fina_mainbz`、`audit_opinion` → `fina_audit`。

```json
{"ts_code":"600519.SH","period":"20251231","composition_type":"P"}
```

需要同时检查多种主营口径时，应分别调用三次，不要把 `composition_type` 写成数组。

### 5.4 `get_earnings_and_disclosure`

用途：检查业绩预告、业绩快报和财报披露日程。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `period` | 是 | `YYYYMMDD` | 报告期 |

内部数据集：`earnings_forecast` → `forecast`、`earnings_express` → `express`、
`disclosure_schedule` → `disclosure_date`。

```json
{"ts_code":"000001.SZ","period":"20260630"}
```

预告或快报为空是常见的合法情况，只能表述为“该报告期在当前数据源中没有对应记录”。

### 5.5 `get_dividend_and_ownership_context`

用途：检查分红、前十大股东、前十大流通股东和股东人数变化。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `period` | 是 | `YYYYMMDD` | 股东名单对应报告期 |
| `start_date` | 是 | date | 股东人数查询起点 |
| `end_date` | 是 | date | 股东人数查询终点 |

内部数据集：`dividends` → `dividend`、`top_holders` → `top10_holders`、
`top_floating_holders` → `top10_floatholders`、`holder_counts` → `stk_holdernumber`。

```json
{
  "ts_code":"000001.SZ",
  "period":"20251231",
  "start_date":"2025-01-01",
  "end_date":"2026-08-20"
}
```

注意：分红接口本身没有日期参数，可能返回该股票的历史分红；必须结合行内公告日和 `data_as_of`
判断可用范围。

### 5.6 `get_pledge_risk_context`

用途：检查股权质押比例、笔数和质押明细。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |

内部数据集：`pledge_statistics` → `pledge_stat`、`pledge_details` → `pledge_detail`。

```json
{"ts_code":"000001.SZ"}
```

该 Tool 暂无日期窗口。如果历史明细超过 Tool 输出限制，会返回 `too_large`；此时 Agent 不得
自行提高限制，应把数据缺口交给协调器；后续需通过专门的服务端筛选或摘要能力解决。

### 5.7 `get_valuation_context`

用途：取得区间 PE、PB、股息率、换手率和总/流通市值。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `start_date` | 是 | date | 起始日期 |
| `end_date` | 是 | date | 结束日期 |

内部数据集：`daily_valuation` → `daily_basic`。

```json
{"ts_code":"600519.SH","start_date":"2026-01-01","end_date":"2026-08-20"}
```

### 5.8 `get_china_macro_context`

用途：一次取得中国增长、价格、货币、社融和景气数据。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `start_month` | 是 | `YYYYMM` | 月度指标起点 |
| `end_month` | 是 | `YYYYMM` | 月度指标终点 |
| `start_quarter` | 是 | `YYYYQn` | GDP 起始季度 |
| `end_quarter` | 是 | `YYYYQn` | GDP 结束季度 |

内部数据集：

| `label` / API | 内容 |
|---|---|
| `gdp` / `cn_gdp` | GDP |
| `cn_cpi` | CPI |
| `cn_ppi` | PPI |
| `cn_m` | 货币供应 |
| `sf_month` | 社会融资规模 |
| `cn_pmi` | PMI |

```json
{
  "start_month":"202601",
  "end_month":"202607",
  "start_quarter":"2025Q1",
  "end_quarter":"2026Q2"
}
```

月度/季度观测期不等于真实发布日期，且宏观数据可能修订。第一版只适合当前研究，不得把它
宣称为已经完全消除修订穿越的历史回测数据。

### 5.9 `get_interest_rate_context`

用途：读取一条利率序列，判断流动性、融资环境和估值贴现率变化。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `series` | 是 | enum | 见下方列表 |
| `start_date` | 是 | date | 起始日期 |
| `end_date` | 是 | date | 结束日期 |

允许的 `series`：

```text
shibor       上海银行间同业拆放利率
shibor_lpr   LPR
wz_index     温州民间融资综合利率
gz_index     广州民间借贷利率
us_tycr      美国国债收益率曲线
us_trycr     美国国债实际收益率曲线
us_tbr       美国短期国债利率
us_tltr      美国长期国债利率
us_trltr     美国实际长期利率平均值
```

内部数据集标签固定为 `interest_rate_series`，实际 Provider API 等于 `series`。

```json
{"series":"shibor_lpr","start_date":"2026-01-01","end_date":"2026-08-20"}
```

## 6. 事件分析 Tool

### 6.1 `get_daily_event_snapshot`

用途：新闻事件每日模式的首个入口。它聚合三路最近快讯、近期全市场公告索引、近期卖方研报摘要、
当月券商荐股和当前上市股票目录。完整标准化源表写入 run-scoped `ResearchDataStore`，只把
确定性筛选后的有限候选和 `context_ref` 返回给模型。

| 字段 | 必填 | 类型 | 默认值 | 范围 |
|---|---|---|---|---|
| `candidate_count` | 否 | integer | `10` | 新闻候选和公告候选各保留 3～20 条 |
| `news_lookback_hours` | 否 | integer | `24` | 从冻结时间向前 1～24 小时 |
| `announcement_lookback_days` | 否 | integer | `3` | 从冻结日期向前 1～7 个自然日 |
| `research_lookback_days` | 否 | integer | `7` | 从冻结日期向前逐日读取 1～14 天卖方研报摘要 |

```json
{
  "candidate_count": 10,
  "news_lookback_hours": 24,
  "announcement_lookback_days": 3,
  "research_lookback_days": 7
}
```

`snapshot.market_news[]` 包含标题、摘要、发布时间、来源名/URL、源数据集标签、原始
`record_keys`、`citable` 和程序按当前上市股票全名确定性建立的 `related_stocks`；
`snapshot.announcements[]` 包含股票、公告类型/日期、URL、选择信号和原始定位；
`snapshot.sell_side_reports[]` 聚合同一份研报的多个预测期，保留机构、作者、评级、目标价、
预测点和全部 `supporting_record_keys`；`snapshot.broker_recommendations[]` 按月份和股票聚合券商
名单，并保留构成聚合结果的全部 `supporting_record_keys`。引用聚合后的预测期、机构数量或名单时
必须完整引用这些键，不能用一条原始行替代整个聚合结论。
`coverage.recent_feed_is_complete_history=false` 是固定事实：公开快讯只返回各站点最近若干条，
不能据此宣称窗口内新闻已经穷尽。研报评级、预测、目标价和荐股只证明机构表达过观点，不是结果
ground truth；结构化 `report_rc` 不代表已取得研报全文。

任一公开来源失败时，只要仍有其他数据集可用就返回 `partial`；全部失败返回 `error`。快照过大时
返回 `too_large`，但已经保存的 `context_ref` 仍保留。

### 6.2 `search_market_news`

用途：读取冻结时间之前的公开市场快讯，适合市场或板块层面的补充查证。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `start_at` | 是 | aware datetime | — | 窗口起点，必须含时区 |
| `end_at` | 是 | aware datetime | — | 窗口终点，必须含时区 |
| `source` | 否 | enum | `ALL` | `ALL`、`EASTMONEY`、`THS` 或 `CLS` |

内部数据集按来源为 `market_news_eastmoney` → `stock_info_global_em`、
`market_news_ths` → `stock_info_global_ths`、`market_news_cls` → `stock_info_global_cls`。
`ALL` 会并行查询三项。

```json
{
  "start_at":"2026-08-20T14:00:00+08:00",
  "end_at":"2026-08-20T15:00:00+08:00",
  "source":"ALL"
}
```

调用规则：

- 单次窗口最长 24 小时，推荐先用 1～6 小时；
- `end_at` 不得晚于冻结 `as_of`；
- 该 Tool 不按股票代码过滤；个股新闻应使用 6.3；
- 三个最近快讯数据集的 `complete=false`，即使返回 `empty` 也不证明历史窗口内没有新闻；
- 财联社当前行没有原文 URL，`citable=false`，只能用于发现或交叉验证；
- 新闻过多或正文过长会返回 `too_large`，此时缩短时间窗口或指定来源；
- 不得把“某个窗口没有新闻”推广成“公司近期没有任何事件”。

该 Tool 通过 `PublicNewsEventService` 调用 AKShare，不调用旧 Tushare `major_news`。

### 6.3 `get_targeted_news_and_disclosures`

用途：指定股票的新闻和公告联合查证入口。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `ts_code` | 是 | string | — | A 股代码，例如 `000001.SZ` |
| `start_date` | 是 | date | — | 查询起点 |
| `end_date` | 是 | date | — | 查询终点，不能晚于冻结日期 |
| `announcement_category` | 否 | enum | `全部` | 全部、重大事项、财务报告、融资公告、风险提示、资产重组、信息变更或持股变动 |

```json
{
  "ts_code":"000001.SZ",
  "start_date":"2026-08-01",
  "end_date":"2026-08-20",
  "announcement_category":"重大事项"
}
```

单次 Tool 日期窗口最多 31 天。内部数据集：`stock_news` → `stock_news_em`、
`stock_announcements` → `stock_individual_notice_report`。新闻接口只返回最近若干条，因此
`stock_news.complete=false`；公告只含索引和 URL，不代表正文已经下载或解析。

### 6.4 `get_sell_side_research_context`

用途：指定股票的卖方研报摘要与月度券商荐股联合查证入口。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | A 股代码 |
| `start_date` | 是 | date | 查询起点 |
| `end_date` | 是 | date | 查询终点，不能晚于冻结日期 |

```json
{"ts_code":"000001.SZ","start_date":"2026-01-01","end_date":"2026-08-20"}
```

日期窗口最多 366 天。内部数据集：`sell_side_reports` → `report_rc`，以及闭区间覆盖月份的
`broker_recommendations_YYYYMM` → `broker_recommend`。前者返回研报标题、类型、机构、作者、
评级、目标价和预测摘要，不含研报全文；后者只表示该券商把该股票列入该月名单。两者都不得被
改写为未来业绩、合理价值或收益已经得到验证。

当前 `broker_recommend` 上游不提供足以证明历史快照的时点语义，因此该数据集只允许当前运行使用；
历史 `as_of` 会失败关闭，避免把今天看到的名单倒灌到过去。

### 6.5 `get_corporate_action_events`

用途：读取回购、解禁、股东增减持和分红事件。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `start_date` | 是 | date | 事件查询起点 |
| `end_date` | 是 | date | 事件查询终点 |

内部数据集：`repurchase_events` → `repurchase`、`unlock_events` → `share_float`、
`holder_trades` → `stk_holdertrade`、`dividends` → `dividend`。

```json
{"ts_code":"000001.SZ","start_date":"2026-01-01","end_date":"2026-08-20"}
```

解禁日期可以位于未来，只要解禁计划是在 `as_of` 之前公告。分红数据本身不按输入区间查询，
Service 会在本地按 `ann_date` 公告日再次执行闭区间过滤；这不是除权日、股权登记日窗口。

### 6.6 `get_economic_calendar`

用途：读取财经事件日历，检查宏观事件与市场表现是否存在时间共现。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `start_date` | 是 | date | 起始日期 |
| `end_date` | 是 | date | 结束日期 |

内部数据集：`economic_calendar` → `eco_cal`。

```json
{"start_date":"2026-08-01","end_date":"2026-08-20"}
```

当前 Service 要求 `end_date <= as_of`，因此它用于已发生/已冻结的事件，不用于查询未来日历。

`get_earnings_and_disclosure` 也在事件研究员白名单中，字段见 5.4。

## 7. 情绪与资金分析 Tool

### 7.1 `get_daily_sentiment_flow_snapshot`

用途：情绪与资金研究员每日模式的首个入口。程序自动选择最近完整交易日，并一次返回市场、
行业和异常个股尺度的资金与情绪候选；完整源表通过 `context_ref` 保存在当前 run。

| 字段 | 必填 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `candidate_count` | 否 | integer，3～20 | `10` | 每个资金流或涨跌停候选分组最多保留的证券数量 |

```json
{"candidate_count":10}
```

返回的 `snapshot` 主要包含：

| 字段 | 内容 |
|---|---|
| `trade_date` | 自动选出的最近完整交易日 |
| `technical_context` | 复用每日技术快照的指数、行业宽度和异常个股背景 |
| `market_flow` | 最近 14 日沪深港通资金、大盘资金流和沪深两市融资融券汇总 |
| `industry_top_inflows/outflows` | 同花顺行业净流入/净流出候选 |
| `stock_candidates` | THS/DC 个股净流入/流出、涨跌停强度和开板次数候选 |
| `coverage` | 各来源实际记录数和可选来源失败数 |

内部组合 `moneyflow_ths`、`moneyflow_dc`、`moneyflow_ind_ths`、
`moneyflow_mkt_dc`、`moneyflow_hsgt`、`limit_list_d`、两市 `margin`，并复用
`get_daily_technical_market_snapshot` 的确定性 Service。单项上游失败时返回 `partial`；所有资金与
情绪来源都失败时返回 `error`。THS 与 DC 数值口径不能相加，只用于方向和持续性的交叉验证。

### 7.2 `get_stock_active_money_flow_context`

用途：对每日快照发现的少量候选，或查证节点指定的个股，读取两套主动资金流口径做区间验证。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `start_date` | 是 | date | 区间起点 |
| `end_date` | 是 | date | 区间终点 |

内部数据集：`stock_moneyflow_ths` → `moneyflow_ths`、
`stock_moneyflow_dc` → `moneyflow_dc`。

```json
{"ts_code":"600519.SH","start_date":"2026-08-01","end_date":"2026-08-21"}
```

模型应分别观察两个来源的大/中/小单方向、净额占比和连续性，不能把两套供应商的净额求和。

### 7.3 `get_capital_flow_context`

用途：结合北向持股、融资融券和两融市场汇总观察外资与杠杆资金行为。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `start_date` | 是 | date | 区间起点 |
| `end_date` | 是 | date | 区间终点，同时作为资格和市场汇总查询日 |
| `exchange_id` | 是 | string，1～20 字符 | 例如 `SSE` 或 `SZSE` |

内部数据集：

| `label` | Provider API | 内容 |
|---|---|---|
| `northbound_holdings` | `hk_hold` | 北向持股 |
| `stock_margin_detail` | `margin_detail` | 个股融资融券明细 |
| `margin_eligibility` | `margin_secs` | `end_date` 当日两融资格 |
| `market_margin_summary` | `margin` | `exchange_id` 市场在 `end_date` 的汇总 |

```json
{
  "ts_code":"600519.SH",
  "start_date":"2026-05-01",
  "end_date":"2026-08-20",
  "exchange_id":"SSE"
}
```

`exchange_id` 必须和股票所在交易所一致。若不确定，应从身份 Tool 的市场字段推导，而不是猜测。

### 7.4 `get_unusual_trading_activity`

用途：检查某一交易日的大宗交易、龙虎榜和机构席位。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ts_code` | 是 | string | 股票代码 |
| `trade_date` | 是 | date | 单个交易日 |

内部数据集：`block_trades` → `block_trade`、`top_list` → `top_list`、
`top_institutions` → `top_inst`。

```json
{"ts_code":"000001.SZ","trade_date":"2026-08-20"}
```

三个子查询全部为空可以是合法 `empty`，表示该股票在该日没有这些记录；但不能据此推断整个
时间区间都没有异常交易。需要多日研究时，应按交易日分次调用。

情绪与资金研究员还共享 `get_fund_market_context` 和 `search_market_news`，字段分别见 4.4 和 6.1。

## 8. 返回 JSON 的读取规则

除三个行情原始 Tool 和四个每日快照 Tool 外，其他原始 Tool 仍返回完整 `rows`：

```text
tool_name
status
as_of
datasets[]
  label
  api_name
  query_params
  requested_fields
  rows[]
    data
    source
      provider
      from_cache
      fetched_at
      page_index
      source_offset
  received_item_count
  returned_item_count
  discarded_item_count
  data_as_of
  complete
  source_summary[]
issues[]
total_returned_items
complete
```

三个存储型行情 Tool 返回：

```text
tool_name / status / as_of / context_ref
datasets[]
  label / api_name / query_params / requested_fields
  preview_rows[]
  received_item_count / stored_item_count / preview_item_count / discarded_item_count
  data_as_of / complete / preview_complete / preview_strategy
  source_summary[]
issues[]
total_stored_items / total_preview_items / complete
```

`context_ref` 是对 Store 中完整 `ServiceDataset` 的不透明引用，不是 URL，也不是跨运行
持久 ID。第一版 Store 在进程内存中实现；当前 run 清理后引用立即失效。

四个每日快照 Tool 使用各自同构的小型信封：

```text
tool_name / status / as_of / context_ref
snapshot
issues[]
source_dataset_count / total_stored_items / complete
```

`snapshot` 分别是技术、情绪资金、基本面或新闻事件的确定性聚合结果，`context_ref` 指向生成它的全部原始 `ServiceDataset`。若聚合前的
任何必要查询失败，Tool 返回 `error` 且不伪造半份市场快照；若最终 JSON 超限，则保留引用并
返回 `too_large`，但不把超限快照放进模型上下文。

五个计算器返回共同信封：

```text
tool_name / status / as_of
source_context_refs[]
source_subjects[]
  context_ref / bundle_kind / ts_code / frequency
calculation
issues[]
complete
```

`source_context_refs[]` 保留供程序追踪的不透明引用；`source_subjects[]` 按输入顺序提供可读
标的身份，避免 Agent 仅凭引用猜测这是个股、指数还是基金。相对强弱固定先列目标、再列
基准。

`calculation` 中是确定性计算模型的 JSON，具体字段已按 4.5～4.9 固化。计算器只会实际
返回 `ok`、`partial` 或 `error`；其中 `partial` 会保留
可用的 `calculation`，必须结合 `issues` 和各指标的 `status` 阅读。

### 8.1 `status`

| 状态 | 含义 | Agent 行为 |
|---|---|---|
| `ok` | 全部子查询成功，至少有一行 | 可以分析数据并生成候选证据 |
| `empty` | 全部子查询成功，但所有数据集均为空 | 只证明查询条件下无记录，不得扩大结论范围 |
| `partial` | 原始 Tool 部分数据集失败，或计算器存在不可得指标/相关源数据不完整 | 可使用成功部分，但必须披露缺失项；按 issue 决定是否重试 |
| `error` | 所有子查询失败 | 不得生成“数据为零/事件不存在”的证据 |
| `too_large` | 输出超过 Tool 限制 | 普通 Tool 需缩小窗口；存储型 Tool 若仍有 `context_ref`，可直接交给计算器 |

### 8.2 `issues[].code`

| 错误码 | 含义 | 是否通常重试 |
|---|---|---|
| `INVALID_ARGUMENT` | 参数格式或业务范围错误 | 否；修正参数 |
| `DATA_INTEGRITY` | 分页/schema/计算必需源数据不完整 | 取决于 `retryable`；计算缺数据时重新获取行情引用，否则通知维护者 |
| `UPSTREAM_UNAVAILABLE` | 主备均不可用、传输失败或限流 | 是；由协调器按预算稍后重试 |
| `CAPABILITY_UNAVAILABLE` | 权限、认证或接口能力不存在 | 否；换用现有能力 |
| `RESULT_TOO_LARGE` | 行数、单行或总 JSON 超限 | 普通 Tool 缩小窗口；存储型 Tool 若保留 ref 则无需重取 |
| `CALCULATION_INCOMPLETE` | 技术计算完成，但短历史、缺可选输入或不可用指标使结果不完整 | 否；扩大原始行情窗口或补齐源数据后重新获取 ref |
| `INTERNAL_ERROR` | 程序错误 | 否；保留 `correlation_id` 给维护者 |

只在 `retryable=true` 时由协调器安排重试。Agent 自己不应无限循环调用。

### 8.3 `datasets[]`

- `label` 是复合 Tool 内稳定的业务名称，后续 Agent prompt 应优先引用它；
- `api_name` 用于追踪实际底层接口，不允许把它复制回下一次 Tool 参数；
- `rows[].data` 是原始字段，不同接口可能拥有不同 schema；
- `rows[].source` 是该行的真实来源；
- `received_item_count` 是上游收到的行数；
- `returned_item_count` 是本地筛选、`as_of` 过滤和去重后交给 Agent 的行数；
- `discarded_item_count` 不能被当成异常，它可能来自股票过滤、未来数据过滤或去重；
- `data_as_of` 是最终保留数据中可验证的最新日期；
- `complete=false` 时不得宣称数据完整。

对存储型行情 Tool：

- `stored_item_count` 是 Store 中最终保存的完整行数；
- `preview_item_count` 是真正进入 LLM 上下文的行数，每数据集默认最多 5 行；
- `preview_strategy="provider_order_head"` 表示保留 Service 结果顺序的前几行，不宣称它是随机样本；
- `preview_complete=false` 时计算器仍会读取 Store 内的所有行；
- `partial` 会把成功的数据集保存并返回 `context_ref`，失败数据集仍记录在 `issues`；
- 所有子查询全部失败时不会创建数据包，`context_ref=null`。

### 8.4 来源规则

原始 Tool 的逐行来源中，`provider` 可能是 `PRIMARY` 或 `BACKUP`，`from_cache=true` 表示使用了
备用缓存。这些字段保留在 Tool observation 和原始数据追踪中。当前 `SourceReference` 会提升
`provider`、`interface`、`record_key`、`published_at`、`fetched_at`、`data_as_of` 和可用的 `url`，
但没有 `from_cache` 字段，
因此 Evidence 不能声称自己保存了缓存命中状态。不要因为来自备用服务器就自动降低为假数据，
也不要因为来自主服务器就省略现有的溯源信息。

新闻事件 Agent 还要求草稿同时填写直接支持事实的 `source_record_keys`。每日快照中的新闻、公告、
研报和荐股候选都有稳定或派生的 `record_key`；程序用它反查 `ResearchDataStore` 中的实际原始行与
逐行 trace。定向查证 Tool 对没有原生 key 的结构化行也会生成确定性 key。无法定位到行、
`citable=false` 或行内标的与证据目标不一致时，证据会被拒绝，而不是退化成调用级粗引用。

## 9. 输出体积限制

默认限制：

```text
单次 Tool 最多 500 行
单行最多 40,000 字符
完整 JSON 最多 200,000 字符
存储型行情 Tool 每数据集默认预览 5 行
```

普通原始 Tool 超限时仍整体返回 `too_large`，不会静默返回前 500 行。三个存储型
行情 Tool 的 `max_items`、单行和 JSON 限制只针对预览；每日快照 Tool 只限制聚合后的 JSON。
完整数据都已进入 Store。如果预览或快照仍超限，Tool 会保留 `context_ref`。

## 10. 证据 Agent 必须遵守的通用约束

当前四位证据 Agent 都把这些约束分别落实在 system prompt、结构化输出 schema 和确定性子图中，
不是让模型直接调用 Tool 时才生效：

```text
1. 只能调用分配给你角色的 Tool，不得请求通用 API、Provider、分页或字段参数。
2. 业务输入未提供可信证券身份时，先由受控 Tool 或确定性编排器解析；不得凭代码猜公司名称。
3. 不得修改或推测 as_of；不得使用 as_of 之后的数据。
4. status=error 时不得把失败解释为数据为零；status=too_large 时先检查是否仍有 context_ref。
5. status=partial 时只使用成功部分；新闻事件装配器会自动把缺少的 dataset 写入 limitations。
6. status=empty 没有可引用行，不能形成 EvidenceRecord；只能记录为未解决问题，且不能扩大到其他日期或来源。
7. 原始 Tool 数据不是 EvidenceRecord；需要提炼事实、时间、单位和来源后再提交证据。
8. 不得让 LLM 心算可由程序计算的技术指标或财务比率。
9. 只在 retryable=true 且协调器预算允许时重试；禁止无限 Tool 循环。
10. 生成证据时必须保留 SourceReference 当前支持的 provider、interface、record_key、published_at、
    fetched_at、data_as_of 和可用的 url；from_cache 保留在原始 Tool observation 中，不得伪造为 Evidence 字段。
11. 计算器只传 context_ref 和受控参数，不得复制、改写或自行构造原始数组。
12. context_ref 不得跨 run 使用；引用失效时应在当前 run 重新调用原始 Tool。
13. 新闻事件证据还必须引用直接支持它的行级 record_key；公开新闻不可引用时只能用于发现，
    卖方评级、预测和荐股只能写成机构观点记录。
```

## 11. 程序中的注册与调用

```python
from stock_research_agent.agents.sentiment_flow import build_sentiment_flow_agent_graph
from stock_research_agent.agents.technical import build_technical_agent_graph
from stock_research_agent.tools import (
    EvidenceAgentRole,
    ResearchToolContext,
    build_agent_tool_registry,
)

context = ResearchToolContext(
    services=services,
    as_of=state["as_of"],
    run_id=state["run_id"],
    data_store=run_scoped_data_store,
)
registry = build_agent_tool_registry(context)

technical_tools = registry.for_role(EvidenceAgentRole.TECHNICAL)
technical_graph = build_technical_agent_graph(
    model=technical_reasoning_model,
    tool_context=context,
    tools=technical_tools,
)

sentiment_tools = registry.for_role(EvidenceAgentRole.SENTIMENT_FLOW)
sentiment_graph = build_sentiment_flow_agent_graph(
    model=sentiment_flow_reasoning_model,
    tool_context=context,
    tools=sentiment_tools,
)

fundamental_tools = registry.for_role(EvidenceAgentRole.FUNDAMENTAL)
fundamental_graph = build_fundamental_agent_graph(
    model=fundamental_reasoning_model,
    tool_context=context,
    tools=fundamental_tools,
)
```

当前阿里云 Qwen 配置使用 `with_structured_output(..., method="json_schema")` 生成测量或查证计划，
子图程序再调用上述 Tool；四个已实现 Agent 都没有把 Tool 直接绑定给 LLM。

直接测试某个工具：

```python
tool = next(tool for tool in technical_tools if tool.name == "get_stock_price_context")

result = await tool.ainvoke(
    {
        "ts_code": "000001.SZ",
        "start_date": "2026-05-01",
        "end_date": "2026-08-20",
        "frequency": "daily",
    }
)
```

`DataServices`、`ResearchDataStore` 和 Tool registry 必须由外层应用或一次研究运行创建并复用，
不能在每次 Tool 调用时重新打开 HTTP 连接池，也不能放进可持久化的
`ResearchGraphState`。运行结束并完成必要工件持久化后，应调用 `data_store.cleanup(run_id)`。
