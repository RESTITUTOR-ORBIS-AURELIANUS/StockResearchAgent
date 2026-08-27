"""把两位经理的独立建议确定性标记为 v1 正式输出。"""

from stock_research_agent.domain.enums import RecommendationProfile
from stock_research_agent.graph.state import ResearchGraphState


def finalize_independent_recommendations_node(
    state: ResearchGraphState,
) -> ResearchGraphState:
    """确认两份独立建议完整且同属本轮，然后结束无辩论工作流。"""

    if state.get("independent_recommendations_finalized"):
        return {}
    aggressive = state.get("aggressive_recommendation")
    conservative = state.get("conservative_recommendation")
    if aggressive is None or conservative is None:
        return {
            "errors": [
                "IndependentRecommendationsFinalizerNode skipped: "
                "both manager recommendations are required"
            ]
        }
    expected_scope = (state.get("run_id"), state.get("as_of"), state.get("target"))
    for label, recommendation, profile in (
        ("aggressive", aggressive, RecommendationProfile.AGGRESSIVE),
        ("conservative", conservative, RecommendationProfile.CONSERVATIVE),
    ):
        if recommendation.profile is not profile:
            return {"errors": [f"{label} recommendation profile mismatch"]}
        if (
            recommendation.run_id,
            recommendation.as_of,
            recommendation.target,
        ) != expected_scope:
            return {"errors": [f"{label} recommendation scope mismatch"]}
    return {"independent_recommendations_finalized": True}
