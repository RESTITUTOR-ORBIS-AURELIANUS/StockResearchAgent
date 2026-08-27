# Stock Research Agent

面向 A 股日线级研究的证据驱动多 Agent 系统。

第一版工作流以四类结构化对象为核心：

- `EvidenceRecord`：有来源、时点和验证状态的证据；
- `ThesisRecord`：由证据支持或反驳的观点；
- `ResearchRequest`：观点查证产生的定向研究任务；
- `RecommendationRecord`：进取、保守和委员会投资建议。

## 当前进度

目前已实现工程骨架、领域模型、LangGraph 共享状态、正式工作流的第一个
`InitializeRunNode`、行情数据的主备 Provider 基础层，以及覆盖当前 89 个接口的
八个确定性源数据 Service、技术/情绪资金/基本面三个每日快照聚合 Service，以及按技术、基本面、事件、情绪与资金四类研究员隔离的
25 个受控数据 Tool、run-scoped `ResearchDataStore` 以及 5 个按需调用的确定性技术
计算 Tool，全局共 30 个语义 Tool。新闻事件链另有独立的 `AkshareNewsProvider`、
`PublicNewsEventService` 和第四个每日聚合 `DailyEventSnapshotService`；它们不会混入原有
89 项 Tushare 路由。`TechnicalResearchAnalyst`、`SentimentAndFlowAnalyst`、
`FundamentalResearchAnalyst` 和 `EventDrivenResearchAnalyst` 的每日/查证 LangGraph 子图、
受控查证和确定性 Evidence 装配均已实现。四个 factory 同时注入时，主图当前按技术、情绪资金、
基本面、新闻事件顺序执行，随后由确定性 `EvidenceCollectorNode` 检查运行/时点/来源边界并生成
`evidence_collection`。注入 `LeadResearchStrategist` 模型后，主图会把全部合法摘要交给结构化 LLM，
生成最多 8 条状态固定为 `UNVERIFIED` 的候选 `ThesisRecord`。注入 `ThesisValidationAnalyst` 后，
主图会逐观点串行审阅；每轮最多提出一个定向请求，执行结果会以 `ResearchFinding` 和 Collector 接受的
新证据立即回到同一观点上下文，最终固化为 `SUPPORTED/REFUTED/MIXED/INCONCLUSIVE`。Collector
不做语义去重。观点查证结束后，`AggressivePortfolioManager` 与
`ConservativePortfolioManager` 会并行读取同一批终态观点，分别输出独立的结构化投资建议；程序只
允许 `SUPPORTED/MIXED` 观点成为直接建议依据，并负责目标绑定、稳定 ID 和初始坚持分装配。双方
两份经理建议的确定性规范化、首次交叉评分严格 Schema、模型适配器、并行节点、确定性冲突评分校验，
以及只重评违规经理的有界纠错循环均已完成。一次首评加最多两次纠错，单经理总调用次数不超过 3，
纠错不增加 `debate_round`。评分合法后的确定性 `ConsensusGateNode` 和最多三轮正式协商也已接入：
每轮按“理由交换 → 原提议方修订 → 必要时对受影响冲突组闭包重评”原子执行；只有实质修订触发
重评，无变化仍消耗一轮，重评后再次校验每位经理对互斥建议评分和 `<= 0`。达到轮次上限后，仍未达成共识的
条目固化为 `EXCLUDED`，不进入最终委员会建议；主图直接进入
`ConsensusRecommendationAssemblerNode`。组装节点只把 `AGREED` 条目交给受限的结构化模型做顶层
文字压缩，不能新增、修改或仲裁原子建议。缺少 `ACTION/HORIZON/RISK_CONTROL` 或没有
任何 `AGREED` 条目时，程序以 `no_actionable_consensus` 正常结束且不生成
`consensus_recommendation`。两份经理原始建议始终保留。第一版不设主席或仲裁路由。主图的所有
正常和失败分支最终都会进入确定性的 `ReportComposerNode`，生成严格 `ResearchReport` 与 Markdown；
长期持久化仍将在后续接入。

Provider 对 89 个已支持接口统一采用主服务器优先策略。主服务器发生网络、HTTP、
认证、权限、限流、业务或响应格式错误时，自动回退备用服务器；成功的备用结果会在
进程内缓存一小时。`code=0` 的空数据仍被视为合法响应，不会触发回退。

新闻事件 Tool 已不再依赖不可用的 `major_news`、`news`、`cctv_news` 或 `anns_d` 链路。
公开新闻与公告通过 AKShare `1.18.94` 读取东方财富、同花顺、财联社快讯，以及东方财富个股新闻和公告
索引；卖方研究摘要和月度券商荐股分别读取 Tushare-compatible `report_rc` 与
`broker_recommend`。快讯只代表各站点最近 N 条，因此结果会明确标记为非完整历史；卖方评级、
预测和荐股只证明某机构表达过该观点，不等于公司经营事实。完整源表保存在
`ResearchDataStore`，只有有界候选进入模型上下文。

八个源 Service 负责业务参数校验、固定字段、完整分页或受控时间窗口、`as_of` 截止检查、本地筛选
以及逐行、逐页来源追踪；Agent 不需要记忆底层接口字段，也不会直接选择主备服务器。
同一次分页若发生主备切换，Service 会失败关闭，避免拼接两个不同快照。

`DailyTechnicalSnapshotService` 在源 Service 上进一步聚合最近完整交易日的市场宽度、三大市场指数、
申万一级行业宽度、指数权重以及五类异常个股候选，专供技术分析 Agent 的每日模式使用。
`DailyFundamentalSnapshotService` 则把全市场估值、近期业绩预告/快报、财务质量同比变化、
`stock_basic.industry` 行业横截面和宏观利率压缩为候选快照；完整批量源表仍保存在当前 run 的
`ResearchDataStore` 中。

Tool 层只暴露业务查询意图，不允许模型填写 `api_name`、Provider、字段、分页或
`as_of`。一次研究运行会冻结 `as_of` 并复用同一个 `DataServices` 生命周期；复合 Tool
允许返回 `partial`，但不会把上游失败伪装成合法空表。

个股、指数和基金三个行情原始 Tool 会把完整 `ServiceDataset` 保存到当前 run 的 Store，
只向 LLM 返回 `context_ref`、数据清单和每数据集默认 5 行预览。技术计算器凭引用读取
完整行情，不让 LLM 复制数百行 K 线或心算指标。`context_ref` 只在当前 run 内有效；
进程内 Store 不支持重启后恢复旧引用。

五个技术计算器统一支持个股、市场/中证/申万行业指数以及场内基金/ETF 的日频行情引用。
价格趋势、动量、价格风险、核心量价和相对强弱按同一套算法计算；股票专属的换手率、
涨跌停和停牌指标在指数或基金结果中明确标为 `not_applicable`，不会误报为数据缺失。

## 本地运行

先在项目根目录的 `.env` 中配置主备行情服务和 LLM 凭据，再执行：

```bash
uv sync
uv run stock-research-agent \
  --as-of 2026-08-18T15:30:00+08:00 \
  --format markdown \
  --output report.md
```

结构化 LLM 调用默认启用 strict JSON Schema，并把每次调用的耗时、token、`finish_reason`、失败字段
和脱敏后的原始响应写入 `.artifacts/llm-structured-output.jsonl`。失败报告中的
`diagnostic_event=llm_...` 可直接关联该文件；本地校验失败默认携具体错误自动纠正一次。

该 CLI 固定以 `MARKET / A_SHARE / A股市场` 作为每日研究范围，并会装配共享的 Provider、
Service、DataStore、Tool、四个证据子图以及完整投资决策链。Runtime 默认把 LangGraph
`recursion_limit` 设置为 300，给多观点串行查证和最多三轮协商预留足够步数。
并使用 `await graph.ainvoke(...)` 执行真实工作流。省略 `--output` 时报告写到标准输出；
`--format json` 可输出结构化 `ResearchReport`。运行结束或异常退出时，HTTP 连接池、AKShare
线程池和本轮进程内原始数据都会被回收。退出码 `0` 表示报告完整（包括正常的“无可执行共识”），
`3` 表示已生成报告但研究链不完整，`1` 表示未能完成运行。

已实现 Agent 的构建方式见
[`docs/technical-agent-v1.md`](docs/technical-agent-v1.md)、
[`docs/sentiment-flow-agent-v1.md`](docs/sentiment-flow-agent-v1.md)、
[`docs/fundamental-agent-v1.md`](docs/fundamental-agent-v1.md)、
[`docs/event-agent-v1.md`](docs/event-agent-v1.md)。
候选观点节点见 [`docs/candidate-thesis-agent-v1.md`](docs/candidate-thesis-agent-v1.md)。
逐观点查证节点见 [`docs/thesis-validation-agent-v1.md`](docs/thesis-validation-agent-v1.md)。
投资建议 Schema 与两位经理节点见
[`docs/investment-recommendation-schema-v1.md`](docs/investment-recommendation-schema-v1.md) 和
[`docs/portfolio-manager-nodes-v1.md`](docs/portfolio-manager-nodes-v1.md)。
交叉评分、确定性校验与有界纠错契约见
[`docs/cross-review-nodes-v1.md`](docs/cross-review-nodes-v1.md)、
[`docs/cross-review-schema-v1.md`](docs/cross-review-schema-v1.md) 和
[`docs/conflict-score-validator-v1.md`](docs/conflict-score-validator-v1.md)。
共识门、正式协商结构体和三阶段节点路由见
[`docs/formal-negotiation-schema-v1.md`](docs/formal-negotiation-schema-v1.md) 与
[`docs/formal-negotiation-nodes-v1.md`](docs/formal-negotiation-nodes-v1.md)。
最终委员会建议的受限组装契约见
[`docs/consensus-recommendation-assembler-v1.md`](docs/consensus-recommendation-assembler-v1.md)。
真实运行装配与报告终点见
[`docs/runtime-assembly-v1.md`](docs/runtime-assembly-v1.md) 和
[`docs/report-composer-v1.md`](docs/report-composer-v1.md)。

完整文档入口见
[`docs/README.md`](docs/README.md)。该索引会明确区分
当前已经实现的 Provider、Service、Tool、四位证据 Agent、投资决策、正式协商与最终共识建议组装切片，
真实运行装配和报告生成，以及仍待实现的长期持久化边界。

## 运行测试

```bash
uv run pytest
```
