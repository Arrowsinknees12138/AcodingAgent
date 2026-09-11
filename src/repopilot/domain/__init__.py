"""领域层：纯数据契约，禁止依赖任何框架（见实施设计第 6 节）。

`domain` 不得 import FastAPI、Temporal、SQLAlchemy、OpenAI 或 Docker SDK，
这条规则由 `pyproject.toml` 中的 import-linter contract 在 CI 中强制检查，
而不是靠人工 review 记住。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """所有领域契约的基类：严格类型、拒绝多余字段、创建后不可变。

    - `strict=True`：不做隐式类型转换（例如字符串 "1" 不会被当作 int 1）。
    - `extra="forbid"`：未声明字段直接报错，而不是静默丢弃。
    - `frozen=True`：实例创建后不可变，天然适合作为 Temporal Activity/
      Workflow 之间传递的消息，也避免共享可变状态引入的隐蔽 bug。
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
    )


__all__ = ["StrictModel"]
