# Stock Research Agent

面向 A 股日线级研究的证据驱动多 Agent 系统。

第一版工作流以四类结构化对象为核心：

- `EvidenceRecord`：有来源、时点和验证状态的证据；
- `ThesisRecord`：由证据支持或反驳的观点；
- `ResearchRequest`：观点查证产生的定向研究任务；
- `RecommendationRecord`：进取、保守和委员会投资建议。

## 当前进度

目前已实现工程骨架、领域模型、LangGraph 共享状态、正式工作流的第一个
`InitializeRunNode`，以及行情数据的主备 Provider 基础层。LLM Agent、持久化缓存
和完整图将在后续阶段接入。

## 本地运行

```bash
uv sync
uv run stock-research-agent \
  --code 000001.SZ \
  --name 平安银行 \
  --as-of 2026-08-18T15:30:00+08:00
```

## 运行测试

```bash
uv run pytest
```
