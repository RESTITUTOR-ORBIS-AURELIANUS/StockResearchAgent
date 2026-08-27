"""数据 Service 层的异常。

Provider 异常会原样向上传递；这里的异常只描述 Service 自己发现的输入、
分页或数据契约问题。
"""


class ServiceError(RuntimeError):
    """所有数据 Service 自身异常的基类。"""


class ServiceInputError(ServiceError, ValueError):
    """调用方给出的业务参数不合法。"""


class ServiceApiOwnershipError(ServiceError):
    """某个 Service 试图调用不属于自己的接口。"""


class ServicePaginationError(ServiceError):
    """分页响应无法安全、完整地继续读取。"""


class ServiceDataValidationError(ServiceError):
    """上游虽然返回成功，但数据不满足 Service 的确定性约束。"""
