"""两份投资建议确定性汇合和对称冲突生成测试。"""

from datetime import datetime

from stock_research_agent.domain import ResearchTarget
from stock_research_agent.domain.enums import (
    DecisionDimension,
    PortfolioManager,
    ProposalStatus,
    RecommendationAction,
    RecommendationProfile,
    TargetType,
)
from stock_research_agent.domain.recommendation import (
    ProposalEvaluation,
    ProposalItem,
    RecommendationRecord,
)
from stock_research_agent.graph.nodes.proposal_normalization import (
    proposal_normalization_node,
)

RUN_ID = "run_20260825_170000_A_SHARE_normalize"
AS_OF = datetime.fromisoformat("2026-08-25T17:00:00+08:00")
MARKET = ResearchTarget(type=TargetType.MARKET, code="A_SHARE", name="A股市场")
STOCK = ResearchTarget(type=TargetType.STOCK, code="000001.SZ", name="平安银行")


def test_normalization_builds_symmetric_conflicts_without_mutating_originals() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [
            _item("item_aggressive_action", PortfolioManager.AGGRESSIVE),
            _item(
                "item_aggressive_horizon",
                PortfolioManager.AGGRESSIVE,
                dimension=DecisionDimension.HORIZON,
            ),
            _item(
                "item_aggressive_risk",
                PortfolioManager.AGGRESSIVE,
                dimension=DecisionDimension.RISK_CONTROL,
            ),
        ],
    )
    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [
            _item("item_conservative_action", PortfolioManager.CONSERVATIVE),
            _item(
                "item_conservative_horizon",
                PortfolioManager.CONSERVATIVE,
                dimension=DecisionDimension.HORIZON,
            ),
            _item(
                "item_conservative_risk",
                PortfolioManager.CONSERVATIVE,
                dimension=DecisionDimension.RISK_CONTROL,
            ),
        ],
    )

    result = proposal_normalization_node(_state(aggressive, conservative))

    pool = result["normalized_proposal_pool"]
    items = {item.item_id: item for item in pool.proposal_items}
    assert items["item_aggressive_action"].conflicts_with == [
        "item_conservative_action"
    ]
    assert items["item_conservative_action"].conflicts_with == [
        "item_aggressive_action"
    ]
    assert items["item_aggressive_horizon"].conflicts_with == [
        "item_conservative_horizon"
    ]
    assert items["item_aggressive_risk"].conflicts_with == [
        "item_conservative_risk"
    ]
    assert all(item.conflicts_with == [] for item in aggressive.proposal_items)
    assert all(item.conflicts_with == [] for item in conservative.proposal_items)
    assert pool.proposal_items[0] is not aggressive.proposal_items[0]

    summary = result["proposal_normalization_run_summary"]
    assert summary.stop_reason == "complete"
    assert summary.input_proposal_count == 6
    assert summary.output_proposal_count == 6
    assert summary.conflict_pair_count == 3


def test_same_decision_slot_conflicts_even_when_proposal_text_is_compatible() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [
            _item(
                "item_aggressive_same_direction",
                PortfolioManager.AGGRESSIVE,
                proposal="维持适度超配。",
            )
        ],
    )
    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [
            _item(
                "item_conservative_same_direction",
                PortfolioManager.CONSERVATIVE,
                proposal="维持适度超配，但采用分批执行。",
            )
        ],
    )

    result = proposal_normalization_node(_state(aggressive, conservative))
    items = result["normalized_proposal_pool"].proposal_items

    assert items[0].conflicts_with == [items[1].item_id]
    assert items[1].conflicts_with == [items[0].item_id]
    assert result["proposal_normalization_run_summary"].conflict_pair_count == 1


def test_different_target_or_dimension_does_not_create_conflict() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [_item("item_market_action", PortfolioManager.AGGRESSIVE)],
    )
    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [
            _item(
                "item_stock_action",
                PortfolioManager.CONSERVATIVE,
                target=STOCK,
            )
        ],
    )

    result = proposal_normalization_node(_state(aggressive, conservative))

    assert all(
        item.conflicts_with == []
        for item in result["normalized_proposal_pool"].proposal_items
    )
    assert result["proposal_normalization_run_summary"].conflict_pair_count == 0


def test_missing_or_incompatible_recommendation_fails_closed() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [_item("item_aggressive_only", PortfolioManager.AGGRESSIVE)],
    )
    missing = proposal_normalization_node(_state(aggressive, None))
    assert "normalized_proposal_pool" not in missing
    assert missing["proposal_normalization_run_summary"].stop_reason == (
        "missing_recommendation"
    )

    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [_item("item_conservative_other_run", PortfolioManager.CONSERVATIVE)],
        run_id="run_other_001",
    )
    mismatched = proposal_normalization_node(_state(aggressive, conservative))
    assert "normalized_proposal_pool" not in mismatched
    assert mismatched["proposal_normalization_run_summary"].stop_reason == (
        "invalid_state"
    )
    assert "run_id mismatch" in mismatched["errors"][0]


def test_duplicate_manager_decision_slot_fails_closed() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [
            _item("item_aggressive_duplicate_1", PortfolioManager.AGGRESSIVE),
            _item("item_aggressive_duplicate_2", PortfolioManager.AGGRESSIVE),
        ],
    )
    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [_item("item_conservative_valid", PortfolioManager.CONSERVATIVE)],
    )

    result = proposal_normalization_node(_state(aggressive, conservative))

    assert "normalized_proposal_pool" not in result
    assert result["proposal_normalization_run_summary"].stop_reason == "invalid_state"
    assert "normalization failed" in result["errors"][0]


def test_compatible_existing_pool_makes_node_idempotent() -> None:
    aggressive = _record(
        PortfolioManager.AGGRESSIVE,
        [_item("item_aggressive_idempotent", PortfolioManager.AGGRESSIVE)],
    )
    conservative = _record(
        PortfolioManager.CONSERVATIVE,
        [_item("item_conservative_idempotent", PortfolioManager.CONSERVATIVE)],
    )
    state = _state(aggressive, conservative)
    first = proposal_normalization_node(state)

    assert proposal_normalization_node(
        {**state, "normalized_proposal_pool": first["normalized_proposal_pool"]}
    ) == {}


def _item(
    item_id: str,
    manager: PortfolioManager,
    *,
    dimension: DecisionDimension = DecisionDimension.ACTION,
    target: ResearchTarget = MARKET,
    proposal: str = "根据已查证观点形成一条独立建议。",
) -> ProposalItem:
    return ProposalItem(
        item_id=item_id,
        target=target,
        decision_dimension=dimension,
        conflict_group=(
            f"{target.type.value}:{target.code.upper()}:{dimension.value}"
        ),
        conflicts_with=[],
        proposer=manager,
        revision=1,
        proposal=proposal,
        supporting_thesis_ids=[f"th_{item_id.removeprefix('item_')}"],
        evaluations=[
            ProposalEvaluation(
                manager=manager,
                support_score=0.75,
                reason="该经理独立形成的初始坚持理由。",
            )
        ],
        status=ProposalStatus.PROPOSED,
    )


def _record(
    manager: PortfolioManager,
    items: list[ProposalItem],
    *,
    run_id: str = RUN_ID,
) -> RecommendationRecord:
    profile = {
        PortfolioManager.AGGRESSIVE: RecommendationProfile.AGGRESSIVE,
        PortfolioManager.CONSERVATIVE: RecommendationProfile.CONSERVATIVE,
    }[manager]
    supporting_ids = list(
        dict.fromkeys(
            thesis_id
            for item in items
            for thesis_id in item.supporting_thesis_ids
        )
    )
    return RecommendationRecord(
        recommendation_id=f"rec_{manager.name.lower()}_normalization",
        run_id=run_id,
        as_of=AS_OF,
        profile=profile,
        target=MARKET,
        action=RecommendationAction.HOLD,
        horizon="未来一个季度",
        confidence=0.7,
        supporting_thesis_ids=supporting_ids,
        summary="独立投资建议。",
        risk_summary="核心观点失效时降低风险暴露。",
        proposal_items=items,
        generated_by=manager.value,
        created_at=AS_OF,
    )


def _state(
    aggressive: RecommendationRecord | None,
    conservative: RecommendationRecord | None,
):
    return {
        "run_id": RUN_ID,
        "target": MARKET,
        "as_of": AS_OF,
        "aggressive_recommendation": aggressive,
        "conservative_recommendation": conservative,
        "errors": [],
    }
