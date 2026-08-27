"""Model construction helpers shared by technical calculators."""

from stock_research_agent.analytics.technical.models import (
    MetricStatus,
    ScalarMetric,
    WindowMetric,
)


def scalar_available(value: float, sample_size: int) -> ScalarMetric:
    return ScalarMetric(value=value, status=MetricStatus.AVAILABLE, sample_size=sample_size)


def scalar_unavailable(
    status: MetricStatus,
    sample_size: int,
    reason: str,
) -> ScalarMetric:
    return ScalarMetric(value=None, status=status, sample_size=sample_size, reason=reason)


def window_available(window: int, value: float, sample_size: int) -> WindowMetric:
    return WindowMetric(
        window=window,
        value=value,
        status=MetricStatus.AVAILABLE,
        sample_size=sample_size,
    )


def window_unavailable(
    window: int,
    status: MetricStatus,
    sample_size: int,
    reason: str,
) -> WindowMetric:
    return WindowMetric(
        window=window,
        value=None,
        status=status,
        sample_size=sample_size,
        reason=reason,
    )
