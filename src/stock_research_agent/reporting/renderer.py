"""把严格结构化报告确定性渲染为便于阅读的 Markdown。"""

from __future__ import annotations

from collections import defaultdict

from stock_research_agent.domain.recommendation import RecommendationRecord
from stock_research_agent.reporting.models import ResearchReport


def render_research_report_markdown(report: ResearchReport) -> str:
    """不调用大模型；相同报告对象始终得到相同 Markdown。"""

    lines = [
        f"# {report.target.name}（{report.target.code}）投研报告",
        "",
        "## 报告信息",
        "",
        f"- 报告 ID：`{report.report_id}`",
        f"- 运行 ID：`{report.run_id}`",
        f"- 数据截止时间：{report.as_of.isoformat()}",
        f"- 研究范围：{report.target.type.value}",
        f"- 结果状态：`{report.outcome.value}`",
        f"- 运行健康状态：`{report.health.value}`",
        "",
        "## 证据",
        "",
        f"共收录 {report.evidence.total_count} 条原始证据。",
        "",
    ]
    lines.extend(_render_evidence(report))
    lines.extend(["", "## 观点", "", f"共收录 {report.theses.total_count} 条观点。", ""])
    lines.extend(_render_theses(report))
    lines.extend(["", "## 投资建议", ""])
    lines.extend(
        _render_recommendation(
            "激进型基金经理原始建议",
            report.recommendations.aggressive,
        )
    )
    lines.extend(
        _render_recommendation(
            "保守型基金经理原始建议",
            report.recommendations.conservative,
        )
    )
    lines.extend(
        _render_recommendation(
            "协商后的最终建议",
            report.recommendations.consensus,
        )
    )
    lines.extend(["", "## 未决分歧", ""])
    lines.extend(_render_disagreements(report))
    lines.extend(["", "## 运行诊断", ""])
    lines.extend(_render_diagnostics(report))
    lines.extend(["", "## 免责声明", "", report.disclaimer, ""])
    return "\n".join(lines)


def _render_evidence(report: ResearchReport) -> list[str]:
    if not report.evidence.records:
        return ["当前运行未产出证据。"]
    grouped = defaultdict(list)
    for record in report.evidence.records:
        grouped[record.domain.value].append(record)
    lines: list[str] = []
    for domain in sorted(grouped):
        lines.extend([f"### {domain}", ""])
        for record in grouped[domain]:
            lines.extend(
                [
                    f"#### {_text(record.title)} (`{record.evidence_id}`)",
                    "",
                    f"- 对象：{_text(record.target.name)}（`{record.target.code}`）",
                    f"- 核验状态：`{record.verification_status.value}`",
                    f"- 采集者：`{_text(record.collected_by)}`",
                    f"- 描述：{_text(record.description)}",
                    "- 来源：",
                ]
            )
            for source in record.source_refs:
                location = (
                    f"`{_text(source.provider)}/{_text(source.interface)}`；"
                    f"记录键 `{_text(source.record_key)}`"
                )
                if source.url:
                    location += f"；[原始链接]({source.url})"
                lines.append(f"  - {location}")
            lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _render_theses(report: ResearchReport) -> list[str]:
    if not report.theses.records:
        return ["当前运行未形成观点。"]
    lines: list[str] = []
    for thesis in report.theses.records:
        confidence = (
            "未完成查证"
            if thesis.validation.confidence is None
            else f"{thesis.validation.confidence:.2f}"
        )
        lines.extend(
            [
                f"### {_text(thesis.title)} (`{thesis.thesis_id}`)",
                "",
                f"- 方向：`{thesis.direction.value}`",
                f"- 查证状态：`{thesis.validation.status.value}`",
                f"- 置信度：{confidence}",
                f"- 期限：{_text(thesis.horizon)}",
                f"- 描述：{_text(thesis.description)}",
                "- 支持证据："
                + _id_list(thesis.supporting_evidence_ids, empty="无"),
                "- 反证：" + _id_list(thesis.contradicting_evidence_ids, empty="无"),
            ]
        )
        if thesis.reasoning_summary:
            lines.append(f"- 查证总结：{_text(thesis.reasoning_summary)}")
        lines.append("")
    lines.pop()
    return lines


def _render_recommendation(
    title: str,
    recommendation: RecommendationRecord | None,
) -> list[str]:
    lines = [f"### {title}", ""]
    if recommendation is None:
        return [*lines, "未生成。", ""]
    lines.extend(
        [
            f"- 建议 ID：`{recommendation.recommendation_id}`",
            f"- 动作：`{recommendation.action.value}`",
            f"- 投资期限：{_text(recommendation.horizon)}",
            f"- 置信度：{recommendation.confidence:.2f}",
            f"- 摘要：{_text(recommendation.summary)}",
            f"- 风险：{_text(recommendation.risk_summary)}",
            "- 支持观点：" + _id_list(recommendation.supporting_thesis_ids, empty="无"),
        ]
    )
    if recommendation.valuation_guidance:
        lines.append(f"- 估值/价格指引：{_text(recommendation.valuation_guidance)}")
    lines.extend(["", "建议条目：", ""])
    for item in recommendation.proposal_items:
        lines.append(
            f"- `{item.item_id}` · `{item.decision_dimension.value}` · "
            f"`{item.status.value}`：{_text(item.proposal)}"
        )
    lines.append("")
    return lines


def _render_disagreements(report: ResearchReport) -> list[str]:
    disclosure = report.recommendations.disagreement
    if report.recommendations.consensus is None and (
        not disclosure.excluded_item_ids
        and not disclosure.remaining_disagreements
        and not disclosure.missing_required_dimensions
    ):
        return ["协商后的最终建议未生成，因此不能认定当前不存在未决分歧。"]
    if (
        not disclosure.excluded_item_ids
        and not disclosure.remaining_disagreements
        and not disclosure.missing_required_dimensions
    ):
        return ["没有需要披露的未决分歧。"]
    lines = [f"- 协商轮次：{disclosure.debate_round}"]
    if disclosure.missing_required_dimensions:
        dimensions = "、".join(item.value for item in disclosure.missing_required_dimensions)
        lines.append(f"- 缺失的必要决策维度：{dimensions}")
    if disclosure.excluded_item_ids:
        lines.append(
            "- 未进入最终建议的条目："
            + _id_list(disclosure.excluded_item_ids, empty="无")
        )
    if disclosure.excluded_items:
        lines.extend(["", "被排除条目详情：", ""])
        for item in disclosure.excluded_items:
            lines.append(
                f"- `{item.item_id}` · `{item.decision_dimension.value}`："
                f"{_text(item.proposal)}"
            )
    if disclosure.remaining_disagreements:
        lines.extend(["", "剩余分歧说明：", ""])
        lines.extend(f"- {_text(item)}" for item in disclosure.remaining_disagreements)
    return lines


def _render_diagnostics(report: ResearchReport) -> list[str]:
    diagnostics = report.diagnostics
    if not diagnostics.upstream_errors and not diagnostics.integrity_warnings:
        return ["未记录上游错误或完整性警告。"]
    lines: list[str] = []
    if diagnostics.upstream_errors:
        lines.extend(["### 上游错误", ""])
        lines.extend(f"- {_text(error)}" for error in diagnostics.upstream_errors)
        lines.append("")
    if diagnostics.integrity_warnings:
        lines.extend(["### 完整性警告", ""])
        lines.extend(f"- {_text(warning)}" for warning in diagnostics.integrity_warnings)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _id_list(values, *, empty: str) -> str:
    if not values:
        return empty
    return "、".join(f"`{_text(value)}`" for value in values)


def _text(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")
