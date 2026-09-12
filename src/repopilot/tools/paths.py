"""跨平台、fail-closed 的 Agent 相对路径校验。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ToolPathDeniedError(PermissionError):
    pass


def normalize_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise ToolPathDeniedError("路径不能为空或包含 NUL")
    if value.startswith("/") or value.startswith("\\\\") or _DRIVE_RE.match(value):
        raise ToolPathDeniedError("只允许 workspace 内的相对路径")
    normalized_input = value.replace("\\", "/")
    parts = normalized_input.split("/")
    if any(part in {"", ".."} for part in parts):
        raise ToolPathDeniedError("路径不能包含空段或 ..")
    normalized = PurePosixPath(normalized_input).as_posix()
    if normalized in {"", "."}:
        raise ToolPathDeniedError("路径必须指向 workspace 内的文件")
    return normalized


def require_allowed_path(value: str, allowed: tuple[str, ...]) -> str:
    normalized = normalize_relative_path(value)
    normalized_allowed = {normalize_relative_path(path) for path in allowed}
    if normalized not in normalized_allowed:
        raise ToolPathDeniedError(f"路径未在精确 allowlist 中: {normalized}")
    return normalized
