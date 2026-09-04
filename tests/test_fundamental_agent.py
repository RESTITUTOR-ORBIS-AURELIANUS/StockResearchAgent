"""基本面 Agent 每日模式、个股查证与主图接入的离线测试。"""

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_research_agent.agents.fundamental import (
    FundamentalAgentLimits,
    FundamentalResearchMode,
    build_fundamental_agent_graph,
)
from stock_research_agent.agents.fundamental.model import (
    _normalize_request_periods,
    _review_json_schema,
    _validate_review_call_ids,
)
from stock_research_agent.agents.fundamental.models import (
    DailyFundamentalAnalysis,
    FundamentalAgentRunSummary,
    FundamentalCheck,
    FundamentalEvidenceDraft,
    FundamentalReviewDecision,
    FundamentalToolObservation,
    FundamentalVerificationRequestDraft,
    TargetedFundamentalPlan,
)
from stock_research_agent.agents.fundamental.prompts import (
    DAILY_ANALYSIS_SYSTEM_PROMPT,
    TARGETED_PLANNING_SYSTEM_PROMPT,
    VERIFICATION_REVIEW_SYSTEM_PROMPT,
)
from stock_research_agent.agents.fundamental.subgraph import _period_validation_error
from stock_research_agent.agents.fundamental.subgraph import (
    _uncitable_observation_error as _fundamental_observation_error,
)
from stock_research_agent.agents.sentiment_flow.models import (
    SentimentFlowAgentRunSummary,
    SentimentFlowResearchMode,
)
from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
)
from stock_research_agent.domain import ResearchRequest, ResearchTarget, TimeRange
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchPriority,
    ResearchRequestStatus,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph import build_research_graph
from stock_research_agent.providers.models import ProviderSource
from stock_research_agent.research_data import InMemoryResearchDataStore, ResearchDataBundle
from stock_research_agent.services import build_data_services
from stock_research_agent.services.models import (
    ServiceDataset,
    ServiceItemTrace,
    ServicePageTrace,
)
from stock_research_agent.tools import ResearchToolContext
from stock_research_agent.tools.execution import create_structured_tool
from stock_research_agent.tools.models import (
    DailyFundamentalSnapshotInput,
    DividendOwnershipInput,
    FinancialQualityInput,
    StockCodeInput,
    StockDateRangeToolInput,
    StockIdentityInput,
    StockPeriodInput,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI)
FETCHED_AT = datetime(2026, 8, 20, 15, 10, tzinfo=SHANGHAI)
RUN_ID = "run_20260820_160000_A_SHARE_cccccccc"
MARKET_TARGET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
SECTOR_TARGET = ResearchTarget(type=TargetType.SECTOR, code="SBI-abcdef123456", name="银行")
STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
OTHER_STOCK_TARGET = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


class NoopProvider:
    async def query(self, request):  # pragma: no cover - fake Tools bypass provider
        raise AssertionError(f"unexpected provider call: {request.api_name}")


class ScriptedFundamentalModel:
    def __init__(
        self,
        *,
        daily: DailyFundamentalAnalysis | None = None,
        targeted: TargetedFundamentalPlan | None = None,
        reviews: tuple[FundamentalReviewDecision, ...] = (),
    ) -> None:
        self.daily = daily
        self.targeted = targeted
        self.reviews = list(reviews)
        self.daily_calls = 0
        self.targeted_calls = 0
        self.review_calls = 0

    async def analyze_daily(self, request):
        self.daily_calls += 1
        assert request.snapshot_result["snapshot"]["report_period"] == "20260630"
        assert request.snapshot_result["snapshot"]["sector_fundamentals"]
        assert self.daily is not None
        return self.daily

    async def plan_targeted(self, request):
        self.targeted_calls += 1
        assert request.research_request.target == STOCK_TARGET
        assert self.targeted is not None
        return self.targeted

    async def review_verification(self, request):
        self.review_calls += 1
        assert request.observations
        return self.reviews.pop(0)


def test_default_limits_leave_room_for_two_round_fundamental_verification() -> None:
    limits = FundamentalAgentLimits()

    assert limits.max_verification_rounds == 2
    assert limits.max_requests_per_round == 4
    assert limits.max_total_tool_calls == 48


def test_missing_or_future_fundamental_period_is_normalized_before_schema_validation() -> None:
    payload = {
        "verification_requests": [
            {
                "target": STOCK_TARGET.model_dump(mode="json"),
                "checks": ["FINANCIAL_STATEMENTS"],
                "report_period": "20260930",
                "comparison_period": "20241231",
            }
        ]
    }

    normalized = _normalize_request_periods(
        payload,
        field_name="verification_requests",
        cutoff=AS_OF.date(),
        periods_by_target={},
    )

    request = normalized["verification_requests"][0]
    assert request["report_period"] == "20260630"
    assert request["comparison_period"] == "20250630"


def test_financial_report_period_may_precede_research_observation_window() -> None:
    request = FundamentalVerificationRequestDraft(
        target=STOCK_TARGET,
        question="近期信息是否得到最近财报支持？",
        requested_evidence="查询观察窗口开始前已经结束的最近季度。",
        checks=(FundamentalCheck.FINANCIAL_STATEMENTS,),
        report_period="20260630",
        reason="财报报告期与资料观察窗口不是同一时间语义。",
    )

    error = _period_validation_error(
        request,
        as_of_date=AS_OF.date(),
        daily_report_period=None,
        daily_comparison_period=None,
        allowed_report_range=TimeRange(start=date(2026, 8, 1), end=AS_OF.date()),
    )

    assert error is None


def test_review_schema_uses_only_real_tool_call_ids() -> None:
    allowed = frozenset(
        {
            "fc_r1_1_financial_statements_current",
            "fc_r1_1_financial_statements_comparison",
        }
    )
    schema = _review_json_schema(allowed)
    source_ids = schema["$defs"]["FundamentalEvidenceDraft"]["properties"]["source_call_ids"]

    assert source_ids["items"]["enum"] == sorted(allowed)
    valid = FundamentalReviewDecision(
        review_summary="引用真实调用。",
        evidence=(
            FundamentalEvidenceDraft(
                target=STOCK_TARGET,
                title="真实调用证据",
                description="来自本轮实际工具调用。",
                source_call_ids=("fc_r1_1_financial_statements_current",),
            ),
        ),
    )
    assert _validate_review_call_ids(valid, allowed) is valid

    invalid = FundamentalReviewDecision(
        review_summary="模型改写了调用编号。",
        evidence=(
            FundamentalEvidenceDraft(
                target=STOCK_TARGET,
                title="错误调用编号",
                description="编号不存在。",
                source_call_ids=("fcr1_fabricated",),
            ),
        ),
    )
    with pytest.raises(ValueError, match="fcr1_fabricated"):
        _validate_review_call_ids(invalid, allowed)


@pytest.mark.parametrize("status", ["ok", "partial", "empty"])
def test_fundamental_observation_semantics_keep_citable_statuses(status: str) -> None:
    observation = FundamentalToolObservation(
        call_id="fc_r1_1_test",
        tool_name="get_financial_statements",
        arguments={"ts_code": STOCK_TARGET.code},
        result={"status": status},
    )

    assert _fundamental_observation_error(observation, target_code=STOCK_TARGET.code) is None


def test_fundamental_observation_semantics_report_error_without_citing_it() -> None:
    observation = FundamentalToolObservation(
        call_id="fc_r1_1_test",
        tool_name="get_financial_statements",
        arguments={"ts_code": STOCK_TARGET.code},
        result={"status": "error"},
    )

    message = _fundamental_observation_error(observation, target_code=STOCK_TARGET.code)
    assert message is not None
    assert "status=error" in message


def test_daily_mode_builds_market_sector_and_verified_stock_evidence() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        model = ScriptedFundamentalModel(
            daily=DailyFundamentalAnalysis(
                market_summary="宏观和行业横截面已有事实，一只新披露公司需要查证。",
                snapshot_evidence=(
                    FundamentalEvidenceDraft(
                        target=MARKET_TARGET,
                        title="制造业 PMI 最近两期回升",
                        description="cn_pmi 的两期输入值从 49.8 变为 50.2。",
                        source_call_ids=("fc_daily_snapshot_1",),
                        limitations=("两期变化不能证明持续趋势",),
                    ),
                    FundamentalEvidenceDraft(
                        target=SECTOR_TARGET,
                        title="银行行业正 PE 中位数处于快照低值候选",
                        description="stock_basic.industry 口径下，银行有 18 个正 PE 样本。",
                        source_call_ids=("fc_daily_snapshot_1",),
                        limitations=("该口径不是申万行业",),
                    ),
                ),
                verification_requests=(_report_verification_request(STOCK_TARGET),),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="本期与同期报表已经取得。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行本期与同期利润表均已取得",
                            description="20260630 与 20250630 两期利润表记录均可追溯。",
                            source_call_ids=(
                                "fc_r1_1_financial_statements_current",
                                "fc_r1_1_financial_statements_comparison",
                            ),
                            limitations=("还需结合现金流和审计口径",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_fundamental_snapshot",
            "resolve_stock_identity",
            "get_financial_statements:20260630",
            "get_financial_statements:20250630",
            "get_financial_quality:20260630",
            "get_financial_quality:20250630",
        ]
        assert [item.title for item in result["evidence_records"]] == [
            "制造业 PMI 最近两期回升",
            "银行行业正 PE 中位数处于快照低值候选",
            "平安银行本期与同期利润表均已取得",
        ]
        assert all(item.domain is EvidenceDomain.FUNDAMENTAL for item in result["evidence_records"])
        assert all(
            item.verification_status is VerificationStatus.VERIFIED
            for item in result["evidence_records"]
        )
        assert result["evidence_records"][0].raw_payload_ref is not None
        assert result["run_summary"].tool_call_count == 6
        assert model.daily_calls == 1
        assert model.review_calls == 1

    asyncio.run(scenario())


def test_verification_mode_skips_snapshot_and_completes_request() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        request = _research_request("rq_fundamental_001")
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="核对区间估值即可。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="估值在请求区间内如何变化？",
                        requested_evidence="列出区间 PE、PB 和股息率记录。",
                        checks=(FundamentalCheck.VALUATION_HISTORY,),
                        lookback_days=365,
                        priority=ResearchPriority.MEDIUM,
                        reason="问题只涉及历史估值。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="估值区间数据已取得。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="平安银行区间估值记录已取得",
                            description="查询窗口内 PE、PB 和股息率记录可追溯。",
                            source_call_ids=("fc_r1_1_valuation_history_window",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": request,
            }
        )

        assert calls == ["resolve_stock_identity", "get_valuation_context"]
        assert "get_daily_fundamental_snapshot" not in calls
        assert result["completed_research_request"].status is ResearchRequestStatus.COMPLETED
        assert result["completed_research_request"].result_evidence_ids == [
            result["evidence_records"][0].evidence_id
        ]
        assert model.daily_calls == 0
        assert model.targeted_calls == 1

    asyncio.run(scenario())


def test_targeted_result_cannot_be_written_to_another_stock() -> None:
    async def scenario() -> None:
        context, tools, _, _ = await fake_tool_runtime()
        model = ScriptedFundamentalModel(
            daily=DailyFundamentalAnalysis(
                market_summary="一家公司需要查证。",
                verification_requests=(_report_verification_request(STOCK_TARGET),),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="模型错误地偷换了股票。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=OTHER_STOCK_TARGET,
                            title="错误股票的报表证据",
                            description="这条证据必须由程序拒绝。",
                            source_call_ids=("fc_r1_1_financial_statements_current",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.DAILY,
            }
        )

        assert result["evidence_records"] == []
        assert any("错误标的" in message for message in result["errors"])

    asyncio.run(scenario())


def test_future_report_period_is_rejected_before_tool_call() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="错误地请求未来报告期。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="未来报告期质量如何？",
                        requested_evidence="查询未来财务指标。",
                        checks=(FundamentalCheck.FINANCIAL_QUALITY,),
                        report_period="20260930",
                        reason="用于验证程序的时间边界。",
                    ),
                ),
            )
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": _research_request("rq_fundamental_future"),
            }
        )

        assert calls == []
        assert result["completed_research_request"].status is ResearchRequestStatus.FAILED
        assert any("晚于 as_of" in message for message in result["errors"])

    asyncio.run(scenario())


def test_identity_mismatch_stops_before_fundamental_tool_calls() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime(returned_ts_code=OTHER_STOCK_TARGET.code)
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="先核对股票身份，再读取估值。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="估值在请求区间内如何变化？",
                        requested_evidence="取得区间估值记录。",
                        checks=(FundamentalCheck.VALUATION_HISTORY,),
                        reason="验证身份失败时必须关闭。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="身份不匹配，不能形成证据。",
                    unresolved_questions=("股票身份未能确认",),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": _research_request("rq_fundamental_identity"),
            }
        )

        assert calls == ["resolve_stock_identity"]
        assert result["evidence_records"] == []
        assert any("身份核对失败" in message for message in result["errors"])

    asyncio.run(scenario())


def test_historical_request_anchors_lookback_to_request_end() -> None:
    async def scenario() -> None:
        context, tools, calls, arguments_seen = await fake_tool_runtime()
        request = ResearchRequest(
            **{
                **_research_request("rq_fundamental_historical").model_dump(),
                "time_range": TimeRange(start=date(2020, 1, 1), end=date(2020, 12, 31)),
            }
        )
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="查询历史区间估值。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="2020 年估值如何变化？",
                        requested_evidence="取得 2020 年区间估值记录。",
                        checks=(FundamentalCheck.VALUATION_HISTORY,),
                        lookback_days=365,
                        reason="验证历史请求窗口锚定。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="历史估值区间已取得。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="2020 年估值记录已取得",
                            description="查询区间没有晚于 ResearchRequest 的结束日期。",
                            source_call_ids=("fc_r1_1_valuation_history_window",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": request,
            }
        )

        assert calls == ["resolve_stock_identity", "get_valuation_context"]
        assert arguments_seen == [{"start_date": date(2020, 1, 1), "end_date": date(2020, 12, 31)}]
        assert result["completed_research_request"].status is ResearchRequestStatus.COMPLETED

    asyncio.run(scenario())


def test_comparison_period_must_be_previous_year_same_quarter() -> None:
    with pytest.raises(ValueError, match="上年同期"):
        FundamentalVerificationRequestDraft(
            target=STOCK_TARGET,
            question="跨期财务质量是否变化？",
            requested_evidence="取得可比财务指标。",
            checks=(FundamentalCheck.FINANCIAL_QUALITY,),
            report_period="20260630",
            comparison_period="20241231",
            reason="验证同期硬约束。",
        )


def test_identity_call_cannot_be_used_as_fundamental_evidence() -> None:
    async def scenario() -> None:
        context, tools, _, _ = await fake_tool_runtime()
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="查询区间估值。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="估值在区间内如何变化？",
                        requested_evidence="取得区间估值记录。",
                        checks=(FundamentalCheck.VALUATION_HISTORY,),
                        reason="验证身份调用不能冒充基本面来源。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="错误地只引用身份查询。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="错误的利润增长结论",
                            description="身份查询不能证明利润增长。",
                            source_call_ids=("fc_r1_1_identity",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": _research_request("rq_fundamental_identity_citation"),
            }
        )

        assert result["evidence_records"] == []
        assert any("不存在或失败" in message for message in result["errors"])

    asyncio.run(scenario())


def test_sector_representative_stock_can_enter_daily_verification() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime(returned_ts_code=OTHER_STOCK_TARGET.code)
        model = ScriptedFundamentalModel(
            daily=DailyFundamentalAnalysis(
                market_summary="行业代表股需要进一步查证。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=OTHER_STOCK_TARGET,
                        question="该行业代表股的估值记录如何？",
                        requested_evidence="取得区间估值记录，不把行业中位数套给个股。",
                        checks=(FundamentalCheck.VALUATION_HISTORY,),
                        reason="该股只来自行业候选的 representative_stocks。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="代表股估值记录已取得。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=OTHER_STOCK_TARGET,
                            title="行业代表股估值记录已取得",
                            description="只陈述查询窗口内的估值记录。",
                            source_call_ids=("fc_r1_1_valuation_history_window",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_fundamental_snapshot",
            "resolve_stock_identity",
            "get_valuation_context",
        ]
        assert [item.target.code for item in result["evidence_records"]] == [
            OTHER_STOCK_TARGET.code
        ]

    asyncio.run(scenario())


def test_returned_report_period_must_match_tool_arguments() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime(returned_period="20250331")
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="查询当前报告期三张报表。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="当前报告期三张报表是什么？",
                        requested_evidence="取得 20260630 三张报表。",
                        checks=(FundamentalCheck.FINANCIAL_STATEMENTS,),
                        report_period="20260630",
                        reason="验证上游返回报告期。",
                    ),
                ),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="上游实际返回了错误报告期。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="2026 半年报记录已取得",
                            description="这条证据必须因为返回期不匹配而被拒绝。",
                            source_call_ids=("fc_r1_1_financial_statements_current",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": _research_request("rq_fundamental_wrong_period"),
            }
        )

        assert calls == ["resolve_stock_identity", "get_financial_statements:20260630"]
        assert result["evidence_records"] == []
        assert any("错误标的" in message for message in result["errors"])

    asyncio.run(scenario())


def test_report_period_cannot_be_later_than_research_request_end() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        historical_request = ResearchRequest(
            **{
                **_research_request("rq_fundamental_period_range").model_dump(),
                "time_range": TimeRange(start=date(2020, 1, 1), end=date(2020, 12, 31)),
            }
        )
        model = ScriptedFundamentalModel(
            targeted=TargetedFundamentalPlan(
                planning_summary="错误地查询请求范围外的报告期。",
                verification_requests=(
                    FundamentalVerificationRequestDraft(
                        target=STOCK_TARGET,
                        question="2020 年研究请求需要什么报表？",
                        requested_evidence="错误地请求 2026 半年报。",
                        checks=(FundamentalCheck.FINANCIAL_STATEMENTS,),
                        report_period="20260630",
                        reason="验证 ResearchRequest 时间边界。",
                    ),
                ),
            )
        )
        graph = build_fundamental_agent_graph(model=model, tool_context=context, tools=tools)

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": STOCK_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.VERIFICATION,
                "research_request": historical_request,
            }
        )

        assert calls == []
        assert result["completed_research_request"].status is ResearchRequestStatus.FAILED
        assert any("time_range" in message for message in result["errors"])

    asyncio.run(scenario())


def test_program_budget_stops_before_identity_and_fundamental_calls() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        model = ScriptedFundamentalModel(
            daily=DailyFundamentalAnalysis(
                market_summary="一家公司需要查证。",
                verification_requests=(_report_verification_request(STOCK_TARGET),),
            )
        )
        graph = build_fundamental_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
            limits=FundamentalAgentLimits(max_total_tool_calls=1),
        )
        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.DAILY,
            }
        )

        assert calls == ["get_daily_fundamental_snapshot"]
        assert result["run_summary"].budget_exhausted is True
        assert result["run_summary"].stop_reason == "verification_budget_reached"
        assert model.review_calls == 0

    asyncio.run(scenario())


def test_budget_reservation_counts_reused_calls_only_once() -> None:
    async def scenario() -> None:
        context, tools, calls, _ = await fake_tool_runtime()
        first = FundamentalVerificationRequestDraft(
            target=STOCK_TARGET,
            question="估值记录如何？",
            requested_evidence="取得区间估值。",
            checks=(FundamentalCheck.VALUATION_HISTORY,),
            reason="第一项查证。",
        )
        second = FundamentalVerificationRequestDraft(
            target=STOCK_TARGET,
            question="估值背景下是否存在质押记录？",
            requested_evidence="复用估值查询并补充质押记录。",
            checks=(FundamentalCheck.VALUATION_HISTORY, FundamentalCheck.PLEDGE_RISK),
            reason="第二项查证与第一项共享身份和估值调用。",
        )
        model = ScriptedFundamentalModel(
            daily=DailyFundamentalAnalysis(
                market_summary="同一股票有两项可复用调用的查证。",
                verification_requests=(first, second),
            ),
            reviews=(
                FundamentalReviewDecision(
                    review_summary="共享调用只执行一次，质押记录已补充。",
                    evidence=(
                        FundamentalEvidenceDraft(
                            target=STOCK_TARGET,
                            title="质押记录已取得",
                            description="质押统计与明细查询结果可追溯。",
                            source_call_ids=("fc_r1_2_pledge_risk_current",),
                        ),
                    ),
                ),
            ),
        )
        graph = build_fundamental_agent_graph(
            model=model,
            tool_context=context,
            tools=tools,
            limits=FundamentalAgentLimits(max_total_tool_calls=4),
        )

        result = await graph.ainvoke(
            {
                "run_id": RUN_ID,
                "target": MARKET_TARGET,
                "as_of": AS_OF,
                "mode": FundamentalResearchMode.DAILY,
            }
        )

        assert calls == [
            "get_daily_fundamental_snapshot",
            "resolve_stock_identity",
            "get_valuation_context",
            "get_pledge_risk_context",
        ]
        assert result["run_summary"].budget_exhausted is False
        assert result["run_summary"].tool_call_count == 4
        assert len(result["evidence_records"]) == 1

    asyncio.run(scenario())


def test_main_graph_runs_three_implemented_research_stages() -> None:
    stages: list[str] = []

    class StubTechnicalGraph:
        async def ainvoke(self, state):
            stages.append("technical")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": TechnicalAgentRunSummary(
                    mode=TechnicalResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    class StubSentimentGraph:
        async def ainvoke(self, state):
            stages.append("sentiment_flow")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": SentimentFlowAgentRunSummary(
                    mode=SentimentFlowResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    class StubFundamentalGraph:
        async def ainvoke(self, state):
            stages.append("fundamental")
            return {
                "evidence_records": [],
                "errors": [],
                "run_summary": FundamentalAgentRunSummary(
                    mode=FundamentalResearchMode.DAILY,
                    snapshot_status="ok",
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=0,
                    rejected_evidence_count=0,
                    stop_reason="snapshot_evidence_complete",
                ),
            }

    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: StubTechnicalGraph(),
        sentiment_flow_agent_graph_factory=lambda **_: StubSentimentGraph(),
        fundamental_agent_graph_factory=lambda **_: StubFundamentalGraph(),
    )
    result = asyncio.run(graph.ainvoke({"target": MARKET_TARGET, "as_of": AS_OF}))

    assert stages == ["technical", "sentiment_flow", "fundamental"]
    assert result["fundamental_run_summary"].mode is FundamentalResearchMode.DAILY
    assert result["fundamental_request_count"] == 0


def test_prompts_define_schema_few_shot_and_fundamental_boundaries() -> None:
    assert "snapshot_evidence" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "verification_requests" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "sector_fundamentals" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "Few-shot" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "不得输出 Markdown" in DAILY_ANALYSIS_SYSTEM_PROMPT
    assert "最小充分" in TARGETED_PLANNING_SYSTEM_PROMPT
    assert "status=partial" in VERIFICATION_REVIEW_SYSTEM_PROMPT
    assert "unresolved_questions" in VERIFICATION_REVIEW_SYSTEM_PROMPT


def _report_verification_request(
    target: ResearchTarget,
) -> FundamentalVerificationRequestDraft:
    return FundamentalVerificationRequestDraft(
        target=target,
        question="近期预增是否得到报表和现金流质量支持？",
        requested_evidence="核对本期与上年同期三张报表、财务指标和主营构成。",
        checks=(
            FundamentalCheck.FINANCIAL_STATEMENTS,
            FundamentalCheck.FINANCIAL_QUALITY,
        ),
        report_period="20260630",
        comparison_period="20250630",
        lookback_days=365,
        priority=ResearchPriority.HIGH,
        reason="该股进入近期业绩改善候选。",
    )


def _research_request(request_id: str) -> ResearchRequest:
    return ResearchRequest(
        request_id=request_id,
        run_id=RUN_ID,
        thesis_id="th_candidate_fundamental_001",
        target=STOCK_TARGET,
        assigned_domain=EvidenceDomain.FUNDAMENTAL,
        question="估值和财务质量是否支持已有观点？",
        requested_evidence="查询区间估值与最近可用报告期。",
        time_range=TimeRange(start=date(2025, 8, 20), end=date(2026, 8, 20)),
        priority=ResearchPriority.HIGH,
        status=ResearchRequestStatus.PENDING,
        requested_by="ThesisValidationAnalyst",
        created_at=AS_OF,
    )


async def fake_tool_runtime(
    *,
    returned_ts_code: str | None = None,
    returned_period: str | None = None,
) -> tuple[ResearchToolContext, tuple[Any, ...], list[str], list[dict[str, object]]]:
    data_store = InMemoryResearchDataStore()
    context = ResearchToolContext(
        services=build_data_services(NoopProvider()),
        as_of=AS_OF,
        run_id=RUN_ID,
        data_store=data_store,
    )
    calls: list[str] = []
    arguments_seen: list[dict[str, object]] = []
    actual_code = returned_ts_code or STOCK_TARGET.code

    async def daily_snapshot(
        candidate_count: int = 10,
        announcement_lookback_days: int = 14,
    ) -> dict[str, object]:
        calls.append("get_daily_fundamental_snapshot")
        context_ref = await data_store.put(
            RUN_ID,
            ResearchDataBundle(
                kind="daily_fundamental_snapshot",
                tool_name="get_daily_fundamental_snapshot",
                as_of=AS_OF,
                datasets={"daily_valuation": _service_dataset("daily_basic")},
                metadata={
                    "candidate_count": candidate_count,
                    "announcement_lookback_days": announcement_lookback_days,
                },
            ),
        )
        return {
            "tool_name": "get_daily_fundamental_snapshot",
            "status": "ok",
            "as_of": AS_OF.isoformat(),
            "context_ref": context_ref,
            "snapshot": {
                "trade_date": "2026-08-20",
                "report_period": "20260630",
                "comparison_period": "20250630",
                "announcement_lookback_days": 14,
                "valuations": {
                    "lowest_positive_pe": [],
                    "highest_pe": [],
                    "highest_pb": [],
                    "highest_dividend_yield": [],
                },
                "earnings_events": {
                    "strongest_forecast_improvements": [
                        {
                            "ts_code": STOCK_TARGET.code,
                            "name": STOCK_TARGET.name,
                            "industry": "银行",
                            "announcement_date": "2026-08-18",
                            "report_period": "20260630",
                            "profit_change_min": 20.0,
                            "profit_change_max": 40.0,
                        }
                    ],
                    "strongest_forecast_deteriorations": [],
                    "recent_earnings_express": [],
                },
                "financial_quality": {
                    "highest_roe": [],
                    "largest_roe_improvements": [],
                    "largest_roe_deteriorations": [],
                    "highest_debt_to_assets": [],
                    "lowest_cash_flow_to_revenue": [],
                },
                "sector_fundamentals": {
                    "classification_basis": "stock_basic.industry",
                    "classification_note": "不是申万行业",
                    "minimum_metric_sample_size": 5,
                    "valuation_extremes": [
                        {
                            "sector_code": SECTOR_TARGET.code,
                            "sector_name": SECTOR_TARGET.name,
                            "member_count": 24,
                            "positive_pe_sample_count": 18,
                            "median_positive_pe_ttm": 6.2,
                            "representative_stocks": [
                                {
                                    "ts_code": OTHER_STOCK_TARGET.code,
                                    "name": OTHER_STOCK_TARGET.name,
                                    "industry": "银行",
                                    "selection_signals": ["sector_low_positive_pe"],
                                }
                            ],
                        }
                    ],
                    "financial_quality_extremes": [],
                    "recent_reporting_activity": [],
                },
                "macro_and_rates": [
                    {
                        "series": "cn_pmi",
                        "latest_period": "202607",
                        "latest": {"value": 50.2},
                        "previous_period": "202606",
                        "previous": {"value": 49.8},
                    }
                ],
                "coverage": {"optional_failure_count": 0},
            },
            "issues": [],
            "source_dataset_count": 1,
            "total_stored_items": 1,
            "complete": True,
        }

    async def identity(ts_code: str, list_status: str = "L") -> dict[str, object]:
        calls.append("resolve_stock_identity")
        return _targeted_result("resolve_stock_identity", ("stock_basic",), actual_code)

    async def financial_statements(ts_code: str, period: str) -> dict[str, object]:
        calls.append(f"get_financial_statements:{period}")
        return _targeted_result(
            "get_financial_statements",
            ("income_statement", "balance_sheet", "cash_flow_statement"),
            actual_code,
            period=returned_period or period,
        )

    async def financial_quality(
        ts_code: str,
        period: str,
        composition_type: str = "P",
    ) -> dict[str, object]:
        calls.append(f"get_financial_quality:{period}")
        return _targeted_result(
            "get_financial_quality",
            ("financial_indicators", "business_composition", "audit_opinion"),
            actual_code,
            period=returned_period or period,
        )

    async def earnings(ts_code: str, period: str) -> dict[str, object]:
        calls.append(f"get_earnings_and_disclosure:{period}")
        return _targeted_result(
            "get_earnings_and_disclosure",
            ("earnings_forecast", "earnings_express", "disclosure_schedule"),
            actual_code,
            period=returned_period or period,
        )

    async def valuation(
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        calls.append("get_valuation_context")
        arguments_seen.append({"start_date": start_date, "end_date": end_date})
        return _targeted_result(
            "get_valuation_context",
            ("daily_valuation",),
            actual_code,
            trade_date=end_date.isoformat(),
        )

    async def dividend_ownership(
        ts_code: str,
        period: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        calls.append("get_dividend_and_ownership_context")
        return _targeted_result(
            "get_dividend_and_ownership_context",
            ("dividends", "top_holders", "top_floating_holders", "holder_counts"),
            actual_code,
            period=returned_period or period,
        )

    async def pledge(ts_code: str) -> dict[str, object]:
        calls.append("get_pledge_risk_context")
        return _targeted_result(
            "get_pledge_risk_context",
            ("pledge_statistics", "pledge_details"),
            actual_code,
        )

    tools = (
        create_structured_tool(
            name="get_daily_fundamental_snapshot",
            description="fake daily fundamental snapshot",
            args_schema=DailyFundamentalSnapshotInput,
            coroutine=daily_snapshot,
        ),
        create_structured_tool(
            name="resolve_stock_identity",
            description="fake stock identity",
            args_schema=StockIdentityInput,
            coroutine=identity,
        ),
        create_structured_tool(
            name="get_financial_statements",
            description="fake statements",
            args_schema=StockPeriodInput,
            coroutine=financial_statements,
        ),
        create_structured_tool(
            name="get_financial_quality",
            description="fake quality",
            args_schema=FinancialQualityInput,
            coroutine=financial_quality,
        ),
        create_structured_tool(
            name="get_earnings_and_disclosure",
            description="fake earnings",
            args_schema=StockPeriodInput,
            coroutine=earnings,
        ),
        create_structured_tool(
            name="get_valuation_context",
            description="fake valuation",
            args_schema=StockDateRangeToolInput,
            coroutine=valuation,
        ),
        create_structured_tool(
            name="get_dividend_and_ownership_context",
            description="fake dividend and ownership",
            args_schema=DividendOwnershipInput,
            coroutine=dividend_ownership,
        ),
        create_structured_tool(
            name="get_pledge_risk_context",
            description="fake pledge",
            args_schema=StockCodeInput,
            coroutine=pledge,
        ),
    )
    return context, tools, calls, arguments_seen


def _service_dataset(api_name: str) -> ServiceDataset:
    fields = ("ts_code", "trade_date", "pe_ttm")
    return ServiceDataset(
        api_name=api_name,
        query_params={"trade_date": "20260820"},
        requested_fields=fields,
        items=[{"ts_code": STOCK_TARGET.code, "trade_date": "20260820", "pe_ttm": 6.2}],
        item_traces=(
            ServiceItemTrace(
                page_index=0,
                source_offset=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
            ),
        ),
        pages=(
            ServicePageTrace(
                page_index=0,
                provider=ProviderSource.PRIMARY,
                from_cache=False,
                fetched_at=FETCHED_AT,
                offset=0,
                item_count=1,
                returned_fields=fields,
                response_bytes=128,
            ),
        ),
        as_of=AS_OF.date(),
        data_as_of=AS_OF.date(),
        received_item_count=1,
        discarded_item_count=0,
        complete=True,
    )


def _targeted_result(
    tool_name: str,
    labels: tuple[str, ...],
    ts_code: str,
    *,
    period: str | None = None,
    trade_date: str | None = None,
) -> dict[str, object]:
    datasets = []
    for label in labels:
        row = {"ts_code": ts_code, "ann_date": "20260818", "value": 100.0}
        if period is not None:
            row["end_date"] = period
        if trade_date is not None:
            row["trade_date"] = trade_date
        datasets.append(
            {
                "label": label,
                "api_name": label,
                "query_params": {"ts_code": ts_code, **({"period": period} if period else {})},
                "requested_fields": list(row),
                "rows": [
                    {
                        "data": row,
                        "source": {
                            "provider": "PRIMARY",
                            "from_cache": False,
                            "fetched_at": FETCHED_AT.isoformat(),
                            "page_index": 0,
                            "source_offset": 0,
                        },
                    }
                ],
                "received_item_count": 1,
                "returned_item_count": 1,
                "discarded_item_count": 0,
                "data_as_of": AS_OF.date().isoformat(),
                "complete": True,
                "source_summary": [
                    {
                        "provider": "PRIMARY",
                        "from_cache": False,
                        "page_count": 1,
                        "item_count": 1,
                        "response_bytes": 128,
                        "latest_fetched_at": FETCHED_AT.isoformat(),
                    }
                ],
            }
        )
    return {
        "tool_name": tool_name,
        "status": "ok",
        "as_of": AS_OF.isoformat(),
        "datasets": datasets,
        "issues": [],
        "total_returned_items": len(datasets),
        "complete": True,
    }
