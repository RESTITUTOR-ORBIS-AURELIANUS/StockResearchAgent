"""正式研究工作流的入口节点。"""

from uuid import uuid4

from stock_research_agent.graph.state import ResearchGraphState

DEFAULT_TOKEN_BUDGET = 120_000
DEFAULT_TIME_BUDGET_SECONDS = 900


def initialize_run_node(state: ResearchGraphState) -> ResearchGraphState:
    """校验启动输入，并初始化本轮研究的共享容器与预算。"""

    target = state.get("target")
    as_of = state.get("as_of")
    if target is None:
        raise ValueError("启动研究必须提供 target")
    if as_of is None:
        raise ValueError("启动研究必须提供带时区的 as_of")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 必须包含时区，不能使用朴素 datetime")

    code_part = target.code.replace(".", "_").replace("-", "_")
    run_id = state.get("run_id") or (f"run_{as_of:%Y%m%d_%H%M%S}_{code_part}_{uuid4().hex[:8]}")

    return {
        "run_id": run_id,
        "evidence_pool": [],
        "thesis_pool": [],
        "research_requests": [],
        "aggressive_recommendation": None,
        "conservative_recommendation": None,
        "consensus_recommendation": None,
        "validation_round": 0,
        "research_request_count": 0,
        "debate_round": 0,
        "token_budget_remaining": DEFAULT_TOKEN_BUDGET,
        "time_budget_remaining_seconds": DEFAULT_TIME_BUDGET_SECONDS,
        "errors": [],
    }
