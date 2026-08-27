# 可运行第一版：双经理独立正式输出

## 目标

`codex/v1-no-debate` 是当前可运行的第一版分支。它保留完整的证据采集、候选观点生成、逐观点查证
和双基金经理决策，但暂不执行交叉评分、正式协商或委员会共识合成。

这不是把辩论失败伪装成共识，而是明确采用一种不同的输出契约：激进型和保守型基金经理的两份
独立建议共同构成正式输出，用户自行对照两种风险偏好阅读。

## 默认主图

```text
四位证据研究员
        ↓
EvidenceCollectorNode
        ↓
LeadResearchStrategist
        ↓
ThesisValidationAnalyst（逐观点查证）
        ↓
┌───────────────────────────────┐
│ AggressivePortfolioManager    │
│ ConservativePortfolioManager  │  并行
└───────────────────────────────┘
        ↓
IndependentRecommendationsFinalizerNode
        ↓
ReportComposerNode
```

`IndependentRecommendationsFinalizerNode` 不调用大模型，也不修改任何建议内容。它只验证：

- 两位经理的建议都已生成；
- profile 分别为 `AGGRESSIVE` 与 `CONSERVATIVE`；
- `run_id`、`as_of` 和研究目标与当前运行完全一致。

验证通过后，状态写入 `independent_recommendations_finalized=true`。

## 报告契约

成功的双输出报告使用：

- `outcome = DUAL_RECOMMENDATIONS_READY`；
- `recommendations.output_mode = DUAL_INDEPENDENT`；
- `recommendations.aggressive` 和 `recommendations.conservative` 均为正式输出；
- `recommendations.consensus = null`。

Markdown 报告把两节分别命名为“激进型基金经理正式建议”和“保守型基金经理正式建议”，并明确说明
本版本没有启用辩论与共识组装。它不会显示“没有未决分歧”，因为系统实际上没有执行分歧判断。

如果任一经理建议缺失或作用域不匹配，确认节点会失败关闭，报告保持 `INCOMPLETE`，不会把单份建议
冒充完整的双经理正式输出。

## 与开发版的边界

交叉评分、冲突校验、三轮协商和共识建议组装代码仍保留在仓库中，也仍可通过显式向
`build_research_graph()` 注入对应模型来构造辩论图。当前稳定分支的 Runtime 不做这种注入。

完整辩论版由 `codex/develop` 分支持续开发；本分支只接收第一版运行所需的缺陷修复。
