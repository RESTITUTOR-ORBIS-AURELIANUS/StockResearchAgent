"""技术分析 Agent 内部的结构化计划、草稿和运行摘要。"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from stock_research_agent.domain import ResearchRequest, ResearchTarget
from stock_research_agent.domain.base import DomainModel
from stock_research_agent.domain.enums import ResearchPriority, TargetType

_INDEX_CODE = re.compile(r"^(?:[0-9]{6}\.(?:SH|SZ|BJ|SI)|[A-Za-z0-9]{1,20}\.CSI)$")
_EXCHANGE_TRADED_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_ABSTRACT_A_SHARE_CODE = "A_SHARE"


class TechnicalResearchMode(StrEnum):
    DAILY = "DAILY"
    VERIFICATION = "VERIFICATION"


class TechnicalInstrumentKind(StrEnum):
    STOCK = "stock"
    INDEX = "index"
    FUND = "fund"


class TechnicalMeasurement(StrEnum):
    RETURN_TREND = "RETURN_TREND"
    MOMENTUM = "MOMENTUM"
    RISK_TRADABILITY = "RISK_TRADABILITY"
    VOLUME_LIQUIDITY = "VOLUME_LIQUIDITY"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"


class TechnicalBenchmarkTarget(ResearchTarget):
    """云端 Schema 可见的基准目标；指数或 ETF 基准都不能伪装成个股。"""

    type: Literal[TargetType.MARKET, TargetType.SECTOR]


class TechnicalBenchmark(DomainModel):
    target: TechnicalBenchmarkTarget
    instrument_kind: Literal[
        TechnicalInstrumentKind.INDEX,
        TechnicalInstrumentKind.FUND,
    ]

    @field_validator("target", mode="before")
    @classmethod
    def normalize_existing_research_target(cls, value: Any) -> Any:
        """保持现有领域调用兼容，同时让传输 Schema 使用更窄的目标类型。"""

        if isinstance(value, ResearchTarget):
            return value.model_dump(mode="python")
        return value

    @model_validator(mode="after")
    def validate_target_kind(self) -> "TechnicalBenchmark":
        _validate_target_kind(self.target, TechnicalInstrumentKind(self.instrument_kind))
        return self


class TechnicalEvidenceDraft(DomainModel):
    """LLM 只填写事实文字及可核验调用编号；系统字段由装配器补齐。"""

    target: ResearchTarget = Field(description="这条证据直接描述的市场、板块或股票")
    title: str = Field(
        min_length=1,
        max_length=200,
        description="不含预测和交易建议的简短事实标题",
    )
    description: str = Field(
        min_length=1,
        description="写明日期、对象、方向、输入中可见数值及事实含义",
    )
    source_call_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=12,
        description="直接支持该证据的真实 Tool 调用编号",
    )
    tags: tuple[str, ...] = Field(
        default=(),
        max_length=12,
        description="用于后续检索的少量技术事实标签",
    )
    limitations: tuple[str, ...] = Field(
        default=(),
        max_length=8,
        description="数据部分缺失、只有单日快照等使用限制",
    )


class TechnicalVerificationRequestDraft(DomainModel):
    """Agent 描述“要验证什么”；程序把测量类型映射到具体 Tool。"""

    target: ResearchTarget = Field(description="必须来自快照或定向任务的待查证标的")
    instrument_kind: TechnicalInstrumentKind = Field(
        description="决定程序应调用 stock、index 还是 fund 行情 Tool"
    )
    question: str = Field(
        min_length=1,
        max_length=500,
        description="需要用多日数据回答的具体技术问题",
    )
    requested_evidence: str = Field(
        min_length=1,
        max_length=1000,
        description="希望计算器产出的可验证事实，不得预写结论",
    )
    measurements: tuple[TechnicalMeasurement, ...] = Field(
        min_length=1,
        max_length=5,
        description="回答该问题所需的最小测量类型集合",
    )
    lookback_days: int = Field(
        default=180,
        ge=30,
        le=800,
        description="向前获取历史行情的自然日数",
    )
    benchmark: TechnicalBenchmark | None = Field(
        default=None,
        description="仅相对强弱测量必填的指数或基金基准",
    )
    priority: ResearchPriority = Field(
        default=ResearchPriority.MEDIUM,
        description="该查证对当日技术证据的优先级",
    )
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="说明快照中哪个现象促使本次查证",
    )

    @field_validator("measurements", mode="before")
    @classmethod
    def deduplicate_measurements(cls, value: Any) -> Any:
        """测量是幂等集合；保序去重模型偶发生成的重复枚举。"""

        if not isinstance(value, (list, tuple)):
            return value
        deduplicated: list[Any] = []
        for item in value:
            if item not in deduplicated:
                deduplicated.append(item)
        return tuple(deduplicated)

    @model_validator(mode="after")
    def validate_measurement_plan(self) -> "TechnicalVerificationRequestDraft":
        if len(set(self.measurements)) != len(self.measurements):
            raise ValueError("measurements 不能重复")
        if TechnicalMeasurement.RELATIVE_STRENGTH in self.measurements and self.benchmark is None:
            raise ValueError("相对强弱查证必须提供 benchmark")
        _validate_target_kind(self.target, self.instrument_kind)
        return self


class TechnicalVerificationTask(TechnicalVerificationRequestDraft):
    task_id: str = Field(pattern=r"^tv_[A-Za-z0-9_]+$")
    origin: Literal["DAILY", "RESEARCH_REQUEST", "FOLLOW_UP"]


class DailyTechnicalAnalysis(DomainModel):
    """每日快照的唯一合法模型输出；不接受 Schema 之外的自由文本。"""

    snapshot_evidence: tuple[TechnicalEvidenceDraft, ...] = Field(
        default=(),
        max_length=20,
        description="仅由当日快照已直接证明的结构化事实",
    )
    verification_requests: tuple[TechnicalVerificationRequestDraft, ...] = Field(
        default=(),
        max_length=3,
        description="为验证多日趋势、动量、风险、量能或相对强弱提出的请求",
    )
    market_summary: str = Field(
        min_length=1,
        max_length=2000,
        description="对两个结果集的简短索引性摘要，不得添加未进入证据或查证请求的结论",
    )


class TargetedTechnicalPlan(DomainModel):
    verification_requests: tuple[TechnicalVerificationRequestDraft, ...] = Field(
        min_length=1, max_length=3
    )
    planning_summary: str = Field(min_length=1, max_length=1200)


class VerificationReviewDecision(DomainModel):
    evidence: tuple[TechnicalEvidenceDraft, ...] = Field(default=(), max_length=20)
    follow_up_requests: tuple[TechnicalVerificationRequestDraft, ...] = Field(
        default=(), max_length=3
    )
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=10)
    review_summary: str = Field(min_length=1, max_length=2000)


class TechnicalToolObservation(DomainModel):
    call_id: str = Field(pattern=r"^tc_[A-Za-z0-9_]+$")
    task_id: str | None = Field(default=None, pattern=r"^tv_[A-Za-z0-9_]+$")
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]
    result: dict[str, Any]


class DailyAnalysisInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    snapshot_call_id: str
    snapshot_result: dict[str, Any]


class TargetedPlanningInput(DomainModel):
    run_id: str
    scope_target: ResearchTarget
    as_of: datetime
    research_request: ResearchRequest


class VerificationReviewInput(DomainModel):
    run_id: str
    as_of: datetime
    round_number: int = Field(ge=1)
    tasks: tuple[TechnicalVerificationTask, ...]
    observations: tuple[TechnicalToolObservation, ...]
    existing_evidence: tuple[TechnicalEvidenceDraft, ...]


class TechnicalAgentRunSummary(DomainModel):
    mode: TechnicalResearchMode
    snapshot_status: str | None = None
    verification_rounds: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    accepted_evidence_count: int = Field(ge=0)
    rejected_evidence_count: int = Field(ge=0)
    budget_exhausted: bool = False
    skipped_task_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    stop_reason: str


def _validate_target_kind(
    target: ResearchTarget,
    instrument_kind: TechnicalInstrumentKind,
) -> None:
    """把 LLM 给出的标的类别限制到程序实际可调用的 Tool 契约。"""

    if instrument_kind is TechnicalInstrumentKind.INDEX:
        if target.type not in {TargetType.MARKET, TargetType.SECTOR}:
            raise ValueError("index 类型查证的 target.type 必须是 MARKET 或 SECTOR")
        if target.code == _ABSTRACT_A_SHARE_CODE:
            if target.type is not TargetType.MARKET:
                raise ValueError("A_SHARE 抽象目标只能使用 MARKET 类型")
            return
        if not _INDEX_CODE.fullmatch(target.code):
            raise ValueError("index 类型查证必须使用合法市场、中证或申万指数代码")
        return

    if instrument_kind is TechnicalInstrumentKind.STOCK:
        if target.type is not TargetType.STOCK:
            raise ValueError("stock 类型查证的 target.type 必须是 STOCK")
    elif target.type not in {TargetType.MARKET, TargetType.SECTOR}:
        raise ValueError("fund 类型查证的 target.type 必须是 MARKET 或 SECTOR")
    if not _EXCHANGE_TRADED_CODE.fullmatch(target.code):
        raise ValueError(f"{instrument_kind.value} 类型查证必须使用沪深京交易所证券代码")


@dataclass(frozen=True, slots=True)
class TechnicalAgentLimits:
    """程序硬限制；Prompt 无法突破这些预算。"""

    daily_candidate_count: int = 6
    max_verification_rounds: int = 2
    # 每日模式只深入最值得查证的三个目标，控制 Tool 数量和复核 Prompt 规模。
    max_requests_per_round: int = 3
    max_total_tool_calls: int = 40

    def __post_init__(self) -> None:
        if not 3 <= self.daily_candidate_count <= 20:
            raise ValueError("daily_candidate_count 必须在 3 到 20 之间")
        if not 1 <= self.max_verification_rounds <= 4:
            raise ValueError("max_verification_rounds 必须在 1 到 4 之间")
        if not 1 <= self.max_requests_per_round <= 10:
            raise ValueError("max_requests_per_round 必须在 1 到 10 之间")
        if self.max_total_tool_calls < 1:
            raise ValueError("max_total_tool_calls 必须大于 0")
