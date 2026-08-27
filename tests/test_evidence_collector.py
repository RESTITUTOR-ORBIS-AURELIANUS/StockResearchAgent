"""EvidenceCollectorNode 的确定性边界测试。"""

from datetime import date, datetime, timedelta

from stock_research_agent.domain import EvidenceRecord, ResearchTarget, SourceReference
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    TargetType,
    VerificationStatus,
)
from stock_research_agent.graph.nodes.evidence_collector import evidence_collector_node

AS_OF = datetime.fromisoformat("2026-08-24T16:00:00+08:00")
RUN_ID = "run_20260824_160000_A_SHARE_collector"
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")


def test_collector_keeps_semantically_similar_records_with_different_ids() -> None:
    records = [
        _evidence(
            evidence_id="ev_event_002",
            target=STOCK,
            domain=EvidenceDomain.EVENT,
            title="同一现象",
            description="两位研究员都观察到相同现象。",
        ),
        _evidence(
            evidence_id="ev_fundamental_001",
            target=STOCK,
            domain=EvidenceDomain.FUNDAMENTAL,
            title="同一现象",
            description="两位研究员都观察到相同现象。",
        ),
        _evidence(
            evidence_id="ev_technical_003",
            target=MARKET,
            domain=EvidenceDomain.TECHNICAL,
            title="市场证据",
            description="市场层面的确定性观察。",
        ),
    ]

    result = evidence_collector_node(_state(records))
    collection = result["evidence_collection"]

    assert collection is not None
    assert collection.total_input_count == 3
    assert collection.accepted_count == 3
    assert collection.rejected_count == 0
    assert [item.evidence_id for item in collection.evidence] == [
        "ev_technical_003",
        "ev_fundamental_001",
        "ev_event_002",
    ]
    assert collection.counts_by_domain["FUNDAMENTAL"] == 1
    assert collection.counts_by_domain["EVENT"] == 1
    assert collection.counts_by_target_type["STOCK"] == 2
    assert "未执行语义重复检查" in collection.policy_notes[0]
    assert "errors" not in result


def test_collector_rejects_cross_run_future_and_retracted_records() -> None:
    wrong_run = _evidence(
        evidence_id="ev_wrong_run_001",
        run_id="run_20260824_160000_A_SHARE_other",
    )
    wrong_as_of = _evidence(
        evidence_id="ev_wrong_asof_001",
        evidence_as_of=AS_OF - timedelta(minutes=1),
    )
    future_source = _evidence(
        evidence_id="ev_future_source_001",
        published_at=AS_OF + timedelta(seconds=1),
    )
    future_data = _evidence(
        evidence_id="ev_future_data_001",
        published_at=None,
        fetched_at=AS_OF + timedelta(minutes=5),
        data_as_of=date(2026, 8, 25),
    )
    retracted = _evidence(
        evidence_id="ev_retracted_001",
        status=VerificationStatus.RETRACTED,
    )

    result = evidence_collector_node(
        _state([wrong_run, wrong_as_of, future_source, future_data, retracted])
    )
    collection = result["evidence_collection"]

    assert collection is not None
    assert collection.accepted_count == 0
    assert collection.rejected_count == 5
    reasons = {
        item.evidence_id: {reason.value for reason in item.reasons}
        for item in collection.rejected
    }
    assert reasons["ev_wrong_run_001"] == {"RUN_ID_MISMATCH"}
    assert reasons["ev_wrong_asof_001"] == {"AS_OF_MISMATCH"}
    assert reasons["ev_future_source_001"] == {"SOURCE_AFTER_AS_OF"}
    assert reasons["ev_future_data_001"] == {"DATA_AFTER_AS_OF"}
    assert reasons["ev_retracted_001"] == {"RETRACTED"}
    assert result["errors"] == [
        "evidence collector rejected 5 record(s); inspect evidence_collection.rejected"
    ]


def test_collector_summary_preserves_traceability_without_copying_raw_rows() -> None:
    evidence = _evidence(evidence_id="ev_sources_001")

    collection = evidence_collector_node(_state([evidence]))["evidence_collection"]

    assert collection is not None
    summary = collection.evidence[0]
    assert summary.evidence_id == evidence.evidence_id
    assert summary.source_count == 1
    assert summary.source_providers == ("test_provider",)
    assert summary.source_interfaces == ("test_interface",)
    assert summary.description == evidence.description


def _state(records: list[EvidenceRecord]):
    return {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "evidence_pool": records,
        "errors": [],
    }


def _evidence(
    *,
    evidence_id: str,
    run_id: str = RUN_ID,
    target: ResearchTarget = STOCK,
    domain: EvidenceDomain = EvidenceDomain.EVENT,
    title: str = "测试证据",
    description: str = "用于验证 EvidenceCollectorNode 的测试证据。",
    evidence_as_of: datetime = AS_OF,
    published_at: datetime | None = AS_OF,
    fetched_at: datetime | None = None,
    data_as_of: date | None = None,
    status: VerificationStatus = VerificationStatus.VERIFIED,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        target=target,
        domain=domain,
        as_of=evidence_as_of,
        title=title,
        description=description,
        source_refs=[
            SourceReference(
                provider="test_provider",
                interface="test_interface",
                record_key=f"row:{evidence_id}",
                published_at=published_at,
                fetched_at=fetched_at,
                data_as_of=data_as_of,
            )
        ],
        verification_status=status,
        tags=["测试"],
        collected_by="TestResearchAnalyst",
        created_at=AS_OF,
    )
