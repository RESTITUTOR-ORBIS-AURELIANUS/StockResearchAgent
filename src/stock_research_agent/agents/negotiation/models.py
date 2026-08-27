"""共识门与正式协商阶段使用的严格数据契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.agents.debate import CrossReviewedProposalPool
from stock_research_agent.agents.portfolio import DecisionThesisSummary
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    ConsensusItemOutcome,
    ConsensusRoute,
    DecisionDimension,
    NegotiationArgumentType,
    NegotiationStance,
    PortfolioManager,
    ProposalRevisionAction,
    ProposalStatus,
)
from stock_research_agent.domain.recommendation import (
    ProposalItem,
    SupportScore,
)

_ItemId = Annotated[str, Field(pattern=r"^item_[A-Za-z0-9_]+$")]
_ThesisId = Annotated[str, Field(pattern=r"^th_[A-Za-z0-9_]+$")]
_Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

MAX_DEBATE_ROUNDS = 3
REQUIRED_DECISION_DIMENSIONS = frozenset(
    {
        DecisionDimension.ACTION,
        DecisionDimension.HORIZON,
        DecisionDimension.RISK_CONTROL,
    }
)

NegotiationModelStopReason = Literal[
    "complete",
    "no_work",
    "missing_input",
    "context_limit_exceeded",
    "invalid_state",
    "model_error",
    "rejected_output",
]
NegotiationStage = Literal["REASON_EXCHANGE", "PROPOSAL_REVISION", "DEBATE_SCORE"]
NegotiationStageStopReason = Literal["complete", "no_work", "invalid_state", "stage_failed"]
NegotiationRoundStopReason = Literal[
    "complete",
    "no_material_change",
    "invalid_state",
    "stage_failed",
]
NegotiationScoreValidationStopReason = Literal[
    "valid",
    "missing_pool",
    "invalid_state",
    "invalid_scores",
]


def counterpart_of(manager: PortfolioManager) -> PortfolioManager:
    if manager is PortfolioManager.AGGRESSIVE:
        return PortfolioManager.CONSERVATIVE
    return PortfolioManager.AGGRESSIVE


class NegotiationLimits(DomainModel):
    """正式协商的轮次、观点和上下文硬限制。"""

    max_rounds: int = Field(default=MAX_DEBATE_ROUNDS, ge=1, le=MAX_DEBATE_ROUNDS)
    max_input_theses: int = Field(default=32, ge=1, le=64)
    max_context_characters: int = Field(default=120_000, ge=1_000, le=1_000_000)


class NegotiationProposalPool(DomainModel):
    """当前正式协商版本；原始建议和首评提案池始终保持不变。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    aggressive_recommendation_id: str = Field(pattern=r"^rec_[A-Za-z0-9_]+$")
    conservative_recommendation_id: str = Field(pattern=r"^rec_[A-Za-z0-9_]+$")
    proposal_items: tuple[ProposalItem, ...] = Field(min_length=2, max_length=32)

    @classmethod
    def from_cross_reviewed(cls, source: CrossReviewedProposalPool) -> NegotiationProposalPool:
        return cls(
            run_id=source.run_id,
            as_of=source.as_of,
            research_target=source.research_target,
            aggressive_recommendation_id=source.aggressive_recommendation_id,
            conservative_recommendation_id=source.conservative_recommendation_id,
            proposal_items=tuple(item.model_copy(deep=True) for item in source.proposal_items),
        )

    @model_validator(mode="after")
    def validate_pool(self) -> NegotiationProposalPool:
        if self.aggressive_recommendation_id == self.conservative_recommendation_id:
            raise ValueError("正式协商池的两份 recommendation_id 不能相同")
        item_ids = [item.item_id for item in self.proposal_items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("正式协商池不能包含重复 item_id")
        by_id = {item.item_id: item for item in self.proposal_items}
        slots: set[tuple[str, PortfolioManager]] = set()
        for item in self.proposal_items:
            expected_group = (
                f"{item.target.type.value}:{item.target.code.upper()}:"
                f"{item.decision_dimension.value}"
            )
            if item.conflict_group != expected_group:
                raise ValueError("正式协商池包含非规范化 conflict_group")
            slot = (item.conflict_group, item.proposer)
            if slot in slots:
                raise ValueError("同一经理在同一 conflict_group 只能保留一个条目")
            slots.add(slot)
            if item.arbitration is not None:
                raise ValueError("v1 正式协商池不能包含 arbitration")
            evaluation_managers = [evaluation.manager for evaluation in item.evaluations]
            if evaluation_managers != [item.proposer, counterpart_of(item.proposer)]:
                raise ValueError("正式协商条目必须按提议方、对方顺序保留两份当前评价")
            if item.status not in {
                ProposalStatus.PROPOSED,
                ProposalStatus.NEGOTIATING,
                ProposalStatus.AGREED,
                ProposalStatus.REJECTED,
                ProposalStatus.WITHDRAWN,
                ProposalStatus.EXCLUDED,
            }:
                raise ValueError("正式协商池包含当前阶段不允许的条目状态")
            proposer_score = item.evaluations[0].support_score
            if item.status not in {
                ProposalStatus.REJECTED,
                ProposalStatus.WITHDRAWN,
                ProposalStatus.EXCLUDED,
            }:
                if proposer_score <= 0:
                    raise ValueError("仍存活的建议必须由原提议方保持正向支持；否则应撤回")
            for conflict_id in item.conflicts_with:
                conflict = by_id.get(conflict_id)
                if conflict is None or item.item_id not in conflict.conflicts_with:
                    raise ValueError("正式协商池的 conflicts_with 必须有效且双向对称")
                if conflict.conflict_group != item.conflict_group:
                    raise ValueError("互斥条目必须属于同一 conflict_group")
                if conflict.proposer is item.proposer:
                    raise ValueError("同一经理的条目不能互斥")
        agreed_ids = {
            item.item_id for item in self.proposal_items if item.status is ProposalStatus.AGREED
        }
        if any(
            conflict_id in agreed_ids
            for item in self.proposal_items
            if item.item_id in agreed_ids
            for conflict_id in item.conflicts_with
        ):
            raise ValueError("两个互斥条目不能同时处于 AGREED")
        return self


class ConsensusGateItemDecision(DomainModel):
    """确定性共识门对一条建议作出的结论。"""

    item_id: _ItemId
    item_revision: int = Field(ge=1)
    aggressive_score: SupportScore
    conservative_score: SupportScore
    combined_score: float = Field(ge=-2, le=2)
    minimum_score: SupportScore
    hard_veto: bool
    outcome: ConsensusItemOutcome
    reason_codes: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_derived_scores(self) -> ConsensusGateItemDecision:
        if self.combined_score != self.aggressive_score + self.conservative_score:
            raise ValueError("combined_score 必须等于双方评分之和")
        if self.minimum_score != min(self.aggressive_score, self.conservative_score):
            raise ValueError("minimum_score 必须等于双方较低分")
        return self


class ConsensusGateReport(DomainModel):
    """一次共识门判断的完整可审计结果。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=0, le=MAX_DEBATE_ROUNDS)
    max_rounds: int = Field(default=MAX_DEBATE_ROUNDS, ge=1, le=MAX_DEBATE_ROUNDS)
    source_fingerprint: _Fingerprint
    item_decisions: tuple[ConsensusGateItemDecision, ...] = Field(min_length=1, max_length=32)
    agreed_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    negotiating_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    rejected_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    withdrawn_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    excluded_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    missing_required_dimensions: tuple[DecisionDimension, ...] = Field(default=(), max_length=3)
    all_required_dimensions_resolved: bool
    route: ConsensusRoute

    @model_validator(mode="after")
    def validate_catalogs(self) -> ConsensusGateReport:
        by_outcome = {
            ConsensusItemOutcome.AGREED: set(self.agreed_item_ids),
            ConsensusItemOutcome.NEGOTIATING: set(self.negotiating_item_ids),
            ConsensusItemOutcome.REJECTED: set(self.rejected_item_ids),
            ConsensusItemOutcome.WITHDRAWN: set(self.withdrawn_item_ids),
            ConsensusItemOutcome.EXCLUDED: set(self.excluded_item_ids),
        }
        decision_ids = [decision.item_id for decision in self.item_decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("共识门不能重复判断同一 item_id")
        for outcome, expected_ids in by_outcome.items():
            actual_ids = {
                decision.item_id
                for decision in self.item_decisions
                if decision.outcome is outcome
            }
            if actual_ids != expected_ids:
                raise ValueError(f"{outcome.value} 条目目录与 item_decisions 不一致")
        if self.all_required_dimensions_resolved != (not self.missing_required_dimensions):
            raise ValueError("必需维度布尔值必须与缺失维度目录一致")
        if self.excluded_item_ids and self.debate_round < self.max_rounds:
            raise ValueError("只有耗尽正式协商轮次后才能排除未决条目")
        if self.route is ConsensusRoute.ASSEMBLE:
            if self.negotiating_item_ids:
                raise ValueError("ASSEMBLE 不能仍含待协商条目")
        elif self.route is ConsensusRoute.NEGOTIATE:
            if (
                not self.negotiating_item_ids
                or self.excluded_item_ids
                or self.debate_round >= self.max_rounds
            ):
                raise ValueError("NEGOTIATE 必须有待协商条目且尚未耗尽轮次")
        return self


class NegotiationArgumentDraft(DomainModel):
    argument_type: NegotiationArgumentType
    content: str = Field(min_length=1, max_length=2_000)
    supporting_thesis_ids: tuple[_ThesisId, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_theses(self) -> NegotiationArgumentDraft:
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("理由引用的 supporting_thesis_ids 不能重复")
        return self


class NegotiationArgument(NegotiationArgumentDraft):
    argument_id: str = Field(pattern=r"^arg_[A-Za-z0-9_]+$")


class ReasonExchangeItemDraft(DomainModel):
    counterpart_item_id: _ItemId
    counterpart_revision: int = Field(ge=1)
    related_own_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=4)
    stance: NegotiationStance
    arguments: tuple[NegotiationArgumentDraft, ...] = Field(min_length=1, max_length=8)
    modification_suggestion: str | None = Field(default=None, min_length=1, max_length=2_000)


class ReasonExchangeDraft(DomainModel):
    responses: tuple[ReasonExchangeItemDraft, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_unique_items(self) -> ReasonExchangeDraft:
        item_ids = [response.counterpart_item_id for response in self.responses]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("理由交换不能重复回应同一条目")
        return self


class ReasonExchangeItem(DomainModel):
    counterpart_item_id: _ItemId
    counterpart_revision: int = Field(ge=1)
    conflict_group: str = Field(min_length=1, max_length=100)
    related_own_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=4)
    stance: NegotiationStance
    arguments: tuple[NegotiationArgument, ...] = Field(min_length=1, max_length=8)
    modification_suggestion: str | None = Field(default=None, min_length=1, max_length=2_000)


class ReasonExchangeRecord(DomainModel):
    exchange_id: str = Field(pattern=r"^exchange_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    reviewer: PortfolioManager
    source_fingerprint: _Fingerprint
    responses: tuple[ReasonExchangeItem, ...] = Field(default=(), max_length=16)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_items(self) -> ReasonExchangeRecord:
        item_ids = [response.counterpart_item_id for response in self.responses]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("理由交换记录不能重复回应同一条目")
        argument_ids = [
            argument.argument_id
            for response in self.responses
            for argument in response.arguments
        ]
        if len(set(argument_ids)) != len(argument_ids):
            raise ValueError("理由交换记录中的 argument_id 不能重复")
        return self


class ReasonExchangeInput(DomainModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    reviewer: PortfolioManager
    own_proposals: tuple[ProposalItem, ...] = Field(default=(), max_length=16)
    counterpart_proposals: tuple[ProposalItem, ...] = Field(default=(), max_length=16)
    theses: tuple[DecisionThesisSummary, ...] = Field(default=(), max_length=64)
    prior_exchanges: tuple[ReasonExchangeRecord, ...] = Field(default=(), max_length=12)
    policy_notes: tuple[str, ...] = ()


class ProposalRevisionDecisionDraft(DomainModel):
    item_id: _ItemId
    decision: ProposalRevisionAction
    responding_to_argument_ids: tuple[str, ...] = Field(default=(), max_length=16)
    revised_proposal: str | None = Field(default=None, min_length=1, max_length=2_000)
    revised_supporting_thesis_ids: tuple[_ThesisId, ...] | None = Field(
        default=None,
        max_length=16,
    )
    revision_reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_action_payload(self) -> ProposalRevisionDecisionDraft:
        if len(set(self.responding_to_argument_ids)) != len(self.responding_to_argument_ids):
            raise ValueError("responding_to_argument_ids 不能重复")
        if self.decision is ProposalRevisionAction.MODIFY:
            if self.revised_proposal is None or not self.revised_supporting_thesis_ids:
                raise ValueError("MODIFY 必须提供新正文和非空支持观点目录")
            if len(set(self.revised_supporting_thesis_ids)) != len(
                self.revised_supporting_thesis_ids
            ):
                raise ValueError("修订后的 supporting_thesis_ids 不能重复")
        elif self.revised_proposal is not None or self.revised_supporting_thesis_ids is not None:
            raise ValueError("KEEP/WITHDRAW 不能携带修订正文或修订观点目录")
        return self


class ProposalRevisionDraft(DomainModel):
    decisions: tuple[ProposalRevisionDecisionDraft, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_unique_items(self) -> ProposalRevisionDraft:
        item_ids = [decision.item_id for decision in self.decisions]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("提案修订不能重复处理同一 item_id")
        return self


class ProposalRevisionInput(DomainModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    proposer: PortfolioManager
    own_proposals: tuple[ProposalItem, ...] = Field(default=(), max_length=16)
    incoming_exchange: ReasonExchangeRecord
    theses: tuple[DecisionThesisSummary, ...] = Field(default=(), max_length=64)
    prior_revisions: tuple[ProposalRevisionRecord, ...] = Field(default=(), max_length=6)
    policy_notes: tuple[str, ...] = ()


class ProposalRevisionSnapshot(DomainModel):
    revision: int = Field(ge=1)
    proposal: str = Field(min_length=1, max_length=2_000)
    supporting_thesis_ids: tuple[_ThesisId, ...] = Field(min_length=1, max_length=16)
    status: ProposalStatus

    @model_validator(mode="after")
    def validate_unique_theses(self) -> ProposalRevisionSnapshot:
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("修订快照中的 supporting_thesis_ids 不能重复")
        return self


class ProposalRevisionDecision(DomainModel):
    item_id: _ItemId
    conflict_group: str = Field(min_length=1, max_length=100)
    decision: ProposalRevisionAction
    responding_to_argument_ids: tuple[str, ...] = Field(default=(), max_length=16)
    before: ProposalRevisionSnapshot
    after: ProposalRevisionSnapshot
    revision_reason: str = Field(min_length=1, max_length=2_000)
    changed_fields: tuple[Literal["proposal", "supporting_thesis_ids", "status"], ...] = ()
    material_change: bool

    @model_validator(mode="after")
    def validate_change(self) -> ProposalRevisionDecision:
        if len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("changed_fields 不能重复")
        changed = set(self.changed_fields)
        actual: set[str] = set()
        if self.before.proposal != self.after.proposal:
            actual.add("proposal")
        if set(self.before.supporting_thesis_ids) != set(self.after.supporting_thesis_ids):
            actual.add("supporting_thesis_ids")
        if self.before.status is not self.after.status:
            actual.add("status")
        if changed != actual:
            raise ValueError("changed_fields 必须精确对应前后快照的实质变化")
        if self.material_change != bool(actual):
            raise ValueError("material_change 必须精确对应实质变化")
        if self.before.status is not ProposalStatus.NEGOTIATING:
            raise ValueError("正式协商只能修订 NEGOTIATING 条目")
        if self.decision is ProposalRevisionAction.KEEP:
            if actual or self.after.revision != self.before.revision:
                raise ValueError("KEEP 必须完整保留原提案和 revision")
        if self.decision is ProposalRevisionAction.MODIFY:
            if not actual & {"proposal", "supporting_thesis_ids"}:
                raise ValueError("MODIFY 必须改变正文或支持观点集合")
            if "status" in actual or self.after.status is not ProposalStatus.NEGOTIATING:
                raise ValueError("MODIFY 不能改变条目状态")
            if self.after.revision != self.before.revision + 1:
                raise ValueError("MODIFY 必须恰好增加一个 revision")
        if self.decision is ProposalRevisionAction.WITHDRAW:
            if self.after.status is not ProposalStatus.WITHDRAWN:
                raise ValueError("WITHDRAW 必须把状态改为 WITHDRAWN")
            if actual != {"status"}:
                raise ValueError("WITHDRAW 只能改变 status，不能改写正文或观点引用")
            if self.after.revision != self.before.revision + 1:
                raise ValueError("WITHDRAW 必须恰好增加一个 revision")
        return self


class ProposalRevisionRecord(DomainModel):
    revision_record_id: str = Field(pattern=r"^revision_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    proposer: PortfolioManager
    source_exchange_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    source_fingerprint: _Fingerprint
    decisions: tuple[ProposalRevisionDecision, ...] = Field(default=(), max_length=16)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_sources_and_items(self) -> ProposalRevisionRecord:
        if len(set(self.source_exchange_ids)) != len(self.source_exchange_ids):
            raise ValueError("source_exchange_ids 不能重复")
        item_ids = [decision.item_id for decision in self.decisions]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("修订记录不能重复处理同一条目")
        return self


class DebateScoreEntryDraft(DomainModel):
    item_id: _ItemId
    item_revision: int = Field(ge=1)
    support_score: SupportScore
    hard_veto: bool = False
    reason: str = Field(min_length=1, max_length=1_200)
    modification_suggestion: str | None = Field(default=None, min_length=1, max_length=1_200)
    score_change_reason: str = Field(min_length=1, max_length=1_200)

    @model_validator(mode="after")
    def validate_veto(self) -> DebateScoreEntryDraft:
        if self.hard_veto and self.support_score != -1.0:
            raise ValueError("hard_veto=true 时 support_score 必须为 -1.0")
        return self


class DebateScoreDraft(DomainModel):
    evaluations: tuple[DebateScoreEntryDraft, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_unique_items(self) -> DebateScoreDraft:
        item_ids = [evaluation.item_id for evaluation in self.evaluations]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("正式重评不能重复评分同一 item_id")
        return self


class DebateScoreInput(DomainModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    manager: PortfolioManager
    items_to_score: tuple[ProposalItem, ...] = Field(default=(), max_length=32)
    active_proposal_pool: tuple[ProposalItem, ...] = Field(min_length=1, max_length=32)
    source_revision_records: tuple[ProposalRevisionRecord, ...] = Field(min_length=1, max_length=2)
    theses: tuple[DecisionThesisSummary, ...] = Field(default=(), max_length=64)
    policy_notes: tuple[str, ...] = ()


class DebateScoreEntry(DomainModel):
    item_id: _ItemId
    item_revision: int = Field(ge=1)
    previous_score: SupportScore
    support_score: SupportScore
    hard_veto: bool
    reason: str = Field(min_length=1, max_length=1_200)
    modification_suggestion: str | None = Field(default=None, min_length=1, max_length=1_200)
    score_change_reason: str = Field(min_length=1, max_length=1_200)
    trigger_revision_record_id: str = Field(pattern=r"^revision_[A-Za-z0-9_]+$")

    @model_validator(mode="after")
    def validate_veto(self) -> DebateScoreEntry:
        if self.hard_veto and self.support_score != -1.0:
            raise ValueError("hard_veto=true 时 support_score 必须为 -1.0")
        return self


class DebateScoreRecord(DomainModel):
    score_record_id: str = Field(pattern=r"^score_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    manager: PortfolioManager
    source_revision_record_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    source_fingerprint: _Fingerprint
    evaluations: tuple[DebateScoreEntry, ...] = Field(min_length=1, max_length=32)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_unique_sources_and_items(self) -> DebateScoreRecord:
        if len(set(self.source_revision_record_ids)) != len(
            self.source_revision_record_ids
        ):
            raise ValueError("source_revision_record_ids 不能重复")
        item_ids = [evaluation.item_id for evaluation in self.evaluations]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("正式重评记录不能重复评分同一条目")
        return self


class NegotiationModelRunSummary(DomainModel):
    stage: NegotiationStage
    manager: PortfolioManager
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    input_item_count: int = Field(ge=0, le=32)
    output_item_count: int = Field(ge=0, le=32)
    context_character_count: int = Field(ge=0)
    model_called: bool
    stop_reason: NegotiationModelStopReason


class NegotiationStageRunSummary(DomainModel):
    stage: NegotiationStage
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    source_fingerprint: _Fingerprint
    requested_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    called_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    staged_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    completed_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    stop_reason: NegotiationStageStopReason

    @model_validator(mode="after")
    def validate_atomic_stages(self) -> NegotiationStageRunSummary:
        requested = set(self.requested_managers)
        called = set(self.called_managers)
        staged = set(self.staged_managers)
        completed = set(self.completed_managers)
        if not completed <= staged <= called <= requested:
            raise ValueError("阶段集合必须满足 completed ⊆ staged ⊆ called ⊆ requested")
        if self.stop_reason in {"complete", "no_work"} and not (
            requested == called == staged == completed
        ):
            raise ValueError("成功或无工作阶段的四组经理必须完全相等")
        if self.stop_reason == "complete" and not requested:
            raise ValueError("complete 阶段必须实际处理至少一位经理")
        if self.stop_reason == "no_work" and requested:
            raise ValueError("no_work 阶段不能声称请求了经理")
        if self.stop_reason == "stage_failed" and completed:
            raise ValueError("原子阶段失败时不能提交任何经理结果")
        return self


class ProposalRevisionApplicationSummary(DomainModel):
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    source_fingerprint: _Fingerprint
    material_change_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    withdrawn_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    touched_conflict_groups: tuple[str, ...] = Field(default=(), max_length=32)
    rescore_item_ids: tuple[_ItemId, ...] = Field(default=(), max_length=32)
    stop_reason: Literal["complete", "no_material_change", "invalid_state"]

    @model_validator(mode="after")
    def validate_result(self) -> ProposalRevisionApplicationSummary:
        catalogs = (
            self.material_change_item_ids,
            self.withdrawn_item_ids,
            self.touched_conflict_groups,
            self.rescore_item_ids,
        )
        if any(len(set(catalog)) != len(catalog) for catalog in catalogs):
            raise ValueError("修订应用摘要中的目录不能重复")
        if self.stop_reason == "complete" and not self.material_change_item_ids:
            raise ValueError("complete 修订应用摘要必须包含实质变化")
        if self.stop_reason != "complete" and any(catalogs):
            raise ValueError("无变化或无效摘要不能声称存在修订影响目录")
        return self


class NegotiationScoreViolation(DomainModel):
    manager: PortfolioManager
    left_item_id: _ItemId
    right_item_id: _ItemId
    left_score: SupportScore
    right_score: SupportScore
    message: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_violation(self) -> NegotiationScoreViolation:
        if self.left_item_id == self.right_item_id:
            raise ValueError("互斥评分违规必须引用两个不同条目")
        if self.left_score + self.right_score <= 0:
            raise ValueError("正式协商评分违规必须对应评分和大于 0")
        return self


class NegotiationScoreValidationReport(DomainModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    source_fingerprint: _Fingerprint
    valid: bool
    violations: tuple[NegotiationScoreViolation, ...] = Field(default=(), max_length=32)
    stop_reason: NegotiationScoreValidationStopReason

    @model_validator(mode="after")
    def validate_result(self) -> NegotiationScoreValidationReport:
        if self.valid:
            if self.stop_reason != "valid" or self.violations:
                raise ValueError("valid 报告不能包含违规且 stop_reason 必须为 valid")
        elif self.stop_reason == "invalid_scores" and not self.violations:
            raise ValueError("invalid_scores 必须包含具体违规")
        elif self.stop_reason != "invalid_scores" and self.violations:
            raise ValueError("输入状态失败不能附带未经完整校验的违规")
        return self


class NegotiationRoundSummary(DomainModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    debate_round: int = Field(ge=1, le=MAX_DEBATE_ROUNDS)
    source_gate_fingerprint: _Fingerprint
    exchanged_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    revised_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    scored_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    material_change_count: int = Field(ge=0, le=32)
    stop_reason: NegotiationRoundStopReason
