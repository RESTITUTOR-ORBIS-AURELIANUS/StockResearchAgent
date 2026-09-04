"""新闻事件研究 Agent 的结构化计划、证据草稿和运行摘要。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from stock_research_agent.domain import ResearchRequest, ResearchTarget
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import ResearchPriority, TargetType


class EventResearchMode(StrEnum):
    DAILY = "DAILY"
    VERIFICATION = "VERIFICATION"


class EventCheck(StrEnum):
    """程序能够映射到确定性语义 Tool 的四种个股事件查证维度。"""

    NEWS_DISCLOSURES = "NEWS_DISCLOSURES"
    SELL_SIDE_RESEARCH = "SELL_SIDE_RESEARCH"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    EARNINGS_DISCLOSURE = "EARNINGS_DISCLOSURE"


EventAnnouncementCategory = Literal[
    "全部",
    "重大事项",
    "财务报告",
    "融资公告",
    "风险提示",
    "资产重组",
    "信息变更",
    "持股变动",
]


class EventEvidenceDraft(DomainModel):
    """LLM 只描述来源直接支持的事件事实，程序负责核验引用和编号。"""

    target: ResearchTarget = Field(description="证据直接描述的市场、板块或股票")
    title: str = Field(
        min_length=1,
        max_length=200,
        description="不含未来预测、因果扩写或投资建议的事件事实标题",
    )
    description: str = Field(
        min_length=1,
        description="写明谁在何时披露或报道了什么，并保留来源口径",
    )
    source_call_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=12,
        description="直接支持该证据的真实 Tool 调用编号",
    )
    source_record_keys: tuple[str, ...] = Field(
        min_length=1,
        max_length=24,
        description="上述调用内直接支持该证据的原始行 record_key",
    )
    tags: tuple[str, ...] = Field(default=(), max_length=12)
    limitations: tuple[str, ...] = Field(
        default=(),
        max_length=8,
        description="单一媒体、正文缺失、来源不完整、卖方观点等限制",
    )


class EventVerificationRequestDraft(DomainModel):
    """模型描述查什么；程序固定 Tool、身份核验、日期窗口和预算。"""

    target: ResearchTarget = Field(description="必须是已授权的 A 股股票")
    question: str = Field(min_length=1, max_length=500)
    requested_evidence: str = Field(
        min_length=1,
        max_length=1200,
        description="希望取得的支持和反驳事实，不得预写结论",
    )
    checks: tuple[EventCheck, ...] = Field(min_length=1, max_length=4)
    lookback_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="新闻、公告、研报和公司行动的自然日回看窗口",
    )
    announcement_category: EventAnnouncementCategory = Field(
        default="全部",
        description="个股公告分类；必须使用 Tool 支持的固定分类，默认全部",
    )
    report_period: str | None = Field(
        default=None,
        pattern=r"^[0-9]{8}$",
        description="查询业绩披露时使用的季度末 YYYYMMDD",
    )
    priority: ResearchPriority = Field(default=ResearchPriority.MEDIUM)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_plan(self) -> "EventVerificationRequestDraft":
        if self.target.type is not TargetType.STOCK:
            raise ValueError("新闻事件定向查证第一版只接受 STOCK 标的")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("checks 不能重复")
        if EventCheck.NEWS_DISCLOSURES in self.checks and self.lookback_days > 31:
            raise ValueError("NEWS_DISCLOSURES 单次查证窗口不能超过 31 天")
        if EventCheck.EARNINGS_DISCLOSURE in self.checks and self.report_period is None:
            raise ValueError("EARNINGS_DISCLOSURE 查证必须提供 report_period")
        if self.report_period is not None and self.report_period[4:] not in {
            "0331",
            "0630",
            "0930",
            "1231",
        }:
            raise ValueError("report_period 必须是季度末")
        return self


class EventVerificationTask(EventVerificationRequestDraft):
    task_id: str = Field(pattern=r"^evr_[A-Za-z0-9_]+$")
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"]


class DailyEventAnalysis(DomainModel):
    """每日新闻事件快照的唯一合法模型输出。"""

    snapshot_evidence: tuple[EventEvidenceDraft, ...] = Field(default=(), max_length=24)
    verification_requests: tuple[EventVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=4,
    )
    market_summary: str = Field(
        min_length=1,
        max_length=2400,
        description="只索引前两项已表达重点，不新增事实、预测或建议",
    )


class TargetedEventPlan(DomainModel):
    verification_requests: tuple[EventVerificationRequestDraft, ...] = Field(
        min_length=1,
        max_length=4,
    )
    planning_summary: str = Field(min_length=1, max_length=1200)


class EventReviewDecision(DomainModel):
    evidence: tuple[EventEvidenceDraft, ...] = Field(default=(), max_length=24)
    follow_up_requests: tuple[EventVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=4,
    )
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=10)
    review_summary: str = Field(min_length=1, max_length=2400)


class EventToolObservation(DomainModel):
    call_id: str = Field(pattern=r"^ec_[A-Za-z0-9_]+$")
    task_id: str | None = Field(default=None, pattern=r"^evr_[A-Za-z0-9_]+$")
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    result: dict[str, Any]


class DailyEventInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    snapshot_call_id: str
    snapshot_result: dict[str, Any]


class TargetedEventInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    research_request: ResearchRequest


class EventReviewInput(DomainModel):
    run_id: str
    as_of: datetime
    round_number: int = Field(ge=1)
    tasks: tuple[EventVerificationTask, ...]
    observations: tuple[EventToolObservation, ...]
    existing_evidence: tuple[EventEvidenceDraft, ...]


class EventAgentRunSummary(DomainModel):
    mode: EventResearchMode
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
class EventAgentLimits:
    """程序硬预算，Prompt 和模型输出均不能绕过。"""

    daily_candidate_count: int = 6
    news_lookback_hours: int = 24
    announcement_lookback_days: int = 3
    research_lookback_days: int = 7
    max_verification_rounds: int = 2
    max_requests_per_round: int = 4
    max_total_tool_calls: int = 32

    def __post_init__(self) -> None:
        if not 3 <= self.daily_candidate_count <= 20:
            raise ValueError("daily_candidate_count 必须在 3 到 20 之间")
        if not 1 <= self.news_lookback_hours <= 24:
            raise ValueError("news_lookback_hours 必须在 1 到 24 之间")
        if not 1 <= self.announcement_lookback_days <= 7:
            raise ValueError("announcement_lookback_days 必须在 1 到 7 之间")
        if not 1 <= self.research_lookback_days <= 14:
            raise ValueError("research_lookback_days 必须在 1 到 14 之间")
        if not 1 <= self.max_verification_rounds <= 4:
            raise ValueError("max_verification_rounds 必须在 1 到 4 之间")
        if not 1 <= self.max_requests_per_round <= 8:
            raise ValueError("max_requests_per_round 必须在 1 到 8 之间")
        if self.max_total_tool_calls < 2:
            raise ValueError("max_total_tool_calls 必须至少为 2")
