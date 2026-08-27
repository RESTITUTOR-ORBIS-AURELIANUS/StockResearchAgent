# 真实运行装配 v1

## 目的

`runtime/application.py` 是程序的应用组合根。它把此前分别可测试的部件装配成一张真正可执行的
完整研究图，而不是在 CLI 中临时散落地创建对象。

## 一次 Runtime 持有的资源

- 一个共享 `httpx.AsyncClient`，供主/备行情 Provider 和 OpenAI-compatible LLM 客户端复用；
- 一个 `RoutedMarketDataProvider` 与一个 `AkshareNewsProvider`；
- 一套共享 `DataServices`；
- 一个 run-scoped `ResearchDataStore`；
- 一个共享 ChatModel，以及模型适配器注册表；当前稳定运行图只注入四位证据研究员、策略师、
  观点审查员和两位投资经理共 8 个适配器；
- 四个按 `run_id` 延迟创建的证据 Agent 子图工厂；
- 一张包含证据、观点、查证、双经理、双建议确认和报告节点的可运行主图。

仓库仍包含交叉评分、正式协商和共识组装适配器，便于开发分支持续演进；
`main` 与 `v1-no-debate` 的组合根不会把它们传给 `build_research_graph()`，因此真实运行不会触发辩论调用。

四个证据 Agent 在同一个 `run_id` 下共享同一个 `ResearchToolContext`、Tool Registry 和
DataStore，因此一个阶段保存的 `context_ref` 可以在后续查证阶段继续读取；不同运行仍保持隔离。
任何一个证据阶段发生节点异常、无证据失败、预算耗尽或返回契约破坏时，主图会先释放该阶段的
run-scoped 资源，再立即进入不完整报告节点，不会继续调用后续证据 Agent 或投资决策链。单条候选
证据被确定性校验拒绝仍会进入报告诊断，但只要该 Agent 已形成有效证据，就不误判为整个节点失败。

## 生命周期

推荐只通过异步上下文管理器使用：

```python
settings = ResearchRuntimeSettings.from_env()
async with open_research_runtime(settings) as runtime:
    result = await runtime.ainvoke({"target": target, "as_of": as_of})
```

`ResearchRuntimeSettings.graph_recursion_limit` 默认是 300。`runtime.ainvoke()` 会在调用方没有
显式指定时自动应用该值，避免完整每日图回退到 LangGraph 默认的较小步数上限；调用方仍可在
`config` 中明确覆盖它。

退出 `async with` 时，无论成功、异常还是取消，都会依次清理尚存的 run 数据、AKShare 线程池和
HTTP 连接池。`ResearchRuntime.cleanup_run(run_id)` 可用于长生命周期应用提前清理已经消费完成的
原始数据；不要在报告或计算器仍需读取 `context_ref` 时调用它。

## 配置边界

`ResearchRuntimeSettings.from_env()` 组合现有三类配置：

- `TUSHARE_*`：主备行情源；
- `AKSHARE_*`：公开新闻阻塞调用边界；
- `LLM_*`：模型地址、名称、凭据、结构化输出方式、strict 开关、纠正次数与诊断 JSONL 路径。

Runtime 会把同一个 `StructuredOutputOptions` 注入全部模型适配器。云端只接收 Pydantic 导出的纯
JSON Schema，本地观测层保留原始响应后再执行完整 Pydantic 校验；字段级错误和 token/finish reason
写入 `.artifacts/llm-structured-output.jsonl`。详见
[`llm-structured-output-observability-v1.md`](llm-structured-output-observability-v1.md)。

Runtime 的构造只创建对象和连接池，不会主动发起行情或 LLM 请求；真正的外部调用发生在
`await runtime.ainvoke(...)` 之后。

## 当前边界

- 已实现单次 CLI 运行和显式资源关闭；
- 尚未实现并发 Web 请求托管、定时调度、数据库持久化和断点恢复；
- 当前自动化测试用假地址和 `MockTransport` 验证装配，不等于真实上游和真实 LLM 已完成端到端运行。
