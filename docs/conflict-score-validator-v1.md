# 冲突评分校验与纠错循环 v1

## 1. 职责边界

`ConflictScoreValidatorNode` 是纯确定性程序节点。它不调用 LLM，只检查两位经理对竞争同一
`conflict_group` 的两个原始版本是否给出数学上一致的分数。

它不负责：

- 判断建议是否达成共识；
- 替模型静默改分；
- 修改建议文本；
- 执行正式理由交换、提案修订或最终建议组装。

评分合法后的下一阶段是已实现的 `ConsensusGateNode`。

## 2. 确定性规则

对每位经理和每一对冲突条目，程序取：

```text
own_support_score         = 该经理对己方条目的初始正分
counterpart_support_score = 该经理对对方冲突条目的交叉评分
```

统一规则是：

```text
own_support_score + counterpart_support_score <= 0
```

评分和大于 `0` 时记录 `CONFLICT_GROUP_SUM_POSITIVE`。评分和恰好为 `0` 是合法边界，因此
`+0.25/-0.25`、`+0.50/-0.50`、`+0.75/-0.75`、`+1.00/-1.00` 均合法；任何正分
搭配 `0` 都不合法。`hard_veto=true` 必须对应 `-1.0` 由基础 Schema 先行保证，不需在本节点
重复修正。

该规则会从源头保证互斥条目不能同时通过。对两位经理的约束相加后，两个互斥条目的
`combined_score` 之和必然小于或等于 `0`；而通过门槛要求单条 `combined_score > 0`，所以两条
不可能同时通过。

## 3. `ConflictScoreValidationReport`

校验结果保存：

```text
run_id / as_of
source_fingerprint
aggressive_review_attempt
conservative_review_attempt
valid
invalid_managers[]
violations[]
stop_reason
```

单条 `ConflictScoreViolation` 包含：

```text
rule_code
manager
own_item_id
counterpart_item_id
own_support_score
counterpart_support_score
message
```

`source_fingerprint` 对完整 `CrossReviewedProposalPool` 与双方 attempt 生成 SHA-256。它同时解决两个问题：

- 对同一份输入重放节点时可幂等跳过；
- 模型在新 attempt 中返回了完全相同的非法分数时，attempt 仍使指纹变化，校验不会被错误
  跳过。

节点还会剔除对方评价后，将评分池的基础提案与 `NormalizedProposalPool`
逐字段比较。因此不能在校验或纠错期间偷换提案正文、引用、顺序或初始坚持分。

`stop_reason` 可为：

```text
valid
retry_required
retry_exhausted
missing_cross_reviewed_pool
invalid_state
```

## 4. 纠错调度

默认 `max_attempts=3`，并且包含首次评分：

```text
attempt 1 = 首次交叉评分
attempt 2 = 第一次纠错
attempt 3 = 第二次纠错
```

只有 `invalid_managers` 会被重新调用。只有一方违规时，另一方的 review 和 attempt 保持不变；
双方都违规时，`CrossReviewCorrectionNode` 在节点内部并发调用双方，只有双方都成功才提交。

`CrossReviewCorrectionRunSummary` 把一次纠错拆成四个可审计阶段：

| 字段 | 含义 |
|---|---|
| `requested_managers` | 校验报告要求纠错的经理 |
| `called_managers` | 已实际调用模型的经理；调用前输入/上下文失败时可以为空 |
| `staged_managers` | 单经理模型输出和校验成功、等待整批原子提交的经理 |
| `completed_managers` | 整次纠错成功后已经原子写回的经理 |

成功时四组经理完全相同。任一经理失败时不会提交任何一方，所以 `completed_managers` 为空；已经
成功的另一方可以出现在 `staged_managers` 中，表示其结果因原子回滚而被丢弃，而不是状态中存在
一份可继续使用的半成品。字段集合始终满足
`completed ⊆ staged ⊆ called ⊆ requested`。

纠错输入比首评多出：

```text
attempt > 1
previous_evaluations = 本经理上一次全部评分
validation_feedback = 只属于本经理的违规记录
```

模型仍须全量返回对方目录，但只能改动 feedback 中 `counterpart_item_id` 指向的评价。
非违规条目必须逐字段等于上一次评价。

纠错成功后，`ApplyCrossReviewsNode` 用新 review 替换旧的派生评分池，然后再进入 Validator。
写入的 `ProposalEvaluation` 保留 `previous_score`，并把 attempt 和违规代码写入
`score_change_reason`。

## 5. 耗尽和失败语义

任一当前仍违规的经理已到 `attempt=3` 时，报告返回 `retry_exhausted`，工作流终止。格式错误或
数学不一致的分数不会进入后续共识判断。

纠错模型异常、输出覆盖不完整、非违规条目被改动或状态指纹不一致时，纠错摘要返回
`attempt_failed/invalid_state`，并失败关闭。输入或上下文在模型调用前被拒绝时，摘要仍记录
`requested_managers`，但 `called/staged/completed` 可以全部为空。不会拿旧 review 无限重试，
也不会把 `staged_managers` 当成已提交结果恢复。

确定性纠错是评分契约修复，不是投资辩论，因此全程不增加 `debate_round`。

## 6. 后续共识阶段

评分报告为 `valid` 后，已实现的 `ConsensusGateNode` 根据双方分数和、最低反对分、硬否决及
评分合法性判断哪些建议通过，哪些建议进入正式理由交换和提案修订。它仍应对“互斥条目同时进入
通过集”保留失败关闭断言，用于发现状态损坏或代码回归，而不把该状态当作正常业务分支。
