"""投资论点审查员的连续查证会话输入与结构化输出。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.agents.strategist.models import CandidateThesisDraft
from stock_research_agent.domain import (
    CollectedEvidenceSummary,
    ResearchFinding,
    ResearchRequest,
    ResearchTarget,
    ThesisRecord,
    TimeRange,
)
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchFindingOutcome,
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    ThesisValidationStatus,
)

_EvidenceId = Annotated[str, Field(pattern=r"^ev_[A-Za-z0-9_]+$")]
_RequestFingerprint = Annotated[str, Field(pattern=r"^rqfp_[a-f0-9]{16,64}$")]


class ThesisValidationAction(StrEnum):
    """审查员每轮只能选择一个动作。"""

    REQUEST_RESEARCH = "REQUEST_RESEARCH"
    FINALIZE = "FINALIZE"


class ValidationResearchRequestDraft(DomainModel):
    """LLM 描述本轮唯一补证需求；ID、状态与指纹由程序装配。"""

    target: ResearchTarget
    assigned_domain: EvidenceDomain
    question: str = Field(
        min_length=1,
        max_length=500,
        description="一个能够改变当前观点判断的具体问题",
    )
    requested_evidence: str = Field(
        min_length=1,
        max_length=1_200,
        description="同时说明希望寻找的支持事实和反向事实，不得预写答案",
    )
    time_range: TimeRange
    priority: ResearchPriority = ResearchPriority.MEDIUM
    rationale: str = Field(
        min_length=1,
        max_length=800,
        description="说明该请求将如何区分当前的竞争性解释",
    )
    novelty_explanation: str = Field(
        min_length=1,
        max_length=500,
        description="说明它为何不等价于 previous_turns 中已经执行的请求",
    )

    @model_validator(mode="after")
    def validate_current_executor_coverage(self) -> "ValidationResearchRequestDraft":
        if self.assigned_domain is EvidenceDomain.MACRO:
            raise ValueError("当前版本尚无可执行的 MACRO 定向查证路由")
        stock_only_domains = {
            EvidenceDomain.FUNDAMENTAL,
            EvidenceDomain.EVENT,
            EvidenceDomain.SENTIMENT_FLOW,
        }
        if self.assigned_domain in stock_only_domains and self.target.type is not TargetType.STOCK:
            raise ValueError("基本面、新闻事件和情绪资金的定向查证当前只接受 STOCK")
        return self


class ThesisFinalizationDraft(DomainModel):
    """完成当前观点查证时由模型给出的判断草稿。"""

    final_status: Literal[
        ThesisValidationStatus.SUPPORTED,
        ThesisValidationStatus.REFUTED,
        ThesisValidationStatus.MIXED,
        ThesisValidationStatus.INCONCLUSIVE,
    ]
    confidence: float = Field(
        ge=0,
        le=1,
        description="对 final_status 判断可靠性的确信程度，不是上涨概率",
    )
    supporting_evidence_ids: tuple[_EvidenceId, ...] = Field(default=(), max_length=64)
    contradicting_evidence_ids: tuple[_EvidenceId, ...] = Field(default=(), max_length=64)
    reasoning_summary: str = Field(
        min_length=1,
        max_length=4_000,
        description="区分事实、推断和不确定性的最终审查理由",
    )
    remaining_questions: tuple[str, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def validate_finalization(self) -> "ThesisFinalizationDraft":
        final_statuses = {
            ThesisValidationStatus.SUPPORTED,
            ThesisValidationStatus.REFUTED,
            ThesisValidationStatus.MIXED,
            ThesisValidationStatus.INCONCLUSIVE,
        }
        if self.final_status not in final_statuses:
            raise ValueError("final_status 必须是已完成查证的四种状态之一")

        supporting = set(self.supporting_evidence_ids)
        contradicting = set(self.contradicting_evidence_ids)
        if len(supporting) != len(self.supporting_evidence_ids):
            raise ValueError("supporting_evidence_ids 不能重复")
        if len(contradicting) != len(self.contradicting_evidence_ids):
            raise ValueError("contradicting_evidence_ids 不能重复")
        if supporting & contradicting:
            raise ValueError("同一 evidence_id 不能同时支持和反驳当前观点")
        if not supporting and not contradicting:
            raise ValueError("最终判断必须至少引用一条当前上下文中的真实证据")
        return self


class ValidationResearchTurn(DomainModel):
    """连续会话中已经完成的一轮“请求 → 查证响应”。"""

    round_number: int = Field(ge=1)
    request_fingerprint: _RequestFingerprint
    request: ResearchRequest
    finding: ResearchFinding
    reviewer_reasoning_before_request: str = Field(min_length=1, max_length=2_500)

    @model_validator(mode="after")
    def validate_request_finding_pair(self) -> "ValidationResearchTurn":
        if self.finding.request_id != self.request.request_id:
            raise ValueError("finding.request_id 必须对应当前 request")
        if self.finding.run_id != self.request.run_id:
            raise ValueError("finding.run_id 必须对应当前 request")
        if self.finding.thesis_id != self.request.thesis_id:
            raise ValueError("finding.thesis_id 必须对应当前 request")
        if self.finding.target != self.request.target:
            raise ValueError("finding.target 必须对应当前 request")
        if self.finding.assigned_domain is not self.request.assigned_domain:
            raise ValueError("finding.assigned_domain 必须对应当前 request")
        if self.finding.attempt != self.request.attempt:
            raise ValueError("finding.attempt 必须对应当前 request")
        if self.round_number != self.request.attempt:
            raise ValueError("round_number 必须对应 request.attempt")
        if self.finding.created_at != self.request.completed_at:
            raise ValueError("finding.created_at 必须对应 request.completed_at")
        expected_statuses = {
            ResearchFindingOutcome.EVIDENCE_FOUND: ResearchRequestStatus.COMPLETED,
            ResearchFindingOutcome.NO_MATCHING_EVIDENCE: (ResearchRequestStatus.NO_NEW_EVIDENCE),
            ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE: (ResearchRequestStatus.FAILED),
            ResearchFindingOutcome.SOURCE_UNAVAILABLE: ResearchRequestStatus.FAILED,
            ResearchFindingOutcome.REQUEST_FAILED: ResearchRequestStatus.FAILED,
            ResearchFindingOutcome.BUDGET_EXHAUSTED: (ResearchRequestStatus.CANCELLED_BY_BUDGET),
        }
        if self.request.status is not expected_statuses[self.finding.outcome]:
            raise ValueError("request.status 与 finding.outcome 不一致")
        if self.finding.outcome is ResearchFindingOutcome.EVIDENCE_FOUND:
            if set(self.finding.evidence_ids) != set(self.request.result_evidence_ids):
                raise ValueError("查到证据时 finding 与 request 的 evidence_ids 必须一致")
        return self


class ThesisValidationSession(DomainModel):
    """主图为当前唯一观点保存的连续会话状态。"""

    thesis_id: str = Field(pattern=r"^th_[A-Za-z0-9_]+$")
    previous_turns: tuple[ValidationResearchTurn, ...] = Field(default=(), max_length=12)
    used_request_fingerprints: tuple[_RequestFingerprint, ...] = Field(
        default=(),
        max_length=32,
    )
    pending_request_fingerprint: _RequestFingerprint | None = None
    pending_reviewer_reasoning: str | None = Field(default=None, max_length=2_500)

    @model_validator(mode="after")
    def validate_pending_request_context(self) -> "ThesisValidationSession":
        if (self.pending_request_fingerprint is None) != (self.pending_reviewer_reasoning is None):
            raise ValueError("pending request fingerprint 与 reviewer reasoning 必须同时存在")
        history_fingerprints = tuple(turn.request_fingerprint for turn in self.previous_turns)
        if len(set(history_fingerprints)) != len(history_fingerprints):
            raise ValueError("previous_turns 不能包含重复请求指纹")
        if not set(history_fingerprints) <= set(self.used_request_fingerprints):
            raise ValueError("历史请求指纹必须包含在 used_request_fingerprints 中")
        return self


class ThesisValidationLimits(DomainModel):
    """跨领域观点查证的外层硬预算。"""

    max_research_rounds_per_thesis: int = Field(default=2, ge=1, le=6)
    # 8 条初始观点 + 最多 2 条衍生观点，各保留两次最小充分补证机会。
    max_research_requests_per_run: int = Field(default=20, ge=1, le=64)
    max_discovered_candidates_per_turn: int = Field(default=1, ge=0, le=1)
    max_discovered_candidates_per_run: int = Field(default=2, ge=0, le=16)
    max_context_characters: int = Field(default=120_000, ge=1_000, le=1_000_000)


class ThesisValidationRunSummary(DomainModel):
    input_thesis_count: int = Field(ge=0)
    completed_thesis_count: int = Field(ge=0)
    status_counts: dict[str, int]
    model_call_count: int = Field(ge=0)
    research_request_count: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    discovered_candidate_count: int = Field(ge=0)
    stop_reason: Literal["complete", "no_theses", "model_error", "invalid_state"]


class ThesisValidationInput(DomainModel):
    """重放整个单观点会话；新证据和查无结果都保留在当前上下文中。"""

    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    as_of: AwareDatetime
    thesis: ThesisRecord
    evidence: tuple[CollectedEvidenceSummary, ...] = Field(min_length=1, max_length=256)
    previous_turns: tuple[ValidationResearchTurn, ...] = Field(default=(), max_length=12)
    used_request_fingerprints: tuple[_RequestFingerprint, ...] = Field(default=(), max_length=32)
    current_round: int = Field(ge=1)
    remaining_research_rounds: int = Field(ge=0, le=12)
    max_discovered_candidates: int = Field(default=1, ge=0, le=1)
    policy_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_session_context(self) -> "ThesisValidationInput":
        if self.thesis.run_id != self.run_id:
            raise ValueError("thesis.run_id 必须对应当前 run_id")
        if self.thesis.as_of != self.as_of:
            raise ValueError("thesis.as_of 必须对应当前 as_of")

        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence 中不能出现重复 evidence_id")
        evidence_catalog = set(evidence_ids)
        referenced_by_thesis = {
            *self.thesis.supporting_evidence_ids,
            *self.thesis.contradicting_evidence_ids,
        }
        if not referenced_by_thesis <= evidence_catalog:
            raise ValueError("当前上下文必须包含观点已经引用的全部证据")

        rounds = [turn.round_number for turn in self.previous_turns]
        if rounds != list(range(1, len(rounds) + 1)):
            raise ValueError("previous_turns 必须从第 1 轮开始连续排列")
        if self.current_round != len(self.previous_turns) + 1:
            raise ValueError("current_round 必须紧接 previous_turns")

        fingerprints = [turn.request_fingerprint for turn in self.previous_turns]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("previous_turns 不能包含重复请求指纹")
        if len(set(self.used_request_fingerprints)) != len(self.used_request_fingerprints):
            raise ValueError("used_request_fingerprints 不能重复")
        if not set(fingerprints) <= set(self.used_request_fingerprints):
            raise ValueError("历史请求指纹必须全部包含在 used_request_fingerprints 中")

        for turn in self.previous_turns:
            if turn.request.run_id != self.run_id:
                raise ValueError("历史 request.run_id 必须对应当前 run_id")
            if turn.request.thesis_id != self.thesis.thesis_id:
                raise ValueError("历史 request.thesis_id 必须对应当前观点")
            if not set(turn.finding.evidence_ids) <= evidence_catalog:
                raise ValueError("查证返回的 evidence_id 必须已加入当前证据上下文")
        return self


class ThesisValidationDecision(DomainModel):
    """每轮唯一合法的模型输出。"""

    action: ThesisValidationAction
    review_summary: str = Field(
        min_length=1,
        max_length=2_500,
        description="本轮如何理解现有证据、上一轮响应及剩余缺口",
    )
    research_request: ValidationResearchRequestDraft | None = None
    finalization: ThesisFinalizationDraft | None = None
    discovered_candidates: tuple[CandidateThesisDraft, ...] = Field(default=(), max_length=1)

    @model_validator(mode="after")
    def validate_exclusive_action(self) -> "ThesisValidationDecision":
        if self.action is ThesisValidationAction.REQUEST_RESEARCH:
            if self.research_request is None or self.finalization is not None:
                raise ValueError("REQUEST_RESEARCH 必须且只能携带一个 research_request")
        elif self.research_request is not None or self.finalization is None:
            raise ValueError("FINALIZE 必须且只能携带 finalization")
        return self


class RequestResearchPayload(DomainModel):
    """Transport-only branch that structurally requires exactly one research request."""

    action: Literal[ThesisValidationAction.REQUEST_RESEARCH]
    research_request: ValidationResearchRequestDraft


class EvidenceAssessmentStance(StrEnum):
    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"


class EvidenceAssessmentPayload(DomainModel):
    """One explicit evidence citation avoids two independently empty LLM arrays."""

    evidence_id: _EvidenceId
    stance: EvidenceAssessmentStance


class ThesisFinalizationPayload(DomainModel):
    """Transport DTO whose minimum one citation is expressible in JSON Schema."""

    final_status: Literal[
        ThesisValidationStatus.SUPPORTED,
        ThesisValidationStatus.REFUTED,
        ThesisValidationStatus.MIXED,
        ThesisValidationStatus.INCONCLUSIVE,
    ]
    confidence: float = Field(ge=0, le=1)
    evidence_assessments: tuple[EvidenceAssessmentPayload, ...] = Field(
        min_length=1,
        max_length=64,
        description="至少引用一条证据，并逐条标记为支持或反驳",
    )
    reasoning_summary: str = Field(min_length=1, max_length=4_000)
    remaining_questions: tuple[str, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> "ThesisFinalizationPayload":
        evidence_ids = [item.evidence_id for item in self.evidence_assessments]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence_assessments 不能重复引用同一 evidence_id")
        return self

    def to_domain_finalization(self) -> ThesisFinalizationDraft:
        supporting = tuple(
            item.evidence_id
            for item in self.evidence_assessments
            if item.stance is EvidenceAssessmentStance.SUPPORTING
        )
        contradicting = tuple(
            item.evidence_id
            for item in self.evidence_assessments
            if item.stance is EvidenceAssessmentStance.CONTRADICTING
        )
        return ThesisFinalizationDraft(
            final_status=self.final_status,
            confidence=self.confidence,
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
            reasoning_summary=self.reasoning_summary,
            remaining_questions=self.remaining_questions,
        )


class FinalizePayload(DomainModel):
    """Transport-only branch that structurally requires exactly one finalization."""

    action: Literal[ThesisValidationAction.FINALIZE]
    finalization: ThesisFinalizationPayload


class ThesisValidationModelOutput(DomainModel):
    """LLM transport schema with a discriminated, mutually exclusive decision branch."""

    review_summary: str = Field(
        min_length=1,
        max_length=2_500,
        description="本轮如何理解现有证据、上一轮响应及剩余缺口",
    )
    decision: Annotated[
        RequestResearchPayload | FinalizePayload,
        Field(discriminator="action"),
    ]
    discovered_candidates: tuple[CandidateThesisDraft, ...] = Field(default=(), max_length=1)

    def to_domain_decision(self) -> ThesisValidationDecision:
        if isinstance(self.decision, RequestResearchPayload):
            return ThesisValidationDecision(
                action=ThesisValidationAction.REQUEST_RESEARCH,
                review_summary=self.review_summary,
                research_request=self.decision.research_request,
                finalization=None,
                discovered_candidates=self.discovered_candidates,
            )
        return ThesisValidationDecision(
            action=ThesisValidationAction.FINALIZE,
            review_summary=self.review_summary,
            research_request=None,
            finalization=self.decision.finalization.to_domain_finalization(),
            discovered_candidates=self.discovered_candidates,
        )
