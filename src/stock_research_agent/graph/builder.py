"""把状态、节点和边连接为可执行 LangGraph。"""

from langgraph.graph import END, START, StateGraph

from stock_research_agent.agents.consensus_assembly import (
    ConsensusRecommendationSynthesisModel,
)
from stock_research_agent.agents.debate import (
    PortfolioCrossReviewLimits,
    PortfolioCrossReviewModel,
)
from stock_research_agent.agents.negotiation import (
    NegotiationLimits,
    PortfolioNegotiationModel,
)
from stock_research_agent.agents.portfolio import (
    PortfolioManagerModel,
    PortfolioRecommendationLimits,
)
from stock_research_agent.agents.strategist import (
    CandidateThesisLimits,
    LeadResearchStrategistModel,
)
from stock_research_agent.agents.validator import (
    ThesisValidationAnalystModel,
    ThesisValidationLimits,
)
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
    event_request_budget_exhausted,
    has_active_event_request,
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
    fundamental_request_budget_exhausted,
    has_active_fundamental_request,
)
from stock_research_agent.graph.nodes.independent_recommendations import (
    finalize_independent_recommendations_node,
)
from stock_research_agent.graph.nodes.initialize_run import initialize_run_node
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
    has_active_sentiment_flow_request,
    sentiment_flow_request_budget_exhausted,
)
from stock_research_agent.graph.nodes.technical_research import (
    RunScopedTechnicalGraphResolver,
    TechnicalAgentGraphFactory,
    build_cancel_pending_technical_requests_node,
    build_initial_technical_evidence_node,
    build_release_technical_runtime_node,
    build_targeted_technical_research_node,
    has_active_technical_request,
    technical_request_budget_exhausted,
)
from stock_research_agent.graph.nodes.thesis_validation import (
    build_execute_validation_research_node,
    build_finalize_thesis_validation_run_node,
    build_review_active_thesis_node,
    build_select_thesis_for_validation_node,
    route_after_validation_review,
    route_after_validation_selection,
)
from stock_research_agent.graph.state import ResearchGraphState


def build_research_graph(
    *,
    technical_agent_graph_factory: TechnicalAgentGraphFactory | None = None,
    sentiment_flow_agent_graph_factory: SentimentFlowAgentGraphFactory | None = None,
    fundamental_agent_graph_factory: FundamentalAgentGraphFactory | None = None,
    event_agent_graph_factory: EventAgentGraphFactory | None = None,
    lead_research_strategist_model: LeadResearchStrategistModel | None = None,
    candidate_thesis_limits: CandidateThesisLimits | None = None,
    thesis_validation_model: ThesisValidationAnalystModel | None = None,
    thesis_validation_limits: ThesisValidationLimits | None = None,
    aggressive_portfolio_manager_model: PortfolioManagerModel | None = None,
    conservative_portfolio_manager_model: PortfolioManagerModel | None = None,
    portfolio_recommendation_limits: PortfolioRecommendationLimits | None = None,
    aggressive_cross_review_model: PortfolioCrossReviewModel | None = None,
    conservative_cross_review_model: PortfolioCrossReviewModel | None = None,
    cross_review_limits: PortfolioCrossReviewLimits | None = None,
    aggressive_negotiation_model: PortfolioNegotiationModel | None = None,
    conservative_negotiation_model: PortfolioNegotiationModel | None = None,
    negotiation_limits: NegotiationLimits | None = None,
    consensus_assembly_model: ConsensusRecommendationSynthesisModel | None = None,
):
    """构建正式工作流：技术、情绪资金、基本面、新闻事件依次执行。"""

    if thesis_validation_model is not None and lead_research_strategist_model is None:
        raise ValueError("启用观点查证前必须同时配置 lead_research_strategist_model")
    portfolio_models = (
        aggressive_portfolio_manager_model,
        conservative_portfolio_manager_model,
    )
    if (portfolio_models[0] is None) != (portfolio_models[1] is None):
        raise ValueError("进取型和防御型投资组合经理必须成对配置")
    if portfolio_models[0] is not None and thesis_validation_model is None:
        raise ValueError("启用投资建议前必须先配置 thesis_validation_model")
    cross_review_models = (
        aggressive_cross_review_model,
        conservative_cross_review_model,
    )
    if (cross_review_models[0] is None) != (cross_review_models[1] is None):
        raise ValueError("进取型和防御型交叉评分模型必须成对配置")
    if cross_review_models[0] is not None and portfolio_models[0] is None:
        raise ValueError("启用交叉评分前必须先配置两位投资组合经理")
    negotiation_models = (
        aggressive_negotiation_model,
        conservative_negotiation_model,
    )
    if (negotiation_models[0] is None) != (negotiation_models[1] is None):
        raise ValueError("进取型和防御型正式协商模型必须成对配置")
    if negotiation_models[0] is not None and cross_review_models[0] is None:
        raise ValueError("启用正式协商前必须先配置双方交叉评分模型")
    if consensus_assembly_model is not None and negotiation_models[0] is None:
        raise ValueError("启用共识建议组装前必须先配置双方正式协商模型")
    if negotiation_models[0] is not None and consensus_assembly_model is None:
        raise ValueError("启用正式协商时必须同时配置共识建议组装模型")

    technical_resolver = (
        RunScopedTechnicalGraphResolver(technical_agent_graph_factory)
        if technical_agent_graph_factory is not None
        else None
    )
    sentiment_resolver = (
        RunScopedSentimentFlowGraphResolver(sentiment_flow_agent_graph_factory)
        if sentiment_flow_agent_graph_factory is not None
        else None
    )
    fundamental_resolver = (
        RunScopedFundamentalGraphResolver(fundamental_agent_graph_factory)
        if fundamental_agent_graph_factory is not None
        else None
    )
    event_resolver = (
        RunScopedEventGraphResolver(event_agent_graph_factory)
        if event_agent_graph_factory is not None
        else None
    )

    builder = StateGraph(ResearchGraphState)
    builder.add_node("initialize_run", initialize_run_node)
    builder.add_node("collect_evidence", evidence_collector_node)
    builder.add_node("compose_report", report_composer_node)
    builder.add_edge("compose_report", END)
    terminal = "compose_report"
    builder.add_edge(START, "initialize_run")
    if lead_research_strategist_model is None:
        builder.add_edge("collect_evidence", terminal)
    else:
        builder.add_node(
            "generate_candidate_theses",
            build_candidate_thesis_generation_node(
                lead_research_strategist_model,
                limits=candidate_thesis_limits,
            ),
        )
        builder.add_edge("collect_evidence", "generate_candidate_theses")
        if thesis_validation_model is None:
            builder.add_edge("generate_candidate_theses", terminal)
        else:
            builder.add_node(
                "select_thesis_for_validation",
                build_select_thesis_for_validation_node(),
            )
            builder.add_node(
                "review_active_thesis",
                build_review_active_thesis_node(
                    thesis_validation_model,
                    limits=thesis_validation_limits,
                ),
            )
            builder.add_node(
                "execute_validation_research",
                build_execute_validation_research_node(
                    technical_resolver=technical_resolver,
                    fundamental_resolver=fundamental_resolver,
                    sentiment_flow_resolver=sentiment_resolver,
                    event_resolver=event_resolver,
                ),
            )
            builder.add_node(
                "finalize_thesis_validation",
                build_finalize_thesis_validation_run_node(
                    resolvers=(
                        technical_resolver,
                        fundamental_resolver,
                        sentiment_resolver,
                        event_resolver,
                    )
                ),
            )
            builder.add_edge(
                "generate_candidate_theses",
                "select_thesis_for_validation",
            )
            builder.add_conditional_edges(
                "select_thesis_for_validation",
                route_after_validation_selection,
                {
                    "review": "review_active_thesis",
                    "done": "finalize_thesis_validation",
                },
            )
            builder.add_conditional_edges(
                "review_active_thesis",
                route_after_validation_review,
                {
                    "research": "execute_validation_research",
                    "next": "select_thesis_for_validation",
                },
            )
            builder.add_edge("execute_validation_research", "review_active_thesis")
            if aggressive_portfolio_manager_model is None:
                builder.add_edge("finalize_thesis_validation", terminal)
            else:
                assert conservative_portfolio_manager_model is not None
                builder.add_node(
                    "generate_aggressive_recommendation",
                    build_aggressive_portfolio_recommendation_node(
                        aggressive_portfolio_manager_model,
                        limits=portfolio_recommendation_limits,
                    ),
                )
                builder.add_node(
                    "generate_conservative_recommendation",
                    build_conservative_portfolio_recommendation_node(
                        conservative_portfolio_manager_model,
                        limits=portfolio_recommendation_limits,
                    ),
                )
                builder.add_edge(
                    "finalize_thesis_validation",
                    "generate_aggressive_recommendation",
                )
                builder.add_edge(
                    "finalize_thesis_validation",
                    "generate_conservative_recommendation",
                )
                if aggressive_cross_review_model is None:
                    builder.add_node(
                        "finalize_independent_recommendations",
                        finalize_independent_recommendations_node,
                    )
                    builder.add_edge(
                        [
                            "generate_aggressive_recommendation",
                            "generate_conservative_recommendation",
                        ],
                        "finalize_independent_recommendations",
                    )
                    builder.add_edge("finalize_independent_recommendations", terminal)
                else:
                    assert conservative_cross_review_model is not None
                    builder.add_node(
                        "normalize_proposals",
                        proposal_normalization_node,
                    )
                    builder.add_edge(
                        [
                            "generate_aggressive_recommendation",
                            "generate_conservative_recommendation",
                        ],
                        "normalize_proposals",
                    )
                    configured_cross_review_limits = (
                        cross_review_limits or PortfolioCrossReviewLimits()
                    )
                    builder.add_node(
                        "aggressive_cross_review",
                        build_aggressive_cross_review_node(
                            aggressive_cross_review_model,
                            limits=configured_cross_review_limits,
                        ),
                    )
                    builder.add_node(
                        "conservative_cross_review",
                        build_conservative_cross_review_node(
                            conservative_cross_review_model,
                            limits=configured_cross_review_limits,
                        ),
                    )
                    builder.add_node("apply_cross_reviews", apply_cross_reviews_node)
                    builder.add_node(
                        "validate_conflict_scores",
                        build_conflict_score_validator_node(
                            max_attempts=configured_cross_review_limits.max_attempts,
                        ),
                    )
                    builder.add_node(
                        "correct_conflict_scores",
                        build_cross_review_correction_node(
                            aggressive_cross_review_model,
                            conservative_cross_review_model,
                            limits=configured_cross_review_limits,
                        ),
                    )
                    builder.add_edge(
                        "normalize_proposals",
                        "aggressive_cross_review",
                    )
                    builder.add_edge(
                        "normalize_proposals",
                        "conservative_cross_review",
                    )
                    builder.add_edge(
                        [
                            "aggressive_cross_review",
                            "conservative_cross_review",
                        ],
                        "apply_cross_reviews",
                    )
                    builder.add_edge("apply_cross_reviews", "validate_conflict_scores")
                    if aggressive_negotiation_model is None:
                        builder.add_conditional_edges(
                            "validate_conflict_scores",
                            route_after_conflict_score_validation,
                            {
                                "valid": terminal,
                                "retry": "correct_conflict_scores",
                                "failed": terminal,
                            },
                        )
                    else:
                        assert conservative_negotiation_model is not None
                        assert consensus_assembly_model is not None
                        configured_negotiation_limits = (
                            negotiation_limits or NegotiationLimits()
                        )
                        builder.add_node(
                            "consensus_gate",
                            build_consensus_gate_node(
                                max_rounds=configured_negotiation_limits.max_rounds
                            ),
                        )
                        builder.add_node(
                            "begin_negotiation_round",
                            build_begin_negotiation_round_node(
                                limits=configured_negotiation_limits
                            ),
                        )
                        builder.add_node(
                            "exchange_negotiation_reasons",
                            build_reason_exchange_stage_node(
                                aggressive_negotiation_model,
                                conservative_negotiation_model,
                                limits=configured_negotiation_limits,
                            ),
                        )
                        builder.add_node(
                            "revise_negotiation_proposals",
                            build_proposal_revision_stage_node(
                                aggressive_negotiation_model,
                                conservative_negotiation_model,
                                limits=configured_negotiation_limits,
                            ),
                        )
                        builder.add_node(
                            "score_revised_proposals",
                            build_debate_score_stage_node(
                                aggressive_negotiation_model,
                                conservative_negotiation_model,
                                limits=configured_negotiation_limits,
                            ),
                        )
                        builder.add_node(
                            "validate_negotiation_scores",
                            validate_negotiation_scores_node,
                        )
                        builder.add_node(
                            "complete_unscored_negotiation_round",
                            complete_negotiation_round_without_rescore_node,
                        )
                        builder.add_node(
                            "complete_scored_negotiation_round",
                            complete_scored_negotiation_round_node,
                        )
                        builder.add_node(
                            "assemble_consensus_recommendation",
                            build_consensus_recommendation_assembler_node(
                                consensus_assembly_model
                            ),
                        )
                        builder.add_conditional_edges(
                            "validate_conflict_scores",
                            route_after_conflict_score_validation,
                            {
                                "valid": "consensus_gate",
                                "retry": "correct_conflict_scores",
                                "failed": terminal,
                            },
                        )
                        builder.add_conditional_edges(
                            "consensus_gate",
                            route_after_consensus_gate,
                            {
                                "assemble": "assemble_consensus_recommendation",
                                "negotiate": "begin_negotiation_round",
                                "failed": terminal,
                            },
                        )
                        builder.add_edge("assemble_consensus_recommendation", terminal)
                        builder.add_edge(
                            "begin_negotiation_round",
                            "exchange_negotiation_reasons",
                        )
                        builder.add_conditional_edges(
                            "exchange_negotiation_reasons",
                            route_after_reason_exchange,
                            {
                                "revise": "revise_negotiation_proposals",
                                "failed": terminal,
                            },
                        )
                        builder.add_conditional_edges(
                            "revise_negotiation_proposals",
                            route_after_proposal_revision,
                            {
                                "score": "score_revised_proposals",
                                "complete_without_score": (
                                    "complete_unscored_negotiation_round"
                                ),
                                "failed": terminal,
                            },
                        )
                        builder.add_conditional_edges(
                            "score_revised_proposals",
                            route_after_debate_score,
                            {
                                "validate": "validate_negotiation_scores",
                                "failed": terminal,
                            },
                        )
                        builder.add_conditional_edges(
                            "validate_negotiation_scores",
                            route_after_negotiation_score_validation,
                            {
                                "valid": "complete_scored_negotiation_round",
                                "failed": terminal,
                            },
                        )
                        builder.add_conditional_edges(
                            "complete_unscored_negotiation_round",
                            route_after_round_completion,
                            {"gate": "consensus_gate", "failed": terminal},
                        )
                        builder.add_conditional_edges(
                            "complete_scored_negotiation_round",
                            route_after_round_completion,
                            {"gate": "consensus_gate", "failed": terminal},
                        )
                    builder.add_conditional_edges(
                        "correct_conflict_scores",
                        route_after_cross_review_correction,
                        {
                            "apply": "apply_cross_reviews",
                            "failed": terminal,
                        },
                    )

    event_entry = "collect_evidence"
    if event_agent_graph_factory is not None:
        assert event_resolver is not None
        event_entry = "initial_event_evidence"
        builder.add_node(event_entry, build_initial_event_evidence_node(event_resolver))
        builder.add_node(
            "targeted_event_research",
            build_targeted_event_research_node(event_resolver),
        )
        builder.add_node(
            "cancel_pending_event_requests",
            build_cancel_pending_event_requests_node(),
        )
        builder.add_node(
            "release_event_runtime",
            build_release_event_runtime_node(event_resolver),
        )
        builder.add_conditional_edges(
            event_entry,
            _route_after_event_node,
            {
                "targeted": "targeted_event_research",
                "budget": "cancel_pending_event_requests",
                "done": "release_event_runtime",
                "failed": "release_event_runtime",
            },
        )
        builder.add_conditional_edges(
            "targeted_event_research",
            _route_after_event_node,
            {
                "targeted": "targeted_event_research",
                "budget": "cancel_pending_event_requests",
                "done": "release_event_runtime",
                "failed": "release_event_runtime",
            },
        )
        builder.add_edge("cancel_pending_event_requests", "release_event_runtime")
        builder.add_conditional_edges(
            "release_event_runtime",
            _route_after_evidence_runtime_release,
            {"continue": "collect_evidence", "failed": terminal},
        )

    fundamental_entry: str = event_entry
    if fundamental_agent_graph_factory is not None:
        assert fundamental_resolver is not None
        fundamental_entry = "initial_fundamental_evidence"
        builder.add_node(
            fundamental_entry,
            build_initial_fundamental_evidence_node(fundamental_resolver),
        )
        builder.add_node(
            "targeted_fundamental_research",
            build_targeted_fundamental_research_node(fundamental_resolver),
        )
        builder.add_node(
            "cancel_pending_fundamental_requests",
            build_cancel_pending_fundamental_requests_node(),
        )
        builder.add_node(
            "release_fundamental_runtime",
            build_release_fundamental_runtime_node(fundamental_resolver),
        )
        builder.add_conditional_edges(
            fundamental_entry,
            _route_after_fundamental_node,
            {
                "targeted": "targeted_fundamental_research",
                "budget": "cancel_pending_fundamental_requests",
                "done": "release_fundamental_runtime",
                "failed": "release_fundamental_runtime",
            },
        )
        builder.add_conditional_edges(
            "targeted_fundamental_research",
            _route_after_fundamental_node,
            {
                "targeted": "targeted_fundamental_research",
                "budget": "cancel_pending_fundamental_requests",
                "done": "release_fundamental_runtime",
                "failed": "release_fundamental_runtime",
            },
        )
        builder.add_edge(
            "cancel_pending_fundamental_requests",
            "release_fundamental_runtime",
        )
        builder.add_conditional_edges(
            "release_fundamental_runtime",
            _route_after_evidence_runtime_release,
            {"continue": event_entry, "failed": terminal},
        )

    sentiment_entry: str = fundamental_entry
    if sentiment_flow_agent_graph_factory is not None:
        assert sentiment_resolver is not None
        sentiment_entry = "initial_sentiment_flow_evidence"
        builder.add_node(
            sentiment_entry,
            build_initial_sentiment_flow_evidence_node(sentiment_resolver),
        )
        builder.add_node(
            "targeted_sentiment_flow_research",
            build_targeted_sentiment_flow_research_node(sentiment_resolver),
        )
        builder.add_node(
            "cancel_pending_sentiment_flow_requests",
            build_cancel_pending_sentiment_flow_requests_node(),
        )
        builder.add_node(
            "release_sentiment_flow_runtime",
            build_release_sentiment_flow_runtime_node(sentiment_resolver),
        )
        builder.add_conditional_edges(
            sentiment_entry,
            _route_after_sentiment_flow_node,
            {
                "targeted": "targeted_sentiment_flow_research",
                "budget": "cancel_pending_sentiment_flow_requests",
                "done": "release_sentiment_flow_runtime",
                "failed": "release_sentiment_flow_runtime",
            },
        )
        builder.add_conditional_edges(
            "targeted_sentiment_flow_research",
            _route_after_sentiment_flow_node,
            {
                "targeted": "targeted_sentiment_flow_research",
                "budget": "cancel_pending_sentiment_flow_requests",
                "done": "release_sentiment_flow_runtime",
                "failed": "release_sentiment_flow_runtime",
            },
        )
        builder.add_edge(
            "cancel_pending_sentiment_flow_requests",
            "release_sentiment_flow_runtime",
        )
        builder.add_conditional_edges(
            "release_sentiment_flow_runtime",
            _route_after_evidence_runtime_release,
            {"continue": fundamental_entry, "failed": terminal},
        )

    if technical_agent_graph_factory is not None:
        assert technical_resolver is not None
        builder.add_node(
            "initial_technical_evidence",
            build_initial_technical_evidence_node(technical_resolver),
        )
        builder.add_node(
            "targeted_technical_research",
            build_targeted_technical_research_node(technical_resolver),
        )
        builder.add_node(
            "release_technical_runtime",
            build_release_technical_runtime_node(technical_resolver),
        )
        builder.add_node(
            "cancel_pending_technical_requests",
            build_cancel_pending_technical_requests_node(),
        )
        builder.add_edge("initialize_run", "initial_technical_evidence")
        builder.add_conditional_edges(
            "initial_technical_evidence",
            _route_after_technical_node,
            {
                "targeted": "targeted_technical_research",
                "budget": "cancel_pending_technical_requests",
                "done": "release_technical_runtime",
                "failed": "release_technical_runtime",
            },
        )
        builder.add_conditional_edges(
            "targeted_technical_research",
            _route_after_technical_node,
            {
                "targeted": "targeted_technical_research",
                "budget": "cancel_pending_technical_requests",
                "done": "release_technical_runtime",
                "failed": "release_technical_runtime",
            },
        )
        builder.add_edge("cancel_pending_technical_requests", "release_technical_runtime")
        builder.add_conditional_edges(
            "release_technical_runtime",
            _route_after_evidence_runtime_release,
            {"continue": sentiment_entry, "failed": terminal},
        )
    else:
        builder.add_edge("initialize_run", sentiment_entry)
    return builder.compile()


def _route_after_technical_node(state: ResearchGraphState) -> str:
    """本阶段只消费技术请求。"""

    if state.get("evidence_stage_failed", False):
        return "failed"
    if not has_active_technical_request(state):
        return "done"
    if technical_request_budget_exhausted(state):
        return "budget"
    return "targeted"


def _route_after_sentiment_flow_node(state: ResearchGraphState) -> str:
    """本阶段只消费情绪与资金流请求。"""

    if state.get("evidence_stage_failed", False):
        return "failed"
    if not has_active_sentiment_flow_request(state):
        return "done"
    if sentiment_flow_request_budget_exhausted(state):
        return "budget"
    return "targeted"


def _route_after_fundamental_node(state: ResearchGraphState) -> str:
    """本阶段只消费基本面请求。"""

    if state.get("evidence_stage_failed", False):
        return "failed"
    if not has_active_fundamental_request(state):
        return "done"
    if fundamental_request_budget_exhausted(state):
        return "budget"
    return "targeted"


def _route_after_event_node(state: ResearchGraphState) -> str:
    """本阶段只消费新闻事件请求。"""

    if state.get("evidence_stage_failed", False):
        return "failed"
    if not has_active_event_request(state):
        return "done"
    if event_request_budget_exhausted(state):
        return "budget"
    return "targeted"


def _route_after_evidence_runtime_release(state: ResearchGraphState) -> str:
    """任一证据阶段失败时，在释放其 run-scoped 资源后立即生成不完整报告。"""

    return "failed" if state.get("evidence_stage_failed", False) else "continue"
