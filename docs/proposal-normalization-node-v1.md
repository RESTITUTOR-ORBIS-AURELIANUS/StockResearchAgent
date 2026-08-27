# ProposalNormalizationNode v1

## 1. 节点位置

两位投资组合经理完成独立建议后，主图等待双方分支结束，再执行确定性规范化：

```text
finalize_thesis_validation
        ├── generate_aggressive_recommendation ──┐
        └── generate_conservative_recommendation ┤
                                                 ↓
                                      normalize_proposals
                                                 ↓
                                    cross-review branches
```

两个交叉评分节点和确定性写入节点现已从 `normalize_proposals` 继续执行，详见
[`cross-review-nodes-v1.md`](cross-review-nodes-v1.md)。

## 2. 输入与输出

节点读取：

```text
run_id
target
as_of
aggressive_recommendation
conservative_recommendation
```

节点写入：

```text
normalized_proposal_pool: NormalizedProposalPool | None
proposal_normalization_run_summary: ProposalNormalizationRunSummary | None
errors[]
```

`NormalizedProposalPool` 保存：

- 本轮 `run_id/as_of/research_target`；
- 两份原始 recommendation ID；
- 两份独立建议中全部 `ProposalItem` 的深拷贝；
- 程序生成的对称 `conflicts_with`。

两份 `aggressive_recommendation` 和 `conservative_recommendation` 是原始方案档案，节点不会修改其中的
`conflicts_with`、评分、文本或状态。后续交叉评分和辩论只操作规范化提案池的副本。

## 3. 决策槽和冲突规则

独立建议生成阶段已经按以下格式创建 `conflict_group`：

```text
target.type + ":" + upper(target.code) + ":" + decision_dimension
```

例如：

```text
STOCK:000001.SZ:POSITION_SIZE
MARKET:A_SHARE:ACTION
SECTOR:801780.SI:RISK_CONTROL
```

两个条目满足以下条件时互为冲突：

```text
conflict_group 相同
proposer 不同
```

程序会同时写入：

```text
aggressive_item.conflicts_with += conservative_item.item_id
conservative_item.conflicts_with += aggressive_item.item_id
```

这里的“冲突”表示它们争夺同一个最终决策槽位，不表示其中一条事实错误。因此下面两条仍会形成
冲突：

```text
进取经理：维持适度超配
防御经理：维持适度超配，但采用分批执行
```

最终方案不能未经协商便原样保留同一目标、同一维度的两个版本。后续节点需要通过互评、修改和
正式协商确定其中哪些条目能够进入共识；轮次耗尽后仍未通过的条目会被排除。

不同目标或不同 `decision_dimension` 不会在本节点自动建立冲突。未来跨维度的整体语义矛盾应由
委员会方案一致性检查处理，不交给规范化节点猜测。

## 4. 确定性约束

节点和 `NormalizedProposalPool` 共同保证：

- 双方 recommendation 的 `run_id/as_of/target/profile` 与当前状态一致；
- 所有 `conflict_group` 都符合规范化格式；
- 全部 `item_id` 唯一；
- 同一经理在同一决策槽中最多一条建议；
- 条目仍为 `revision=1` 和 `PROPOSED`；
- 每条条目只有提议方的一份初始评价；
- 不提前包含协商结论或最终条目状态；
- `conflicts_with` 精确覆盖另一位经理的同决策槽条目；
- 冲突引用有效且双向对称；
- 规范化不会新增或丢弃提案。

节点幂等：如果状态中已经存在来自同一轮、同两份 recommendation 的规范化提案池，则返回空更新，
不重复生成。

## 5. 失败语义

`ProposalNormalizationRunSummary.stop_reason`：

```text
complete
missing_recommendation
invalid_state
```

任一经理没有成功生成建议时，节点返回 `missing_recommendation`。身份、运行、时间、目标、ID、决策槽
或原始条目生命周期不合法时返回 `invalid_state`。失败时不会留下部分提案池，错误内容也不会包含模型
响应或密钥。
