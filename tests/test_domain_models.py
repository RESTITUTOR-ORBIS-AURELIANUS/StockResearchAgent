"""核心 JSON 数据契约的单元测试。"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from stock_research_agent.domain.common import ResearchTarget, SourceReference
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    ResearchFindingOutcome,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.evidence import EvidenceRecord
from stock_research_agent.domain.research_finding import (
    ResearchFinding,
    build_research_finding_id,
)
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation
from stock_research_agent.graph.state import merge_research_findings

AS_OF = datetime.fromisoformat("2026-08-18T15:30:00+08:00")


def stock_target() -> ResearchTarget:
    return ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")


def test_stock_target_rejects_invalid_code() -> None:
    with pytest.raises(ValidationError):
        ResearchTarget(type=TargetType.STOCK, code="1", name="错误示例")


def test_evidence_requires_timezone_aware_source_time() -> None:
    with pytest.raises(ValidationError):
        SourceReference(
            provider="primary_tushare_compatible",
            interface="daily",
            record_key="000001.SZ_20260818",
            published_at=datetime(2026, 8, 18, 15, 30),
        )


def test_market_source_can_use_fetch_time_and_data_date_without_fake_publish_time() -> None:
    source = SourceReference(
        provider="PRIMARY",
        interface="get_stock_price_context",
        record_key="ctx_abcdefghijklmnopqrstuvwxyzABCDEF",
        fetched_at=AS_OF,
        data_as_of=AS_OF.date(),
    )

    assert source.published_at is None
    assert source.fetched_at == AS_OF
    assert source.data_as_of == AS_OF.date()


def test_valid_evidence_can_be_serialized_to_json() -> None:
    evidence = EvidenceRecord(
        evidence_id="ev_20260818_000001_001",
        run_id="run_20260818_000001_abcd1234",
        target=stock_target(),
        domain=EvidenceDomain.TECHNICAL,
        as_of=AS_OF,
        title="收盘价站上二十日均线",
        description="截至收盘，前复权收盘价连续三个交易日位于二十日均线上方。",
        source_refs=[
            SourceReference(
                provider="primary_tushare_compatible",
                interface="daily",
                record_key="000001.SZ_20260818",
                published_at=AS_OF,
            )
        ],
        verification_status=VerificationStatus.VERIFIED,
        collected_by="TechnicalResearchAnalyst",
        created_at=AS_OF,
    )

    assert '"domain":"TECHNICAL"' in evidence.model_dump_json()


def test_unverified_thesis_cannot_have_confidence() -> None:
    with pytest.raises(ValidationError):
        ThesisRecord(
            thesis_id="th_20260818_000001_001",
            run_id="run_20260818_000001_abcd1234",
            target=stock_target(),
            as_of=AS_OF,
            title="候选观点",
            description="尚未查证的候选观点。",
            direction=ThesisDirection.BULLISH,
            horizon="未来一个季度",
            origin=ThesisOrigin(
                type=ThesisOriginType.LEAD_STRATEGIST,
                agent="LeadResearchStrategist",
            ),
            validation=ThesisValidation(
                status=ThesisValidationStatus.UNVERIFIED,
                confidence=0.8,
            ),
            created_by="LeadResearchStrategist",
            created_at=AS_OF,
            updated_at=AS_OF,
        )


def test_research_finding_with_real_evidence_is_auditable() -> None:
    finding = ResearchFinding(
        finding_id=build_research_finding_id(
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_technical_001",
            attempt=1,
        ),
        run_id="run_20260818_000001_abcd1234",
        request_id="rq_technical_001",
        thesis_id="th_20260818_000001_001",
        target=stock_target(),
        assigned_domain=EvidenceDomain.TECHNICAL,
        outcome=ResearchFindingOutcome.EVIDENCE_FOUND,
        summary="查到能够回答该问题的日线行情证据。",
        searched_sources=["primary:daily"],
        evidence_ids=["ev_20260818_000001_002"],
        created_at=AS_OF,
    )

    assert finding.evidence_ids == ["ev_20260818_000001_002"]
    assert finding.finding_id == build_research_finding_id(
        run_id=finding.run_id,
        request_id=finding.request_id,
        attempt=finding.attempt,
    )


def test_no_matching_evidence_is_not_fabricated_as_evidence_record() -> None:
    finding = ResearchFinding(
        finding_id=build_research_finding_id(
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            attempt=1,
        ),
        run_id="run_20260818_000001_abcd1234",
        request_id="rq_event_001",
        thesis_id="th_20260818_000001_001",
        target=stock_target(),
        assigned_domain=EvidenceDomain.EVENT,
        outcome=ResearchFindingOutcome.NO_MATCHING_EVIDENCE,
        summary="指定时间范围内没有找到匹配公告。",
        searched_sources=["akshare:stock_notice_report"],
        limitations=["没有结果不能证明该事件从未发生。"],
        evidence_ids=[],
        created_at=AS_OF,
    )

    assert finding.evidence_ids == []
    assert finding.outcome is ResearchFindingOutcome.NO_MATCHING_EVIDENCE


@pytest.mark.parametrize(
    "outcome",
    [
        ResearchFindingOutcome.NO_MATCHING_EVIDENCE,
        ResearchFindingOutcome.INSUFFICIENT_TOOL_COVERAGE,
        ResearchFindingOutcome.SOURCE_UNAVAILABLE,
        ResearchFindingOutcome.REQUEST_FAILED,
        ResearchFindingOutcome.BUDGET_EXHAUSTED,
    ],
)
def test_non_evidence_finding_rejects_fake_evidence_ids(
    outcome: ResearchFindingOutcome,
) -> None:
    with pytest.raises(ValidationError, match="不能关联 evidence_id"):
        ResearchFinding(
            finding_id=build_research_finding_id(
                run_id="run_20260818_000001_abcd1234",
                request_id="rq_event_001",
                attempt=1,
            ),
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            thesis_id="th_20260818_000001_001",
            target=stock_target(),
            assigned_domain=EvidenceDomain.EVENT,
            outcome=outcome,
            summary="没有形成真实证据。",
            searched_sources=["akshare:stock_notice_report"],
            limitations=["当前结果不足以回答问题。"],
            evidence_ids=["ev_fabricated_001"],
            created_at=AS_OF,
        )


def test_no_matching_evidence_requires_a_searched_source() -> None:
    with pytest.raises(ValidationError, match="实际搜索过的数据源"):
        ResearchFinding(
            finding_id=build_research_finding_id(
                run_id="run_20260818_000001_abcd1234",
                request_id="rq_event_001",
                attempt=1,
            ),
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            thesis_id="th_20260818_000001_001",
            target=stock_target(),
            assigned_domain=EvidenceDomain.EVENT,
            outcome=ResearchFindingOutcome.NO_MATCHING_EVIDENCE,
            summary="没有匹配结果。",
            limitations=["搜索范围有限。"],
            created_at=AS_OF,
        )


def test_unavailable_or_failed_finding_requires_limitations() -> None:
    with pytest.raises(ValidationError, match="必须说明 limitations"):
        ResearchFinding(
            finding_id=build_research_finding_id(
                run_id="run_20260818_000001_abcd1234",
                request_id="rq_event_001",
                attempt=1,
            ),
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            thesis_id="th_20260818_000001_001",
            target=stock_target(),
            assigned_domain=EvidenceDomain.EVENT,
            outcome=ResearchFindingOutcome.SOURCE_UNAVAILABLE,
            summary="数据源暂时不可用。",
            created_at=AS_OF,
        )


def test_research_finding_stable_id_changes_between_attempts() -> None:
    first = build_research_finding_id(
        run_id="run_20260818_000001_abcd1234",
        request_id="rq_event_001",
        attempt=1,
    )
    repeated = build_research_finding_id(
        run_id="run_20260818_000001_abcd1234",
        request_id="rq_event_001",
        attempt=1,
    )
    second = build_research_finding_id(
        run_id="run_20260818_000001_abcd1234",
        request_id="rq_event_001",
        attempt=2,
    )

    assert first == repeated
    assert first != second


def test_research_finding_rejects_noncanonical_id() -> None:
    with pytest.raises(ValidationError, match="finding_id 必须由"):
        ResearchFinding(
            finding_id="rf_arbitrary",
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            thesis_id="th_20260818_000001_001",
            target=stock_target(),
            assigned_domain=EvidenceDomain.EVENT,
            outcome=ResearchFindingOutcome.NO_MATCHING_EVIDENCE,
            summary="没有匹配结果。",
            searched_sources=["akshare:stock_notice_report"],
            limitations=["没有结果不构成反证。"],
            created_at=AS_OF,
        )


def test_research_finding_reducer_rejects_same_id_with_different_content() -> None:
    common = {
        "finding_id": build_research_finding_id(
            run_id="run_20260818_000001_abcd1234",
            request_id="rq_event_001",
            attempt=1,
        ),
        "run_id": "run_20260818_000001_abcd1234",
        "request_id": "rq_event_001",
        "thesis_id": "th_20260818_000001_001",
        "target": stock_target(),
        "assigned_domain": EvidenceDomain.EVENT,
        "outcome": ResearchFindingOutcome.NO_MATCHING_EVIDENCE,
        "searched_sources": ["akshare:stock_notice_report"],
        "limitations": ["没有结果不构成反证。"],
        "created_at": AS_OF,
    }
    original = ResearchFinding(summary="首次记录。", **common)
    conflicting = ResearchFinding(summary="同 ID 的不同内容。", **common)

    with pytest.raises(ValueError, match="immutable once recorded"):
        merge_research_findings([original], [conflicting])
