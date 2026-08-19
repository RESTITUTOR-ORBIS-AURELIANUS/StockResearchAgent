"""LangGraph 所有节点共享的状态以及并行更新合并规则。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from typing_extensions import TypedDict

from stock_research_agent.domain import (
    EvidenceRecord,
    RecommendationRecord,
    ResearchRequest,
    ResearchTarget,
    ThesisRecord,
)


def _merge_by_id[RecordT](
    current: list[RecordT],
    updates: list[RecordT],
    get_id: Callable[[RecordT], str],
) -> list[RecordT]:
    """保持顺序地合并记录；相同 ID 的新版本覆盖旧版本。"""

    result = list(current)
    positions = {get_id(record): index for index, record in enumerate(result)}
    for record in updates:
        record_id = get_id(record)
        if record_id in positions:
            result[positions[record_id]] = record
        else:
            positions[record_id] = len(result)
            result.append(record)
    return result


def merge_evidence_records(
    current: list[EvidenceRecord], updates: list[EvidenceRecord]
) -> list[EvidenceRecord]:
    return _merge_by_id(current, updates, lambda record: record.evidence_id)


def merge_thesis_records(
    current: list[ThesisRecord], updates: list[ThesisRecord]
) -> list[ThesisRecord]:
    return _merge_by_id(current, updates, lambda record: record.thesis_id)


def merge_research_requests(
    current: list[ResearchRequest], updates: list[ResearchRequest]
) -> list[ResearchRequest]:
    return _merge_by_id(current, updates, lambda record: record.request_id)


def append_errors(current: list[str], updates: list[str]) -> list[str]:
    return [*current, *updates]


class ResearchGraphState(TypedDict, total=False):
    """工作流在任意时刻的完整状态快照。

    节点只需要返回自己修改的字段，不必复制并返回整个状态。
    Annotated 后面的函数是 reducer，用来合并并行节点的列表更新。
    """

    run_id: str
    target: ResearchTarget
    as_of: datetime
    evidence_pool: Annotated[list[EvidenceRecord], merge_evidence_records]
    thesis_pool: Annotated[list[ThesisRecord], merge_thesis_records]
    research_requests: Annotated[list[ResearchRequest], merge_research_requests]
    aggressive_recommendation: RecommendationRecord | None
    conservative_recommendation: RecommendationRecord | None
    consensus_recommendation: RecommendationRecord | None
    validation_round: int
    research_request_count: int
    debate_round: int
    token_budget_remaining: int
    time_budget_remaining_seconds: int
    errors: Annotated[list[str], append_errors]
