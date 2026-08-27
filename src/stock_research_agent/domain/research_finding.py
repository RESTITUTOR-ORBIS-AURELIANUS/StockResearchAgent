"""定向查证请求的执行结果，不把“查不到”伪装成事实证据。"""

from hashlib import sha256
from typing import Annotated

from pydantic import AwareDatetime, Field, model_validator

from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.common import ResearchTarget
from stock_research_agent.domain.enums import EvidenceDomain, ResearchFindingOutcome

FindingSource = Annotated[str, Field(min_length=1, max_length=200)]
FindingLimitation = Annotated[str, Field(min_length=1)]
EvidenceId = Annotated[str, Field(pattern=r"^ev_[A-Za-z0-9_]+$")]


class ResearchFinding(DomainModel):
    """一个 ResearchRequest 的可审计响应。

    `EvidenceRecord` 只保存有真实来源的事实。没有找到匹配数据、当前工具无法
    回答或数据源失败时，使用本模型记录执行结果，并保持 `evidence_ids` 为空。
    """

    finding_id: str = Field(pattern=r"^rf_[A-Za-z0-9_]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_]+$")
    request_id: str = Field(pattern=r"^rq_[A-Za-z0-9_]+$")
    thesis_id: str = Field(pattern=r"^th_[A-Za-z0-9_]+$")
    target: ResearchTarget
    assigned_domain: EvidenceDomain
    outcome: ResearchFindingOutcome
    summary: str = Field(min_length=1)
    searched_sources: list[FindingSource] = Field(default_factory=list)
    limitations: list[FindingLimitation] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    attempt: int = Field(default=1, ge=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_outcome_contract(self) -> "ResearchFinding":
        expected_id = build_research_finding_id(
            run_id=self.run_id,
            request_id=self.request_id,
            attempt=self.attempt,
        )
        if self.finding_id != expected_id:
            raise ValueError("finding_id 必须由 run_id、request_id 和 attempt 稳定生成")
        if len(set(self.searched_sources)) != len(self.searched_sources):
            raise ValueError("searched_sources 不能包含重复项")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids 不能包含重复项")
        if self.outcome is ResearchFindingOutcome.EVIDENCE_FOUND:
            if not self.evidence_ids:
                raise ValueError("EVIDENCE_FOUND 必须至少关联一个真实 evidence_id")
            if not self.searched_sources:
                raise ValueError("EVIDENCE_FOUND 必须记录至少一个 searched_source")
            return self

        if self.evidence_ids:
            raise ValueError("非 EVIDENCE_FOUND 结果不能关联 evidence_id")
        if self.outcome is ResearchFindingOutcome.NO_MATCHING_EVIDENCE:
            if not self.searched_sources:
                raise ValueError("NO_MATCHING_EVIDENCE 必须记录实际搜索过的数据源")
        if not self.limitations:
            raise ValueError("未产生证据的 ResearchFinding 必须说明 limitations")
        return self


def build_research_finding_id(*, run_id: str, request_id: str, attempt: int) -> str:
    """为同一轮查证生成可重复计算的 ID，供 LangGraph reducer 幂等合并。"""

    if attempt < 1:
        raise ValueError("attempt 必须大于等于 1")
    material = f"{run_id}|{request_id}|{attempt}"
    return f"rf_{sha256(material.encode()).hexdigest()[:20]}"
