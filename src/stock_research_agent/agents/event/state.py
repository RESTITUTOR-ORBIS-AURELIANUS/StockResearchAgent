"""新闻事件研究 Agent 子图私有状态。"""

from datetime import datetime
from typing import Any

from typing_extensions import TypedDict

from stock_research_agent.agents.event.models import (
    EventAgentRunSummary,
    EventEvidenceDraft,
    EventResearchMode,
    EventToolObservation,
    EventVerificationTask,
)
from stock_research_agent.domain import EvidenceRecord, ResearchRequest, ResearchTarget


class EventAgentState(TypedDict, total=False):
    run_id: str
    target: ResearchTarget
    as_of: datetime
    mode: EventResearchMode
    research_request: ResearchRequest | None
    snapshot_result: dict[str, Any] | None
    evidence_drafts: list[EventEvidenceDraft]
    pending_tasks: list[EventVerificationTask]
    observations: list[EventToolObservation]
    seen_request_fingerprints: list[str]
    verification_round: int
    tool_call_count: int
    budget_exhausted: bool
    skipped_task_ids: list[str]
    unresolved_questions: list[str]
    errors: list[str]
    evidence_records: list[EvidenceRecord]
    completed_research_request: ResearchRequest | None
    run_summary: EventAgentRunSummary
