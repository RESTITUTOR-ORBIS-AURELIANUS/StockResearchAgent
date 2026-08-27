# StockResearchAgent 文档索引

本目录只保留两类资料：

1. **当前实现契约**：必须与 `src/` 和测试保持一致；
2. **目标设计与外部参考**：用于指导后续开发，不能当成已经完成的功能。

若文档与代码冲突，以代码、类型契约和测试为准。

## 当前实现状态

| 层级 | 当前状态 |
|---|---|
| Provider | 已实现 89 个 Tushare 白名单 API 的主备路由，并新增独立 `AkshareNewsProvider` 适配 6 个公开新闻/公告函数 |
| Service | 已实现 9 个源 Service（84 个公开方法）和 4 个每日聚合 Service；公开新闻链使用 `PublicNewsEventService`，卖方研究使用扩展后的 `NewsEventDataService` |
| Tool | 已实现 25 个数据 Tool 和 5 个确定性技术计算 Tool，共 30 个唯一 Tool；Event 角色包含每日快照、公开新闻公告和卖方研究入口 |
| 行情存储 | 已实现 run-scoped 进程内 `ResearchDataStore`；重启后不保留 |
| 领域模型 | `EvidenceRecord`、运行时 `EvidenceCollection`、`ThesisRecord`、`ResearchRequest`、`ResearchFinding`、`RecommendationRecord` 已实现 |
| LangGraph | 主图支持四位证据 Agent 串行采集、证据错误立即停止并生成不完整报告、Collector、候选观点、逐观点查证、两位经理独立建议、提案规范化、交叉评分与有界纠错、确定性共识门、最多三轮的正式协商循环、最终共识建议组装，以及所有终止分支统一进入的确定性报告节点 |
| LLM Agent | 四位证据研究员、策略师、观点审查员、两位投资经理及其交叉评分/纠错和正式协商三阶段适配器、受限的共识建议文字合成适配器已实现；第一版不设主席或仲裁路由，报告节点不调用 LLM |
| LLM 观测 | 所有结构化模型通道启用 strict JSON Schema、原始响应保留、字段级 Pydantic 诊断、一次受控纠正和脱敏 JSONL |
| 应用层 | 已实现共享资源的异步运行时和完整 CLI；每日调度、对外 HTTP API、数据库/对象存储长期持久化均未实现 |

当前测试均为确定性测试；具体数量以 `uv run pytest` 的最新输出为准。它们不代表真实上游、真实 LLM 或完整投研流程已经完成。

## 当前实现契约

按调用层级阅读：

1. [`data-pipeline-reference.md`](data-pipeline-reference.md)
   - 解释 `Tool → Service → Provider → 上游接口` 的完整数据流；
   - 适合第一次理解项目分层。
2. [`tool-call-reference.md`](tool-call-reference.md)
   - 30 个 Tool 的正式输入、输出、角色白名单和错误语义；
   - 编写 Agent prompt 或 Tool-calling 节点时以此为准。
3. [`data-service-call-reference.md`](data-service-call-reference.md)
   - 9 个源 Service、4 个聚合 Service、84 个源业务方法及其 Python 调用方式；
   - 编写确定性业务代码时以此为准。
4. [`dual-provider-routing-api.md`](dual-provider-routing-api.md)
   - 主 REST、备用 Tushare DataApi、缓存和故障转移契约；
   - 编写或排查 Provider 时以此为准。
5. [`technical-agent-v1.md`](technical-agent-v1.md)
   - 已实现技术 Agent 的每日模式、查证模式、Prompt、预算和 LLM 配置；
6. [`sentiment-flow-agent-v1.md`](sentiment-flow-agent-v1.md)
   - 已实现情绪资金 Agent 的每日/查证子图、三类 Tool 映射、口径约束、Prompt 和确定性来源装配；
7. [`fundamental-agent-v1.md`](fundamental-agent-v1.md)
   - 已实现基本面 Agent 的每日/查证子图、行业横截面、六类 Tool 映射、报告期约束和来源装配；
8. [`event-agent-v1.md`](event-agent-v1.md)
   - 已实现新闻事件 Agent 的每日/查证子图、行级引用、卖方观点边界、四类 Tool 映射和硬预算；
9. [`candidate-thesis-agent-v1.md`](candidate-thesis-agent-v1.md)
   - 已实现首席研究策略师的全量证据摘要输入、结构化候选观点、硬限制和确定性引用核验；
10. [`thesis-validation-agent-v1.md`](thesis-validation-agent-v1.md)
   - 已实现逐观点连续审阅、即时定向补证、`ResearchFinding`、Collector 回灌、预算和最终状态固化；
11. [`investment-recommendation-schema-v1.md`](investment-recommendation-schema-v1.md)
   - 已实现两位投资组合经理共同使用的独立建议 Draft Schema；
12. [`portfolio-manager-nodes-v1.md`](portfolio-manager-nodes-v1.md)
   - 已实现两位经理的独立角色 Prompt、并行节点、引用校验、稳定 ID 装配和失败关闭边界；
13. [`proposal-normalization-node-v1.md`](proposal-normalization-node-v1.md)
   - 已实现两份独立建议的不可变汇合、规范化决策槽及对称冲突生成；
14. [`cross-review-schema-v1.md`](cross-review-schema-v1.md)
   - 已实现两位经理首评与纠错使用的冻结输入、尝试轨迹、确定性反馈和严格 LLM 输出 Schema；
15. [`cross-review-nodes-v1.md`](cross-review-nodes-v1.md)
   - 已实现两位经理的角色 Prompt、结构化模型适配器、并行首评、确定性评价写入及违规经理局部纠错；
16. [`conflict-score-validator-v1.md`](conflict-score-validator-v1.md)
   - 已实现确定性冲突评分规则、来源指纹、最多三次调用预算、局部纠错路由和失败关闭语义；
17. [`formal-negotiation-schema-v1.md`](formal-negotiation-schema-v1.md)
   - 已实现共识门和正式协商使用的 Draft、Input、Record、运行摘要及 `ResearchGraphState` 字段；
18. [`formal-negotiation-nodes-v1.md`](formal-negotiation-nodes-v1.md)
   - 已实现共识阈值、三阶段原子流程、冲突组闭包重评、无变化轮、最多三轮及未决条目排除路由；
19. [`consensus-recommendation-assembler-v1.md`](consensus-recommendation-assembler-v1.md)
   - 已实现只消费 `AGREED` 条目的受限文字合成、确定性组装、信心度规则、无可执行共识和失败边界；
20. [`runtime-assembly-v1.md`](runtime-assembly-v1.md)
   - 已实现配置到 Provider、Service、Tool、Agent 子图、全套模型适配器和主图的真实异步组合根与资源生命周期；
21. [`llm-structured-output-observability-v1.md`](llm-structured-output-observability-v1.md)
   - 已实现 strict JSON Schema、本地完整校验、字段级失败纠正、脱敏 JSONL 和报告关联 ID；
22. [`report-composer-v1.md`](report-composer-v1.md)
   - 已实现不调用 LLM 的结构化报告、确定性 Markdown、无共识披露和上游失败诊断；

`agent-tools-v1.md` 和 `data-services-v1.md` 保留为两层的设计说明；精确字段分别以上述 Tool
和 Service 调用手册为准。

## 目标设计

- [`research-agent-v1-design.md`](research-agent-v1-design.md)
  - LangGraph 目标结构、Agent 职责、四类 JSON 契约和停止条件；
  - 当前已实现到真实运行装配和最终报告；长期持久化仍是后续设计。

## 外部参考

- [`tradingagents-reference.md`](tradingagents-reference.md)
  - 对指定 TradingAgents 版本的源码阅读笔记；
  - 不是本项目的实现说明，也不保证跟随上游最新版本。
- [`akshare-news-data-sources.md`](akshare-news-data-sources.md)
  - AKShare 新闻、公告接口的官方契约、2026-08-24 实测结果和当前接入边界；
  - Provider、Service、每日快照和 Event 新闻 Tool 已实现并已接入新闻事件 LLM Agent；长期持久化与真实部署尚未完成。

## 维护规则

- 不在正式文档中保存 API Key、Token、密码或带凭据的完整命令；
- 不把某一天的成功、限流、无权限数量写成长期路由规则；
- Provider 白名单从 `providers/routes.py` 读取，Service 归属从 `services/catalog.py` 读取；
- Tool 名称和角色白名单从 `tools/registry.py` 读取；
- 设计中的节点必须明确标注“已实现”或“待实现”；
- 临时探测结果默认只用于当次诊断；若为评估接口接入而保留，必须标明日期，并明确它不构成静态路由规则。
