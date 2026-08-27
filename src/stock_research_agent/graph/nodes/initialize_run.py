"""正式研究工作流的入口节点。"""

from uuid import uuid4
from zoneinfo import ZoneInfo

from stock_research_agent.graph.state import ResearchGraphState

DEFAULT_TOKEN_BUDGET = 120_000
DEFAULT_TIME_BUDGET_SECONDS = 900
_SHANGHAI = ZoneInfo("Asia/Shanghai")


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

    as_of = as_of.astimezone(_SHANGHAI).replace(microsecond=0)

    code_part = target.code.replace(".", "_").replace("-", "_")
    run_id = state.get("run_id") or (f"run_{as_of:%Y%m%d_%H%M%S}_{code_part}_{uuid4().hex[:8]}")
    forbidden_seed_fields = (
        "evidence_pool",
        "thesis_pool",
        "research_findings",
        "consensus_gate_reports",
        "reason_exchange_records",
        "proposal_revision_records",
        "debate_score_records",
        "negotiation_model_run_summaries",
        "negotiation_stage_run_summaries",
        "negotiation_round_summaries",
        "errors",
        "evidence_stage_failed",
    )
    dirty_fields = [field for field in forbidden_seed_fields if state.get(field)]
    if state.get("active_validation_session") is not None:
        dirty_fields.append("active_validation_session")
    if state.get("active_validation_request_id") is not None:
        dirty_fields.append("active_validation_request_id")
    for field in (
        "aggressive_recommendation",
        "conservative_recommendation",
        "consensus_recommendation",
        "consensus_assembly_run_summary",
        "research_report",
        "research_report_markdown",
        "aggressive_recommendation_run_summary",
        "conservative_recommendation_run_summary",
        "normalized_proposal_pool",
        "proposal_normalization_run_summary",
        "aggressive_cross_review",
        "conservative_cross_review",
        "aggressive_cross_review_run_summary",
        "conservative_cross_review_run_summary",
        "cross_reviewed_proposal_pool",
        "cross_review_application_run_summary",
        "conflict_score_validation_report",
        "cross_review_correction_run_summary",
        "negotiation_proposal_pool",
        "consensus_gate_report",
        "proposal_revision_application_summary",
        "negotiation_score_validation_report",
    ):
        if state.get(field) is not None:
            dirty_fields.append(field)
    if dirty_fields:
        raise ValueError(
            "主图启动输入不能携带上一轮运行状态；当前不支持用完整 state 重新启动："
            + ", ".join(dirty_fields)
        )
    research_requests = list(state.get("research_requests", []))
    mismatched_request_ids = [
        request.request_id for request in research_requests if request.run_id != run_id
    ]
    if mismatched_request_ids:
        raise ValueError(
            "预置 ResearchRequest 的 run_id 必须与本轮一致：" + ", ".join(mismatched_request_ids)
        )

    return {
        "run_id": run_id,
        "as_of": as_of,
        "evidence_pool": [],
        "thesis_pool": [],
        "research_requests": research_requests,
        "research_findings": [],
        "aggressive_recommendation": None,
        "conservative_recommendation": None,
        "consensus_recommendation": None,
        "consensus_assembly_run_summary": None,
        "research_report": None,
        "research_report_markdown": None,
        "aggressive_recommendation_run_summary": None,
        "conservative_recommendation_run_summary": None,
        "normalized_proposal_pool": None,
        "proposal_normalization_run_summary": None,
        "aggressive_cross_review": None,
        "conservative_cross_review": None,
        "aggressive_cross_review_run_summary": None,
        "conservative_cross_review_run_summary": None,
        "cross_reviewed_proposal_pool": None,
        "cross_review_application_run_summary": None,
        "conflict_score_validation_report": None,
        "cross_review_correction_run_summary": None,
        "negotiation_proposal_pool": None,
        "consensus_gate_report": None,
        "consensus_gate_reports": [],
        "reason_exchange_records": [],
        "proposal_revision_records": [],
        "debate_score_records": [],
        "negotiation_model_run_summaries": [],
        "negotiation_stage_run_summaries": [],
        "proposal_revision_application_summary": None,
        "negotiation_score_validation_report": None,
        "negotiation_round_summaries": [],
        "validation_round": 0,
        "research_request_count": 0,
        "validation_research_request_count": 0,
        "technical_request_count": 0,
        "sentiment_flow_request_count": 0,
        "fundamental_request_count": 0,
        "event_request_count": 0,
        "debate_round": 0,
        "token_budget_remaining": DEFAULT_TOKEN_BUDGET,
        "time_budget_remaining_seconds": DEFAULT_TIME_BUDGET_SECONDS,
        "technical_run_summary": None,
        "sentiment_flow_run_summary": None,
        "fundamental_run_summary": None,
        "event_run_summary": None,
        "evidence_stage_failed": False,
        "evidence_collection": None,
        "candidate_thesis_run_summary": None,
        "active_validation_session": None,
        "active_validation_request_id": None,
        "validation_model_call_count": 0,
        "validation_input_thesis_count": 0,
        "validation_discovered_candidate_count": 0,
        "validation_stop_reason": None,
        "thesis_validation_run_summary": None,
        "errors": [],
    }
