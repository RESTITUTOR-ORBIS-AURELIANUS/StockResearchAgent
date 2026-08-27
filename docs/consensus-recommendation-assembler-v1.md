# 最终共识建议组装器 v1

## 1. 职责与安全边界

`ConsensusRecommendationAssemblerNode` 已接入主 LangGraph，是正式协商后的唯一出口。第一版不设
主席、仲裁模型或仲裁路由：达到最多三轮后仍未通过共识门的条目固化为 `EXCLUDED`，然后直接
进入本节点。

组装节点将权力分为两部分：

- **确定性程序**：决定哪些条目能进入委员会建议，校验来源、决策维度和观点引用，计算信心度，并组装最终 `RecommendationRecord`；
- **受限合成模型**：只把已通过的原子条目压缩为顶层 `action/horizon/summary/valuation_guidance/risk_summary`文字，不能决定纳入集合。

模型永远看不到 `REJECTED`、`WITHDRAWN`、`EXCLUDED` 或尚未结束的 `NEGOTIATING` 条目，因此不能把
分歧意见偷偷补回最终建议。进取型和防御型两份原始 `RecommendationRecord` 始终保留，组装器不覆盖它们。

## 2. 前置路由

`ConsensusRoute` 只有：

```text
NEGOTIATE
ASSEMBLE
```

- 尚有可协商条目且 `debate_round < max_rounds` 时路由 `NEGOTIATE`；
- 无待协商条目，或达到 `max_rounds` 并把未决条目标记为 `EXCLUDED` 后，路由 `ASSEMBLE`；
- v1 不存在 `ARBITRATE` 路由。

`ConsensusRecommendationAssemblerNode` 只接受最新、来源指纹一致、且没有 `negotiating_item_ids` 的
`ASSEMBLE` Gate 报告。

## 3. 模型协议与适配器

### 3.1 `ConsensusRecommendationSynthesisModel`

```python
class ConsensusRecommendationSynthesisModel(Protocol):
    async def synthesize(
        self,
        request: ConsensusRecommendationSynthesisInput,
    ) -> ConsensusRecommendationSynthesisDraft: ...
```

`OpenAIConsensusRecommendationSynthesisModel` 使用 `BaseChatModel.with_structured_output(...)` 绑定严格
`ConsensusRecommendationSynthesisDraft`，默认 `function_calling`，也可配置 `json_schema`。它只发送一条系统
Prompt 和一条序列化后的结构化请求。

## 4. 模型输入

### 4.1 `ConsensusRecommendationSynthesisInput`

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 当前运行 ID |
| `as_of` | `AwareDatetime` | 统一数据截止时点 |
| `research_target` | `ResearchTarget` | 整体研究目标 |
| `debate_round` | `int` | 实际执行的正式协商轮数，范围 `0..3` |
| `source_fingerprint` | `str` | Gate、建议池、原始建议和引用观点的 SHA-256 指纹 |
| `accepted_items` | `tuple[ProposalItem, ...]` | 仅 `AGREED` 条目，`1..32` 条 |
| `supporting_theses` | `tuple[DecisionThesisSummary, ...]` | 精确覆盖所有接受条目引用的终态观点，`1..64` 条 |
| `policy_notes` | `tuple[str, ...]` | 不得增删改原子建议的程序级约束 |

程序在调用模型前已经排除未达成共识的条目。`supporting_theses` 只允许 `SUPPORTED/MIXED` 终态观点，
每个观点必须与当前 `run_id/as_of` 一致，并具有完整信心度与推理摘要。

## 5. 模型输出

### 5.1 `ConsensusRecommendationSynthesisDraft`

| 字段 | 类型 | 含义 |
|---|---|---|
| `action` | `RecommendationAction` | 只从总体目标的唯一 `AGREED ACTION` 条目归纳 |
| `horizon` | `str` | 只压缩总体目标的唯一 `AGREED HORIZON` 条目 |
| `summary` | `str` | 忠实覆盖全部 `accepted_items`，不得新增建议 |
| `valuation_guidance` | `str \| null` | 仅存在 `AGREED VALUATION` 条目时允许填写 |
| `risk_summary` | `str` | 只归纳总体目标的全部 `AGREED RISK_CONTROL` 条目 |
| `action_source_item_id` | `str` | `action` 的唯一来源条目 |
| `horizon_source_item_id` | `str` | `horizon` 的唯一来源条目 |
| `risk_source_item_ids` | `tuple[str, ...]` | 必须等于总体目标下所有 `AGREED RISK_CONTROL` ID |
| `summary_source_item_ids` | `tuple[str, ...]` | 必须等于所有 `accepted_items` ID |
| `valuation_source_item_ids` | `tuple[str, ...]` | 必须等于所有 `AGREED VALUATION` ID |

模型不输出 `proposal_items`、`confidence`、ID、时间、辩论状态或纳入决策。所有 source item ID 都由
确定性节点反向校验。

## 6. 确定性组装

模型输出通过 Schema 和来源目录校验后，节点生成 `RecommendationRecord(profile=CONSENSUS)`：

- `proposal_items`：对 Gate `agreed_item_ids` 的深拷贝，所有条目必须是 `ProposalStatus.AGREED`；
- `supporting_thesis_ids`：全部纳入条目引用观点的有序去重并集；
- `confidence`：上述终态观点 `validation.confidence` 的最小值，不由 LLM 乐观抬高；
- `debate.status`：存在 `excluded_item_ids` 时为 `PARTIAL_CONSENSUS`，否则为 `AGREED`；
- `debate.excluded_item_ids`：精确复制最终 Gate 中被排除的条目；
- `debate.remaining_disagreements`：按 `conflict_group` 合并，每个冲突组只生成一条可审计说明，并在其中列出该组全部被排除的 item ID；
- `generated_by`：固定为 `ConsensusRecommendationAssemblerNode`；
- `recommendation_id`：由 run、来源指纹和合成 Draft 生成稳定 SHA-256 派生 ID。

## 7. 无可执行共识

存在以下任一条件时，节点 **不调用模型**：

- `agreed_item_ids` 为空；
- 总体研究目标缺少 `ACTION`、`HORIZON`、`RISK_CONTROL` 任一必需维度。

这是有效的业务结果，不是系统错误：

```text
consensus_recommendation = null
consensus_assembly_run_summary.stop_reason = "no_actionable_consensus"
consensus_assembly_run_summary.model_called = false
```

程序不会自动填写 `HOLD`、默认期限或虚构风险条件。用户仍可查看两份原始经理建议和全部协商审计记录。

## 8. `ConsensusAssemblyRunSummary`

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时点 |
| `source_fingerprint` | `str \| null` | 成功建立输入目录后的组装指纹 |
| `debate_round` | `int` | 已进入的正式协商轮数 |
| `agreed_item_ids` | `tuple[str, ...]` | 最终纳入候选条目 |
| `excluded_item_ids` | `tuple[str, ...]` | 轮次耗尽后未达成共识的条目 |
| `rejected_item_ids` | `tuple[str, ...]` | 因同冲突组已有获胜条目而被拒绝的条目 |
| `withdrawn_item_ids` | `tuple[str, ...]` | 原提议方撤回的条目 |
| `missing_required_dimensions` | `tuple[DecisionDimension, ...]` | 总体目标缺少的必需维度 |
| `input_thesis_count` | `int` | 实际进入合成请求的观点数 |
| `context_character_count` | `int` | 序列化合成请求的字符数 |
| `model_called` | `bool` | 是否真正调用合成模型 |
| `recommendation_id` | `str \| null` | 仅成功完成时存在 |
| `stop_reason` | 枚举字符串 | 组装结果或失败原因 |

`stop_reason` 取值：

```text
complete
no_actionable_consensus
missing_input
stale_input
invalid_state
context_limit_exceeded
model_error
rejected_output
```

## 9. 失败、幂等与边界

- 缺 `run_id/as_of/target`、最终 Gate、协商池或任一份原始建议：`missing_input`；
- Gate 指纹、轮次或条目版本过期：`stale_input`；
- Gate/池/原始建议的 run、时点、目标、ID、状态目录或必需维度不一致：`invalid_state`；
- 支持观点不存在、不同 scope、非 `SUPPORTED/MIXED` 或缺完整判断上下文：`invalid_state`；
- 结构化输入超过 `120000` 字符：`context_limit_exceeded`，不静默截断；
- 模型抛错：`model_error`；
- 模型来源 ID、必需维度来源或估值来源不符合精确目录：`rejected_output`；
- 失败时不写入半成品 `consensus_recommendation`，错误只保存异常类型或确定性摘要；
- 相同来源指纹和相同已完成产物幂等返回；不同指纹或不完整/旧失败产物不得被静默覆盖。

## 10. 主图接线

```text
ConsensusGateNode
  -> NEGOTIATE: BeginNegotiationRoundNode
  -> ASSEMBLE: ConsensusRecommendationAssemblerNode
       -> complete: consensus_recommendation + consensus_assembly_run_summary
       -> no_actionable_consensus: consensus_recommendation=null + run summary
       -> failure: error + run summary, no partial recommendation
  -> END
```

构建器要求两个正式协商模型与 `consensus_assembly_model` 一起配置；启用正式协商却不提供组装模型会在建图时
立即拒绝。
