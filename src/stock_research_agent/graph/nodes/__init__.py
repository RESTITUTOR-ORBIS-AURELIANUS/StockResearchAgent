"""工作流节点。"""

from stock_research_agent.graph.nodes.candidate_thesis import (
    build_candidate_thesis_generation_node,
)
from stock_research_agent.graph.nodes.conflict_score_validation import (
    build_conflict_score_validator_node,
    route_after_conflict_score_validation,
)
from stock_research_agent.graph.nodes.consensus_gate import (
    build_consensus_gate_node,
    route_after_consensus_gate,
)
from stock_research_agent.graph.nodes.consensus_recommendation import (
    build_consensus_recommendation_assembler_node,
)
from stock_research_agent.graph.nodes.cross_review import (
    apply_cross_reviews_node,
    build_aggressive_cross_review_node,
    build_conservative_cross_review_node,
    build_cross_review_correction_node,
    route_after_cross_review_correction,
)
from stock_research_agent.graph.nodes.event_research import (
    EventAgentGraphFactory,
    RunScopedEventGraphResolver,
    build_cancel_pending_event_requests_node,
    build_initial_event_evidence_node,
    build_release_event_runtime_node,
    build_targeted_event_research_node,
)
from stock_research_agent.graph.nodes.evidence_collector import evidence_collector_node
from stock_research_agent.graph.nodes.formal_negotiation import (
    build_begin_negotiation_round_node,
    build_debate_score_stage_node,
    build_proposal_revision_stage_node,
    build_reason_exchange_stage_node,
    complete_negotiation_round_without_rescore_node,
    complete_scored_negotiation_round_node,
    route_after_debate_score,
    route_after_proposal_revision,
    route_after_reason_exchange,
    route_after_round_completion,
)
from stock_research_agent.graph.nodes.fundamental_research import (
    FundamentalAgentGraphFactory,
    RunScopedFundamentalGraphResolver,
    build_cancel_pending_fundamental_requests_node,
    build_initial_fundamental_evidence_node,
    build_release_fundamental_runtime_node,
    build_targeted_fundamental_research_node,
)
from stock_research_agent.graph.nodes.independent_recommendations import (
    finalize_independent_recommendations_node,
)
from stock_research_agent.graph.nodes.negotiation_score_validation import (
    route_after_negotiation_score_validation,
    validate_negotiation_scores_node,
)
from stock_research_agent.graph.nodes.portfolio_recommendation import (
    build_aggressive_portfolio_recommendation_node,
    build_conservative_portfolio_recommendation_node,
)
from stock_research_agent.graph.nodes.proposal_normalization import (
    proposal_normalization_node,
)
from stock_research_agent.graph.nodes.report_composer import report_composer_node
from stock_research_agent.graph.nodes.sentiment_flow_research import (
    RunScopedSentimentFlowGraphResolver,
    SentimentFlowAgentGraphFactory,
    build_cancel_pending_sentiment_flow_requests_node,
    build_initial_sentiment_flow_evidence_node,
    build_release_sentiment_flow_runtime_node,
    build_targeted_sentiment_flow_research_node,
)
from stock_research_agent.graph.nodes.technical_research import (
    RunScopedTechnicalGraphResolver,
    TechnicalAgentGraphFactory,
    build_cancel_pending_technical_requests_node,
    build_initial_technical_evidence_node,
    build_release_technical_runtime_node,
    build_targeted_technical_research_node,
)
from stock_research_agent.graph.nodes.thesis_validation import (
    build_execute_validation_research_node,
    build_finalize_thesis_validation_run_node,
    build_review_active_thesis_node,
    build_select_thesis_for_validation_node,
)

__all__ = [
    "apply_cross_reviews_node",
    "build_aggressive_cross_review_node",
    "build_candidate_thesis_generation_node",
    "build_begin_negotiation_round_node",
    "build_consensus_gate_node",
    "build_consensus_recommendation_assembler_node",
    "build_debate_score_stage_node",
    "build_cancel_pending_event_requests_node",
    "build_cancel_pending_fundamental_requests_node",
    "build_cancel_pending_sentiment_flow_requests_node",
    "build_cancel_pending_technical_requests_node",
    "build_initial_event_evidence_node",
    "build_initial_fundamental_evidence_node",
    "build_initial_sentiment_flow_evidence_node",
    "build_initial_technical_evidence_node",
    "finalize_independent_recommendations_node",
    "build_proposal_revision_stage_node",
    "build_reason_exchange_stage_node",
    "build_aggressive_portfolio_recommendation_node",
    "build_conservative_portfolio_recommendation_node",
    "build_conservative_cross_review_node",
    "build_conflict_score_validator_node",
    "build_cross_review_correction_node",
    "proposal_normalization_node",
    "report_composer_node",
    "route_after_conflict_score_validation",
    "route_after_consensus_gate",
    "route_after_cross_review_correction",
    "route_after_debate_score",
    "route_after_negotiation_score_validation",
    "route_after_proposal_revision",
    "route_after_reason_exchange",
    "route_after_round_completion",
    "build_release_event_runtime_node",
    "build_release_fundamental_runtime_node",
    "build_release_sentiment_flow_runtime_node",
    "build_release_technical_runtime_node",
    "build_targeted_event_research_node",
    "build_targeted_fundamental_research_node",
    "build_targeted_sentiment_flow_research_node",
    "build_targeted_technical_research_node",
    "build_execute_validation_research_node",
    "build_finalize_thesis_validation_run_node",
    "build_review_active_thesis_node",
    "build_select_thesis_for_validation_node",
    "evidence_collector_node",
    "complete_negotiation_round_without_rescore_node",
    "complete_scored_negotiation_round_node",
    "EventAgentGraphFactory",
    "FundamentalAgentGraphFactory",
    "RunScopedEventGraphResolver",
    "RunScopedFundamentalGraphResolver",
    "RunScopedSentimentFlowGraphResolver",
    "RunScopedTechnicalGraphResolver",
    "SentimentFlowAgentGraphFactory",
    "TechnicalAgentGraphFactory",
    "validate_negotiation_scores_node",
]
