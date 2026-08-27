"""一次研究运行内的数据引用错误。"""


class ResearchDataStoreError(RuntimeError):
    """ResearchDataStore 的公共错误基类。"""


class InvalidResearchRunIdError(ResearchDataStoreError, ValueError):
    """run_id 不符合研究工作流约定。"""


class InvalidContextReferenceError(ResearchDataStoreError, ValueError):
    """context_ref 不是本系统生成的引用格式。"""


class ResearchDataNotFoundError(ResearchDataStoreError, LookupError):
    """指定引用已经清理或从未存在。"""


class ResearchDataScopeError(ResearchDataStoreError, PermissionError):
    """调用方试图跨研究运行读取数据。"""
