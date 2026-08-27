"""LangGraph 所有节点共享的状态以及并行更新合并规则。"""

from collections.abc import Callable, Hashable
from datetime import datetime
from typing import Annotated

from typing_extensions import TypedDict

from stock_research_agent.agents.consensus_assembly import ConsensusAssemblyRunSummary
from stock_research_agent.agents.debate import (
    ConflictScoreValidationReport,
    CrossReviewApplicationRunSummary,
    CrossReviewCorrectionRunSummary,
    CrossReviewedProposalPool,
    NormalizedProposalPool,
    PortfolioCrossReviewRecord,
    PortfolioCrossReviewRunSummary,
    ProposalNormalizationRunSummary,
)
from stock_research_agent.agents.event.models import EventAgentRunSummary
from stock_research_agent.agents.fundamental.models import FundamentalAgentRunSummary
from stock_research_agent.agents.negotiation import (
    ConsensusGateReport,
    DebateScoreRecord,
    NegotiationModelRunSummary,
    NegotiationProposalPool,
    NegotiationRoundSummary,
    NegotiationScoreValidationReport,
    NegotiationStageRunSummary,
    ProposalRevisionApplicationSummary,
    ProposalRevisionRecord,
    ReasonExchangeRecord,
)
from stock_research_agent.agents.portfolio.models import PortfolioRecommendationRunSummary
from stock_research_agent.agents.sentiment_flow.models import SentimentFlowAgentRunSummary
from stock_research_agent.agents.strategist.models import CandidateThesisRunSummary
from stock_research_agent.agents.technical.models import TechnicalAgentRunSummary
from stock_research_agent.agents.validator.models import (
    ThesisValidationRunSummary,
    ThesisValidationSession,
)
from stock_research_agent.domain import (
    EvidenceCollection,
    EvidenceRecord,
    RecommendationRecord,
    ResearchFinding,
    ResearchRequest,
    ResearchTarget,
    ThesisRecord,
)
from stock_research_agent.reporting import ResearchReport


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


def _merge_immutable_by_key[RecordT](
    current: list[RecordT],
    updates: list[RecordT],
    get_key: Callable[[RecordT], Hashable],
) -> list[RecordT]:
    """追加不可变审计记录；相同键只能幂等重放，不能被静默覆盖。"""

    result = list(current)
    by_key = {get_key(record): record for record in result}
    for record in updates:
        key = get_key(record)
        existing = by_key.get(key)
        if existing is not None:
            if existing != record:
                raise ValueError(f"immutable audit record changed for key: {key}")
            continue
        by_key[key] = record
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


def merge_research_findings(
    current: list[ResearchFinding], updates: list[ResearchFinding]
) -> list[ResearchFinding]:
    existing_by_id = {record.finding_id: record for record in current}
    for update in updates:
        existing = existing_by_id.get(update.finding_id)
        if existing is not None and existing != update:
            raise ValueError(f"ResearchFinding is immutable once recorded: {update.finding_id}")
    return _merge_by_id(current, updates, lambda record: record.finding_id)


def append_errors(current: list[str], updates: list[str]) -> list[str]:
    return [*current, *updates]


def merge_consensus_gate_reports(
    current: list[ConsensusGateReport], updates: list[ConsensusGateReport]
) -> list[ConsensusGateReport]:
    return _merge_immutable_by_key(
        current,
        updates,
        lambda record: record.debate_round,
    )


def merge_reason_exchange_records(
    current: list[ReasonExchangeRecord], updates: list[ReasonExchangeRecord]
) -> list[ReasonExchangeRecord]:
    return _merge_immutable_by_key(current, updates, lambda record: record.exchange_id)


def merge_proposal_revision_records(
    current: list[ProposalRevisionRecord], updates: list[ProposalRevisionRecord]
) -> list[ProposalRevisionRecord]:
    return _merge_immutable_by_key(current, updates, lambda record: record.revision_record_id)


def merge_debate_score_records(
    current: list[DebateScoreRecord], updates: list[DebateScoreRecord]
) -> list[DebateScoreRecord]:
    return _merge_immutable_by_key(current, updates, lambda record: record.score_record_id)


def merge_negotiation_model_summaries(
    current: list[NegotiationModelRunSummary], updates: list[NegotiationModelRunSummary]
) -> list[NegotiationModelRunSummary]:
    return _merge_immutable_by_key(
        current,
        updates,
        lambda record: (record.debate_round, record.stage, record.manager),
    )


def merge_negotiation_stage_summaries(
    current: list[NegotiationStageRunSummary], updates: list[NegotiationStageRunSummary]
) -> list[NegotiationStageRunSummary]:
    return _merge_immutable_by_key(
        current,
        updates,
        lambda record: (record.debate_round, record.stage),
    )


def merge_negotiation_round_summaries(
    current: list[NegotiationRoundSummary], updates: list[NegotiationRoundSummary]
) -> list[NegotiationRoundSummary]:
    return _merge_immutable_by_key(current, updates, lambda record: record.debate_round)


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
    research_findings: Annotated[list[ResearchFinding], merge_research_findings]
    aggressive_recommendation: RecommendationRecord | None
    conservative_recommendation: RecommendationRecord | None
    independent_recommendations_finalized: bool
    consensus_recommendation: RecommendationRecord | None
    consensus_assembly_run_summary: ConsensusAssemblyRunSummary | None
    research_report: ResearchReport | None
    research_report_markdown: str | None
    aggressive_recommendation_run_summary: PortfolioRecommendationRunSummary | None
    conservative_recommendation_run_summary: PortfolioRecommendationRunSummary | None
    normalized_proposal_pool: NormalizedProposalPool | None
    proposal_normalization_run_summary: ProposalNormalizationRunSummary | None
    aggressive_cross_review: PortfolioCrossReviewRecord | None
    conservative_cross_review: PortfolioCrossReviewRecord | None
    aggressive_cross_review_run_summary: PortfolioCrossReviewRunSummary | None
    conservative_cross_review_run_summary: PortfolioCrossReviewRunSummary | None
    cross_reviewed_proposal_pool: CrossReviewedProposalPool | None
    cross_review_application_run_summary: CrossReviewApplicationRunSummary | None
    conflict_score_validation_report: ConflictScoreValidationReport | None
    cross_review_correction_run_summary: CrossReviewCorrectionRunSummary | None
    negotiation_proposal_pool: NegotiationProposalPool | None
    consensus_gate_report: ConsensusGateReport | None
    consensus_gate_reports: Annotated[list[ConsensusGateReport], merge_consensus_gate_reports]
    reason_exchange_records: Annotated[
        list[ReasonExchangeRecord], merge_reason_exchange_records
    ]
    proposal_revision_records: Annotated[
        list[ProposalRevisionRecord], merge_proposal_revision_records
    ]
    debate_score_records: Annotated[list[DebateScoreRecord], merge_debate_score_records]
    negotiation_model_run_summaries: Annotated[
        list[NegotiationModelRunSummary], merge_negotiation_model_summaries
    ]
    negotiation_stage_run_summaries: Annotated[
        list[NegotiationStageRunSummary], merge_negotiation_stage_summaries
    ]
    proposal_revision_application_summary: ProposalRevisionApplicationSummary | None
    negotiation_score_validation_report: NegotiationScoreValidationReport | None
    negotiation_round_summaries: Annotated[
        list[NegotiationRoundSummary], merge_negotiation_round_summaries
    ]
    validation_round: int
    research_request_count: int
    validation_research_request_count: int
    technical_request_count: int
    sentiment_flow_request_count: int
    fundamental_request_count: int
    event_request_count: int
    debate_round: int
    token_budget_remaining: int
    time_budget_remaining_seconds: int
    technical_run_summary: TechnicalAgentRunSummary | None
    sentiment_flow_run_summary: SentimentFlowAgentRunSummary | None
    fundamental_run_summary: FundamentalAgentRunSummary | None
    event_run_summary: EventAgentRunSummary | None
    evidence_stage_failed: bool
    evidence_collection: EvidenceCollection | None
    candidate_thesis_run_summary: CandidateThesisRunSummary | None
    active_validation_session: ThesisValidationSession | None
    active_validation_request_id: str | None
    validation_model_call_count: int
    validation_input_thesis_count: int
    validation_discovered_candidate_count: int
    validation_stop_reason: str | None
    thesis_validation_run_summary: ThesisValidationRunSummary | None
    errors: Annotated[list[str], append_errors]
