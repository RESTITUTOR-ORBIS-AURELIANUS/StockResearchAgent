"""情绪与资金分析 Agent 的结构化计划、证据草稿和运行摘要。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from stock_research_agent.domain import ResearchRequest, ResearchTarget
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import ResearchPriority, TargetType


class SentimentFlowResearchMode(StrEnum):
    DAILY = "DAILY"
    VERIFICATION = "VERIFICATION"


class SentimentFlowCheck(StrEnum):
    """程序能够映射到确定性语义 Tool 的三种查证维度。"""

    ACTIVE_MONEY_FLOW = "ACTIVE_MONEY_FLOW"
    CAPITAL_POSITIONING = "CAPITAL_POSITIONING"
    UNUSUAL_TRADING = "UNUSUAL_TRADING"


class SentimentFlowEvidenceDraft(DomainModel):
    """LLM 只描述事实并引用真实调用；系统负责补齐来源与编号。"""

    target: ResearchTarget = Field(description="证据直接描述的市场、行业或股票")
    title: str = Field(
        min_length=1,
        max_length=200,
        description="不含因果猜测、预测和交易建议的简短事实标题",
    )
    description: str = Field(
        min_length=1,
        description="写明日期、对象、资金/情绪方向、输入中可见数值和口径",
    )
    source_call_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=8,
        description="直接支持该证据的真实 Tool 调用编号",
    )
    tags: tuple[str, ...] = Field(
        default=(),
        max_length=12,
        description="资金流、杠杆、涨跌停、龙虎榜等检索标签",
    )
    limitations: tuple[str, ...] = Field(
        default=(),
        max_length=8,
        description="口径差异、部分数据失败、时间窗口短等限制",
    )


class SentimentFlowVerificationRequestDraft(DomainModel):
    """模型提出要查证的股票与维度；程序决定具体 Tool 参数。"""

    target: ResearchTarget = Field(description="必须是快照候选或外部请求指定的 A 股股票")
    question: str = Field(
        min_length=1,
        max_length=500,
        description="需要资金行为数据回答的具体问题",
    )
    requested_evidence: str = Field(
        min_length=1,
        max_length=1000,
        description="希望取得的可验证事实，不得预写因果结论",
    )
    checks: tuple[SentimentFlowCheck, ...] = Field(
        min_length=1,
        max_length=3,
        description="回答问题所需的最小查证维度集合",
    )
    lookback_days: int = Field(
        default=30,
        ge=5,
        le=365,
        description="主动资金流、持股和两融查询的自然日回看窗口",
    )
    event_trade_date: date | None = Field(
        default=None,
        description="查龙虎榜/大宗交易时必填的单个交易日",
    )
    priority: ResearchPriority = Field(default=ResearchPriority.MEDIUM)
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="说明快照或外部问题中的哪个现象触发本次查证",
    )

    @model_validator(mode="after")
    def validate_plan(self) -> "SentimentFlowVerificationRequestDraft":
        if self.target.type is not TargetType.STOCK:
            raise ValueError("情绪资金定向查证第一版只接受 STOCK 标的")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("checks 不能重复")
        if SentimentFlowCheck.UNUSUAL_TRADING in self.checks and self.event_trade_date is None:
            raise ValueError("UNUSUAL_TRADING 查证必须提供 event_trade_date")
        if SentimentFlowCheck.CAPITAL_POSITIONING in self.checks and self.target.code.endswith(
            ".BJ"
        ):
            raise ValueError("当前 CAPITAL_POSITIONING 查询只支持沪深股票")
        return self


class SentimentFlowVerificationTask(SentimentFlowVerificationRequestDraft):
    task_id: str = Field(pattern=r"^sfv_[A-Za-z0-9_]+$")
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"]


class DailySentimentFlowAnalysis(DomainModel):
    """每日快照的唯一合法模型输出。"""

    snapshot_evidence: tuple[SentimentFlowEvidenceDraft, ...] = Field(
        default=(),
        max_length=20,
        description="仅由每日资金与情绪快照直接证明的事实",
    )
    verification_requests: tuple[SentimentFlowVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=4,
        description="针对少量异常股票提出的定向资金查证",
    )
    market_summary: str = Field(
        min_length=1,
        max_length=2000,
        description="只索引前两项已表达的重点，不新增事实或预测",
    )


class TargetedSentimentFlowPlan(DomainModel):
    verification_requests: tuple[SentimentFlowVerificationRequestDraft, ...] = Field(
        min_length=1,
        max_length=3,
    )
    planning_summary: str = Field(min_length=1, max_length=1200)


class SentimentFlowReviewDecision(DomainModel):
    evidence: tuple[SentimentFlowEvidenceDraft, ...] = Field(default=(), max_length=20)
    follow_up_requests: tuple[SentimentFlowVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=3,
    )
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=10)
    review_summary: str = Field(min_length=1, max_length=2000)


class SentimentFlowToolObservation(DomainModel):
    call_id: str = Field(pattern=r"^sfc_[A-Za-z0-9_]+$")
    task_id: str | None = Field(default=None, pattern=r"^sfv_[A-Za-z0-9_]+$")
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    result: dict[str, Any]


class DailySentimentFlowInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    snapshot_call_id: str
    snapshot_result: dict[str, Any]


class TargetedSentimentFlowInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    research_request: ResearchRequest


class SentimentFlowReviewInput(DomainModel):
    run_id: str
    as_of: datetime
    round_number: int = Field(ge=1)
    tasks: tuple[SentimentFlowVerificationTask, ...]
    observations: tuple[SentimentFlowToolObservation, ...]
    existing_evidence: tuple[SentimentFlowEvidenceDraft, ...]


class SentimentFlowAgentRunSummary(DomainModel):
    mode: SentimentFlowResearchMode
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
class SentimentFlowAgentLimits:
    """程序硬预算，不能被 Prompt 或模型输出绕过。"""

    daily_candidate_count: int = 6
    max_verification_rounds: int = 2
    max_requests_per_round: int = 4
    max_total_tool_calls: int = 12

    def __post_init__(self) -> None:
        if not 3 <= self.daily_candidate_count <= 20:
            raise ValueError("daily_candidate_count 必须在 3 到 20 之间")
        if not 1 <= self.max_verification_rounds <= 4:
            raise ValueError("max_verification_rounds 必须在 1 到 4 之间")
        if not 1 <= self.max_requests_per_round <= 8:
            raise ValueError("max_requests_per_round 必须在 1 到 8 之间")
        if self.max_total_tool_calls < 1:
            raise ValueError("max_total_tool_calls 必须大于 0")
