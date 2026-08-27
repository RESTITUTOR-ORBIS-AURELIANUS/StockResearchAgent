"""Dependency-free deterministic technical analytics.

The LLM chooses *which* calculator to call.  The calculator receives rows loaded
from a run-scoped data bundle and performs all numerical work deterministically.
"""

from stock_research_agent.analytics.technical.errors import (
    TechnicalAnalyticsError,
    TechnicalAnalyticsErrorCode,
)
from stock_research_agent.analytics.technical.models import (
    AdjustmentMode,
    MomentumInput,
    MomentumResult,
    RelativeStrengthInput,
    RelativeStrengthResult,
    ReturnAndTrendInput,
    ReturnAndTrendResult,
    RiskAndTradabilityInput,
    RiskAndTradabilityResult,
    TechnicalInstrumentKind,
    TechnicalSeriesInput,
    VolumeAndLiquidityInput,
    VolumeAndLiquidityResult,
)
from stock_research_agent.analytics.technical.momentum import calculate_momentum
from stock_research_agent.analytics.technical.preparation import prepare_price_series
from stock_research_agent.analytics.technical.relative_strength import (
    calculate_relative_strength,
)
from stock_research_agent.analytics.technical.return_and_trend import (
    calculate_return_and_trend,
)
from stock_research_agent.analytics.technical.risk_and_tradability import (
    calculate_risk_and_tradability,
)
from stock_research_agent.analytics.technical.volume_and_liquidity import (
    calculate_volume_and_liquidity,
)

__all__ = [
    "AdjustmentMode",
    "MomentumInput",
    "MomentumResult",
    "RelativeStrengthInput",
    "RelativeStrengthResult",
    "ReturnAndTrendInput",
    "ReturnAndTrendResult",
    "RiskAndTradabilityInput",
    "RiskAndTradabilityResult",
    "TechnicalAnalyticsError",
    "TechnicalAnalyticsErrorCode",
    "TechnicalSeriesInput",
    "TechnicalInstrumentKind",
    "VolumeAndLiquidityInput",
    "VolumeAndLiquidityResult",
    "calculate_momentum",
    "calculate_relative_strength",
    "calculate_return_and_trend",
    "calculate_risk_and_tradability",
    "calculate_volume_and_liquidity",
    "prepare_price_series",
]
