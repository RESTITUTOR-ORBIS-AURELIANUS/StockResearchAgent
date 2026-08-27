# 进取型 / 防御型投资组合经理节点 v1

## 1. 当前实现

观点查证全部结束后，主图并行执行：

```text
finalize_thesis_validation
        ├── generate_aggressive_recommendation
        └── generate_conservative_recommendation
                    ↓ 等待双方完成
           normalize_proposals
               ├── aggressive_cross_review
               └── conservative_cross_review
                          ↓
                 apply_cross_reviews
                          ↓
              validate_conflict_scores
                  ├── valid ─────────────→ END
                  ├── retry_required
                  │          ↓
                  │ correct_conflict_scores
                  │  只重评违规经理
                  │          ↓
                  │ apply_cross_reviews
                  │          ↓
                  │ validate_conflict_scores
                  └── retry_exhausted / failure ─→ END
```

两条分支读取同一份终态 `thesis_pool`，互相看不到对方建议，因此保存的是两份真正独立的原始
方案。两份方案随后由确定性 `ProposalNormalizationNode` 汇合并生成对称冲突关系，再由两位经理
并行完成首次交叉评分。确定性 Validator 会检查冲突分数，并让违规经理最多纠错两次；一次首评加
两次纠错使单经理总调用次数不超过 3，且这一契约修复循环不增加 `debate_round`。评分合法后的
`ConsensusGateNode` 和最多三轮正式理由交换/提案修订/受影响冲突组重评已经实现；委员会最终
建议组装也已实现。轮次耗尽后未决条目转为 `EXCLUDED`，只有 `AGREED` 条目进入最终建议；v1 不设仲裁路由。

对应 Agent：

| Agent | 角色 | State 输出 |
|---|---|---|
| `AggressivePortfolioManager` | 进取型投资组合经理 | `aggressive_recommendation` |
| `ConservativePortfolioManager` | 防御型投资组合经理 | `conservative_recommendation` |

## 2. 模型输入

两位经理都接收 `PortfolioRecommendationInput`：

```text
run_id
as_of
research_target
manager
profile
theses: DecisionThesisSummary[]
eligible_supporting_thesis_ids[]
policy_notes[]
```

`theses` 包含全部四种终态观点：

- `SUPPORTED`：可以作为直接建议依据；
- `MIXED`：可以支持带条件、较低风险预算的建议；
- `REFUTED`：只表示原观点被反驳，不代表反方向自动成立；
- `INCONCLUSIVE`：表示未知，可以影响风险态度，但不能伪装成直接支持依据。

只有 `SUPPORTED/MIXED` 会进入 `eligible_supporting_thesis_ids`。节点在模型返回后重新检查每一条
引用，不能只依赖 Prompt。

## 3. 模型输出与程序装配

LLM 只输出 [`PortfolioRecommendationDraft`](investment-recommendation-schema-v1.md)。程序负责：

1. 检查引用 ID 是否存在且属于 `SUPPORTED/MIXED`；
2. 对总体 `research_target` 允许综合多个已查证子目标观点；额外板块/股票条目必须被至少一个引用观点直接覆盖；
3. 要求总体 `research_target` 至少有 `ACTION/HORIZON/RISK_CONTROL` 三条原子建议；
4. 按 `target.type + target.code + decision_dimension` 生成跨经理一致的 `conflict_group`；
5. 生成稳定 `recommendation_id` 与 `item_id`；
6. 把经理自己的 `insistence_score` 装成第一条 `ProposalEvaluation`；
7. 写入固定的 `profile/proposer/generated_by`，模型不能冒充另一位经理；
8. 整套原子提交：任一条失败时拒绝整份 Draft，不保存半成品。

独立建议的初始条目统一为：

```text
revision = 1
status = PROPOSED
conflicts_with = []
arbitration = null
debate = null
```

## 4. 角色 Prompt

两位经理共享相同事实和引用边界，但有不同风险偏好。

### 4.1 进取型经理

Prompt 明确规定：当上行观点、催化剂和风险收益比得到充分支持时，可以更早承担风险、提出更积极
动作或更高风险预算；但必须保留入场条件、失效条件和退出纪律。进取不等于机械 `BUY`。

### 4.2 防御型经理

Prompt 明确规定：优先控制永久损失、回撤和证据缺口，要求更清晰的安全边际与确认条件；但防御
不等于机械 `HOLD/AVOID`。证据充分且风险可控时仍须输出清晰方案。

两份 Prompt 都包含严格 JSON 规则和独立 Few-shot。完整原文位于：

- `src/stock_research_agent/agents/portfolio/prompts.py`

## 5. 停止和失败语义

每位经理有独立 `PortfolioRecommendationRunSummary`：

```text
complete
no_decision_theses
thesis_limit_exceeded
context_limit_exceeded
invalid_state
model_error
rejected_output
```

硬边界：

- 两位经理必须成对配置；
- 未启用观点查证时不能启用投资经理；
- 默认最多输入 32 条观点；
- 默认上下文最多 120000 字符，超限不静默截断；
- 没有 `SUPPORTED/MIXED` 观点时不调用模型、不制造建议；
- 模型异常只记录异常类型，不保存可能含敏感响应的异常正文；
- 已存在兼容建议时节点幂等跳过，不重复覆盖。

## 6. 尚未实现

- 报告组装与长期持久化。

## 7. 主图配置示例

两位经理可以复用同一个底层 `ChatOpenAI` 连接，但分别绑定不同 Prompt：

```python
chat_model = build_chat_model(llm_settings)

aggressive_model = OpenAIAggressivePortfolioManagerModel(
    chat_model,
    structured_output_method=llm_settings.structured_output_method,
)
conservative_model = OpenAIConservativePortfolioManagerModel(
    chat_model,
    structured_output_method=llm_settings.structured_output_method,
)

graph = build_research_graph(
    # 省略四位证据研究员和策略师配置
    thesis_validation_model=validator_model,
    aggressive_portfolio_manager_model=aggressive_model,
    conservative_portfolio_manager_model=conservative_model,
)
```

`build_research_graph` 会拒绝只配置其中一位经理，也会拒绝在没有观点查证节点时直接启用经理。
