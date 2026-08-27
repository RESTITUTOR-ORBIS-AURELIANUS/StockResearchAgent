"""系统内部稳定的领域数据契约。"""

from stock_research_agent.domain.common import ResearchTarget, SourceReference, TimeRange
from stock_research_agent.domain.evidence import EvidenceRecord
from stock_research_agent.domain.evidence_collection import (
    CollectedEvidenceSummary,
    EvidenceCollection,
    EvidenceRejectionReason,
    RejectedEvidenceSummary,
)
from stock_research_agent.domain.recommendation import RecommendationRecord
from stock_research_agent.domain.research_finding import (
    ResearchFinding,
    build_research_finding_id,
)
from stock_research_agent.domain.research_request import ResearchRequest
from stock_research_agent.domain.thesis import ThesisRecord

__all__ = [
    "EvidenceRecord",
    "CollectedEvidenceSummary",
    "EvidenceCollection",
    "EvidenceRejectionReason",
    "RecommendationRecord",
    "ResearchFinding",
    "ResearchRequest",
    "ResearchTarget",
    "SourceReference",
    "RejectedEvidenceSummary",
    "ThesisRecord",
    "TimeRange",
    "build_research_finding_id",
]
