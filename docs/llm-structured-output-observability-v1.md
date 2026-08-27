# LLM 结构化输出与可观测性契约 v1

> 实现状态：所有真实 LLM Agent 适配器已接入统一观测层；配置、失败纠正、字段级诊断和脱敏
> JSONL 已实现。该层不改变各节点的领域输出，只负责模型传输边界。

## 1. 为什么不能直接依赖 `with_structured_output(PydanticModel)`

阿里云百炼的 JSON Schema 严格模式负责保证云端可见的 JSON 形状，但 Pydantic
`model_validator` 中的跨字段规则并不会自动全部进入 JSON Schema。例如：

- 相对强弱测量要求同时提供 benchmark；
- 新闻业绩披露要求 report period 为季度末；
- 观点完成判断必须至少引用一条证据；
- `REQUEST_RESEARCH` 与 `FINALIZE` 必须互斥。

另外，若把 Pydantic 类直接交给 OpenAI SDK，SDK 可能在 LangChain 的 `include_raw` 接管之前解析
响应。此时本地校验失败会直接从传输调用抛出，原始 `AIMessage` 无法进入应用诊断层。

当前实现因此采用两阶段边界：

```text
Pydantic model_json_schema()
        ↓
云端 strict JSON Schema 生成
        ↓
include_raw=True 保留 AIMessage
        ↓
本地 Pydantic 完整校验
        ├─ 成功：返回领域草稿
        └─ 失败：写诊断 → 携字段错误纠正一次 → 再校验
```

## 2. 统一入口

所有 Agent 模型通过：

```python
build_observable_structured_output(
    chat_model,
    OutputModel,
    method="json_schema",
    operation="technical.analyze_daily",
    options=structured_output_options,
)
```

构建。实现位于：

```text
src/stock_research_agent/llm/structured_output.py
```

每个通道都有稳定 `operation` 名称，例如 `validator.review_thesis`、
`portfolio.aggressiveportfoliomanager.generate_recommendation`。它用于跨节点定位失败，不是业务 ID。

## 3. 配置

```dotenv
LLM_STRUCTURED_OUTPUT_METHOD=json_schema
LLM_STRUCTURED_OUTPUT_STRICT=true
LLM_STRUCTURED_OUTPUT_REPAIR_ATTEMPTS=1
LLM_STRUCTURED_OUTPUT_DIAGNOSTICS_PATH=.artifacts/llm-structured-output.jsonl
LLM_STRUCTURED_OUTPUT_RAW_MAX_CHARACTERS=20000
```

| 配置 | 语义 |
|---|---|
| `METHOD` | 当前阿里云兼容端点使用 `json_schema`；具体模型由 `LLM_MODEL` 配置 |
| `STRICT` | 把 `strict=true` 交给支持该能力的上游 |
| `REPAIR_ATTEMPTS` | 本地校验失败后的纠正调用次数，默认 1，最大 3 |
| `DIAGNOSTICS_PATH` | 成功和失败事件的追加式 JSONL |
| `RAW_MAX_CHARACTERS` | 单条原始模型正文的持久化上限；超出部分明确标记截断 |

`.artifacts/` 已加入 `.gitignore`，诊断文件不会提交到仓库。

## 4. 每条诊断事件

每次调用至少记录：

| 字段 | 内容 |
|---|---|
| `event_id` | `llm_...` 关联 ID；失败报告会回显 |
| `timestamp` | UTC 时间 |
| `operation` / `schema` | 调用节点与输出模型 |
| `method` / `strict` | 实际结构化输出模式 |
| `attempt` / `status` | 第几次调用；`success`、`validation_error`、`repaired` 或 `transport_error` |
| `duration_ms` | 单次模型等待时间 |
| `trace` | 从请求提取的 `run_id`、`as_of`、target、thesis ID 和轮次 |
| `validation_errors` | Pydantic 字段路径、错误类型、错误值的受限预览 |
| `response.content` | 脱敏并按上限截断的原始模型正文 |
| `response_metadata` | 模型名、`finish_reason` 等 SDK 元数据 |
| `usage_metadata` | input/output/total token 与 reasoning token（上游提供时） |
| `repaired_from_event` | 修复成功事件指向前一次失败事件 |

错误进入最终报告时只携带受控摘要与 `diagnostic_event`，不会把任意 Provider 异常正文直接放进报告。

## 5. 安全边界

- 不记录 system/user prompt 全文，只从序列化请求抽取少量关联字段；
- 对 API Key、authorization、password、secret 和常见 `sk-`/`tk-` 值脱敏；
- 任意未审计的 Provider 异常只向报告暴露异常类型；
- 结构化失败事件可以保存模型响应，但目录必须继续留在 Git 之外；
- 诊断日志包含研究内容，部署时仍应按后端运行日志对待，不应公开下载。

## 6. 观点查证的专用传输 Schema

观点审查员不再直接把领域模型 `ThesisValidationDecision` 交给云端，而使用
`ThesisValidationModelOutput`：

```text
review_summary
decision (discriminator=action)
  ├─ REQUEST_RESEARCH → research_request
  └─ FINALIZE         → finalization
discovered_candidates
```

`FINALIZE` 的传输对象使用非空 `evidence_assessments[]`，每项包含 `evidence_id` 与
`SUPPORTING/CONTRADICTING`。程序再确定性转换为领域层原有的 supporting 和 contradicting 两个集合。
这样“至少一条引用”和两种动作互斥都成为云端可见的结构约束，领域 JSON 与最终报告契约不变。

## 7. 排查方法

失败报告会出现类似：

```text
diagnostic_event=llm_xxx; diagnostic_path=.artifacts/llm-structured-output.jsonl
```

可以按事件 ID 查看：

```bash
rg 'llm_xxx' .artifacts/llm-structured-output.jsonl
```

判断顺序建议为：

1. `transport_error`：先看网络、HTTP、模型端点或上游 Schema 拒绝；
2. `validation_error`：按字段路径检查模型输出和本地业务规则；
3. `repaired`：说明第一次失败已由同一通道自动纠正；
4. `finish_reason != stop`：检查截断、内容过滤等上游终止原因；
5. token 与时长：判断上下文膨胀和成本，而不是把所有失败都归因于超时。
