"""集中创建可复用的 OpenAI-compatible ChatModel。"""

import httpx
from langchain_openai import ChatOpenAI

from stock_research_agent.config import LLMSettings


def build_chat_model(
    settings: LLMSettings,
    *,
    http_async_client: httpx.AsyncClient | None = None,
) -> ChatOpenAI:
    """只负责连接参数；可由应用组合根注入并统一关闭 HTTP 连接池。"""

    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=str(settings.base_url).rstrip("/"),
        timeout=settings.request_timeout_seconds,
        max_retries=settings.max_retries,
        temperature=settings.temperature,
        http_async_client=http_async_client,
    )
