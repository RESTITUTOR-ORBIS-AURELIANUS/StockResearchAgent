"""完整原始数据快照的运行期仓库。"""

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from stock_research_agent.research_data.errors import (
    InvalidContextReferenceError,
    InvalidResearchRunIdError,
    ResearchDataNotFoundError,
    ResearchDataScopeError,
)
from stock_research_agent.research_data.models import ResearchDataBundle

_RUN_ID_PATTERN = re.compile(r"^run_[A-Za-z0-9_]+$")
_CONTEXT_REF_PATTERN = re.compile(r"^ctx_[A-Za-z0-9_-]{32}$")


def validate_run_id(run_id: str) -> str:
    """返回合法 run_id；非法值在进入仓库前立即失败。"""

    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise InvalidResearchRunIdError("run_id 必须类似 run_20260820_150000_000001_SZ_abcd1234")
    return run_id


def _validate_context_ref(context_ref: str) -> str:
    if not isinstance(context_ref, str) or not _CONTEXT_REF_PATTERN.fullmatch(context_ref):
        raise InvalidContextReferenceError("context_ref 不是 ResearchDataStore 生成的引用")
    return context_ref


@runtime_checkable
class ResearchDataStore(Protocol):
    """计算器读取完整数据时依赖的最小接口。"""

    async def put(self, run_id: str, bundle: ResearchDataBundle) -> str:
        """保存复合数据包并返回仅在 ``run_id`` 内有效的不可猜引用。"""

        ...

    async def get(self, run_id: str, context_ref: str) -> ResearchDataBundle:
        """读取独立的数据包快照，不允许跨 run_id 访问。"""

        ...

    async def cleanup(self, run_id: str) -> int:
        """删除一次研究运行产生的所有数据，返回删除数量。"""

        ...


@dataclass(frozen=True, slots=True)
class _StoredBundle:
    run_id: str
    payload: str


class InMemoryResearchDataStore:
    """进程内、按研究运行隔离的 DataStore 第一版实现。

    仓库保存 Pydantic JSON 快照而不是调用方传入的对象引用。这样 ``put`` 后修改
    原对象，或修改一次 ``get`` 的结果，都不会改变下一次读取到的原始数据。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _StoredBundle] = {}
        self._refs_by_run: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def put(self, run_id: str, bundle: ResearchDataBundle) -> str:
        run_id = validate_run_id(run_id)
        if not isinstance(bundle, ResearchDataBundle):
            raise TypeError("bundle 必须是 ResearchDataBundle")

        payload = bundle.model_dump_json()
        async with self._lock:
            context_ref = self._new_context_ref()
            self._entries[context_ref] = _StoredBundle(run_id=run_id, payload=payload)
            self._refs_by_run.setdefault(run_id, set()).add(context_ref)
        return context_ref

    async def get(self, run_id: str, context_ref: str) -> ResearchDataBundle:
        run_id = validate_run_id(run_id)
        context_ref = _validate_context_ref(context_ref)

        async with self._lock:
            stored = self._entries.get(context_ref)
            if stored is None:
                raise ResearchDataNotFoundError("context_ref 不存在或已经清理")
            if stored.run_id != run_id:
                raise ResearchDataScopeError("context_ref 不属于当前研究运行")
            payload = stored.payload

        return ResearchDataBundle.model_validate_json(payload)

    async def cleanup(self, run_id: str) -> int:
        run_id = validate_run_id(run_id)
        async with self._lock:
            context_refs = self._refs_by_run.pop(run_id, set())
            for context_ref in context_refs:
                self._entries.pop(context_ref, None)
            return len(context_refs)

    def _new_context_ref(self) -> str:
        while True:
            candidate = f"ctx_{secrets.token_urlsafe(24)}"
            if candidate not in self._entries:
                return candidate
