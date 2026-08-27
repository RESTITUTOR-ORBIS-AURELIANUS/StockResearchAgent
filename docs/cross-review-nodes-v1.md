# 进取型 / 防御型交叉评分节点 v1

## 1. 当前已实现流程

`ProposalNormalizationNode` 完成后，两位经理并行阅读同一份规范化提案池，但各自只评价
对方条目。首评写入后，确定性节点检查冲突分数，并对违规经理做有界纠错：

```text
normalize_proposals
        ├── aggressive_cross_review ──┐
        └── conservative_cross_review ─┤
                                      ↓ 等待双方完成
                             apply_cross_reviews
                                      ↓
                           validate_conflict_scores
                     ┌── valid ─────────────────→ END
                     ├── retry_required ──────┐
                     │                             ↓
                     │                  correct_conflict_scores
                     │                  只重评违规经理
                     │                             ↓
                     │                  apply_cross_reviews
                     │                             ↓
                     │                  validate_conflict_scores
                     └── retry_exhausted / failure ───→ END
```

`valid` 现在进入确定性 `ConsensusGateNode`。未过门条目才进入最多三轮的理由交换、原提议方
修订和冲突组闭包重评；首评纠错仍不计入 `debate_round`。

## 2. 并行首评与确定性写入

两个 LLM 节点不能并行写同一个标量提案池，否则 LangGraph 会把同一 superstep 中的更新视为
冲突。因此：

- `aggressive_cross_review` 只写进取经理的 review 及运行摘要；
- `conservative_cross_review` 只写防御经理的 review 及运行摘要；
- `apply_cross_reviews` 是唯一有权生成或替换 `cross_reviewed_proposal_pool` 的确定性节点。

两份原始建议和 `normalized_proposal_pool` 保持不变。每条写入后的提案恒为：

```text
evaluations[0] = 原提议方的初始正向坚持分
evaluations[1] = 对方当前 attempt 的交叉评分
revision = 1
status = PROPOSED
arbitration = null
```

此时尚未判断条目是否通过，也不会提前写成 `AGREED/NEGOTIATING`。

## 3. 模型输入与硬限制

每位经理收到严格的 `PortfolioCrossReviewInput`：

```text
run_id / as_of / research_target
reviewer
own_recommendation_id
counterpart_recommendation_id
attempt
own_proposals[]
counterpart_proposals[]
theses[]
eligible_supporting_thesis_ids[]
previous_evaluations[]
validation_feedback[]
policy_notes[]
```

`own_proposals` 只用于比较风险偏好和冲突槽，模型不能评价自己的条目；
`counterpart_proposals` 必须逐条评价。完整的终态 `DecisionThesisSummary[]` 使无会话状态的 LLM
可以独立核对建议。`SUPPORTED/MIXED` 可作为直接依据；`REFUTED/INCONCLUSIVE` 只用于理解
风险与未知边界。

默认硬限制：

```text
max_input_theses = 32
max_context_characters = 120000
max_attempts = 3
```

超过观点或上下文硬限制时不截断输入，也不调用模型。`max_attempts=3` 包含一次首评和
最多两次确定性纠错。

## 4. 模型输出、身份绑定与纠错轨迹

LLM 始终只输出 [`PortfolioCrossReviewDraft`](cross-review-schema-v1.md)：

```text
evaluations[]:
  item_id
  support_score
  hard_veto
  reason
  modification_suggestion
```

程序随后：

1. 校验输出恰好覆盖全部 `counterpart_proposals`；
2. 拒绝漏评、重复、评价自己或伪造 ID；
3. 按对方目录顺序重排评价；
4. 注入 `reviewer/run_id/as_of/recommendation_id/attempt/created_at`；
5. 保存为 `PortfolioCrossReviewRecord`。

首次评分没有 `previous_score` 和 `score_change_reason`。纠错后，程序从上一 attempt 填充
`previous_score`，并根据违规代码写入可审计的 `score_change_reason`。这些字段不由模型伪造。

## 5. Prompt 和评分语义

两个模型使用同一 Schema、不同角色 Prompt：

- `AggressivePortfolioManager`：允许反对过度等待或过低风险暴露，但必须正视回撤和失效条件；
- `ConservativePortfolioManager`：优先永久损失、安全边际和执行风险，但不能机械反对进取方案。

`support_score` 表示“该对方条目是否应原样进入委员会方案”，不是观点置信度、上涨概率或
买卖方向。对冲突条目的合法映射是：

```text
own +0.25 -> counterpart -0.25/-0.50/-0.75/-1.00
own +0.50 -> counterpart -0.50/-0.75/-1.00
own +0.75 -> counterpart -0.75/-1.00
own +1.00 -> counterpart -1.00
```

统一判定公式是 `own_support_score + counterpart_support_score <= 0`。它允许等强度的正负边界，
但不允许正分搭配 `0`。

`support_score=-1.00` 表示原始版本完全不可接受，但不自动等于 `hard_veto=true`。
`hard_veto=true` 时分数必须是 `-1.00`。完整 Prompt 位于：

- `src/stock_research_agent/agents/debate/prompts.py`

## 6. 确定性校验与局部纠错

`ConflictScoreValidatorNode` 不调用 LLM，不会替模型静默改分。当前只有一个统一违规代码：

```text
CONFLICT_GROUP_SUM_POSITIVE
```

校验报告记录双方 attempt、违规经理、具体条目和源内容指纹。指纹同时包含评分池和双方
attempt，因此即使模型重复返回相同的非法分数，也会被视为新的已消耗尝试，不会死循环。

`CrossReviewCorrectionNode` 只重新调用 `invalid_managers`。如果双方都违规，节点内部并发调用
两个模型，并在全部成功后原子写回；合法一方保持原记录。纠错时：

- 模型仍须返回对方全部条目；
- 只能改动 `validation_feedback.counterpart_item_id` 点名的评价；
- 程序会拒绝对任何非违规评价的改动；
- 程序会核对旧评分池与规范化原提案，拒绝中途被替换的提案正文或初始评分；
- 成功后重新执行 `apply_cross_reviews → validate_conflict_scores`；
- 纠错不增加 `debate_round`。

任一被点名经理已到第 3 次仍违规时，阶段以 `retry_exhausted` 失败关闭，非法分数不得进入
共识门或最终建议。详见 [`conflict-score-validator-v1.md`](conflict-score-validator-v1.md)。

## 7. 停止和失败语义

`PortfolioCrossReviewRunSummary.stop_reason`：

```text
complete
missing_normalized_pool
thesis_limit_exceeded
context_limit_exceeded
invalid_state
model_error
rejected_output
```

`CrossReviewApplicationRunSummary.stop_reason`：

```text
complete
missing_cross_review
invalid_state
```

`CrossReviewCorrectionRunSummary.stop_reason`：

```text
complete
missing_retry_report
invalid_state
attempt_failed
```

该摘要用四组经理集合区分“调度、模型调用、暂存成功和原子提交”，不能把它们当成同一个
`success` 标志：

| 字段 | 审计含义 |
|---|---|
| `requested_managers` | Validator 本轮要求纠错的经理；正常 retry 时等于 `invalid_managers` |
| `called_managers` | 本轮实际已经调用交叉评分模型的经理；若输入或上下文在调用前失败，可以为空 |
| `staged_managers` | 模型调用和单经理输出校验已经成功，但结果仍在本次原子事务的暂存区；另一位失败时，这些结果会被丢弃 |
| `completed_managers` | 新评分已经随整次纠错原子提交到 Graph State 的经理 |

成功时四组集合都等于本轮被请求经理。`attempt_failed` 时 `completed_managers` 必须为空；即使
`staged_managers` 非空，也只证明某个模型结果曾通过单经理校验，不代表它已写入状态。对于
`missing_retry_report` 或模型调用前发现的 `invalid_state`，后三组集合为空。

任一方首评失败时不会生成半成品评分池。一次纠错中任一被点名经理失败，本次纠错不会部分
提交。模型异常只记录异常类型，不保存可能包含敏感内容的异常正文。兼容的现有评分、纠错和写入
结果会幂等跳过；内容陈旧或损坏的结果不会被误认为兼容。

## 8. 配置示例

```python
cross_review_limits = PortfolioCrossReviewLimits(max_attempts=3)

aggressive_cross_review = OpenAIAggressivePortfolioCrossReviewModel(
    chat_model,
    structured_output_method=llm_settings.structured_output_method,
)
conservative_cross_review = OpenAIConservativePortfolioCrossReviewModel(
    chat_model,
    structured_output_method=llm_settings.structured_output_method,
)

graph = build_research_graph(
    # 省略证据、观点查证和独立建议配置
    aggressive_cross_review_model=aggressive_cross_review,
    conservative_cross_review_model=conservative_cross_review,
    cross_review_limits=cross_review_limits,
)
```

两个交叉评分模型必须成对配置，并且必须先启用两位独立投资组合经理。
