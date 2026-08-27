# 投资建议交叉评分 JSON Schema v1

## 1. 当前实现边界

本文件定义两位投资组合经理首次评价对方方案，以及确定性校验失败后局部纠错时使用的契约：

- `PortfolioCrossReviewInput`：程序冻结后提供给一位评审经理的完整上下文；
- `PortfolioCrossReviewDraft`：评审经理必须按严格 JSON Schema 返回的评分；
- `CrossReviewEvaluationDraft`：一条对对方原子建议的评价；
- `CrossReviewProposalContext`：原始 `ProposalItem` 的只读投影；
- `ConflictScoreValidationReport`：确定性评分校验结果和纠错指令。

`ProposalNormalizationNode`、两个并行首评节点、确定性写入、
`ConflictScoreValidatorNode` 和最多两次局部纠错已经实现。这一阶段不是正式辩论，也不是最终共识判断。
`ConsensusGateNode`、理由交换/提案修订、最多三轮正式协商和委员会最终建议组装已实现。

## 2. 只读提案上下文

模型需要同时阅读自己的条目和对方的条目，但不能修改原提案的文本、引用、提议方、版本、状态或 ID。
因此输入使用 `CrossReviewProposalContext`：

```json
{
  "item_id": "item_conservative_action",
  "target": {"type": "MARKET", "code": "A_SHARE", "name": "A股市场"},
  "decision_dimension": "ACTION",
  "conflict_group": "MARKET:A_SHARE:ACTION",
  "conflicts_with": ["item_aggressive_action"],
  "proposer": "ConservativePortfolioManager",
  "proposal": "在盈利预期进一步确认前维持中性权益暴露。",
  "supporting_thesis_ids": ["th_20260825_000001_001"],
  "proposer_insistence_score": 0.75,
  "proposer_score_reason": "证据方向积极，但确认程度不足以承担更高风险预算。"
}
```

`conflicts_with` 只能引用本次目录中另一位经理的同 `conflict_group` 条目，并且必须双向对称。

## 3. 模型输入

首次评分的关键字段如下：

```json
{
  "run_id": "run_20260825_000001",
  "as_of": "2026-08-25T17:00:00+08:00",
  "research_target": {"type": "MARKET", "code": "A_SHARE", "name": "A股市场"},
  "reviewer": "AggressivePortfolioManager",
  "own_recommendation_id": "rec_20260825_000001_aggressive",
  "counterpart_recommendation_id": "rec_20260825_000001_conservative",
  "attempt": 1,
  "own_proposals": ["...CrossReviewProposalContext..."],
  "counterpart_proposals": ["...CrossReviewProposalContext..."],
  "theses": ["...DecisionThesisSummary..."],
  "eligible_supporting_thesis_ids": ["th_20260825_000001_001"],
  "previous_evaluations": [],
  "validation_feedback": [],
  "policy_notes": []
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `reviewer` | 当前交叉评分经理 | 由程序指定，模型不能冒充 |
| `attempt` | 当前评分尝试 | `1` 是首评，`2/3` 是确定性纠错 |
| `own_proposals` | 评审者自己的原始建议 | 只用于比较，不允许在输出中评价或改写 |
| `counterpart_proposals` | 对方经理的原始建议 | 输出必须恰好逐条覆盖 |
| `theses` | 双方共同可见的全部终态观点摘要 | 只有 `SUPPORTED/MIXED` 可作为直接依据 |
| `eligible_supporting_thesis_ids` | 可直接支撑原建议的观点 | 必须精确对应 `SUPPORTED/MIXED` |
| `previous_evaluations` | 本经理上一次对全部对方条目的评分 | 首评必须为空；纠错时必须完整覆盖 |
| `validation_feedback` | 确定性校验器给本经理的违规反馈 | 首评必须为空；纠错时只能包含本经理的违规 |
| `policy_notes` | 评分口径和风险边界 | 程序维护的只读规则 |

## 4. LLM 输出

首评和纠错都使用同一个 `PortfolioCrossReviewDraft`：

```json
{
  "evaluations": [
    {
      "item_id": "item_conservative_action",
      "support_score": -0.75,
      "hard_veto": false,
      "reason": "该版本与己方冲突条目争夺同一决策槽，但风险限制具有价值。",
      "modification_suggestion": "保留确认条件，同时允许小规模试探仓位。"
    }
  ]
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `item_id` | 被评价的对方条目 | 必须来自 `counterpart_proposals`，每条恰好一次 |
| `support_score` | 该条原样进入委员会方案的支持度 | 只允许九档离散分数 |
| `hard_veto` | 当前条件下是否完全不可接受 | `true` 时分数必须是 `-1.0`；反向不强制 |
| `reason` | 评分原因 | 必须联系观点、收益风险或执行条件 |
| `modification_suggestion` | 如何修改后更可接受 | 没有建议时必须为 `null` |

九档分数是：

```text
-1.00, -0.75, -0.50, -0.25, 0.00, 0.25, 0.50, 0.75, 1.00
```

`reviewer`、`previous_score`、`score_change_reason`、条目版本和状态不在 LLM 输出 Schema 中：

- `reviewer` 由当前节点身份注入；
- 首评的 `previous_score` 为空；
- 纠错后的 `previous_score` 由上一次记录写入；
- 纠错后的 `score_change_reason` 由程序根据 attempt 和违规代码生成；
- 条目状态由已实现的共识门与正式协商流程确定性维护。

## 5. Schema 与纠错输入校验

当前代码会拒绝：

- 非九档离散分数；
- `hard_veto=true` 但分数不是 `-1.0`；
- 同一 `item_id` 重复评价；
- 漏掉对方条目、评价自己条目或伪造目录外 ID；
- 输入中的身份错位、重复 ID、悬空或非对称冲突引用；
- 评分池的基础提案与 `NormalizedProposalPool` 不一致；
- `attempt=1` 却携带历史评分或校验反馈；
- `attempt>1` 却没有上一次全量评分或没有本经理的校验反馈；
- 纠错时改动未被 `validation_feedback` 点名的评价。

## 6. 已实现的跨条目确定性校验

`ConflictScoreValidatorNode` 负责：

1. 要求同一经理对一对互斥建议的评分和小于或等于 `0`；
2. 接受评分和恰好为 `0` 的边界组合，例如 `+0.50/-0.50`；
3. 拒绝正分搭配 `0`、双正分或任何评分和大于 `0` 的组合；
4. 输出带双方 attempt、违规条目及源指纹的 `ConflictScoreValidationReport`；
5. 在尚可纠错时返回 `retry_required`，耗尽后返回 `retry_exhausted`。

每位经理最多三次模型调用（一次首评加两次纠错）。确定性纠错不增加 `debate_round`。

纠错节点另用 `CrossReviewCorrectionRunSummary` 保存原子事务的审计轨迹：

```text
requested_managers = 本轮被要求纠错的经理
called_managers    = 实际已经调用模型的经理
staged_managers    = 单经理调用和校验成功、但尚未原子提交的经理
completed_managers = 整批原子提交成功的经理
```

这些集合满足 `completed ⊆ staged ⊆ called ⊆ requested`。`complete` 时四者相等；
`attempt_failed` 时 `completed_managers` 必须为空，而 `staged_managers` 可以记录因另一位经理失败
而最终被丢弃的成功结果。若输入或上下文在模型调用前失败，`requested_managers` 仍可保留纠错意图，
但 `called/staged/completed` 均为空。该区分只用于审计，不允许据 `staged_managers` 恢复或部分提交
评分。

## 7. 已接入的后续共识规则

交叉评分合法后，`ConsensusGateNode` 与正式协商子图负责：

1. 每条建议是否满足双方分数和、最低反对分和硬否决条件；
2. 防御性确认已通过集合没有出现理论上不可能存在的互斥双通过状态；
3. 未通过条目如何进入理由交换、原提议方修订、重新评分，或在轮次耗尽后转为 `EXCLUDED`。

交叉评分契约本身只保证评分材料和数学关系合法；是否通过、继续协商或排除由后续确定性节点判断。
最终组装只消费 `AGREED` 条目，原始两份经理建议不被覆盖。
