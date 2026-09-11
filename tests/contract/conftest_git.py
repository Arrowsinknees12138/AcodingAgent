"""`tests/contract/test_git_repository_service.py` 专用的 fixture 辅助函数。

之所以不放进 `conftest.py`：这里用同步 `subprocess` 直接操作一个本地
git 仓库作为测试用的"origin"，跟 `conftest.py` 里 MinIO/PostgreSQL 那套
异步 fixture 关注点不同，分开更容易读。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@localhost",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@localhost",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stdout}\n{result.stderr}")
    return result.stdout


def init_origin_repo(path: Path) -> str:
    """创建一个包含两个 Python 文件的普通（非 bare）仓库，返回初始 commit 的 SHA。"""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", "main"], cwd=path)

    (path / "pkg").mkdir()
    (path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (path / "pkg" / "foo.py").write_text(
        "def foo():\n    return 1\n",
        encoding="utf-8",
    )
    (path / "pkg" / "bar.py").write_text(
        "def bar():\n    return 2\n",
        encoding="utf-8",
    )
    (path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")

    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-m", "initial commit"], cwd=path)
    return run_git(["rev-parse", "HEAD"], cwd=path).strip()


def make_patch(
    origin_path: Path, base_revision: str, file_contents: dict[str, str | None]
) -> tuple[bytes, tuple[str, ...]]:
    """在 `origin_path`（假设当前就 checkout 在 `base_revision`）里写入/删除
    给定文件，用 `git diff` 生成一个真实、能被 `git apply` 接受的 unified
    diff，再把工作区还原回干净状态。比手写 diff 文本可靠得多。
    """
    current = run_git(["rev-parse", "HEAD"], cwd=origin_path).strip()
    if current != base_revision:
        raise AssertionError("origin 必须正好 checkout 在 base_revision 上")

    touched_paths = tuple(sorted(file_contents))
    for relative_path, content in file_contents.items():
        file_path = origin_path / relative_path
        if content is None:
            file_path.unlink()
        else:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

    diff_text = run_git(["diff", "--no-color", "HEAD"], cwd=origin_path)
    run_git(["checkout", "--", "."], cwd=origin_path)
    run_git(["clean", "-fd"], cwd=origin_path)
    return diff_text.encode("utf-8"), touched_paths
