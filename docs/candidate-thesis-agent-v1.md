# LeadResearchStrategist 候选观点生成节点 v1

> 实现状态：`EvidenceCollection → CandidateThesisGeneration → ThesisRecord[]` 已实现。
> 本节点只提出待查证猜想；后续 `ThesisValidationAnalyst` 已实现，详见
> [`thesis-validation-agent-v1.md`](thesis-validation-agent-v1.md)。

## 1. 节点职责

`LeadResearchStrategist` 是首席研究策略师。它读取四位证据研究员经过
`EvidenceCollectorNode` 接受的全部证据摘要，寻找技术、基本面、新闻事件、情绪资金和宏观证据
之间的印证、背离与竞争性解释。

它可以提出大胆猜想，但不能：

- 宣布观点已经被证实或被反驳；
- 填写观点置信度；
- 输出买卖、仓位、目标价或其他投资建议；
- 调用数据 Tool，或绕过 `EvidenceCollection` 阅读原始 Provider 数据；
- 引用输入中不存在的 `evidence_id`；
- 只凭市场/行业证据创建没有股票自身证据的股票观点。

## 2. 当前主图位置

```text
TechnicalResearchAnalyst
    ↓
SentimentAndFlowAnalyst
    ↓
FundamentalResearchAnalyst
    ↓
EventDrivenResearchAnalyst
    ↓
EvidenceCollectorNode
    ↓
CandidateThesisGenerationNode
    ↓
thesis_pool: ThesisRecord[]
    ↓
ThesisValidationAnalyst（逐观点连续查证）
```

当前四位证据研究员仍暂时串行。若 `build_research_graph()` 没有注入
`lead_research_strategist_model`，主图会在 Collector 后结束，默认 CLI 因而不会触发 LLM 调用。

## 3. 模型输入

模型接收单个 `LeadStrategistInput`：

```text
run_id
research_target
as_of
counts_by_domain
counts_by_verification_status
counts_by_target_type
evidence: CollectedEvidenceSummary[]
policy_notes
max_candidates
```

`evidence` 是 Collector 接受的全部证据摘要，包含完整 `description`、验证状态、标签、来源数量、
Provider 和接口。模型不会收到完整 `SourceReference` 或原始行情/新闻行；后续可通过 `evidence_id`
回溯 `evidence_pool`。

第一版不对语义相似证据做去重。Prompt 明确要求：表述相似的两条 Evidence 不一定是两个独立来源，
不得仅凭数量提高确信程度。

## 4. 上下文完整性

节点采用“全部输入或停止”，不会静默截断：

| 限制 | 默认值 |
|---|---:|
| 最大候选观点数 | 8 |
| 最大证据摘要数 | 128 |
| 最大序列化输入字符数 | 120000 |

超过证据数或字符数硬限制时，模型不会被调用，运行摘要分别标记为
`evidence_limit_exceeded` 或 `context_limit_exceeded`。未来如果真实运行经常超过限制，应新增明确的
分批压缩节点，而不是简单删除列表末尾证据。

## 5. 结构化模型输出

模型只能返回 `CandidateThesisGeneration`：

```json
{
  "candidates": [
    {
      "target": {"type": "STOCK", "code": "000001.SZ", "name": "平安银行"},
      "title": "经营改善尚未得到市场行为确认",
      "description": "事实显示经营改善但资金偏弱，据此猜想市场仍怀疑改善持续性。",
      "direction": "MIXED",
      "horizon": "未来一个至两个季度",
      "supporting_evidence_ids": ["ev_fundamental_001", "ev_technical_001"],
      "contradicting_evidence_ids": ["ev_flow_001"],
      "reasoning_summary": "基本面和市场行为之间存在需要解释的背离。",
      "missing_questions": ["盈利改善是否来自可持续核心业务？"],
      "catalysts": ["下一季度核心收入继续增长"],
      "invalidation_conditions": ["经营现金流重新转负"]
    }
  ],
  "generation_summary": "生成一条跨领域待查证观点。"
}
```

`supporting_evidence_ids` 在这里表示“促使策略师提出该猜想的证据”，不表示观点已经通过验证。
真正的支持、反驳、混合或无法判断状态由后续 `ThesisValidationNode` 更新。

## 6. 确定性装配

模型返回后，程序逐条检查：

1. 所有证据 ID 都存在于本轮 `EvidenceCollection`；
2. 支持和反向 ID 不重复且互不重叠；
3. 至少一条支持证据的 `target` 与候选观点 `target` 完全一致；
4. 候选数不超过配置限制；
5. 完全相同的结构化草稿不会重复进入本轮观点池。

通过后程序生成稳定 `thesis_id`，并固定写入：

```text
origin.type = LEAD_STRATEGIST
origin.agent = LeadResearchStrategist
validation.status = UNVERIFIED
validation.confidence = null
validation.round = 0
revision = 1
```

被拒绝的草稿不会进入 `thesis_pool`，原因进入主图 `errors`；输入数量、模型调用情况、接受/拒绝
数量和生成摘要记录在 `candidate_thesis_run_summary`。

## 7. LLM 配置和构建

本节点复用现有 `LLMSettings` 和全局 ChatModel：

```python
chat_model = build_chat_model(llm_settings)
strategist = OpenAILeadResearchStrategistModel(
    chat_model,
    structured_output_method=llm_settings.structured_output_method,
)

graph = build_research_graph(
    technical_agent_graph_factory=technical_factory,
    sentiment_flow_agent_graph_factory=sentiment_factory,
    fundamental_agent_graph_factory=fundamental_factory,
    event_agent_graph_factory=event_factory,
    lead_research_strategist_model=strategist,
)
```

完整 Prompt 位于：

```text
src/stock_research_agent/agents/strategist/prompts.py
```

它包含严格 JSON Schema、证据状态边界、跨领域推理规则、few-shot 和错误反例。
