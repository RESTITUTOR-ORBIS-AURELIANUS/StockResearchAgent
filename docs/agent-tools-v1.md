# StockResearchAgent Tool 层设计

> 状态：25 个数据 Tool 与 5 个技术计算 Tool 已实现
> 代码位置：`src/stock_research_agent/tools/`

本文只解释 Tool 层为什么存在以及怎样绑定给 Agent。30 个 Tool 的字段、返回结构、示例和
错误码统一见 [`tool-call-reference.md`](tool-call-reference.md)；完整分层见
[`data-pipeline-reference.md`](data-pipeline-reference.md)。

## 1. Tool 在项目中的位置

```text
LLM 证据研究员
      ↓ StructuredTool：表达一个研究意图
Tool 层
      ├─ 注入当前 run_id 和冻结 as_of
      ├─ 组合 1～N 个确定性 Service 查询
      ├─ 普通结果 → 受控 JSON
      └─ 大型行情 → ResearchDataStore + context_ref
五个技术计算 Tool
      └─ context_ref → 完整行情 → 确定性指标
```

它很像 Java 项目中 Controller 的入口地位，但不是 HTTP Controller：

- 调用者是 LLM，不是浏览器；
- 不监听端口，也不会自动暴露到公网；
- 参数由 Pydantic schema 限制；
- Agent 不能填写 Provider、API 名、字段、分页和凭据；
- 一个 Tool 可以组合多个 Service 方法来表达一个业务问题。

## 2. 为什么不是把 89 个 API 都交给模型

Tushare Provider 的 89 个接口、AKShare 的 6 个新闻/公告接口和源 Service 的公开方法
是程序员视角的细粒度能力。全部暴露会让
模型记忆供应商字段、选择 VIP/批量接口并承担分页、时点和主备路由责任。

当前只暴露：

- 25 个业务语义明确的数据 Tool，其中包含每日技术、每日情绪资金、每日基本面、每日事件四个聚合 Tool；
- 5 个读取 `context_ref` 的确定性技术计算 Tool；
- 去重后共 30 个 Tool。

四位证据研究员实际看到的白名单数量分别是 Technical 11、Fundamental 11、Event 9、
Sentiment/Flow 8。数量之和大于 30，是因为身份、交易日历、新闻和基金行情等 Tool 会跨角色共享。

`*_batch`、`*_vip`、任意 `query(api_name, params)` 以及 `limit/offset/fields/provider` 都不进入
模型白名单。

## 3. 每次运行冻结上下文

```python
from stock_research_agent.tools import ResearchToolContext, build_agent_tool_registry

context = ResearchToolContext(
    services=services,
    as_of=state["as_of"],
    run_id=state["run_id"],
    data_store=run_scoped_data_store,
)
registry = build_agent_tool_registry(context)
```

`as_of`、`run_id` 和 `data_store` 不属于 Tool 输入，因此 LLM 不能改写研究截止时间或跨 run
读取行情。`DataServices` 在应用生命周期内复用；registry 和进程内 Store 按研究 run 创建、清理。

## 4. 按角色把白名单交给 Agent 编排器

```python
from stock_research_agent.agents.technical import build_technical_agent_graph
from stock_research_agent.tools import EvidenceAgentRole

technical_tools = registry.for_role(EvidenceAgentRole.TECHNICAL)
technical_graph = build_technical_agent_graph(
    model=technical_reasoning_model,
    tool_context=context,
    tools=technical_tools,
)
```

当前四位证据 Agent 都不把这些 Tool 直接 `bind_tools()` 给 LLM。LLM 通过
`with_structured_output(..., method="json_schema")` 输出证据草稿和测量请求，LangGraph
子图再把测量/查证枚举确定性映射为白名单中的原始数据 Tool 与计算 Tool。这样模型不能自行构造
`context_ref`、Provider、接口名或分页参数。

registry 为四位证据研究员分别定义白名单；情绪资金、基本面与事件 Agent 都沿用技术 Agent 的
“结构化计划 + 程序执行”模式。若未来某个 Agent 改用
`llm.bind_tools(...)`，那是另一种明确的编排选择，不能当作当前四个 Agent 的实现。首席策略师、观点审查员和投资组合经理不直接调用
原始数据 Tool；它们通过 `ResearchRequest` 把补证任务交回证据研究员。

程序也可以直接调用某个 Tool：

```python
tool = next(tool for tool in technical_tools if tool.name == "get_stock_price_context")
result = await tool.ainvoke(
    {
        "ts_code": "000001.SZ",
        "start_date": "2026-08-01",
        "end_date": "2026-08-20",
        "frequency": "daily",
    }
)
```

## 5. 当前边界

已经完成：

- 角色白名单和 Pydantic 输入；
- 复合查询、部分失败、输出大小保护和逐行来源；
- run-scoped 完整行情引用；
- 基本面批量源表的 run-scoped 每日快照与候选压缩；
- AKShare 三路市场快讯、个股新闻和公告的事件 Tool 链，以及 run-scoped 每日事件候选快照；
- 个股、指数、ETF 可复用的五个确定性技术计算 Tool；
- `TechnicalResearchAnalyst` 每日/查证 LLM 子图；
- `SentimentAndFlowAnalyst` 与 `FundamentalResearchAnalyst` 每日/查证 LLM 子图；
- `EventDrivenResearchAnalyst` 每日/查证 LLM 子图、有限补证与行级来源核验；
- 技术 Tool observation 到可追溯 `EvidenceRecord` 的确定性装配；
- 候选观点生成、逐观点连续查证和即时定向补证循环。
- 两位投资组合经理基于终态观点并行生成独立结构化建议。

尚未完成：

- 支持重启、多进程和 checkpoint 恢复的持久化数据引用；
- 报告生成和长期持久化。

所以现在已经完成受控 Tool 基础设施、四位证据 Agent、候选观点、查证阶段、两份独立投资建议、
并行交叉评分、确定性冲突校验、有界纠错、确定性共识门、最多三轮正式协商及只纳入 `AGREED` 条目的最终组装。
未决条目会转为 `EXCLUDED`，两份原始经理建议仍完整保留，并由报告节点披露为未决分歧。
