"""所有领域模型共同使用的校验规则。"""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """领域模型基类。

    extra="forbid" 可以阻止 LLM 或外部接口偷偷塞入未定义字段；
    validate_assignment=True 可以在对象创建后修改属性时继续执行校验；
    str_strip_whitespace=True 会自动清除字符串首尾的无意义空白。
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )
