# AKShare 新闻与公告数据源实现说明

> 文档状态：Provider → Service → Tool → 新闻事件 LLM Agent 已在本地实现；长期持久化与真实部署尚未完成
> 核验日期：2026-08-24
> 核验版本：AKShare `1.18.94`
> 官方文档：<https://akshare.akfamily.xyz/data/stock/stock.html>

## 1. 结论

AKShare 已用于补齐当前 Tushare-compatible 数据源的三类关键缺口：

1. 全市场实时财经快讯；
2. 可按股票代码或关键词查询的个股新闻；
3. 全市场每日公告和指定个股公告。

新闻事件 Tool 已不再调用 `major_news`，而是通过独立的 `AkshareNewsProvider` 读取公开网页数据。
旧的 Tushare `major_news` 路由暂时保留作兼容代码，但不在 Event Tool 的公开新闻路径中；
`NewsEventDataService` 现用于 `report_rc` 与 `broker_recommend` 卖方观点数据：

```text
                         ┌─ Tushare Provider：结构化财务、行情、公司行动、研报摘要和月度荐股
Event Tool / Snapshot ───┤
                         └─ AkshareNewsProvider → PublicNewsEventService：快讯、个股新闻和公告索引
```

不过 AKShare 不能被描述为 `major_news` 的无条件等价替代：多数快讯接口只返回“最近 N 条”，
没有任意历史时间窗口查询能力。它适合每日及时采集并由本项目自行持久化，不适合在数月后临时
回放某个历史日的完整新闻集合。

## 2. 数据来源性质

AKShare 是 Python 数据接口库，不是拥有统一 SLA 的新闻数据库。它把东方财富、同花顺、财联社、
巨潮资讯等公开网站的数据清洗成 `pandas.DataFrame`。因此：

- `provider` 不记录成模糊的 `AKSHARE`，当前代码保留
  `AKSHARE_EASTMONEY`、`AKSHARE_THS` 和 `AKSHARE_CLS`；公告接口当前同样标记为
  `AKSHARE_EASTMONEY`，并在行内保留 `source_kind="announcement"`；
- 网页结构、反爬策略或上游字段变化可能导致接口临时失效；
- 必须保存获取时间、原始发布时间、原文 URL 和 AKShare 版本；
- 应遵守原始网站的使用条款、robots 规则和版权要求，不绕过登录、付费墙或访问控制；
- 新闻正文和研报 PDF 应主要用于内部研究与证据抽取，不应未经授权重新公开分发。

AKShare 官方也将这些公开数据接口主要定位于学术研究，并提醒使用者注意商业风险：
<https://akshare.akfamily.xyz/introduction.html>。

## 3. 2026-08-24 实际连通性验证

下表为本地使用临时依赖 `akshare==1.18.94` 的无凭据 smoke test。行数只代表该次请求，
不是接口固定返回量。

| AKShare 函数 | 本次结果 | 返回字段 | 适合用途 |
|---|---:|---|---|
| `stock_info_global_em()` | 成功，200 行 | 标题、摘要、发布时间、链接 | 每日全市场新闻主源之一 |
| `stock_info_global_ths()` | 成功，20 行 | 标题、内容、发布时间、链接 | 全市场快讯交叉验证 |
| `stock_info_global_cls(symbol="全部")` | 成功，20 行 | 标题、内容、发布日期、发布时间 | 事件发现；当前结果没有原文 URL，不能单独承担最终可追溯证据 |
| `stock_news_em(symbol="000001")` | 成功，10 行 | 关键词、新闻标题、新闻内容、发布时间、文章来源、新闻链接 | 指定个股新闻查证 |
| `stock_notice_report(symbol="全部", date="20260821")` | 成功，3256 行 | 代码、名称、公告标题、公告类型、公告日期、网址 | 每日全市场公告扫描 |
| `stock_individual_notice_report(...)` | 成功，10 行 | 代码、名称、公告标题、公告类型、公告日期、网址 | 指定个股公告查证 |
| `stock_research_report_em(symbol="000001")` | 成功，226 行 | 报告名、评级、机构、盈利预测、行业、日期、PDF 链接等 | 卖方研报元数据和原文入口 |
| `stock_sns_sseinfo(symbol="600519")` | 本次 35 秒内未完成 | 问题、回答及时间等 | 暂不进入第一版生产候选 |

另有 `stock_irm_cninfo()` 可读取互动易问答，但官方说明单次可能返回近期 10000 行。本轮没有
执行大结果调用；接入前必须确认其筛选、分页、超时和增量同步策略，不能直接在每日 Agent 中全量调用。

## 4. 已纳入新闻节点的六个接口

### 4.1 全市场新闻快讯

```python
ak.stock_info_global_em()
ak.stock_info_global_ths()
ak.stock_info_global_cls(symbol="全部")
```

推荐优先级：

1. 东方财富和同花顺作为带 URL 的主要来源；
2. 财联社用于发现和交叉验证；
3. 同一事件按规范化 URL、标题指纹和发布时间去重；
4. 多来源报道同一事件时保留来源列表，不简单拼接重复正文。

这些接口只返回最近 20～200 条。每日任务必须在固定时点采集并落库，不能指望以后重新请求时
仍能取回当时的新闻。

### 4.2 指定个股新闻

```python
ak.stock_news_em(symbol="000001")
```

`symbol` 可以是股票代码或关键词。项目应先用 `resolve_stock_identity` 得到代码、公司现名和曾用名，
再分别查询并做实体匹配。调用结果仍须按照冻结的 `as_of` 过滤，不能因为接口返回了未来文章就进入
历史研究上下文。

### 4.3 全市场每日公告

```python
ak.stock_notice_report(symbol="全部", date="20260821")
```

`symbol` 可选：`全部`、`重大事项`、`财务报告`、`融资公告`、`风险提示`、`资产重组`、
`信息变更`、`持股变动`。

该接口可以实质性替代当前不可用的 `anns_d`，但返回的是公告索引和详情 URL，不是已解析的完整公告
正文。每日快照应先保存全部索引，再根据候选重要性受控下载详情或 PDF；不能把当天数千份公告全部
塞入 LLM 上下文。

### 4.4 指定个股公告

```python
ak.stock_individual_notice_report(
    security="000001",
    symbol="全部",
    begin_date="20260801",
    end_date="20260824",
)
```

适合查证节点按股票和日期范围补证。AKShare 使用六位纯数字代码，本项目进入 Provider 前需要把
`000001.SZ`、`600519.SH` 规范化为 `000001`、`600519`，返回后再恢复统一的 `ts_code`。

### 4.5 尚未纳入本轮实现：个股研报元数据

```python
ak.stock_research_report_em(symbol="000001")
```

它能补充当前 `report_rc` 的报告 PDF 链接，但返回的评级、目标预期和盈利预测仍然是券商观点。
证据只能表述为“某机构在某日发布某评级或预测”，不能把观点本身标记为 ground truth。

PDF 下载与解析应是单独、受预算控制的步骤：先按日期和标的筛选元数据，再下载少量必要报告，
记录 PDF 哈希、URL、发布时间和解析状态。

## 5. 暂不进入第一版的接口

| 接口 | 原因 |
|---|---|
| `stock_news_main_cx()` | 主要是最新精选摘要，部分原文可能受订阅限制；不绕过付费墙 |
| `stock_sns_sseinfo()` | 本次调用未在 35 秒内完成，延迟和分页边界尚未验证 |
| `stock_irm_cninfo()` | 单次可能返回 10000 行，必须先实现有界增量同步 |
| `stock_irm_ans_cninfo()` | 依赖前一个接口取得提问者编号，当前调用链和规模未验证 |
| 其他未测试新闻聚合接口 | 尚未验证字段、发布时间、来源 URL 和稳定性 |

## 6. 当前 Provider、Service 与 Tool 映射

| 层级 | 当前接口 | 作用 |
|---|---|---|
| `AkshareNewsProvider` | `query(ProviderQuery)` | 用有界线程池调用六个同步 AKShare 函数，校验中文字段并标准化为 `ProviderResult` |
| `PublicNewsEventService` | `get_market_news(...)` | 按来源读取最近快讯，并按带时区窗口和冻结 `as_of` 过滤 |
| `PublicNewsEventService` | `get_stock_news(...)` | 把 `000001.SZ` 转成六位代码，读取最多 31 天的个股新闻 |
| `PublicNewsEventService` | `get_daily_announcements(...)` | 按日期和公告分类读取全市场公告索引 |
| `PublicNewsEventService` | `get_stock_announcements(...)` | 按股票、日期区间和分类读取指定股票公告索引 |
| `NewsEventDataService` | `get_sell_side_reports(...)` / `get_daily_sell_side_reports(...)` | 读取 `report_rc` 研报结构化摘要；不含全文 |
| `NewsEventDataService` | `get_broker_recommendations(...)` | 读取 `broker_recommend` 月度券商金股名单 |
| `DailyEventSnapshotService` | `build_daily_snapshot(...)` | 聚合三路市场快讯、近期公告、逐日研报摘要、当月荐股和股票目录 |
| Event Tool | `get_daily_event_snapshot` | 每日模式唯一的大范围入口 |
| Event Tool | `search_market_news` | 按冻结时间窗口查询 ALL/EASTMONEY/THS/CLS 市场快讯 |
| Event Tool | `get_targeted_news_and_disclosures` | 指定标的查证新闻和公告 |
| Event Tool | `get_sell_side_research_context` | 指定标的查证区间研报摘要和覆盖月份的券商荐股 |

`DailyEventSnapshotService` 聚合三路公开快讯、近期公告索引、卖方研报摘要、月度荐股和
当前上市股票目录；宏观日历、公司行动和业绩披露
仍由既有 `get_economic_calendar`、`get_corporate_action_events`、`get_earnings_and_disclosure`
分别提供，并未被硬塞入每日快照。股票目录只用于精确匹配新闻中出现的当前上市公司全名，不生成
概念股、供应链或合作关系映射。

## 7. 实现约束

1. AKShare 调用是同步阻塞函数，`AkshareNewsProvider` 通过受控线程池执行，并设置超时和并发上限；
2. 所有日期和时间统一转换为 `Asia/Shanghai`，随后执行 `published_at <= as_of`；
3. 每日快照的完整标准化 `ServiceDataset` 当前保存到 run-scoped `ResearchDataStore`；它只在本次
   进程/研究运行内有效，长期新闻回放仍需后续持久化；
4. 公告当前只保存索引元数据和 URL，不批量下载正文或 PDF；
5. 任一来源失败必须标记 `partial` 或 `error`，不能伪装成“当天没有新闻”；
6. 公开新闻来源必须包含可追溯 URL；没有 URL 的财联社结果只用作候选发现或交叉验证；卖方
   `report_rc`/`broker_recommend` 使用结构化上游行与派生 `record_key` 定位，但不得被描述成研报全文；
7. 当前确定性测试覆盖字段漂移、超时/并发边界、空结果、时间过滤、去重、部分快照和 Tool
   上下文存储；它们不证明公开网站会永久稳定；
8. `major_news` 仅为兼容保留，Event 的公开新闻 Tool 不调用它；Event 会调用同一 Service 新增的
   `report_rc` 与 `broker_recommend`；
9. `EventDrivenResearchAnalyst` 的每条证据必须同时引用真实 Tool 调用和行级 `record_key`；
   具体边界见 [`event-agent-v1.md`](event-agent-v1.md)。
