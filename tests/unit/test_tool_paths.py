"""Agent 文件/命令工具的安全边界测试。"""

from __future__ import annotations

import pytest

from repopilot.services.sandbox_service import RunCommandRequest
from repopilot.tools.paths import (
    ToolPathDeniedError,
    normalize_relative_path,
    require_allowed_path,
)
from repopilot.tools.run_command import _validate_arguments


def test_normalize_relative_path_is_cross_platform_and_exact() -> None:
    assert normalize_relative_path(r"src\pkg\module.py") == "src/pkg/module.py"
    assert require_allowed_path("src/pkg/module.py", ("src/pkg/module.py",)) == (
        "src/pkg/module.py"
    )
    with pytest.raises(ToolPathDeniedError):
        require_allowed_path("src/pkg/module.py.bak", ("src/pkg/module.py",))


@pytest.mark.parametrize(
    "path",
    ("", "../secret", "src/../secret", "/etc/passwd", r"C:\secret", r"\\server\share"),
)
def test_normalize_relative_path_rejects_escape_forms(path: str) -> None:
    with pytest.raises(ToolPathDeniedError):
        normalize_relative_path(path)


def test_run_command_rejects_python_inline_code() -> None:
    with pytest.raises(ValueError):
        _validate_arguments(
            RunCommandRequest(
                executable="python",
                args=("-c", "print('escape')"),
                cwd=".",
                timeout_seconds=10,
            )
        )
