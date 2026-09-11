"""`tests/contract` 目录级 fixture。

MinIO/PostgreSQL/`artifact_store` 这几个跨目录共用的 fixture 已经上移到
`tests/conftest.py`（`tests/security` 下的 Docker Sandbox 测试也需要
它们）；这个文件先留空占位，`tests/contract` 专属、不跟其他目录共享的
fixture 以后加在这里。
"""

from __future__ import annotations
