# ReportComposerNode v1

## 职责

`ReportComposerNode` 是研究主图的统一确定性终点。它不调用大模型，也不补写缺失的
投资结论，只把当前状态中已经存在的内容组装成：

1. 严格 Pydantic `ResearchReport`；
2. 由该结构化报告确定性渲染出的 Markdown。

节点只要求 `run_id`、`as_of` 和 `target` 已初始化。因此完整运行、没有可执行共识、
数据源失败和上游提前结束都可以得到一份诚实的报告。

## 输出字段

- `research_report`：结构化报告；
- `research_report_markdown`：同一报告的 Markdown 表达。

报告包含：

- 完整 `EvidenceRecord` 和证据计数；
- 完整 `ThesisRecord` 和方向、查证状态计数；
- 激进、保守、共识三套建议（缺失时为 `null`）；
- 共识门、共识组装摘要和被排除意见；
- 上游 `errors` 与报告组装发现的完整性警告。

## 两个相互独立的状态维度

`outcome` 描述投资决策链是否完成：

- `CONSENSUS_READY`：三套建议都存在；
- `NO_ACTIONABLE_CONSENSUS`：保留两位经理原始建议，但共识组装明确判断缺少可执行共识；
- `INCOMPLETE`：上游提前结束或关键建议尚未生成。

`health` 描述运行质量：

- `CLEAN`：没有上游错误或完整性警告；
- `WITH_WARNINGS`：没有上游错误，但存在引用或运行范围完整性问题；
- `WITH_ERRORS`：状态中记录了上游错误。

二者不可混用。例如，数据采集阶段可以记录非致命错误，同时仍生成
`CONSENSUS_READY + WITH_ERRORS` 的报告。

## 不虚构原则

- 没有共识建议时，Markdown 明确写“未生成”，不会自动补一个 `HOLD`；
- `EXCLUDED` 条目只进入“未决分歧”，不能进入最终共识建议；
- 报告保留上游对象本身，而不是让另一个模型重新解释证据或观点；
- `report_id` 由报告输入的 SHA-256 指纹稳定生成，相同状态重复执行是幂等的。

## 代码位置

- `src/stock_research_agent/reporting/models.py`
- `src/stock_research_agent/reporting/renderer.py`
- `src/stock_research_agent/graph/nodes/report_composer.py`
- `tests/test_report_composer.py`
