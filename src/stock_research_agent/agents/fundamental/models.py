"""基本面研究 Agent 的结构化计划、证据草稿和运行摘要。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from stock_research_agent.domain import ResearchRequest, ResearchTarget
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import ResearchPriority, TargetType

_PERIOD_PATTERN = r"^[0-9]{8}$"
_PERIOD_CHECKS = frozenset(
    {
        "FINANCIAL_STATEMENTS",
        "FINANCIAL_QUALITY",
        "EARNINGS_DISCLOSURE",
        "DIVIDEND_OWNERSHIP",
    }
)
_COMPARABLE_CHECKS = frozenset(
    {
        "FINANCIAL_STATEMENTS",
        "FINANCIAL_QUALITY",
        "EARNINGS_DISCLOSURE",
    }
)


class FundamentalResearchMode(StrEnum):
    DAILY = "DAILY"
    VERIFICATION = "VERIFICATION"


class FundamentalCheck(StrEnum):
    """程序能够映射到确定性语义 Tool 的六种个股查证维度。"""

    FINANCIAL_STATEMENTS = "FINANCIAL_STATEMENTS"
    FINANCIAL_QUALITY = "FINANCIAL_QUALITY"
    EARNINGS_DISCLOSURE = "EARNINGS_DISCLOSURE"
    VALUATION_HISTORY = "VALUATION_HISTORY"
    DIVIDEND_OWNERSHIP = "DIVIDEND_OWNERSHIP"
    PLEDGE_RISK = "PLEDGE_RISK"


class FundamentalEvidenceDraft(DomainModel):
    """LLM 只写可核验事实并引用真实调用，程序补齐来源与证据编号。"""

    target: ResearchTarget = Field(description="证据直接描述的市场、板块或股票")
    title: str = Field(
        min_length=1,
        max_length=200,
        description="不含预测、投资建议或未经证明因果关系的事实标题",
    )
    description: str = Field(
        min_length=1,
        description="写清报告期/日期、口径、输入中可见数值及同比或横截面关系",
    )
    source_call_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=12,
        description="直接支持该证据的真实 Tool 调用编号",
    )
    tags: tuple[str, ...] = Field(default=(), max_length=12)
    limitations: tuple[str, ...] = Field(
        default=(),
        max_length=8,
        description="披露口径、样本、数据缺失、未经审计等限制",
    )


class FundamentalVerificationRequestDraft(DomainModel):
    """模型描述查什么；程序固定 Tool、日期窗口和调用边界。"""

    target: ResearchTarget = Field(description="必须是已授权的 A 股股票")
    question: str = Field(min_length=1, max_length=500)
    requested_evidence: str = Field(
        min_length=1,
        max_length=1200,
        description="希望取得的支持和反驳事实，不得预写结论",
    )
    checks: tuple[FundamentalCheck, ...] = Field(min_length=1, max_length=4)
    report_period: str | None = Field(
        default=None,
        pattern=_PERIOD_PATTERN,
        description="报告期 YYYYMMDD；报表、质量、业绩或股东查证时必填",
    )
    comparison_period: str | None = Field(
        default=None,
        pattern=_PERIOD_PATTERN,
        description="可选对比报告期；仅用于报表、质量或业绩的跨期查证",
    )
    lookback_days: int = Field(
        default=365,
        ge=30,
        le=1825,
        description="估值、股东人数等日期序列的自然日回看窗口",
    )
    composition_type: Literal["P", "D", "I"] = Field(
        default="P",
        description="主营构成口径：P 产品、D 地区、I 行业",
    )
    priority: ResearchPriority = Field(default=ResearchPriority.MEDIUM)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_plan(self) -> "FundamentalVerificationRequestDraft":
        if self.target.type is not TargetType.STOCK:
            raise ValueError("基本面定向查证第一版只接受 STOCK 标的")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("checks 不能重复")
        check_values = {item.value for item in self.checks}
        if check_values.intersection(_PERIOD_CHECKS) and self.report_period is None:
            raise ValueError("所选基本面 checks 必须填写 report_period")
        if self.comparison_period is not None:
            if self.report_period is None:
                raise ValueError("comparison_period 必须与 report_period 配合使用")
            if self.comparison_period == self.report_period:
                raise ValueError("comparison_period 不能等于 report_period")
            if not check_values.intersection(_COMPARABLE_CHECKS):
                raise ValueError("当前 checks 不支持 comparison_period")
            if self.comparison_period[4:] != self.report_period[4:] or int(
                self.comparison_period[:4]
            ) + 1 != int(self.report_period[:4]):
                raise ValueError("comparison_period 必须是 report_period 的上年同期")
        for value in (self.report_period, self.comparison_period):
            if value is not None and value[4:] not in {"0331", "0630", "0930", "1231"}:
                raise ValueError("报告期必须是 0331、0630、0930 或 1231")
        return self


class FundamentalVerificationTask(FundamentalVerificationRequestDraft):
    task_id: str = Field(pattern=r"^fv_[A-Za-z0-9_]+$")
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"]


class DailyFundamentalAnalysis(DomainModel):
    """每日基本面快照的唯一合法模型输出。"""

    snapshot_evidence: tuple[FundamentalEvidenceDraft, ...] = Field(
        default=(),
        max_length=24,
    )
    verification_requests: tuple[FundamentalVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=4,
    )
    market_summary: str = Field(
        min_length=1,
        max_length=2400,
        description="只索引前两项已表达重点，不新增事实、预测或建议",
    )


class TargetedFundamentalPlan(DomainModel):
    verification_requests: tuple[FundamentalVerificationRequestDraft, ...] = Field(
        min_length=1,
        max_length=4,
    )
    planning_summary: str = Field(min_length=1, max_length=1200)


class FundamentalReviewDecision(DomainModel):
    evidence: tuple[FundamentalEvidenceDraft, ...] = Field(default=(), max_length=24)
    follow_up_requests: tuple[FundamentalVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=4,
    )
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=10)
    review_summary: str = Field(min_length=1, max_length=2400)


class FundamentalToolObservation(DomainModel):
    call_id: str = Field(pattern=r"^fc_[A-Za-z0-9_]+$")
    task_id: str | None = Field(default=None, pattern=r"^fv_[A-Za-z0-9_]+$")
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    result: dict[str, Any]


class DailyFundamentalInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    snapshot_call_id: str
    snapshot_result: dict[str, Any]


class TargetedFundamentalInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    research_request: ResearchRequest


class FundamentalReviewInput(DomainModel):
    run_id: str
    as_of: datetime
    round_number: int = Field(ge=1)
    tasks: tuple[FundamentalVerificationTask, ...]
    observations: tuple[FundamentalToolObservation, ...]
    existing_evidence: tuple[FundamentalEvidenceDraft, ...]


class FundamentalAgentRunSummary(DomainModel):
    mode: FundamentalResearchMode
    snapshot_status: str | None = None
    verification_rounds: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    accepted_evidence_count: int = Field(ge=0)
    rejected_evidence_count: int = Field(ge=0)
    budget_exhausted: bool = False
    skipped_task_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    stop_reason: str


@dataclass(frozen=True, slots=True)
class FundamentalAgentLimits:
    """程序硬预算，Prompt 和模型输出均不能绕过。"""

    daily_candidate_count: int = 6
    announcement_lookback_days: int = 14
    max_verification_rounds: int = 2
    max_requests_per_round: int = 4
    max_total_tool_calls: int = 48

    def __post_init__(self) -> None:
        if not 3 <= self.daily_candidate_count <= 20:
            raise ValueError("daily_candidate_count 必须在 3 到 20 之间")
        if not 7 <= self.announcement_lookback_days <= 60:
            raise ValueError("announcement_lookback_days 必须在 7 到 60 之间")
        if not 1 <= self.max_verification_rounds <= 4:
            raise ValueError("max_verification_rounds 必须在 1 到 4 之间")
        if not 1 <= self.max_requests_per_round <= 8:
            raise ValueError("max_requests_per_round 必须在 1 到 8 之间")
        if self.max_total_tool_calls < 1:
            raise ValueError("max_total_tool_calls 必须大于 0")
