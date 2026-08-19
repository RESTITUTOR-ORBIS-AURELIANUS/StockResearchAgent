"""系统内部稳定的领域数据契约。"""

from stock_research_agent.domain.common import ResearchTarget, SourceReference, TimeRange
from stock_research_agent.domain.evidence import EvidenceRecord
from stock_research_agent.domain.recommendation import RecommendationRecord
from stock_research_agent.domain.research_request import ResearchRequest
from stock_research_agent.domain.thesis import ThesisRecord

__all__ = [
    "EvidenceRecord",
    "RecommendationRecord",
    "ResearchRequest",
    "ResearchTarget",
    "SourceReference",
    "ThesisRecord",
    "TimeRange",
]
