"""正式工作流入口节点测试。"""

from datetime import datetime

from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import TargetType
from stock_research_agent.graph import build_research_graph


def test_initialize_run_creates_budgets_and_empty_pools() -> None:
    graph = build_research_graph()
    target = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
    as_of = datetime.fromisoformat("2026-08-18T15:30:00+08:00")

    result = graph.invoke({"target": target, "as_of": as_of})

    assert result["run_id"].startswith("run_20260818_153000_000001_SZ_")
    assert result["target"] == target
    assert result["as_of"] == as_of
    assert result["evidence_pool"] == []
    assert result["thesis_pool"] == []
    assert result["validation_round"] == 0
    assert result["token_budget_remaining"] > 0
