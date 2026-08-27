"""大模型客户端组合根。"""

from stock_research_agent.llm.factory import build_chat_model
from stock_research_agent.llm.structured_output import (
    StructuredOutputInvocationError,
    StructuredOutputOptions,
    build_observable_structured_output,
    describe_exception,
)

__all__ = [
    "StructuredOutputInvocationError",
    "StructuredOutputOptions",
    "build_chat_model",
    "build_observable_structured_output",
    "describe_exception",
]
