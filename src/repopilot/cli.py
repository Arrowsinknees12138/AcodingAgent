"""RepoPilot CLI。

设计约束（第 19.4 节）：CLI 只调用 HTTP API，不复制 Workflow 逻辑。
Milestone 1 阶段 API 只有健康检查路由，因此 CLI 暂时只提供 `health`
和 `version` 两个命令；`run` / `status` / `approve` / `cancel` /
`artifacts` 在 Milestone 3、7 落地对应 API 路由后再加入。
"""

from __future__ import annotations

import httpx
import typer

from repopilot import __version__
from repopilot.config import get_settings

app = typer.Typer(help="RepoPilot：本地多智能体代码修复原型 CLI")


@app.command()
def version() -> None:
    """打印 CLI 版本。"""
    typer.echo(__version__)


@app.command()
def health() -> None:
    """调用 API 的 /health/live，验证 CLI 与 API 之间的连通性。"""
    settings = get_settings()
    base_url = f"http://{settings.api_host}:{settings.api_port}"
    try:
        response = httpx.get(f"{base_url}/health/live", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(response.json())


def main() -> None:
    """`repopilot` console script 入口。"""
    app()


if __name__ == "__main__":
    main()
