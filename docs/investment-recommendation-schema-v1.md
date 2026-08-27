# 投资建议 JSON Schema v1

## 1. 当前实现边界

`PortfolioRecommendationDraft` 是进取型和防御型投资组合经理共同使用的 **LLM 输出
Schema**。它只描述一位经理的原始独立方案，不包含：

- `recommendation_id`、`run_id`、`as_of`、`created_at` 等程序字段；
- 对方经理的评分；
- 协商轮次、接受、排除状态或最终组装结果；
- 最终委员会建议。

这些字段由后续确定性节点装配进领域对象 `RecommendationRecord`。这样可以避免让模型伪造
ID、时间、状态，或者提前替另一位经理作答。

## 2. JSON 结构

```json
{
  "action": "OVERWEIGHT",
  "horizon": "未来一个至三个月",
  "confidence": 0.68,
  "summary": "已验证的盈利改善支持适度配置，但短期估值约束要求分批执行。",
  "valuation_guidance": "估值回到历史中位附近且盈利预期未下修时再增加配置。",
  "risk_summary": "盈利修复中断或资金面持续转弱时应降低风险暴露。",
  "proposal_items": [
    {
      "target": {"type": "STOCK", "code": "000001.SZ", "name": "平安银行"},
      "decision_dimension": "ACTION",
      "proposal": "将该标的设为适度超配，而不是一次性重仓买入。",
      "supporting_thesis_ids": [
        "th_20260825_000001_001"
      ],
      "insistence_score": 0.75,
      "score_reason": "盈利改善已得到查证，但估值和资金面仍限制风险预算。"
    },
    {
      "target": {"type": "STOCK", "code": "000001.SZ", "name": "平安银行"},
      "decision_dimension": "HORIZON",
      "proposal": "以未来一个至三个季度作为主要验证窗口。",
      "supporting_thesis_ids": [
        "th_20260825_000001_001"
      ],
      "insistence_score": 0.5,
      "score_reason": "核心观点依赖后续财报确认，不适合解释为短线交易信号。"
    },
    {
      "target": {"type": "STOCK", "code": "000001.SZ", "name": "平安银行"},
      "decision_dimension": "RISK_CONTROL",
      "proposal": "若盈利修复中断或核心资金指标连续恶化，则降低风险暴露。",
      "supporting_thesis_ids": [
        "th_20260825_000001_002"
      ],
      "insistence_score": 1.0,
      "score_reason": "该失效条件直接决定原投资逻辑是否仍成立。"
    }
  ]
}
```

## 3. 字段规则

### 3.1 整套方案

| 字段 | 含义 | 约束 |
|---|---|---|
| `action` | 对当前研究目标的总体动作 | `BUY/OVERWEIGHT/HOLD/UNDERWEIGHT/SELL/AVOID` |
| `horizon` | 投资期限 | 1 到 100 字符 |
| `confidence` | 对整套判断可靠性的确信程度 | `[0, 1]`；不是上涨概率或收益率 |
| `summary` | 总体建议及执行逻辑 | 自然语言，最多 4000 字符 |
| `valuation_guidance` | 可选估值或价格条件 | 证据不足时为 `null`，不得捏造目标价 |
| `risk_summary` | 风险、失效条件和风控重点 | 自然语言，最多 2000 字符 |
| `proposal_items` | 可独立协商的原子建议 | 3 到 16 条 |

### 3.2 原子建议

| 字段 | 含义 | 约束 |
|---|---|---|
| `target` | 该条建议直接针对的市场、板块或股票 | 总体研究目标可综合子目标观点；额外目标必须被引用观点直接覆盖 |
| `decision_dimension` | 决策维度 | 见下方枚举 |
| `proposal` | 可以单独接受、修改或拒绝的建议 | 自然语言，最多 2000 字符 |
| `supporting_thesis_ids` | 直接支持这条建议的已查证观点 | 1 到 16 个、不重复 |
| `insistence_score` | 提议方希望该条进入最终方案的坚持程度 | 只允许 `0.25/0.5/0.75/1.0` |
| `score_reason` | 为什么给出该坚持分 | 必须联系证据强弱、收益或风险 |

`decision_dimension` 允许：

```text
TARGET
ACTION
POSITION_SIZE
ENTRY_STRATEGY
EXIT_STRATEGY
VALUATION
HORIZON
RISK_CONTROL
```

每套独立建议至少包含 `ACTION`、`HORIZON`、`RISK_CONTROL` 三个原子维度。其他维度按
实际研究对象和证据充分度选填。

`conflict_group` 不再由 LLM 自由命名。装配节点根据 `target.type + target.code +
decision_dimension` 生成规范化分组，使两位经理对同一对象、同一决策维度的建议必然进入同一个
冲突组。同一份独立方案内，同一目标和决策维度只能出现一条建议。

## 4. 为什么独立方案不允许负坚持分

负分的含义是“反对某条已经存在的建议”。一位经理不应先提出一条自己反对的建议：如果他主张
`SELL` 或 `AVOID`，应当对这条负向动作本身给正坚持分。完整的九档 `-1` 到 `1` 分值将在双方
交叉评审 Schema 中使用。

## 5. 与最终 `RecommendationRecord` 的关系

后续节点会：

1. 校验 `supporting_thesis_ids` 只引用已完成查证、且适合进入决策阶段的观点；
2. 为建议和条目生成稳定 ID；
3. 将提议方的 `insistence_score` 转成第一条 `ProposalEvaluation`；
4. 加入对方评分、修订历史和条目状态；未达成共识的条目在轮次耗尽后标记为 `EXCLUDED`；
5. 分别保存进取型原始方案、防御型原始方案与委员会最终方案。

因此，Draft Schema 是“模型可以说什么”，`RecommendationRecord` 是“系统最终保存什么”。
