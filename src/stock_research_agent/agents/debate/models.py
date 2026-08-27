"""两位投资组合经理交叉评分、校验与纠错重试的严格数据契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.agents.portfolio.models import (
    DecisionThesisSummary,
    InitialInsistenceScore,
)
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    ThesisValidationStatus,
)
from stock_research_agent.domain.recommendation import ProposalItem, SupportScore

_ItemId = Annotated[str, Field(pattern=r"^item_[A-Za-z0-9_]+$")]
_RecommendationId = Annotated[str, Field(pattern=r"^rec_[A-Za-z0-9_]+$")]
_ThesisId = Annotated[str, Field(pattern=r"^th_[A-Za-z0-9_]+$")]

MAX_CROSS_REVIEW_ATTEMPTS = 3

ProposalNormalizationStopReason = Literal[
    "complete",
    "missing_recommendation",
    "invalid_state",
]
PortfolioCrossReviewStopReason = Literal[
    "complete",
    "missing_normalized_pool",
    "thesis_limit_exceeded",
    "context_limit_exceeded",
    "invalid_state",
    "model_error",
    "rejected_output",
]
CrossReviewApplicationStopReason = Literal[
    "complete",
    "missing_cross_review",
    "invalid_state",
]
CrossReviewCorrectionStopReason = Literal[
    "complete",
    "missing_retry_report",
    "invalid_state",
    "attempt_failed",
]
ConflictScoreRuleCode = Literal[
    "CONFLICT_GROUP_SUM_POSITIVE",
]
ConflictScoreValidationStopReason = Literal[
    "valid",
    "retry_required",
    "retry_exhausted",
    "missing_cross_reviewed_pool",
    "invalid_state",
]


class PortfolioCrossReviewLimits(DomainModel):
    """单个交叉评分节点的确定性输入与上下文硬限制。"""

    max_input_theses: int = Field(default=32, ge=1, le=64)
    max_context_characters: int = Field(default=120_000, ge=1_000, le=1_000_000)
    max_attempts: int = Field(
        default=MAX_CROSS_REVIEW_ATTEMPTS,
        ge=1,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
        description="包括首次评分在内，每位经理最多允许的模型调用次数",
    )


class NormalizedProposalPool(DomainModel):
    """两份原始建议的不可变协商副本及其确定性冲突关系。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    aggressive_recommendation_id: _RecommendationId
    conservative_recommendation_id: _RecommendationId
    proposal_items: tuple[ProposalItem, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_normalized_pool(self) -> NormalizedProposalPool:
        if self.aggressive_recommendation_id == self.conservative_recommendation_id:
            raise ValueError("规范化提案池的两份 recommendation_id 不能相同")
        _validate_pool_items(self.proposal_items, cross_reviewed=False)
        return self


class ProposalNormalizationRunSummary(DomainModel):
    """确定性规范化节点的可审计运行摘要。"""

    aggressive_recommendation_id: _RecommendationId | None = None
    conservative_recommendation_id: _RecommendationId | None = None
    input_proposal_count: int = Field(ge=0, le=32)
    output_proposal_count: int = Field(ge=0, le=32)
    conflict_pair_count: int = Field(ge=0, le=16)
    stop_reason: ProposalNormalizationStopReason

    @model_validator(mode="after")
    def validate_result(self) -> ProposalNormalizationRunSummary:
        if self.stop_reason == "complete":
            if (
                self.aggressive_recommendation_id is None
                or self.conservative_recommendation_id is None
            ):
                raise ValueError("complete 规范化摘要必须包含双方 recommendation_id")
            if self.input_proposal_count != self.output_proposal_count:
                raise ValueError("规范化不能新增或丢弃原子建议")
        elif self.output_proposal_count != 0 or self.conflict_pair_count != 0:
            raise ValueError("规范化失败时不能声称已经输出提案或冲突关系")
        return self


class CrossReviewedProposalPool(DomainModel):
    """双方本轮互评写入后、尚未经过冲突评分校验的协商提案池。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    aggressive_recommendation_id: _RecommendationId
    conservative_recommendation_id: _RecommendationId
    proposal_items: tuple[ProposalItem, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_cross_reviewed_pool(self) -> CrossReviewedProposalPool:
        if self.aggressive_recommendation_id == self.conservative_recommendation_id:
            raise ValueError("交叉评分提案池的两份 recommendation_id 不能相同")
        _validate_pool_items(self.proposal_items, cross_reviewed=True)
        return self

    def matches_normalized_source(self, source: NormalizedProposalPool) -> bool:
        """确认本池只在规范化原提案上追加了对方评分。"""

        if (
            self.run_id != source.run_id
            or self.as_of != source.as_of
            or self.research_target != source.research_target
            or self.aggressive_recommendation_id != source.aggressive_recommendation_id
            or self.conservative_recommendation_id != source.conservative_recommendation_id
            or len(self.proposal_items) != len(source.proposal_items)
        ):
            return False
        stripped_items: list[ProposalItem] = []
        for item in self.proposal_items:
            proposer_evaluations = [
                evaluation
                for evaluation in item.evaluations
                if evaluation.manager is item.proposer
            ]
            if len(proposer_evaluations) != 1:
                return False
            stripped_items.append(
                item.model_copy(
                    deep=True,
                    update={"evaluations": proposer_evaluations},
                )
            )
        return tuple(stripped_items) == source.proposal_items


class CrossReviewApplicationRunSummary(DomainModel):
    """双方交叉评分写入规范化提案池时的可审计摘要。"""

    input_proposal_count: int = Field(ge=0, le=32)
    output_proposal_count: int = Field(ge=0, le=32)
    applied_evaluation_count: int = Field(ge=0, le=32)
    aggressive_review_attempt: int = Field(
        default=0,
        ge=0,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
    )
    conservative_review_attempt: int = Field(
        default=0,
        ge=0,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
    )
    stop_reason: CrossReviewApplicationStopReason

    @model_validator(mode="after")
    def validate_result(self) -> CrossReviewApplicationRunSummary:
        if self.stop_reason == "complete":
            if self.input_proposal_count != self.output_proposal_count:
                raise ValueError("交叉评分写入不能新增或丢弃原子建议")
            if self.applied_evaluation_count != self.output_proposal_count:
                raise ValueError("每条原子建议必须恰好写入一份对方评价")
            if not self.aggressive_review_attempt or not self.conservative_review_attempt:
                raise ValueError("交叉评分写入成功时双方 attempt 都必须大于零")
        elif self.output_proposal_count != 0 or self.applied_evaluation_count != 0:
            raise ValueError("交叉评分写入失败时不能声称已经生成输出")
        return self


class ConflictScoreViolation(DomainModel):
    """一位经理在一个竞争决策槽中违反的确定性评分规则。"""

    rule_code: ConflictScoreRuleCode
    manager: PortfolioManager
    own_item_id: _ItemId
    counterpart_item_id: _ItemId
    own_support_score: SupportScore
    counterpart_support_score: SupportScore
    message: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_distinct_items(self) -> ConflictScoreViolation:
        if self.own_item_id == self.counterpart_item_id:
            raise ValueError("评分违规记录必须引用两个不同条目")
        if self.own_support_score <= 0:
            raise ValueError("冲突评分校验中的己方初始坚持分必须为正分")
        if self.own_support_score + self.counterpart_support_score <= 0:
            raise ValueError("CONFLICT_GROUP_SUM_POSITIVE 必须对应互斥建议评分和大于 0")
        return self


class ConflictScoreValidationReport(DomainModel):
    """ConflictScoreValidatorNode 对一次已汇合评分的完整审计结果。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_attempts: int = Field(
        default=MAX_CROSS_REVIEW_ATTEMPTS,
        ge=1,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
    )
    aggressive_review_attempt: int = Field(
        ge=0,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
    )
    conservative_review_attempt: int = Field(
        ge=0,
        le=MAX_CROSS_REVIEW_ATTEMPTS,
    )
    valid: bool
    invalid_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    violations: tuple[ConflictScoreViolation, ...] = Field(default=(), max_length=32)
    stop_reason: ConflictScoreValidationStopReason

    @model_validator(mode="after")
    def validate_result(self) -> ConflictScoreValidationReport:
        if len(set(self.invalid_managers)) != len(self.invalid_managers):
            raise ValueError("invalid_managers 不能重复")
        violation_keys = [
            (
                violation.manager,
                violation.own_item_id,
                violation.counterpart_item_id,
            )
            for violation in self.violations
        ]
        if len(set(violation_keys)) != len(violation_keys):
            raise ValueError("同一经理和冲突条目对只能保留一条主违规")
        violation_managers = {violation.manager for violation in self.violations}
        completed_validation = self.valid or self.stop_reason in {
            "retry_required",
            "retry_exhausted",
        }
        if completed_validation and (
            self.aggressive_review_attempt > self.max_attempts
            or self.conservative_review_attempt > self.max_attempts
        ):
            raise ValueError("完成评分校验时 review attempt 不能超过本次硬上限")
        if self.valid:
            if self.stop_reason != "valid":
                raise ValueError("valid=true 时 stop_reason 必须为 valid")
            if not self.aggressive_review_attempt or not self.conservative_review_attempt:
                raise ValueError("合法评分报告必须引用双方有效的 review attempt")
            if self.invalid_managers or self.violations:
                raise ValueError("合法评分不能同时包含违规经理或违规项")
        elif self.stop_reason in {"retry_required", "retry_exhausted"}:
            if not self.aggressive_review_attempt or not self.conservative_review_attempt:
                raise ValueError("完成评分校验前双方 review attempt 都必须大于零")
            if not self.invalid_managers or not self.violations:
                raise ValueError("评分违规时必须包含违规经理和违规项")
            if set(self.invalid_managers) != violation_managers:
                raise ValueError("invalid_managers 必须精确对应 violations 中的经理")
            attempts = {
                PortfolioManager.AGGRESSIVE: self.aggressive_review_attempt,
                PortfolioManager.CONSERVATIVE: self.conservative_review_attempt,
            }
            exhausted = any(
                attempts[manager] >= self.max_attempts for manager in self.invalid_managers
            )
            if self.stop_reason == "retry_required" and exhausted:
                raise ValueError("仍有可用重试次数时才能标记 retry_required")
            if self.stop_reason == "retry_exhausted" and not exhausted:
                raise ValueError("retry_exhausted 必须至少有一位违规经理耗尽次数")
        elif self.invalid_managers or self.violations:
            raise ValueError("输入状态错误时不能附带未经完整校验的违规结论")
        return self


class CrossReviewCorrectionRunSummary(DomainModel):
    """一次按违规经理定向重评的原子提交摘要。"""

    source_validation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    called_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    staged_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    completed_managers: tuple[PortfolioManager, ...] = Field(default=(), max_length=2)
    aggressive_attempt: int = Field(ge=0, le=MAX_CROSS_REVIEW_ATTEMPTS)
    conservative_attempt: int = Field(ge=0, le=MAX_CROSS_REVIEW_ATTEMPTS)
    stop_reason: CrossReviewCorrectionStopReason

    @model_validator(mode="after")
    def validate_atomic_result(self) -> CrossReviewCorrectionRunSummary:
        if len(set(self.requested_managers)) != len(self.requested_managers):
            raise ValueError("requested_managers 不能重复")
        if len(set(self.called_managers)) != len(self.called_managers):
            raise ValueError("called_managers 不能重复")
        if len(set(self.staged_managers)) != len(self.staged_managers):
            raise ValueError("staged_managers 不能重复")
        if len(set(self.completed_managers)) != len(self.completed_managers):
            raise ValueError("completed_managers 不能重复")
        if not set(self.staged_managers) <= set(self.called_managers):
            raise ValueError("staged_managers 必须是 called_managers 的子集")
        if not set(self.completed_managers) <= set(self.staged_managers):
            raise ValueError("completed_managers 必须是 staged_managers 的子集")
        if self.stop_reason == "complete":
            if not self.requested_managers:
                raise ValueError("纠错成功时必须至少请求一位经理")
            if not self.aggressive_attempt or not self.conservative_attempt:
                raise ValueError("纠错成功时双方 attempt 都必须大于零")
            requested = set(self.requested_managers)
            if (
                set(self.called_managers) != requested
                or set(self.staged_managers) != requested
                or set(self.completed_managers) != requested
            ):
                raise ValueError("纠错成功时必须原子完成全部被请求经理")
        elif self.stop_reason == "attempt_failed":
            if not self.requested_managers:
                raise ValueError("纠错调用失败时必须保留被请求经理")
            if not set(self.called_managers) <= set(self.requested_managers):
                raise ValueError("called_managers 只能包含已请求经理")
            if self.completed_managers:
                raise ValueError("纠错失败时不能声称已经提交部分经理的新评分")
        elif self.called_managers or self.staged_managers or self.completed_managers:
            raise ValueError("纠错尚未调用模型时不能声称已有调用或提交")
        return self


class CrossReviewProposalContext(DomainModel):
    """交叉评审时展示给经理的一条只读原子建议。"""

    item_id: _ItemId
    target: ResearchTarget
    decision_dimension: DecisionDimension
    conflict_group: str = Field(min_length=1, max_length=100)
    conflicts_with: tuple[_ItemId, ...] = Field(default=(), max_length=16)
    proposer: PortfolioManager
    proposal: str = Field(min_length=1, max_length=2_000)
    supporting_thesis_ids: tuple[_ThesisId, ...] = Field(min_length=1, max_length=16)
    proposer_insistence_score: InitialInsistenceScore = Field(
        description="原提议方在独立方案中给出的正向坚持分",
    )
    proposer_score_reason: str = Field(
        min_length=1,
        max_length=1_200,
        description="原提议方给出坚持分的理由",
    )

    @model_validator(mode="after")
    def validate_references(self) -> CrossReviewProposalContext:
        if len(set(self.supporting_thesis_ids)) != len(self.supporting_thesis_ids):
            raise ValueError("supporting_thesis_ids 不能重复")
        if self.item_id in self.conflicts_with:
            raise ValueError("交叉评审上下文中的条目不能与自己冲突")
        if len(set(self.conflicts_with)) != len(self.conflicts_with):
            raise ValueError("conflicts_with 不能重复")
        return self

    @classmethod
    def from_proposal_item(cls, item: ProposalItem) -> CrossReviewProposalContext:
        """从经理的原始条目投影出只读上下文，不暴露可变生命周期字段。"""

        if len(item.evaluations) != 1:
            raise ValueError("交叉评审前的原始条目必须且只能包含提议方初始评价")
        initial_evaluation = item.evaluations[0]
        if initial_evaluation.manager is not item.proposer:
            raise ValueError("原始条目的初始评价必须来自提议方")
        return cls(
            item_id=item.item_id,
            target=item.target,
            decision_dimension=item.decision_dimension,
            conflict_group=item.conflict_group,
            conflicts_with=tuple(item.conflicts_with),
            proposer=item.proposer,
            proposal=item.proposal,
            supporting_thesis_ids=tuple(item.supporting_thesis_ids),
            proposer_insistence_score=initial_evaluation.support_score,
            proposer_score_reason=initial_evaluation.reason,
        )


class PortfolioCrossReviewInput(DomainModel):
    """一位经理首次评价或定向纠错时得到的完整、冻结输入。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    research_target: ResearchTarget
    reviewer: PortfolioManager
    own_recommendation_id: _RecommendationId
    counterpart_recommendation_id: _RecommendationId
    attempt: int = Field(default=1, ge=1, le=MAX_CROSS_REVIEW_ATTEMPTS)
    own_proposals: tuple[CrossReviewProposalContext, ...] = Field(
        min_length=1,
        max_length=16,
        description="评审者自己的原始建议，用于识别互斥项和比较风险偏好",
    )
    counterpart_proposals: tuple[CrossReviewProposalContext, ...] = Field(
        min_length=1,
        max_length=16,
        description="本次必须逐条评分的对方原始建议",
    )
    theses: tuple[DecisionThesisSummary, ...] = Field(
        min_length=1,
        max_length=64,
        description="双方形成独立方案时共同可见的全部终态观点",
    )
    eligible_supporting_thesis_ids: tuple[_ThesisId, ...] = Field(
        min_length=1,
        max_length=64,
        description="原子建议唯一允许直接引用的 SUPPORTED/MIXED 观点 ID",
    )
    previous_evaluations: tuple[CrossReviewEvaluationDraft, ...] = Field(
        default=(),
        max_length=16,
        description="纠错重试时本经理上一次对全部对方条目的评分；首次评分为空",
    )
    validation_feedback: tuple[ConflictScoreViolation, ...] = Field(
        default=(),
        max_length=16,
        description="纠错重试时确定性校验器返回给当前经理的违规信息",
    )
    policy_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_frozen_catalog(self) -> PortfolioCrossReviewInput:
        if self.own_recommendation_id == self.counterpart_recommendation_id:
            raise ValueError("交叉评审的双方 recommendation_id 不能相同")

        counterpart = _counterpart_of(self.reviewer)
        if any(proposal.proposer is not self.reviewer for proposal in self.own_proposals):
            raise ValueError("own_proposals 必须全部来自当前 reviewer")
        if any(proposal.proposer is not counterpart for proposal in self.counterpart_proposals):
            raise ValueError("counterpart_proposals 必须全部来自对方经理")

        catalog = (*self.own_proposals, *self.counterpart_proposals)
        item_ids = [proposal.item_id for proposal in catalog]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("交叉评审输入不能包含重复 item_id")

        by_id = {proposal.item_id: proposal for proposal in catalog}
        for proposal in catalog:
            for conflict_id in proposal.conflicts_with:
                conflict = by_id.get(conflict_id)
                if conflict is None:
                    raise ValueError("conflicts_with 只能引用本次交叉评审目录中的条目")
                if conflict.proposer is proposal.proposer:
                    raise ValueError("独立方案内部不能产生 conflicts_with")
                if conflict.conflict_group != proposal.conflict_group:
                    raise ValueError("互斥条目必须属于同一 conflict_group")
                if proposal.item_id not in conflict.conflicts_with:
                    raise ValueError("conflicts_with 必须双向对称")

        thesis_ids = [thesis.thesis_id for thesis in self.theses]
        if len(set(thesis_ids)) != len(thesis_ids):
            raise ValueError("theses 中不能出现重复 thesis_id")
        completed_statuses = {
            ThesisValidationStatus.SUPPORTED,
            ThesisValidationStatus.REFUTED,
            ThesisValidationStatus.MIXED,
            ThesisValidationStatus.INCONCLUSIVE,
        }
        if any(thesis.validation_status not in completed_statuses for thesis in self.theses):
            raise ValueError("交叉评分输入只能包含已经完成查证的观点")
        expected_eligible = {
            thesis.thesis_id
            for thesis in self.theses
            if thesis.validation_status
            in {ThesisValidationStatus.SUPPORTED, ThesisValidationStatus.MIXED}
        }
        actual_eligible = set(self.eligible_supporting_thesis_ids)
        if len(actual_eligible) != len(self.eligible_supporting_thesis_ids):
            raise ValueError("eligible_supporting_thesis_ids 不能重复")
        if actual_eligible != expected_eligible:
            raise ValueError("eligible_supporting_thesis_ids 必须对应 SUPPORTED/MIXED 观点")
        for proposal in catalog:
            if not set(proposal.supporting_thesis_ids) <= actual_eligible:
                raise ValueError("提案只能引用 eligible_supporting_thesis_ids 中的观点")

        expected_counterpart_ids = {proposal.item_id for proposal in self.counterpart_proposals}
        if self.attempt == 1:
            if self.previous_evaluations or self.validation_feedback:
                raise ValueError("首次交叉评分不能携带历史评分或校验反馈")
        else:
            previous_by_id = {
                evaluation.item_id: evaluation for evaluation in self.previous_evaluations
            }
            if len(previous_by_id) != len(self.previous_evaluations):
                raise ValueError("previous_evaluations 不能重复同一 item_id")
            previous_ids = set(previous_by_id)
            if previous_ids != expected_counterpart_ids:
                raise ValueError("纠错重试必须携带上一轮全部对方条目的评分")
            if not self.validation_feedback:
                raise ValueError("纠错重试必须携带确定性校验反馈")
            if any(feedback.manager is not self.reviewer for feedback in self.validation_feedback):
                raise ValueError("validation_feedback 必须全部属于当前 reviewer")
            feedback_keys = [
                (feedback.own_item_id, feedback.counterpart_item_id)
                for feedback in self.validation_feedback
            ]
            if len(set(feedback_keys)) != len(feedback_keys):
                raise ValueError("validation_feedback 不能重复同一冲突条目对")
            own_by_id = {proposal.item_id: proposal for proposal in self.own_proposals}
            counterpart_by_id = {
                proposal.item_id: proposal for proposal in self.counterpart_proposals
            }
            for feedback in self.validation_feedback:
                own = own_by_id.get(feedback.own_item_id)
                counterpart_proposal = counterpart_by_id.get(feedback.counterpart_item_id)
                if own is None or counterpart_proposal is None:
                    raise ValueError("validation_feedback 必须引用当前双方提案目录")
                if (
                    counterpart_proposal.item_id not in own.conflicts_with
                    or own.item_id not in counterpart_proposal.conflicts_with
                ):
                    raise ValueError("validation_feedback 必须引用真实的双向冲突条目")
                if feedback.own_support_score != own.proposer_insistence_score:
                    raise ValueError("validation_feedback 的己方分数必须匹配冻结提案")
                if (
                    feedback.counterpart_support_score
                    != previous_by_id[counterpart_proposal.item_id].support_score
                ):
                    raise ValueError("validation_feedback 的对方分数必须匹配上一轮评分")
        return self


class CrossReviewEvaluationDraft(DomainModel):
    """评审者对对方一条原子建议的当前轮评分。"""

    item_id: _ItemId = Field(description="被评价的对方建议条目 ID")
    support_score: SupportScore = Field(
        description="九档支持分：-1/-0.75/-0.5/-0.25/0/0.25/0.5/0.75/1",
    )
    hard_veto: bool = Field(
        description="是否认为该建议在当前证据和风险约束下完全不可接受",
    )
    reason: str = Field(
        min_length=1,
        max_length=1_200,
        description="联系观点、收益风险和执行条件解释本次评分",
    )
    modification_suggestion: str | None = Field(
        min_length=1,
        max_length=1_200,
        description="可使该建议更可接受的具体修改；无需修改时为 null",
    )

    @model_validator(mode="after")
    def validate_hard_veto(self) -> CrossReviewEvaluationDraft:
        if self.hard_veto and self.support_score != -1.0:
            raise ValueError("hard_veto=true 时 support_score 必须为 -1.0")
        return self


class PortfolioCrossReviewDraft(DomainModel):
    """一位经理对对方全部原子建议的完整交叉评分输出。"""

    evaluations: tuple[CrossReviewEvaluationDraft, ...] = Field(
        min_length=1,
        max_length=16,
        description="对 counterpart_proposals 的逐条评价，不得评价或重写自己的条目",
    )

    @model_validator(mode="after")
    def validate_unique_items(self) -> PortfolioCrossReviewDraft:
        item_ids = [evaluation.item_id for evaluation in self.evaluations]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("交叉评分不能重复评价同一 item_id")
        return self

    def validate_against(self, review_input: PortfolioCrossReviewInput) -> None:
        """确定性核对模型是否恰好评价了每一条对方建议。"""

        expected_ids = {proposal.item_id for proposal in review_input.counterpart_proposals}
        actual_ids = {evaluation.item_id for evaluation in self.evaluations}
        if actual_ids != expected_ids:
            missing = sorted(expected_ids - actual_ids)
            unknown = sorted(actual_ids - expected_ids)
            raise ValueError(
                f"交叉评分必须恰好覆盖 counterpart_proposals；missing={missing}, unknown={unknown}"
            )


class PortfolioCrossReviewRecord(DomainModel):
    """程序绑定评审者身份、来源方案和尝试序号后保存的评分记录。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    reviewer: PortfolioManager
    attempt: int = Field(default=1, ge=1, le=MAX_CROSS_REVIEW_ATTEMPTS)
    own_recommendation_id: _RecommendationId
    counterpart_recommendation_id: _RecommendationId
    evaluations: tuple[CrossReviewEvaluationDraft, ...] = Field(
        min_length=1,
        max_length=16,
    )
    previous_evaluations: tuple[CrossReviewEvaluationDraft, ...] = Field(
        default=(),
        max_length=16,
    )
    correction_feedback: tuple[ConflictScoreViolation, ...] = Field(
        default=(),
        max_length=16,
    )
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> PortfolioCrossReviewRecord:
        if self.own_recommendation_id == self.counterpart_recommendation_id:
            raise ValueError("交叉评分记录的双方 recommendation_id 不能相同")
        item_ids = [evaluation.item_id for evaluation in self.evaluations]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("交叉评分记录不能重复评价同一 item_id")
        if self.attempt == 1:
            if self.previous_evaluations or self.correction_feedback:
                raise ValueError("首次交叉评分记录不能携带纠错历史")
        else:
            previous_ids = [evaluation.item_id for evaluation in self.previous_evaluations]
            if len(set(previous_ids)) != len(previous_ids):
                raise ValueError("纠错评分记录的历史评分不能重复 item_id")
            if set(previous_ids) != set(item_ids):
                raise ValueError("纠错评分记录必须保留上一轮全部对方评分")
            if not self.correction_feedback:
                raise ValueError("纠错评分记录必须保留确定性校验反馈")
            if any(feedback.manager is not self.reviewer for feedback in self.correction_feedback):
                raise ValueError("correction_feedback 必须属于当前 reviewer")
            feedback_keys = [
                (feedback.own_item_id, feedback.counterpart_item_id)
                for feedback in self.correction_feedback
            ]
            if len(set(feedback_keys)) != len(feedback_keys):
                raise ValueError("correction_feedback 不能重复同一冲突条目对")
            if any(
                feedback.counterpart_item_id not in set(item_ids)
                for feedback in self.correction_feedback
            ):
                raise ValueError("correction_feedback 必须引用当前记录中的对方条目")
        return self


class PortfolioCrossReviewRunSummary(DomainModel):
    """一位经理一次交叉评分或纠错调用的可审计运行摘要。"""

    reviewer: PortfolioManager
    attempt: int = Field(default=1, ge=1, le=MAX_CROSS_REVIEW_ATTEMPTS)
    own_proposal_count: int = Field(ge=0, le=16)
    counterpart_proposal_count: int = Field(ge=0, le=16)
    input_thesis_count: int = Field(ge=0, le=64)
    context_character_count: int = Field(ge=0)
    model_called: bool
    evaluation_count: int = Field(ge=0, le=16)
    stop_reason: PortfolioCrossReviewStopReason

    @model_validator(mode="after")
    def validate_call_result(self) -> PortfolioCrossReviewRunSummary:
        if not self.model_called and self.evaluation_count:
            raise ValueError("未调用模型时不能产生交叉评分")
        if self.stop_reason == "complete":
            if not self.model_called:
                raise ValueError("complete 必须来自一次模型调用")
            if self.evaluation_count != self.counterpart_proposal_count:
                raise ValueError("complete 必须逐条覆盖对方提案")
        return self


def _counterpart_of(manager: PortfolioManager) -> PortfolioManager:
    if manager is PortfolioManager.AGGRESSIVE:
        return PortfolioManager.CONSERVATIVE
    return PortfolioManager.AGGRESSIVE


def _validate_pool_items(
    items: tuple[ProposalItem, ...],
    *,
    cross_reviewed: bool,
) -> None:
    item_ids = [item.item_id for item in items]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("提案池不能包含重复 item_id")

    decision_slots: set[tuple[str, PortfolioManager]] = set()
    by_id = {item.item_id: item for item in items}
    all_managers = {PortfolioManager.AGGRESSIVE, PortfolioManager.CONSERVATIVE}
    for item in items:
        expected_group = (
            f"{item.target.type.value}:{item.target.code.upper()}:{item.decision_dimension.value}"
        )
        if item.conflict_group != expected_group:
            raise ValueError("提案池包含非规范化 conflict_group")
        slot = (item.conflict_group, item.proposer)
        if slot in decision_slots:
            raise ValueError("同一经理在同一 conflict_group 中只能保留一条建议")
        decision_slots.add(slot)
        if item.revision != 1 or item.status is not ProposalStatus.PROPOSED:
            raise ValueError("当前阶段只能接收 revision=1 的 PROPOSED 原始条目")
        if item.arbitration is not None:
            raise ValueError("当前阶段的条目不能提前包含 arbitration")

        evaluation_managers = [evaluation.manager for evaluation in item.evaluations]
        expected_managers = all_managers if cross_reviewed else {item.proposer}
        if set(evaluation_managers) != expected_managers:
            raise ValueError("提案池中的评价人集合与当前阶段不匹配")
        if item.evaluations[0].manager is not item.proposer:
            raise ValueError("第一份评价必须来自原提议方")
        if cross_reviewed and len(item.evaluations) != 2:
            raise ValueError("交叉评分完成后每条建议必须恰好包含两份评价")
        if not cross_reviewed and len(item.evaluations) != 1:
            raise ValueError("规范化阶段必须只保留提议方的初始评价")

        expected_conflicts = {
            candidate.item_id
            for candidate in items
            if candidate.proposer is not item.proposer
            and candidate.conflict_group == item.conflict_group
        }
        if set(item.conflicts_with) != expected_conflicts:
            raise ValueError("conflicts_with 必须精确对应另一位经理的同决策槽条目")
        for conflict_id in item.conflicts_with:
            conflict = by_id.get(conflict_id)
            if conflict is None or item.item_id not in conflict.conflicts_with:
                raise ValueError("conflicts_with 必须引用有效条目且双向对称")
