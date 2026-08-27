# 行情数据双服务器故障转移接口文档

> 当前策略：**89 个已支持接口永远先请求主服务器；主服务器发生 Provider 失败后，自动回退备用服务器。**
> 代码依据：`src/stock_research_agent/providers/router.py` 与 `routes.py`。

## 1. 运行时策略

当前所有受支持接口执行同一条确定性流程：

```text
ProviderQuery
    │
    ├─ api_name 不在 89 项支持清单
    │      └─ UNKNOWN_PROVIDER_API（不产生 HTTP 请求）
    │
    └─ 请求主服务器
           ├─ 成功 → 返回 ProviderSource.PRIMARY
           │
           └─ ProviderError
                  ├─ 备用成功结果缓存命中
                  │      └─ 返回 ProviderSource.BACKUP，from_cache=true
                  │
                  └─ 缓存未命中 → 请求备用服务器
                         ├─ 成功 → 缓存一小时并返回
                         └─ 失败 → DATA_SOURCE_UNAVAILABLE
                                  （错误中保留主备双方的脱敏失败原因）
```

这里的“主服务器优先”具有严格含义：即使相同请求已经存在备用缓存，每次查询仍会先尝试
主服务器；只有主服务器本次失败后才允许读取备用缓存。

### 1.1 什么算主服务器失败

以下异常都继承自 `ProviderError`，因此都会触发回退：

| 错误 | 典型原因 |
| --- | --- |
| `PROVIDER_TRANSPORT_ERROR` | 连接失败、超时、非 2xx HTTP 状态 |
| `PROVIDER_AUTHENTICATION_ERROR` | API Key 无效或鉴权失败 |
| `PROVIDER_PERMISSION_DENIED` | 当前凭据没有接口权限 |
| `PROVIDER_RATE_LIMITED` | 每小时/每日调用次数已用完 |
| `PROVIDER_BUSINESS_ERROR` | 上游返回其他非零业务码 |
| `PROVIDER_SCHEMA_ERROR` | JSON、`fields`、`items` 或行列结构不合法 |

`HTTP 200` 不等于业务成功，两套上游都必须继续检查 JSON `code`。主服务器实测会用同一个
`40203` 表示无权限或限流，因此还必须解析 `msg`。

### 1.2 什么不算失败

- `code=0`、schema 合法但 `items=[]`：代表当前筛选条件没有数据，直接返回主服务器结果；
- `has_more=true`：代表需要由后续 Service 分页，不触发回退；
- 代码缺陷、断言错误等非 `ProviderError`：不应被路由层吞掉或伪装成上游故障。

## 2. Provider 配置

```text
TUSHARE_PRIMARY_BASE_URL=http://datahubco.com/app-api/openapi/v1/tushare
TUSHARE_PRIMARY_API_KEY=<仅在服务端环境配置>
TUSHARE_BACKUP_BASE_URL=https://quantdata888.duckdns.org
TUSHARE_BACKUP_TOKEN=<仅在服务端环境配置>
TUSHARE_REQUEST_TIMEOUT_SECONDS=30
```

不再存在 `TUSHARE_ALLOW_PAID_FALLBACK`：备用回退是固定运行策略，不是可选开关。

- 凭据只能保存在后端环境变量或密钥管理服务中，不能进入 Git、前端、日志或错误响应；
- 主入口目前仍是明文 HTTP，供应商配置 HTTPS 后应切换基地址并轮换曾通过 HTTP 传输的 Key；
- 备用服务器按流量计费，因此只缓存成功的备用结果，不缓存失败结果；
- 当前主备使用同一个请求超时。若两端都完整超时，串行故障转移最坏可能接近 60 秒。

## 3. 两套上游协议

### 3.1 主 REST 服务器

```http
GET {PRIMARY_BASE_URL}/{api-name-with-hyphens}?ts_code=000001.SZ&limit=100
X-API-Key: ${TUSHARE_PRIMARY_API_KEY}
Accept: application/json
```

接口路径会把下划线换成连字符：

- `stock_basic` → `/stock-basic`
- `daily_basic` → `/daily-basic`
- `top10_holders` → `/top10-holders`

主服务器响应示意：

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "fields": ["ts_code", "trade_date", "close"],
    "items": [["000001.SZ", "20260813", 12.34]],
    "has_more": false,
    "count": 0
  }
}
```

实测中存在 `items` 时 `count` 仍可能是 `0`，因此不得用 `count` 判断是否有数据。

### 3.2 备用 Tushare DataApi

```http
POST {BACKUP_BASE_URL}
Content-Type: application/json

{
  "api_name": "income",
  "token": "${TUSHARE_BACKUP_TOKEN}",
  "params": {
    "ts_code": "000001.SZ",
    "period": "20251231"
  },
  "fields": "ts_code,ann_date,end_date,total_revenue,n_income_attr_p"
}
```

备用服务器同样可能用 HTTP 200 返回业务错误，因此仍须检查业务码和消息。

Service 的统一分页器可能在 `params` 中加入 `limit/offset`，但当前备用兼容端不接受这两个
参数。`BackupTushareProvider` 会在发出 POST 前移除它们，并把备用端一次返回的完整结果作为
单页交回 Service；主服务器的 `limit/offset` 分页行为不受影响。Service 仍执行 `max_rows`、
字段、`as_of`、去重和来源一致性校验，因此这项协议适配不会绕过业务完整性检查。

## 4. 统一请求与返回契约

Agent 和 Service 只依赖统一 Provider 接口，不直接拼接主备协议。

### 4.1 `ProviderQuery`

```json
{
  "api_name": "daily",
  "params": {
    "ts_code": "000001.SZ",
    "start_date": "20260801",
    "end_date": "20260819"
  },
  "fields": [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close"
  ]
}
```

- `api_name`：89 项支持清单中的接口名；
- `params`：不包含 Token/API Key 的业务查询参数；
- `fields`：期望上游返回的字段名，不允许重复。

### 4.2 `ProviderResult`

```json
{
  "api_name": "daily",
  "provider": "PRIMARY",
  "from_cache": false,
  "fetched_at": "2026-08-19T15:30:00+08:00",
  "data_as_of": "2026-08-19",
  "fields": ["ts_code", "trade_date", "close"],
  "items": [
    {
      "ts_code": "000001.SZ",
      "trade_date": "20260819",
      "close": 12.34
    }
  ],
  "provider_code": 0,
  "has_more": false,
  "response_bytes": 256
}
```

- `provider` 表示本次实际采用 `PRIMARY` 还是 `BACKUP`；
- `from_cache=true` 只会出现在备用服务器成功结果缓存命中时；
- `fetched_at` 是实际获取时间，`data_as_of` 是数据自身时点，两者不能混用；
- `response_bytes` 用于后续统计备用流量成本。

## 5. 备用缓存语义

当前实现为进程内缓存，TTL 固定为 3600 秒：

```text
cache_key = SHA256(api_name + 规范化 params + 排序后的 fields)
```

规则如下：

1. 主服务器成功结果不由路由层缓存；
2. 主服务器失败后才允许读取备用缓存；
3. 备用服务器成功结果写入缓存；
4. 权限、限流、超时等失败不做负缓存；
5. 同一请求键使用异步锁，防止并发请求重复消耗备用流量；
6. 进程重启后缓存消失，后续可替换为 Redis 或持久化快照。

这层缓存是上游故障转移缓存，不替代 Service 层按交易日、报告期和公告时间维护的数据快照。

## 6. 89 项生产支持清单

下列接口全部使用同一条 `PRIMARY → BACKUP` 策略，不再具有单独的静态路由模式。

| 类别 | 数量 | `api_name` |
| --- | ---: | --- |
| 基础数据 | 12 | `trade_cal`, `stock_basic`, `etf_basic`, `etf_index`, `opt_basic`, `fund_basic`, `index_basic`, `index_classify`, `index_member_all`, `index_weight`, `namechange`, `new_share` |
| 行情与技术底层 | 16 | `daily`, `weekly`, `monthly`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `fund_daily`, `fund_adj`, `etf_share_size`, `index_daily`, `index_dailybasic`, `sw_daily`, `opt_daily`, `cb_daily`, `hk_hold` |
| 公司财务与披露 | 17 | `income`, `income_vip`, `balancesheet`, `balancesheet_vip`, `cashflow`, `cashflow_vip`, `forecast`, `forecast_vip`, `express`, `express_vip`, `dividend`, `fina_indicator`, `fina_indicator_vip`, `fina_audit`, `fina_mainbz`, `fina_mainbz_vip`, `disclosure_date` |
| 宏观背景 | 19 | `eco_cal`, `shibor`, `shibor_quote`, `shibor_lpr`, `libor`, `hibor`, `wz_index`, `gz_index`, `cn_gdp`, `cn_cpi`, `cn_ppi`, `cn_m`, `sf_month`, `cn_pmi`, `us_tycr`, `us_trycr`, `us_tbr`, `us_tltr`, `us_trltr` |
| 名单数据 | 2 | `stock_st`, `stock_hsgt` |
| 股东、事件与两融 | 14 | `top10_holders`, `top10_floatholders`, `pledge_stat`, `pledge_detail`, `repurchase`, `share_float`, `block_trade`, `stk_holdernumber`, `stk_holdertrade`, `top_list`, `top_inst`, `margin`, `margin_detail`, `margin_secs` |
| 新闻与卖方研究 | 3 | `major_news`, `report_rc`, `broker_recommend` |
| 特色情绪与资金 | 6 | `moneyflow_ths`, `moneyflow_dc`, `moneyflow_ind_ths`, `moneyflow_mkt_dc`, `moneyflow_hsgt`, `limit_list_d` |

上游权限和限流状态会变化，所以这里不保存“某接口当前只能走哪一端”的静态结论。路由器对
89 项一视同仁：每次先尝试主服务器，发生统一 Provider 错误后再查备用缓存/请求备用。

`major_news` 仍由 `NewsEventDataService` 保留为兼容能力，并使用最长 24 小时的冻结时间窗口，
不套用通用 offset 分页器；当前 Agent Tool 不再调用它。`report_rc` 与 `broker_recommend` 同样不
使用 offset 分页：每日全市场研报按精确报告日读取，指定股票研报使用有界日期窗口，荐股按月读取。

AKShare 新闻和公告接口不属于这 89 项，也不经过本节的 Tushare `PRIMARY → BACKUP` 路由。
当前已通过独立 `AkshareNewsProvider` 接入 6 个函数，由 `PublicNewsEventService` 统一执行
时点过滤、A 股代码标准化和溯源；Event Tool 使用它们替代原先不可用的
`major_news/news/cctv_news/anns_d` 业务链。详见
[`akshare-news-data-sources.md`](akshare-news-data-sources.md)。

### 6.1 新增 6 项情绪资金接口契约

| `api_name` | 生产查询参数 | 固定核心字段 | 用途 |
| --- | --- | --- | --- |
| `moneyflow_ths` | 日截面：`trade_date`；单股：`ts_code,start_date,end_date` | `trade_date,ts_code,name,pct_change,net_amount,net_d5_amount` 及大/中/小单金额与占比 | THS 个股资金方向和连续性 |
| `moneyflow_dc` | 日截面：`trade_date`；单股：`ts_code,start_date,end_date` | `trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate` 及超大/大/中/小单 | DC 个股资金方向和连续性 |
| `moneyflow_ind_ths` | `trade_date` | `ts_code,industry,lead_stock,pct_change,company_num,net_buy_amount,net_sell_amount,net_amount` | 行业资金横截面 |
| `moneyflow_mkt_dc` | `trade_date` | 沪深收盘/涨跌幅、`net_amount,net_amount_rate` 及分档买入额 | 大盘资金快照 |
| `moneyflow_hsgt` | `start_date,end_date` | `trade_date,hgt,sgt,north_money,south_money` | 沪深港通区间资金 |
| `limit_list_d` | `trade_date` | `ts_code,name,industry,pct_chg,fd_amount,open_times,up_stat,limit_times,limit` | 涨停、跌停和炸板候选 |

这些参数和字段由 `TradingBehaviorService` 固定，Agent 不直接填写 `api_name` 或 `fields`。
`moneyflow_ths` 与 `moneyflow_dc` 的金额口径不能相加；程序分别保存来源，只允许在语义层比较
方向、持续性和订单结构。

## 7. 2026-08-23 特色接口连通性快照

本节是一次带日期的诊断记录，用来说明为何仍需要运行时故障转移，**不参与静态路由**。
测试以 `20260821` 为代表交易日，并按接口补充股票、板块或日期范围参数；“空结果”表示
请求和响应协议均成功，只是筛选条件下没有记录。

| 端点 | 可调用 | 有数据 | 空结果 | 本次失败 |
| --- | ---: | ---: | ---: | ---: |
| 主 REST | 14 / 33 | 13 | 1 | 19 |
| 备用 DataApi | 32 / 33 | 30 | 2 | 1 |

逐接口结果如下：

| `api_name` | 主服务器 | 备用服务器 |
| --- | --- | --- |
| `cyq_perf` | 有数据 | 有数据 |
| `cyq_chips` | 有数据 | 有数据 |
| `stk_factor` | 无权限 | 有数据 |
| `stk_factor_pro` | 无权限 | 有数据 |
| `report_rc` | 无权限 | 有数据 |
| `broker_recommend` | 无权限 | 有数据 |
| `stk_surv` | 空结果 | 空结果 |
| `moneyflow` | 有数据 | 有数据 |
| `moneyflow_ths` | 无权限 | 有数据 |
| `moneyflow_dc` | 有数据 | 有数据 |
| `moneyflow_ind_ths` | 有数据 | 有数据 |
| `moneyflow_ind_dc` | 无权限 | 有数据 |
| `moneyflow_mkt_dc` | 有数据 | 有数据 |
| `moneyflow_hsgt` | 有数据 | 有数据 |
| `limit_list_ths` | 无权限 | 有数据 |
| `limit_list_d` | 有数据 | 有数据 |
| `limit_step` | 无权限 | 有数据 |
| `limit_cpt_list` | 无权限 | 有数据 |
| `ths_hot` | 无权限 | 有数据 |
| `dc_hot` | 无权限 | 有数据 |
| `hm_list` | 无权限 | 有数据 |
| `hm_detail` | 无权限 | 有数据 |
| `ths_index` | 有数据 | 有数据 |
| `ths_daily` | 无权限 | 有数据 |
| `ths_member` | 有数据 | 有数据 |
| `dc_index` | 有数据 | 有数据 |
| `dc_daily` | 有数据 | 有数据 |
| `dc_member` | 有数据 | 有数据（返回 8000 行，仍需核验筛选语义） |
| `tdx_index` | 无权限 | 有数据 |
| `tdx_daily` | 无权限 | 读取超时 |
| `tdx_member` | 无权限 | 有数据 |
| `kpl_list` | 无权限 | 有数据 |
| `kpl_concept_cons` | 无权限 | 空结果 |

需要注意：同一次验证期间，主服务器的 `moneyflow_ths`、`moneyflow`、
`moneyflow_mkt_dc`、`limit_list_d` 等接口曾在重复请求间出现“成功/无权限”交替。因此上表只能
代表该轮完整扫描的最终快照，不能据此把接口永久绑定到某一端。

本次只把情绪与资金每日模式确实需要、且字段契约已核验的 6 项加入生产白名单：
`moneyflow_ths`、`moneyflow_dc`、`moneyflow_ind_ths`、`moneyflow_mkt_dc`、
`moneyflow_hsgt`、`limit_list_d`。其余 27 项保留为候选接口；“能返回数据”不等同于已经验证
分页、筛选、字段完整性和业务语义，不能直接暴露给 Agent。

## 8. 双端失败语义

如果主、备 Provider 都失败，路由层抛出：

```text
DATA_SOURCE_UNAVAILABLE
```

异常消息包含双方的错误类别和经过脱敏、截断的上游消息，例如：

```text
主服务器失败：PROVIDER_RATE_LIMITED (...)
备用服务器失败：PROVIDER_PERMISSION_DENIED (...)
```

它不包含 Token、API Key 或完整请求数据。上层 Service 可以据此生成“证据缺失”状态；Agent
不应自行无限重试或猜测缺失数据。

## 9. 可观测性与成本指标

后续接入监控时至少记录：

- `provider_requests_total{provider,api_name,result}`
- `provider_response_bytes_total{provider,api_name}`
- `provider_fallback_total{api_name,primary_error_code}`
- `provider_cache_hits_total{api_name}`
- `provider_latency_seconds{provider,api_name}`
- `provider_rate_limit_total{provider,api_name}`

标签中不得包含 Token、完整参数或新闻正文。每条证据应保存实际 `provider`、`api_name`、
`data_as_of`、`fetched_at` 与缓存状态，不能仅根据预期路由推断来源。

## 10. 验证方式

`tests/test_providers.py` 覆盖主成功、业务失败、网络失败、备用缓存、合法
空数据、双端失败与未知接口。执行：

```bash
uv run pytest tests/test_providers.py
```

实时权限探测只代表执行当时的上游状态，不再保存为长期架构文档，也不参与静态路由。
