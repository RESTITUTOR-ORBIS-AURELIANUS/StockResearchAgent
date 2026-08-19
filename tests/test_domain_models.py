"""核心 JSON 数据契约的单元测试。"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from stock_research_agent.domain.common import ResearchTarget, SourceReference
from stock_research_agent.domain.enums import (
    EvidenceDomain,
    TargetType,
    ThesisDirection,
    ThesisOriginType,
    ThesisValidationStatus,
    VerificationStatus,
)
from stock_research_agent.domain.evidence import EvidenceRecord
from stock_research_agent.domain.thesis import ThesisOrigin, ThesisRecord, ThesisValidation

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
