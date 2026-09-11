"""Git CLI 的安全封装（实施设计第 12.3 节）。

约束：
- 永远用 argv list 调用系统 `git`，不用 `shell=True`，不拼接字符串。
- `GIT_CONFIG_NOSYSTEM=1` + `GIT_CONFIG_GLOBAL` 指向一个隔离、内容固定
  为空的 config 文件，避免读到宿主机全局/系统 git 配置（包括别名、
  凭据助手等可能改变命令行为的设置）。
- `core.hooksPath` 指向一个空目录，彻底禁用仓库自带的 hooks。
- 不通过 `git` 执行仓库内脚本来判断状态（例如不 `git config --get` 一个
  来自仓库内容的键之后直接拿去做 shell 插值）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


class GitCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(args)} 退出码 {returncode}\nstdout: {stdout}\nstderr: {stderr}"
        )
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitCli:
    """所有 Git 调用的唯一入口；`data_dir` 下维护隔离的 HOME/config。"""

    def __init__(self, data_dir: Path) -> None:
        self._isolation_dir = data_dir / "git-isolation"
        self._isolation_dir.mkdir(parents=True, exist_ok=True)
        self._empty_hooks_dir = self._isolation_dir / "empty-hooks"
        self._empty_hooks_dir.mkdir(parents=True, exist_ok=True)
        self._global_config_path = self._isolation_dir / "gitconfig"
        if not self._global_config_path.exists():
            self._global_config_path.write_text("", encoding="utf-8")

    async def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout_seconds: float = 120,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        """执行一次 git 命令，返回 stdout；非零退出抛 `GitCommandError`。"""
        env = {
            # PATH 是定位 git 可执行文件所必需的，不算敏感信息；真正的
            # Secret（模型/MinIO/Sandbox 凭据）从不出现在这个 env 字典里。
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self._global_config_path),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }
        if os.name == "nt":
            # Windows 上部分 git 组件（尤其是 credential helper 相关路径
            # 解析）依赖 SystemRoot；缺失时某些 git 子命令会异常报错。
            env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
        if env_overrides:
            env.update(env_overrides)

        full_args = ["git", "-c", f"core.hooksPath={self._empty_hooks_dir}", *args]
        process = await asyncio.create_subprocess_exec(
            *full_args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise GitCommandError(args, process.returncode or -1, stdout, stderr)
        return stdout
