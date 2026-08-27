"""投资组合经理共同使用的结构化投资建议契约。"""

from stock_research_agent.agents.portfolio.model import (
    OpenAIAggressivePortfolioManagerModel,
    OpenAIConservativePortfolioManagerModel,
    OpenAIPortfolioManagerModel,
    PortfolioManagerModel,
)
from stock_research_agent.agents.portfolio.models import (
    DecisionThesisSummary,
    InitialInsistenceScore,
    PortfolioRecommendationDraft,
    PortfolioRecommendationInput,
    PortfolioRecommendationLimits,
    PortfolioRecommendationRunSummary,
    PortfolioRecommendationStopReason,
    RecommendationProposalDraft,
)

__all__ = [
    "DecisionThesisSummary",
    "InitialInsistenceScore",
    "OpenAIAggressivePortfolioManagerModel",
    "OpenAIConservativePortfolioManagerModel",
    "OpenAIPortfolioManagerModel",
    "PortfolioManagerModel",
    "PortfolioRecommendationDraft",
    "PortfolioRecommendationInput",
    "PortfolioRecommendationLimits",
    "PortfolioRecommendationRunSummary",
    "PortfolioRecommendationStopReason",
    "RecommendationProposalDraft",
]
