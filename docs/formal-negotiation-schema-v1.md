# 投资决策与正式协商结构体字段总表 v1

## 1. 文档范围和状态

本文记录从“已查证观点进入两位投资组合经理”开始，到当前已经接入主图的共识门与三轮正式协商
循环和最终共识建议组装所使用的全部业务结构体及字段。第一版不设主席、仲裁模型或仲裁路由；轮次耗尽后仍
未决的条目被排除于最终建议之外。数据
Provider、Service、Tool 的传输结构分别由
[`data-pipeline-reference.md`](data-pipeline-reference.md)、
[`data-service-call-reference.md`](data-service-call-reference.md) 和
[`tool-call-reference.md`](tool-call-reference.md) 维护，不在本文重复。

状态标记：

- **已实现**：当前 `src/` 中已经存在，并由 Pydantic/TypedDict 与测试约束；
- **已实现，消费节点待实现**：结构体已经存在，但当前图还没有写入它的节点；
- **待实现**：仅描述后续执行节点，不代表当前可以端到端产出；
- **系统字段**：只能由确定性程序生成，LLM 不得填写；
- **模型字段**：允许由结构化输出模型填写，程序仍须二次校验。

正式协商采用“批次 Record”设计：一位经理在一轮中处理的全部条目放入同一份记录，避免每条建议
各调用一次模型。LLM 只输出业务 Draft；节点再绑定 `run_id`、身份、轮次、版本、来源指纹和时间戳。

## 2. 评分与互斥不变量

`SupportScore` 是九档离散值：

```text
-1.00, -0.75, -0.50, -0.25, 0.00, +0.25, +0.50, +0.75, +1.00
```

对每位经理和每一对互斥条目，必须满足：

```text
score(item_a) + score(item_b) <= 0
```

因此等强度边界 `+0.25/-0.25`、`+0.50/-0.50`、`+0.75/-0.75`、`+1.00/-1.00`
合法，正分搭配 `0` 不合法。两位经理的约束相加后，两个互斥条目的总分之和仍然 `<= 0`；而单条
建议通过要求双方总分 `> 0`，故互斥建议不可能同时通过。

## 3. 公共领域结构

### 3.1 `ResearchTarget`（已实现）

一切观点和建议指向的研究对象。

| 字段 | 类型 | 含义 |
|---|---|---|
| `type` | `TargetType` | `MARKET/SECTOR/STOCK` 等目标类型 |
| `code` | `str` | 稳定目标代码，例如股票代码或市场代码 |
| `name` | `str` | 面向人的目标名称 |

### 3.2 `ProposalEvaluation`（已实现）

一位经理对一条原子建议保留的当前评价。

| 字段 | 类型 | 含义 |
|---|---|---|
| `manager` | `PortfolioManager` | 评分经理 |
| `previous_score` | `SupportScore \| null` | 上一版本分数；首次评分为空 |
| `support_score` | `SupportScore` | 当前支持程度 |
| `hard_veto` | `bool` | 是否硬否决；为真时分数必须为 `-1` |
| `reason` | `str` | 当前评分理由 |
| `modification_suggestion` | `str \| null` | 可接受的修改方向 |
| `score_change_reason` | `str \| null` | 分数变化原因和来源 |

### 3.3 `ArbitrationDecision`（仅历史兼容，v1 不生成）

代码中仍保留该领域类型用于兼容旧序列化对象，但 v1 主图没有仲裁节点，不会创建该结构。

| 字段 | 类型 | 含义 |
|---|---|---|
| `decided_by` | `str` | 仲裁者，默认 `InvestmentCommitteeChair` |
| `decision` | `str` | 仲裁决定正文 |
| `reason` | `str` | 决定理由 |
| `remaining_disagreement` | `str \| null` | 仲裁后仍保留的异议 |

### 3.4 `ProposalItem`（已实现）

可被独立评分、修改、接受、撤回或排除的最小建议单元。

| 字段 | 类型 | 含义 |
|---|---|---|
| `item_id` | `str` | 稳定条目 ID，格式 `item_*` |
| `target` | `ResearchTarget` | 条目直接作用的市场、板块或股票 |
| `decision_dimension` | `DecisionDimension` | 动作、期限、风险控制等决策维度 |
| `conflict_group` | `str` | 规范化决策槽，当前由目标和维度确定 |
| `conflicts_with` | `list[str]` | 与本条目互斥的条目 ID |
| `proposer` | `PortfolioManager` | 原提议方 |
| `revision` | `int` | 条目版本，从 `1` 开始 |
| `proposal` | `str` | 原子建议正文 |
| `supporting_thesis_ids` | `list[str]` | 直接支撑该建议的终态观点 ID |
| `evaluations` | `list[ProposalEvaluation]` | 双方当前评价 |
| `status` | `ProposalStatus` | v1 实际生成 `PROPOSED/NEGOTIATING/AGREED/REJECTED/WITHDRAWN/EXCLUDED`；`ARBITRATED` 仅历史兼容 |
| `arbitration` | `ArbitrationDecision \| null` | v1 始终为 `null` |

### 3.5 `DebateSummary`（已实现并由组装器写入）

委员会最终建议中的协商摘要。

| 字段 | 类型 | 含义 |
|---|---|---|
| `rounds` | `int` | 已执行的正式协商轮数，最大 `3` |
| `status` | `DebateStatus` | `AGREED` 或 `PARTIAL_CONSENSUS` |
| `aggressive_original_recommendation_id` | `str` | 进取经理原始独立建议 ID |
| `conservative_original_recommendation_id` | `str` | 防御经理原始独立建议 ID |
| `excluded_item_ids` | `list[str]` | 耗尽轮次后仍未达成共识、因而未纳入委员会建议的条目 |
| `remaining_disagreements` | `list[str]` | 最终仍需向用户披露的分歧 |

`AGREED` 不允许存在 `excluded_item_ids`。`PARTIAL_CONSENSUS` 必须同时披露非空
`excluded_item_ids` 和 `remaining_disagreements`。分歧摘要按 `conflict_group` 合并，每个冲突组一条，并列出该组
全部被排除的 item ID。

### 3.6 `RecommendationRecord`（已实现）

一套可持久化的进取、防御或委员会投资建议。

| 字段 | 类型 | 含义 |
|---|---|---|
| `recommendation_id` | `str` | 建议 ID，格式 `rec_*` |
| `run_id` | `str` | 所属研究运行 ID |
| `as_of` | `AwareDatetime` | 数据截止时间 |
| `profile` | `RecommendationProfile` | `AGGRESSIVE/CONSERVATIVE/CONSENSUS` |
| `target` | `ResearchTarget` | 总体研究对象 |
| `action` | `RecommendationAction` | 总体动作 |
| `horizon` | `str` | 投资期限 |
| `confidence` | `float` | 可靠性确信度，范围 `[0,1]` |
| `supporting_thesis_ids` | `list[str]` | 全部条目引用观点的精确并集 |
| `summary` | `str` | 建议摘要 |
| `valuation_guidance` | `str \| null` | 可选估值或价格条件 |
| `risk_summary` | `str` | 风险与失效条件摘要 |
| `proposal_items` | `list[ProposalItem]` | 原子建议集合 |
| `debate` | `DebateSummary \| null` | 独立建议为空，委员会建议必填 |
| `generated_by` | `str` | 生成角色 |
| `created_at` | `AwareDatetime` | 记录创建时间 |
| `disclaimer` | `str` | 免责声明 |

委员会 `CONSENSUS` 建议必须由 `ConsensusRecommendationAssemblerNode` 生成，必须带 `debate`，
`proposal_items` 只能包含 `AGREED` 条目，且 v1 中所有条目的 `arbitration` 必须为 `null`。

## 4. 独立建议阶段结构

### 4.1 `PortfolioRecommendationLimits`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `max_input_theses` | `int` | 单经理最多接收的观点数 |
| `max_context_characters` | `int` | 拼接上下文字符上限 |

### 4.2 `DecisionThesisSummary`（已实现）

经理可见的终态观点快照。

| 字段 | 类型 | 含义 |
|---|---|---|
| `thesis_id` | `str` | 观点 ID |
| `target` | `ResearchTarget` | 观点对象 |
| `title` | `str` | 标题 |
| `description` | `str` | 观点正文 |
| `direction` | `ThesisDirection` | 方向 |
| `horizon` | `str` | 时间范围 |
| `validation_status` | `ThesisValidationStatus` | 查证终态 |
| `confidence` | `float` | 查证后置信度 |
| `supporting_evidence_ids` | `tuple[str, ...]` | 支持证据 ID |
| `contradicting_evidence_ids` | `tuple[str, ...]` | 反向证据 ID |
| `reasoning_summary` | `str` | 查证推理摘要 |
| `remaining_questions` | `tuple[str, ...]` | 未解决问题 |
| `catalysts` | `tuple[str, ...]` | 催化剂 |
| `invalidation_conditions` | `tuple[str, ...]` | 失效条件 |

### 4.3 `PortfolioRecommendationInput`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 数据截止时间 |
| `research_target` | `ResearchTarget` | 总体研究对象 |
| `manager` | `PortfolioManager` | 当前经理 |
| `profile` | `RecommendationProfile` | 必须与经理身份匹配 |
| `theses` | `tuple[DecisionThesisSummary, ...]` | 全部可见终态观点 |
| `eligible_supporting_thesis_ids` | `tuple[str, ...]` | 仅 `SUPPORTED/MIXED` 的可直接引用观点 ID |
| `policy_notes` | `tuple[str, ...]` | 固定决策政策 |

### 4.4 `RecommendationProposalDraft`（已实现，模型输出）

| 字段 | 类型 | 含义 |
|---|---|---|
| `target` | `ResearchTarget` | 原子建议对象 |
| `decision_dimension` | `DecisionDimension` | 决策维度 |
| `proposal` | `str` | 动作、条件或约束正文 |
| `supporting_thesis_ids` | `tuple[str, ...]` | 直接支持观点 ID |
| `insistence_score` | `0.25/0.5/0.75/1.0` | 提议方初始正向坚持分 |
| `score_reason` | `str` | 坚持分理由 |

### 4.5 `PortfolioRecommendationDraft`（已实现，模型输出）

| 字段 | 类型 | 含义 |
|---|---|---|
| `action` | `RecommendationAction` | 整体动作 |
| `horizon` | `str` | 整体期限 |
| `confidence` | `float` | 整套判断确信度 |
| `summary` | `str` | 整套方案摘要 |
| `valuation_guidance` | `str \| null` | 可选估值条件 |
| `risk_summary` | `str` | 风险摘要 |
| `proposal_items` | `tuple[RecommendationProposalDraft, ...]` | 原子建议 Draft |

### 4.6 `PortfolioRecommendationRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `manager` | `PortfolioManager` | 当前经理 |
| `input_thesis_count` | `int` | 输入观点数 |
| `eligible_thesis_count` | `int` | 可引用观点数 |
| `context_character_count` | `int` | 实际上下文字符数 |
| `model_called` | `bool` | 是否调用模型 |
| `proposal_count` | `int` | 输出原子建议数 |
| `stop_reason` | 枚举字符串 | 停止原因 |

## 5. 规范化、交叉评分与确定性纠错结构

### 5.1 `PortfolioCrossReviewLimits`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `max_input_theses` | `int` | 互评最大观点数 |
| `max_context_characters` | `int` | 上下文字符上限 |
| `max_attempts` | `int` | 首评加纠错的单经理总调用上限，当前最大 `3` |

### 5.2 `NormalizedProposalPool`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `research_target` | `ResearchTarget` | 总体目标 |
| `aggressive_recommendation_id` | `str` | 进取原始建议 ID |
| `conservative_recommendation_id` | `str` | 防御原始建议 ID |
| `proposal_items` | `tuple[ProposalItem, ...]` | 冻结正文、已绑定冲突关系、仅含提议方评分的条目池 |

### 5.3 `ProposalNormalizationRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `aggressive_recommendation_id` | `str \| null` | 进取来源 ID |
| `conservative_recommendation_id` | `str \| null` | 防御来源 ID |
| `input_proposal_count` | `int` | 输入条目数 |
| `output_proposal_count` | `int` | 输出条目数 |
| `conflict_pair_count` | `int` | 互斥对数 |
| `stop_reason` | 枚举字符串 | 停止原因 |

### 5.4 `CrossReviewProposalContext`（已实现）

模型互评时看到的冻结单条建议。

| 字段 | 类型 | 含义 |
|---|---|---|
| `item_id` | `str` | 条目 ID |
| `target` | `ResearchTarget` | 目标 |
| `decision_dimension` | `DecisionDimension` | 决策维度 |
| `conflict_group` | `str` | 决策槽 |
| `conflicts_with` | `tuple[str, ...]` | 互斥条目 ID |
| `proposer` | `PortfolioManager` | 提议方 |
| `proposal` | `str` | 冻结正文 |
| `supporting_thesis_ids` | `tuple[str, ...]` | 支持观点 ID |
| `proposer_insistence_score` | `0.25/0.5/0.75/1.0` | 提议方初始分 |
| `proposer_score_reason` | `str` | 初始分理由 |

### 5.5 `PortfolioCrossReviewInput`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `research_target` | `ResearchTarget` | 总体目标 |
| `reviewer` | `PortfolioManager` | 当前评审者 |
| `own_recommendation_id` | `str` | 己方建议 ID |
| `counterpart_recommendation_id` | `str` | 对方建议 ID |
| `attempt` | `int` | 首评/纠错尝试序号 |
| `own_proposals` | `tuple[CrossReviewProposalContext, ...]` | 己方目录，只用于比较 |
| `counterpart_proposals` | `tuple[CrossReviewProposalContext, ...]` | 必须逐条评分的对方目录 |
| `theses` | `tuple[DecisionThesisSummary, ...]` | 双方共同可见观点 |
| `eligible_supporting_thesis_ids` | `tuple[str, ...]` | 可直接引用观点 ID |
| `previous_evaluations` | `tuple[CrossReviewEvaluationDraft, ...]` | 纠错时的上一轮全量评分 |
| `validation_feedback` | `tuple[ConflictScoreViolation, ...]` | 只属于当前经理的违规反馈 |
| `policy_notes` | `tuple[str, ...]` | 固定规则 |

### 5.6 `CrossReviewEvaluationDraft`（已实现，模型输出）

| 字段 | 类型 | 含义 |
|---|---|---|
| `item_id` | `str` | 被评价的对方条目 |
| `support_score` | `SupportScore` | 当前分数 |
| `hard_veto` | `bool` | 是否硬否决 |
| `reason` | `str` | 评分理由 |
| `modification_suggestion` | `str \| null` | 修改建议 |

### 5.7 `PortfolioCrossReviewDraft`（已实现，模型输出）

| 字段 | 类型 | 含义 |
|---|---|---|
| `evaluations` | `tuple[CrossReviewEvaluationDraft, ...]` | 对全部对方条目的完整评分 |

### 5.8 `PortfolioCrossReviewRecord`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `reviewer` | `PortfolioManager` | 评分经理 |
| `attempt` | `int` | 本经理评分尝试号 |
| `own_recommendation_id` | `str` | 己方建议 ID |
| `counterpart_recommendation_id` | `str` | 对方建议 ID |
| `evaluations` | `tuple[CrossReviewEvaluationDraft, ...]` | 当前全量评分 |
| `previous_evaluations` | `tuple[CrossReviewEvaluationDraft, ...]` | 上一轮全量评分 |
| `correction_feedback` | `tuple[ConflictScoreViolation, ...]` | 本次纠错依据 |
| `created_at` | `AwareDatetime` | 创建时间 |

### 5.9 `PortfolioCrossReviewRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `reviewer` | `PortfolioManager` | 当前经理 |
| `attempt` | `int` | 尝试号 |
| `own_proposal_count` | `int` | 己方条目数 |
| `counterpart_proposal_count` | `int` | 对方条目数 |
| `input_thesis_count` | `int` | 输入观点数 |
| `context_character_count` | `int` | 上下文字符数 |
| `model_called` | `bool` | 是否调用模型 |
| `evaluation_count` | `int` | 输出评分数 |
| `stop_reason` | 枚举字符串 | 停止原因 |

### 5.10 `CrossReviewedProposalPool`（已实现）

字段与 `NormalizedProposalPool` 相同；区别是每个 `ProposalItem.evaluations` 必须恰好包含双方各一份
评分，并且能够剥离对方评分后逐字段还原规范化来源池。

### 5.11 `CrossReviewApplicationRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `input_proposal_count` | `int` | 输入条目数 |
| `output_proposal_count` | `int` | 输出条目数 |
| `applied_evaluation_count` | `int` | 写入的对方评分数 |
| `aggressive_review_attempt` | `int` | 进取评分尝试号 |
| `conservative_review_attempt` | `int` | 防御评分尝试号 |
| `stop_reason` | 枚举字符串 | 停止原因 |

### 5.12 `ConflictScoreViolation`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `rule_code` | `CONFLICT_GROUP_SUM_POSITIVE` | 唯一规则代码，表示互斥对评分和大于 `0` |
| `manager` | `PortfolioManager` | 违规经理 |
| `own_item_id` | `str` | 经理自己的正分条目 |
| `counterpart_item_id` | `str` | 对方互斥条目 |
| `own_support_score` | `SupportScore` | 己方初始分 |
| `counterpart_support_score` | `SupportScore` | 该经理给对方条目的分数 |
| `message` | `str` | 面向纠错模型和审计日志的说明 |

### 5.13 `ConflictScoreValidationReport`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `source_fingerprint` | `str` | 评分池、attempt 和限制的 SHA-256 指纹 |
| `max_attempts` | `int` | 单经理最大调用次数 |
| `aggressive_review_attempt` | `int` | 进取当前尝试号 |
| `conservative_review_attempt` | `int` | 防御当前尝试号 |
| `valid` | `bool` | 是否全部合法 |
| `invalid_managers` | `tuple[PortfolioManager, ...]` | 需要纠错的经理 |
| `violations` | `tuple[ConflictScoreViolation, ...]` | 具体违规 |
| `stop_reason` | 枚举字符串 | `valid/retry_required/retry_exhausted/...` |

### 5.14 `CrossReviewCorrectionRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `source_validation_fingerprint` | `str` | 触发纠错的校验报告指纹 |
| `requested_managers` | `tuple[PortfolioManager, ...]` | 被要求纠错的经理 |
| `called_managers` | `tuple[PortfolioManager, ...]` | 已实际调用模型的经理 |
| `staged_managers` | `tuple[PortfolioManager, ...]` | 单方成功、等待原子提交的经理 |
| `completed_managers` | `tuple[PortfolioManager, ...]` | 整批成功后已提交的经理 |
| `aggressive_attempt` | `int` | 进取纠错后的尝试号 |
| `conservative_attempt` | `int` | 防御纠错后的尝试号 |
| `stop_reason` | 枚举字符串 | 停止原因 |

## 6. 共识门与当前提案池结构

### 6.1 `NegotiationLimits`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `max_rounds` | `int` | 正式协商最大轮数，默认 `3`，且只能配置为 `1..3` |
| `max_input_theses` | `int` | 单次协商模型最多接收的观点数，默认 `32` |
| `max_context_characters` | `int` | 单次结构化请求字符硬上限，默认 `120000` |

### 6.2 `NegotiationProposalPool`（已实现）

首轮由合法的 `CrossReviewedProposalPool` 深拷贝而来，之后只在这份池中推进版本与状态，两份原始
`RecommendationRecord` 和首次交叉评分池均不被覆盖。

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 数据截止时间 |
| `research_target` | `ResearchTarget` | 本轮总体研究对象 |
| `aggressive_recommendation_id` | `str` | 进取原始建议 ID |
| `conservative_recommendation_id` | `str` | 防御原始建议 ID |
| `proposal_items` | `tuple[ProposalItem, ...]` | 当前版本、状态与双方评分的完整条目池 |

池约束包括：每个条目恰好按“提议方、对方”顺序保存两份评价；存活条目的提议方评分必须为正；
`conflicts_with` 必须双向；同一冲突组不能有两个 `AGREED` 条目；v1 任何阶段都不得出现
`arbitration` 内容。`EXCLUDED` 是轮次耗尽后未达成共识的终态，不参与最终建议组装。

### 6.3 `ConsensusGateItemDecision`（已实现）

程序对单条建议作出的确定性门控结论。

| 字段 | 类型 | 含义 |
|---|---|---|
| `item_id` | `str` | 建议条目 ID |
| `item_revision` | `int` | 被判断的精确版本 |
| `aggressive_score` | `SupportScore` | 进取经理当前分数 |
| `conservative_score` | `SupportScore` | 防御经理当前分数 |
| `combined_score` | `float` | 双方分数和 |
| `minimum_score` | `SupportScore` | 双方较低分 |
| `hard_veto` | `bool` | 任一方是否硬否决 |
| `outcome` | `ConsensusItemOutcome` | `AGREED/NEGOTIATING/REJECTED/WITHDRAWN/EXCLUDED` |
| `reason_codes` | `tuple[str, ...]` | 通过、冻结、撤回或未通过原因 |

### 6.4 `ConsensusGateReport`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 当前正式协商轮次；首评门控可为 `0` |
| `max_rounds` | `int` | 本次运行采用的轮次上限 |
| `source_fingerprint` | `str` | 当前提案版本和双方评分的内容指纹 |
| `item_decisions` | `tuple[ConsensusGateItemDecision, ...]` | 全部条目门控结果 |
| `agreed_item_ids` | `tuple[str, ...]` | 本轮已通过条目 |
| `negotiating_item_ids` | `tuple[str, ...]` | 需继续协商条目 |
| `rejected_item_ids` | `tuple[str, ...]` | 因冲突获胜者等原因被冻结拒绝的条目 |
| `withdrawn_item_ids` | `tuple[str, ...]` | 原提议方已撤回条目 |
| `excluded_item_ids` | `tuple[str, ...]` | 已耗尽轮次、未达成共识且不进入最终建议的条目 |
| `missing_required_dimensions` | `tuple[DecisionDimension, ...]` | 总体目标尚缺的 `ACTION/HORIZON/RISK_CONTROL` 维度 |
| `all_required_dimensions_resolved` | `bool` | 委员会建议必需维度是否都有结果 |
| `route` | `ConsensusRoute` | `ASSEMBLE/NEGOTIATE` |

正常门槛为 `combined_score > 0`、`minimum_score >= -0.25`、无硬否决。Gate 在判断前还会再次
验证互斥评分和 `<= 0`；一个冲突组若已有通过条目，该条目冻结为 `AGREED`，其他存活冲突条目确定性
转为 `REJECTED`。轮次未耗尽时的未决条目保持 `NEGOTIATING`；达到 `max_rounds` 后转为
`EXCLUDED` 并路由 `ASSEMBLE`。`ASSEMBLE` 已连接最终共识建议组装器。

## 7. 正式协商 Draft、Input 与 Record

### 7.1 理由交换 Draft 与 Input（已实现）

#### `NegotiationArgumentDraft`

| 字段 | 类型 | 含义 |
|---|---|---|
| `argument_type` | `NegotiationArgumentType` | `SUPPORT_REASON/OBJECTION/RISK_WARNING/FACT_CORRECTION/MODIFICATION_REASON` |
| `content` | `str` | 理由正文 |
| `supporting_thesis_ids` | `tuple[str, ...]` | 输入目录内的观点 ID，可为空 |

#### `ReasonExchangeItemDraft` 与 `ReasonExchangeDraft`

| 结构 | 字段 | 类型 | 含义 |
|---|---|---|---|
| `ReasonExchangeItemDraft` | `counterpart_item_id` | `str` | 被回应的对方条目 |
|  | `counterpart_revision` | `int` | 被回应版本 |
|  | `related_own_item_ids` | `tuple[str, ...]` | 同冲突组的己方条目 |
|  | `stance` | `NegotiationStance` | `SUPPORT/CONDITIONAL_ACCEPT/OPPOSE` |
|  | `arguments` | `tuple[NegotiationArgumentDraft, ...]` | 至少一条理由 |
|  | `modification_suggestion` | `str \| null` | 可接受修改方向 |
| `ReasonExchangeDraft` | `responses` | `tuple[ReasonExchangeItemDraft, ...]` | 完整且按输入顺序覆盖对方待协商条目 |

#### `ReasonExchangeInput`

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 当前正式轮次，范围 `1..3` |
| `reviewer` | `PortfolioManager` | 回应方 |
| `own_proposals` | `tuple[ProposalItem, ...]` | 与对方条目同冲突组的己方存活建议 |
| `counterpart_proposals` | `tuple[ProposalItem, ...]` | 本轮必须逐条回应的对方 `NEGOTIATING` 建议 |
| `theses` | `tuple[DecisionThesisSummary, ...]` | 可引用观点快照 |
| `prior_exchanges` | `tuple[ReasonExchangeRecord, ...]` | 当前回应方此前轮次记录 |
| `policy_notes` | `tuple[str, ...]` | 确定性规则提示 |

### 7.2 `NegotiationArgument`（已实现）

`ReasonExchangeRecord` 内一条可被追踪的理由。

| 字段 | 类型 | 含义 |
|---|---|---|
| `argument_id` | `str` | 系统生成的稳定理由 ID |
| `argument_type` | 枚举 | `SUPPORT_REASON/OBJECTION/RISK_WARNING/FACT_CORRECTION/MODIFICATION_REASON` |
| `content` | `str` | 理由正文 |
| `supporting_thesis_ids` | `tuple[str, ...]` | 支撑该理由的终态观点 ID；没有直接依据时为空 |

### 7.3 `ReasonExchangeItem`（已实现）

一位经理针对对方一条未通过建议的回应。

| 字段 | 类型 | 含义 |
|---|---|---|
| `counterpart_item_id` | `str` | 被回应的对方条目 |
| `counterpart_revision` | `int` | 被回应的精确版本 |
| `conflict_group` | `str` | 所属决策槽 |
| `related_own_item_ids` | `tuple[str, ...]` | 与回应相关的己方条目 |
| `stance` | 枚举 | `SUPPORT/CONDITIONAL_ACCEPT/OPPOSE` |
| `arguments` | `tuple[NegotiationArgument, ...]` | 支持、反对、风险或事实纠正理由 |
| `modification_suggestion` | `str \| null` | 可接受的具体修改方向 |

### 7.4 `ReasonExchangeRecord`（已实现）

一位经理在一轮正式协商中对全部待协商对方条目的批次回应。

| 字段 | 类型 | 生成方 | 含义 |
|---|---|---|---|
| `exchange_id` | `str` | 系统 | 交换记录 ID |
| `run_id` | `str` | 系统 | 运行 ID |
| `as_of` | `AwareDatetime` | 系统 | 数据截止时间 |
| `debate_round` | `int` | 系统 | 正式协商轮次，范围 `1..3` |
| `reviewer` | `PortfolioManager` | 系统 | 作出回应的经理 |
| `source_fingerprint` | `str` | 系统 | 输入条目、版本、评分及观点的内容指纹 |
| `responses` | `tuple[ReasonExchangeItem, ...]` | 模型 | 对全部待协商对方条目的回应 |
| `created_at` | `AwareDatetime` | 系统 | 创建时间 |

### 7.5 原提议方修订 Draft 与 Input（已实现）

#### `ProposalRevisionDecisionDraft` 与 `ProposalRevisionDraft`

| 结构 | 字段 | 类型 | 含义 |
|---|---|---|---|
| `ProposalRevisionDecisionDraft` | `item_id` | `str` | 原提议方自己的待协商条目 |
|  | `decision` | `ProposalRevisionAction` | `KEEP/MODIFY/WITHDRAW` |
|  | `responding_to_argument_ids` | `tuple[str, ...]` | 仅能引用针对该条目的理由 ID |
|  | `revised_proposal` | `str \| null` | `MODIFY` 必填；其他动作禁止携带 |
|  | `revised_supporting_thesis_ids` | `tuple[str, ...] \| null` | `MODIFY` 必填且非空 |
|  | `revision_reason` | `str` | 保留、修改或撤回原因 |
| `ProposalRevisionDraft` | `decisions` | `tuple[ProposalRevisionDecisionDraft, ...]` | 完整且按输入顺序覆盖己方条目 |

#### `ProposalRevisionInput`

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 当前轮次 |
| `proposer` | `PortfolioManager` | 原提议方 |
| `own_proposals` | `tuple[ProposalItem, ...]` | 本人全部 `NEGOTIATING` 条目 |
| `incoming_exchange` | `ReasonExchangeRecord` | 对方本轮针对这些条目的回应 |
| `theses` | `tuple[DecisionThesisSummary, ...]` | 修订可引用的观点快照 |
| `prior_revisions` | `tuple[ProposalRevisionRecord, ...]` | 本人此前轮次修订历史 |
| `policy_notes` | `tuple[str, ...]` | 确定性规则提示 |

### 7.6 `ProposalRevisionSnapshot`（已实现）

修订前后都使用的不可变条目业务快照，不复制评价历史。

| 字段 | 类型 | 含义 |
|---|---|---|
| `revision` | `int` | 条目版本 |
| `proposal` | `str` | 建议正文 |
| `supporting_thesis_ids` | `tuple[str, ...]` | 支持观点 ID |
| `status` | `ProposalStatus` | 条目状态 |

### 7.7 `ProposalRevisionDecision`（已实现）

原提议方对自己一条建议的修订决定。

| 字段 | 类型 | 生成方 | 含义 |
|---|---|---|---|
| `item_id` | `str` | 系统绑定 | 被修订条目 |
| `conflict_group` | `str` | 系统绑定 | 决策槽 |
| `decision` | 枚举 | 模型 | `KEEP/MODIFY/WITHDRAW` |
| `responding_to_argument_ids` | `tuple[str, ...]` | 模型 | 本决定回应的理由 ID |
| `before` | `ProposalRevisionSnapshot` | 系统 | 修订前快照 |
| `after` | `ProposalRevisionSnapshot` | 系统装配 | 修订后快照；撤回时保存撤回状态 |
| `revision_reason` | `str` | 模型 | 保留、修改或撤回原因 |
| `changed_fields` | `tuple[str, ...]` | 系统 | 实际变化字段，只允许正文、引用或状态 |
| `material_change` | `bool` | 系统 | 是否允许进入重新评分 |

`material_change=true` 仅允许出现在正文改变、支持观点集合改变或撤回时。仅增加解释文字不构成
实质变化。

### 7.8 `ProposalRevisionRecord`（已实现）

一位原提议方在一轮中对自己全部待协商条目的批次决定。

| 字段 | 类型 | 生成方 | 含义 |
|---|---|---|---|
| `revision_record_id` | `str` | 系统 | 修订记录 ID |
| `run_id` | `str` | 系统 | 运行 ID |
| `as_of` | `AwareDatetime` | 系统 | 截止时间 |
| `debate_round` | `int` | 系统 | 正式协商轮次 |
| `proposer` | `PortfolioManager` | 系统 | 原提议方 |
| `source_exchange_ids` | `tuple[str, ...]` | 系统 | 触发本次决定的理由交换记录 |
| `source_fingerprint` | `str` | 系统 | 冻结输入内容指纹 |
| `decisions` | `tuple[ProposalRevisionDecision, ...]` | 模型加系统装配 | 全量修订决定 |
| `created_at` | `AwareDatetime` | 系统 | 创建时间 |

### 7.9 重评 Draft 与 Input（已实现）

#### `DebateScoreEntryDraft` 与 `DebateScoreDraft`

| 结构 | 字段 | 类型 | 含义 |
|---|---|---|---|
| `DebateScoreEntryDraft` | `item_id` | `str` | 被重评条目 |
|  | `item_revision` | `int` | 被重评精确版本 |
|  | `support_score` | `SupportScore` | 新分数 |
|  | `hard_veto` | `bool` | 硬否决时分数必须为 `-1` |
|  | `reason` | `str` | 当前评分理由 |
|  | `modification_suggestion` | `str \| null` | 尚需修改方向 |
|  | `score_change_reason` | `str` | 分数变化或保持原因 |
| `DebateScoreDraft` | `evaluations` | `tuple[DebateScoreEntryDraft, ...]` | 完整且按输入顺序覆盖重评闭包 |

#### `DebateScoreInput`

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 当前轮次 |
| `manager` | `PortfolioManager` | 当前重评经理 |
| `items_to_score` | `tuple[ProposalItem, ...]` | 受实质变化影响的冲突组中仍存活、未通过条目 |
| `active_proposal_pool` | `tuple[ProposalItem, ...]` | 当前全部存活条目，供互斥关系校验 |
| `source_revision_records` | `tuple[ProposalRevisionRecord, ...]` | 本轮修订来源 |
| `theses` | `tuple[DecisionThesisSummary, ...]` | 可见观点快照 |
| `policy_notes` | `tuple[str, ...]` | 确定性评分规则提示 |

### 7.10 `DebateScoreEntry`（已实现）

实质修订后，一位经理对一条新版本建议的重新评分。

| 字段 | 类型 | 生成方 | 含义 |
|---|---|---|---|
| `item_id` | `str` | 系统绑定 | 被评分条目 |
| `item_revision` | `int` | 系统绑定 | 被评分版本 |
| `previous_score` | `SupportScore` | 系统 | 上一版本分数 |
| `support_score` | `SupportScore` | 模型 | 新分数 |
| `hard_veto` | `bool` | 模型 | 当前是否硬否决 |
| `reason` | `str` | 模型 | 新评分理由 |
| `modification_suggestion` | `str \| null` | 模型 | 仍需修改的方向 |
| `score_change_reason` | `str` | 模型加系统校验 | 分数变化原因；未变化也要说明 |
| `trigger_revision_record_id` | `str` | 系统 | 触发重评的修订记录 |

### 7.11 `DebateScoreRecord`（已实现）

一位经理在一轮中对所有发生实质修订的条目所作批次重评。

| 字段 | 类型 | 生成方 | 含义 |
|---|---|---|---|
| `score_record_id` | `str` | 系统 | 重评记录 ID |
| `run_id` | `str` | 系统 | 运行 ID |
| `as_of` | `AwareDatetime` | 系统 | 截止时间 |
| `debate_round` | `int` | 系统 | 正式协商轮次 |
| `manager` | `PortfolioManager` | 系统 | 评分经理 |
| `source_revision_record_ids` | `tuple[str, ...]` | 系统 | 本次重评依据的修订记录 |
| `source_fingerprint` | `str` | 系统 | 修订版本、旧分数和观点目录的指纹 |
| `evaluations` | `tuple[DebateScoreEntry, ...]` | 模型加系统装配 | 新评分集合 |
| `created_at` | `AwareDatetime` | 系统 | 创建时间 |

只有至少一条 `ProposalRevisionDecision.material_change=true` 才进入重评。重评范围不是“只重评改过的
条目”，而是这些条目所触及 `conflict_group` 的全部仍存活、未 `AGREED` 条目闭包；闭包之外沿用旧分。
重评后必须再次执行互斥评分和 `<= 0` 校验。

## 8. 正式协商运行摘要与校验结构

### 8.1 `NegotiationModelRunSummary`（已实现）

三阶段共用的“单经理一次模型调用”摘要。

| 字段 | 类型 | 含义 |
|---|---|---|
| `stage` | `REASON_EXCHANGE/PROPOSAL_REVISION/DEBATE_SCORE` | 所属阶段 |
| `manager` | `PortfolioManager` | 被调用经理 |
| `debate_round` | `int` | 当前轮次 |
| `input_item_count` | `int` | 输入条目数 |
| `output_item_count` | `int` | 模型输出条目数 |
| `context_character_count` | `int` | 结构化请求字符数 |
| `model_called` | `bool` | 是否实际调用模型 |
| `stop_reason` | 枚举字符串 | `complete/no_work/missing_input/context_limit_exceeded/invalid_state/model_error/rejected_output` |

### 8.2 `NegotiationStageRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `stage` | `NegotiationStage` | 三阶段之一 |
| `debate_round` | `int` | 当前轮次 |
| `source_fingerprint` | `str` | 整批输入指纹 |
| `requested_managers` | `tuple[PortfolioManager, ...]` | 本批要求处理的经理 |
| `called_managers` | `tuple[PortfolioManager, ...]` | 已实际调用的经理 |
| `staged_managers` | `tuple[PortfolioManager, ...]` | 单方结果已通过校验、等待整批提交的经理 |
| `completed_managers` | `tuple[PortfolioManager, ...]` | 整批成功后真正提交的经理 |
| `stop_reason` | 枚举字符串 | `complete/no_work/invalid_state/stage_failed` |

集合必须满足 `completed ⊆ staged ⊆ called ⊆ requested`。任一经理失败时整阶段
`completed_managers` 为空，防止提交半份协商状态。

### 8.3 `ProposalRevisionApplicationSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `debate_round` | `int` | 当前轮次 |
| `source_fingerprint` | `str` | 修订阶段输入指纹 |
| `material_change_item_ids` | `tuple[str, ...]` | 本轮真正改变的条目 |
| `withdrawn_item_ids` | `tuple[str, ...]` | 其中被撤回的条目 |
| `touched_conflict_groups` | `tuple[str, ...]` | 受影响冲突组 |
| `rescore_item_ids` | `tuple[str, ...]` | 冲突组闭包内需重评条目 |
| `stop_reason` | `complete/no_material_change/invalid_state` | 应用结果 |

当 `stop_reason=no_material_change` 时四个目录必须均为空；系统跳过重评，但该正式协商轮次已经由
`BeginNegotiationRoundNode` 增加，因此仍会生成一份“无实质变化”的轮摘要并消耗一轮预算。

### 8.4 `NegotiationScoreViolation` 与 `NegotiationScoreValidationReport`（已实现）

| `NegotiationScoreViolation` 字段 | 类型 | 含义 |
|---|---|---|
| `manager` | `PortfolioManager` | 违规经理 |
| `left_item_id` / `right_item_id` | `str` | 一对仍存活的互斥条目 |
| `left_score` / `right_score` | `SupportScore` | 该经理对两条建议的分数 |
| `message` | `str` | `left_score + right_score > 0` 的违规说明 |

| `NegotiationScoreValidationReport` 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 当前轮次 |
| `source_fingerprint` | `str` | 当前提案池和轮次指纹 |
| `valid` | `bool` | 所有存活互斥对是否满足规则 |
| `violations` | `tuple[NegotiationScoreViolation, ...]` | 具体违规 |
| `stop_reason` | 枚举字符串 | `valid/missing_pool/invalid_state/invalid_scores` |

正式重评后再次检查“同一经理对每对互斥建议的评分和 `<= 0`”。与首次交叉评分不同，当前正式协商
重评若违法会失败关闭，不再进入局部纠错循环。

### 8.5 `NegotiationRoundSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 运行 ID |
| `as_of` | `AwareDatetime` | 截止时间 |
| `debate_round` | `int` | 本轮编号 |
| `source_gate_fingerprint` | `str` | 触发本轮的上一份共识门指纹 |
| `exchanged_managers` | `tuple[PortfolioManager, ...]` | 完成理由交换的经理 |
| `revised_managers` | `tuple[PortfolioManager, ...]` | 完成己方修订的经理 |
| `scored_managers` | `tuple[PortfolioManager, ...]` | 完成重评的经理；无变化轮为空 |
| `material_change_count` | `int` | 实质变化条目数 |
| `stop_reason` | 枚举字符串 | 正常产出为 `complete` 或 `no_material_change`；模型还约束失败枚举 |

## 9. 最终共识建议组装结构

### 9.1 `ConsensusRecommendationSynthesisInput`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` / `as_of` | `str` / `AwareDatetime` | 运行 scope |
| `research_target` | `ResearchTarget` | 总体研究目标 |
| `debate_round` | `int` | 正式协商轮数，范围 `0..3` |
| `source_fingerprint` | `str` | Gate、建议池、原始建议和观点的 SHA-256 指纹 |
| `accepted_items` | `tuple[ProposalItem, ...]` | 仅 `AGREED` 条目，`1..32` 条 |
| `supporting_theses` | `tuple[DecisionThesisSummary, ...]` | 精确覆盖纳入条目引用的 `SUPPORTED/MIXED` 观点 |
| `policy_notes` | `tuple[str, ...]` | 不得增删改或补回条目的系统规则 |

### 9.2 `ConsensusRecommendationSynthesisDraft`（已实现，模型输出）

| 字段 | 类型 | 含义 |
|---|---|---|
| `action` | `RecommendationAction` | 仅由总体目标的唯一 `AGREED ACTION` 条目归纳 |
| `horizon` | `str` | 仅由总体目标的唯一 `AGREED HORIZON` 条目压缩 |
| `summary` | `str` | 忠实覆盖全部纳入条目 |
| `valuation_guidance` | `str \| null` | 只能来自 `AGREED VALUATION` 条目 |
| `risk_summary` | `str` | 仅由总体目标的 `AGREED RISK_CONTROL` 条目压缩 |
| `action_source_item_id` / `horizon_source_item_id` | `str` | 顶层动作和期限的精确来源 |
| `risk_source_item_ids` | `tuple[str, ...]` | 必须精确等于总体目标全部 `AGREED RISK_CONTROL` ID |
| `summary_source_item_ids` | `tuple[str, ...]` | 必须精确等于全部纳入条目 ID |
| `valuation_source_item_ids` | `tuple[str, ...]` | 必须精确等于全部 `AGREED VALUATION` ID |

模型只负责顶层文字压缩，不输出条目集合、信心度、ID、时间或辩论状态。

### 9.3 `ConsensusAssemblyRunSummary`（已实现）

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` / `as_of` | `str` / `AwareDatetime` | 运行 scope |
| `source_fingerprint` | `str \| null` | 组装输入指纹 |
| `debate_round` | `int` | 已执行的正式协商轮数 |
| `agreed_item_ids` | `tuple[str, ...]` | 最终纳入候选条目 |
| `excluded_item_ids` | `tuple[str, ...]` | 轮次耗尽后被排除的未决条目 |
| `rejected_item_ids` / `withdrawn_item_ids` | `tuple[str, ...]` | 其他不纳入终态目录 |
| `missing_required_dimensions` | `tuple[DecisionDimension, ...]` | 缺失的 `ACTION/HORIZON/RISK_CONTROL` |
| `input_thesis_count` | `int` | 合成输入观点数 |
| `context_character_count` | `int` | 序列化模型输入字符数 |
| `model_called` | `bool` | 是否真正调用合成模型 |
| `recommendation_id` | `str \| null` | 仅 `complete` 时存在 |
| `stop_reason` | 枚举字符串 | `complete/no_actionable_consensus/missing_input/stale_input/invalid_state/context_limit_exceeded/model_error/rejected_output` |

没有纳入条目，或缺少必需维度时，节点不调用模型，以 `no_actionable_consensus` 正常结束且
`consensus_recommendation = null`。成功组装时，`confidence` 由程序取全部纳入观点信心度的最小值。
详细 Prompt、适配器、指纹、幂等和失败边界见
[`consensus-recommendation-assembler-v1.md`](consensus-recommendation-assembler-v1.md)。

## 10. `ResearchGraphState` 中的投资决策字段（均已实现）

`ResearchGraphState` 是 `TypedDict(total=False)`；节点只返回自己更新的字段。列表审计记录均配置
reducer：相同稳定键只允许幂等重放，内容变化会抛错，不能静默覆盖历史。

| 字段 | 类型 | 含义 |
|---|---|---|
| `aggressive_recommendation` | `RecommendationRecord \| null` | 进取独立建议 |
| `conservative_recommendation` | `RecommendationRecord \| null` | 防御独立建议 |
| `consensus_recommendation` | `RecommendationRecord \| null` | 委员会最终建议；无可执行共识时合法为空 |
| `consensus_assembly_run_summary` | `ConsensusAssemblyRunSummary \| null` | 最终组装或无可执行共识的审计摘要 |
| `aggressive_recommendation_run_summary` | `PortfolioRecommendationRunSummary \| null` | 进取建议运行摘要 |
| `conservative_recommendation_run_summary` | `PortfolioRecommendationRunSummary \| null` | 防御建议运行摘要 |
| `normalized_proposal_pool` | `NormalizedProposalPool \| null` | 规范化提案池 |
| `proposal_normalization_run_summary` | `ProposalNormalizationRunSummary \| null` | 规范化摘要 |
| `aggressive_cross_review` | `PortfolioCrossReviewRecord \| null` | 进取互评记录 |
| `conservative_cross_review` | `PortfolioCrossReviewRecord \| null` | 防御互评记录 |
| `aggressive_cross_review_run_summary` | `PortfolioCrossReviewRunSummary \| null` | 进取互评摘要 |
| `conservative_cross_review_run_summary` | `PortfolioCrossReviewRunSummary \| null` | 防御互评摘要 |
| `cross_reviewed_proposal_pool` | `CrossReviewedProposalPool \| null` | 双方评分已写入的提案池 |
| `cross_review_application_run_summary` | `CrossReviewApplicationRunSummary \| null` | 评分写入摘要 |
| `conflict_score_validation_report` | `ConflictScoreValidationReport \| null` | 互斥评分校验报告 |
| `cross_review_correction_run_summary` | `CrossReviewCorrectionRunSummary \| null` | 首评纠错摘要 |
| `negotiation_proposal_pool` | `NegotiationProposalPool \| null` | 正式协商当前可变版本 |
| `consensus_gate_report` | `ConsensusGateReport \| null` | 当前轮共识门报告 |
| `consensus_gate_reports` | `list[ConsensusGateReport]` | 按 `debate_round` 追加的不可变历史 |
| `reason_exchange_records` | `list[ReasonExchangeRecord]` | 按 `exchange_id` 追加的不可变历史 |
| `proposal_revision_records` | `list[ProposalRevisionRecord]` | 按 `revision_record_id` 追加的不可变历史 |
| `debate_score_records` | `list[DebateScoreRecord]` | 按 `score_record_id` 追加的不可变历史 |
| `negotiation_model_run_summaries` | `list[NegotiationModelRunSummary]` | 按“轮次、阶段、经理”追加 |
| `negotiation_stage_run_summaries` | `list[NegotiationStageRunSummary]` | 按“轮次、阶段”追加 |
| `proposal_revision_application_summary` | `ProposalRevisionApplicationSummary \| null` | 当前轮修订影响目录 |
| `negotiation_score_validation_report` | `NegotiationScoreValidationReport \| null` | 当前轮正式重评校验 |
| `negotiation_round_summaries` | `list[NegotiationRoundSummary]` | 按轮次追加的不可变历史 |
| `debate_round` | `int` | 已开始的正式协商轮数；初始化为 `0` |

`InitializeRunNode` 会拒绝带有非空协商历史或当前协商对象的脏 seed，并把列表初始化为空、当前对象
初始化为 `null`、`debate_round` 初始化为 `0`。

## 11. 已实现生命周期与当前出口

1. 首评合法后，`ConsensusGateNode` 在 `debate_round=0` 初始化正式协商池并执行首次门控；
2. Gate 为 `NEGOTIATE` 时，`BeginNegotiationRoundNode` 先把轮次加一，再依次执行理由交换、原提议方
   修订，以及必要时的冲突组闭包重评；
3. 三个模型阶段均按批次原子提交，任一参与经理失败时不写入任何一方的业务 Record；
4. 只有实质修订触发重评；仅 `KEEP` 时不调用重评模型，但仍记一轮 `no_material_change` 并回到
   共识门；
5. 重评后必须再次满足每位经理对每对存活互斥条目评分和 `<= 0`；违规即失败关闭；
6. 默认最多三轮。第 3 轮后仍未解决的条目进入 `EXCLUDED`，记入 `excluded_item_ids`，不进入最终建议；
7. `ConsensusRoute` 只有 `NEGOTIATE/ASSEMBLE`。`ASSEMBLE` 进入已实现的
   `ConsensusRecommendationAssemblerNode`；v1 没有主席、仲裁节点或仲裁路由；
8. 组装模型只能看见 `AGREED` 条目，条目集合与信心度由程序决定；有被排除条目时写入
   `DebateStatus.PARTIAL_CONSENSUS`，分歧摘要按 `conflict_group` 合并；
9. 缺少任一必需维度或没有 `AGREED` 条目时，以 `no_actionable_consensus` 结束，
   `consensus_recommendation` 保持为空，这不是错误；
10. 两份原始独立建议永久保留；`item_id` 与 `conflict_group` 跨版本稳定，实质修订令 `revision + 1`；
11. Gate、Record、阶段摘要、轮摘要和组装摘要均使用来源指纹或不可变输出约束，重复执行相同输入保持幂等。

正式节点及 LangGraph 路由详见
[`formal-negotiation-nodes-v1.md`](formal-negotiation-nodes-v1.md)。
