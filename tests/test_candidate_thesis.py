"""LeadResearchStrategist 候选观点生成节点测试。"""

import asyncio
from datetime import datetime

from stock_research_agent.agents.strategist import (
    CandidateThesisDraft,
    CandidateThesisGeneration,
    CandidateThesisLimits,
    OpenAILeadResearchStrategistModel,
)
from stock_research_agent.agents.strategist.prompts import CANDIDATE_THESIS_SYSTEM_PROMPT
from stock_research_agent.agents.technical.models import (
    TechnicalAgentRunSummary,
    TechnicalResearchMode,
)
from stock_research_agent.domain import EvidenceRecord, ResearchTarget, SourceReference
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.graph import build_research_graph
from stock_research_agent.graph.nodes.candidate_thesis import (
    build_candidate_thesis_generation_node,
)
from stock_research_agent.graph.nodes.evidence_collector import evidence_collector_node

AS_OF = datetime.fromisoformat("2026-08-24T16:00:00+08:00")
RUN_ID = "run_20260824_160000_A_SHARE_strategist"
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")
OTHER_STOCK = ResearchTarget(type=TargetType.STOCK, code="000002.SZ", name="万科A")


class ScriptedStrategistModel:
    def __init__(self, generation: CandidateThesisGeneration) -> None:
        self.generation = generation
        self.calls = []

    async def generate_candidates(self, request):
        self.calls.append(request)
        return self.generation


def test_strategist_receives_every_collected_summary_and_builds_unverified_thesis() -> None:
    evidence = [
        _evidence(
            "ev_fundamental_001",
            EvidenceDomain.FUNDAMENTAL,
            "盈利改善",
            "公司盈利与经营现金流同步改善。",
        ),
        _evidence(
            "ev_technical_001",
            EvidenceDomain.TECHNICAL,
            "盈利改善",
            "公司盈利与经营现金流同步改善。",
        ),
        _evidence(
            "ev_flow_001",
            EvidenceDomain.SENTIMENT_FLOW,
            "资金出现连续流入",
            "近期资金流已经形成连续净流入。",
            status=VerificationStatus.UNVERIFIED,
        ),
    ]
    model = ScriptedStrategistModel(
        CandidateThesisGeneration(
            candidates=(
                CandidateThesisDraft(
                    target=STOCK,
                    title="经营改善尚未得到市场行为确认",
                    description="事实显示经营改善但资金偏弱，据此猜想市场仍怀疑改善持续性。",
                    direction=ThesisDirection.MIXED,
                    horizon="未来一个至两个季度",
                    supporting_evidence_ids=(
                        "ev_fundamental_001",
                        "ev_technical_001",
                    ),
                    contradicting_evidence_ids=("ev_flow_001",),
                    reasoning_summary="基本面与市场行为之间存在需要解释的背离。",
                    missing_questions=("盈利改善是否来自可持续核心业务？",),
                    catalysts=("下一季度核心收入继续增长",),
                    invalidation_conditions=("经营现金流重新转负",),
                ),
            ),
            generation_summary="生成一条跨基本面、技术和资金的待查证观点。",
        )
    )
    state = _collected_state(evidence)
    node = build_candidate_thesis_generation_node(model)

    result = asyncio.run(node(state))

    assert len(model.calls) == 1
    assert [item.evidence_id for item in model.calls[0].evidence] == [
        "ev_technical_001",
        "ev_fundamental_001",
        "ev_flow_001",
    ]
    assert len(result["thesis_pool"]) == 1
    thesis = result["thesis_pool"][0]
    assert thesis.thesis_id.startswith("th_")
    assert thesis.origin.type is ThesisOriginType.LEAD_STRATEGIST
    assert thesis.origin.agent == "LeadResearchStrategist"
    assert thesis.validation.status is ThesisValidationStatus.UNVERIFIED
    assert thesis.validation.confidence is None
    assert thesis.supporting_evidence_ids == [
        "ev_fundamental_001",
        "ev_technical_001",
    ]
    assert result["candidate_thesis_run_summary"].accepted_candidate_count == 1
    assert result["candidate_thesis_run_summary"].rejected_candidate_count == 0
    assert result["candidate_thesis_run_summary"].generation_summary == (
        "生成一条跨基本面、技术和资金的待查证观点。"
    )


def test_strategist_rejects_unknown_evidence_and_ungrounded_target() -> None:
    evidence = [_evidence("ev_known_001", EvidenceDomain.EVENT, "公司公告", "公司发布公告。")]
    model = ScriptedStrategistModel(
        CandidateThesisGeneration(
            candidates=(
                _draft(
                    target=STOCK,
                    title="引用不存在证据",
                    supporting=("ev_missing_001",),
                ),
                _draft(
                    target=OTHER_STOCK,
                    title="把平安银行证据写给万科",
                    supporting=("ev_known_001",),
                ),
            ),
            generation_summary="两个草稿都违反确定性引用规则。",
        )
    )
    node = build_candidate_thesis_generation_node(model)

    result = asyncio.run(node(_collected_state(evidence)))

    assert result["thesis_pool"] == []
    assert result["candidate_thesis_run_summary"].generated_candidate_count == 2
    assert result["candidate_thesis_run_summary"].rejected_candidate_count == 2
    assert "UNKNOWN_EVIDENCE_ID=ev_missing_001" in result["errors"][0]
    assert "TARGET_NOT_GROUNDED_BY_SUPPORTING_EVIDENCE" in result["errors"][0]


def test_empty_collection_does_not_call_model() -> None:
    model = ScriptedStrategistModel(
        CandidateThesisGeneration(candidates=(), generation_summary="没有候选。")
    )
    state = _collected_state([])
    node = build_candidate_thesis_generation_node(model)

    result = asyncio.run(node(state))

    assert model.calls == []
    assert result["candidate_thesis_run_summary"].stop_reason == "no_evidence"
    assert "thesis_pool" not in result


def test_context_hard_limit_fails_without_silent_truncation() -> None:
    evidence = [
        _evidence(
            "ev_large_001",
            EvidenceDomain.EVENT,
            "长文本证据",
            "证" * 2_000,
        )
    ]
    model = ScriptedStrategistModel(
        CandidateThesisGeneration(candidates=(), generation_summary="不会调用。")
    )
    node = build_candidate_thesis_generation_node(
        model,
        limits=CandidateThesisLimits(max_context_characters=1_000),
    )

    result = asyncio.run(node(_collected_state(evidence)))

    assert model.calls == []
    assert result["candidate_thesis_run_summary"].stop_reason == "context_limit_exceeded"
    assert "input was not truncated" in result["errors"][0]


def test_structured_output_method_can_select_json_schema() -> None:
    class RecordingChatModel:
        def __init__(self) -> None:
            self.calls = []

        def with_structured_output(self, schema, *, method, include_raw, strict):
            self.calls.append((schema, method, include_raw, strict))
            return object()

    chat_model = RecordingChatModel()
    OpenAILeadResearchStrategistModel(  # type: ignore[arg-type]
        chat_model,
        structured_output_method="json_schema",
    )

    assert len(chat_model.calls) == 1
    assert chat_model.calls[0][0]["title"] == "CandidateThesisGeneration"
    assert chat_model.calls[0][1] == "json_schema"
    assert chat_model.calls[0][2:] == (True, True)


def test_prompt_and_schema_preserve_candidate_boundaries() -> None:
    schema = CandidateThesisGeneration.model_json_schema()

    assert set(schema["properties"]) == {"candidates", "generation_summary"}
    assert "UNVERIFIED" in CANDIDATE_THESIS_SYSTEM_PROMPT
    assert "不能仅凭数量提高确信程度" in CANDIDATE_THESIS_SYSTEM_PROMPT
    assert "不得输出 Markdown" in CANDIDATE_THESIS_SYSTEM_PROMPT
    assert "invalidation_condition" in CANDIDATE_THESIS_SYSTEM_PROMPT
    assert "Few-shot" in CANDIDATE_THESIS_SYSTEM_PROMPT


def test_main_graph_runs_strategist_after_collector() -> None:
    class StubTechnicalGraph:
        async def ainvoke(self, state):
            return {
                "evidence_records": [
                    _evidence(
                        "ev_market_technical_001",
                        EvidenceDomain.TECHNICAL,
                        "市场宽度收缩",
                        "上涨股票占比下降。",
                        run_id=state["run_id"],
                        target=state["target"],
                        as_of=state["as_of"],
                    )
                ],
                "errors": [],
                "run_summary": TechnicalAgentRunSummary(
                    mode=TechnicalResearchMode.DAILY,
                    verification_rounds=0,
                    tool_call_count=1,
                    accepted_evidence_count=1,
                    rejected_evidence_count=0,
                    stop_reason="test_complete",
                ),
            }

    model = ScriptedStrategistModel(
        CandidateThesisGeneration(
            candidates=(
                _draft(
                    target=MARKET,
                    title="市场风险偏好可能转弱",
                    supporting=("ev_market_technical_001",),
                ),
            ),
            generation_summary="生成市场层面的候选观点。",
        )
    )
    graph = build_research_graph(
        technical_agent_graph_factory=lambda **_: StubTechnicalGraph(),
        lead_research_strategist_model=model,
    )

    result = asyncio.run(graph.ainvoke({"target": MARKET, "as_of": AS_OF}))

    assert result["evidence_collection"].accepted_count == 1
    assert len(result["thesis_pool"]) == 1
    assert result["thesis_pool"][0].title == "市场风险偏好可能转弱"
    assert len(model.calls) == 1


def _collected_state(evidence: list[EvidenceRecord]):
    state = {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "evidence_pool": evidence,
        "errors": [],
    }
    state.update(evidence_collector_node(state))
    return state


def _draft(
    *,
    target: ResearchTarget,
    title: str,
    supporting: tuple[str, ...],
) -> CandidateThesisDraft:
    return CandidateThesisDraft(
        target=target,
        title=title,
        description="根据已有事实提出一个仍需查证的方向性解释。",
        direction=ThesisDirection.MIXED,
        horizon="未来一个季度",
        supporting_evidence_ids=supporting,
        reasoning_summary="当前证据只能形成候选解释，不能宣布观点成立。",
        missing_questions=("还需要哪些反向证据？",),
        invalidation_conditions=("后续数据与当前观察方向相反",),
    )


def _evidence(
    evidence_id: str,
    domain: EvidenceDomain,
    title: str,
    description: str,
    *,
    run_id: str = RUN_ID,
    target: ResearchTarget = STOCK,
    as_of: datetime = AS_OF,
    status: VerificationStatus = VerificationStatus.VERIFIED,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        target=target,
        domain=domain,
        as_of=as_of,
        title=title,
        description=description,
        source_refs=[
            SourceReference(
                provider="test_provider",
                interface=domain.value.lower(),
                record_key=f"row:{evidence_id}",
                published_at=as_of,
            )
        ],
        verification_status=status,
        tags=["测试"],
        collected_by=f"{domain.value}ResearchAnalyst",
        created_at=as_of,
    )
