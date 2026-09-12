"""RepoPilot CLI。

设计约束（第 19.4 节）：CLI 只调用 HTTP API，不复制 Workflow 逻辑。
Milestone 1 阶段 API 只有健康检查路由，因此 CLI 暂时只提供 `health`
和 `version` 两个命令；`run` / `status` / `approve` / `cancel` /
`artifacts` 在 Milestone 3、7 落地对应 API 路由后再加入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import typer

from repopilot import __version__
from repopilot.config import get_settings

app = typer.Typer(help="RepoPilot：本地多智能体代码修复原型 CLI")


def _base_url() -> str:
    settings = get_settings()
    return f"http://{settings.api_host}:{settings.api_port}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().api_token.get_secret_value()}"}


def _print_response(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.secho(f"请求失败（{response.status_code}）：{response.text}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if response.content:
        typer.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))


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


@app.command("run")
def create_run(
    repo: Annotated[str, typer.Option("--repo")],
    task: Annotated[Path, typer.Option("--task", exists=True, dir_okay=False)],
    revision: Annotated[str | None, typer.Option("--revision")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """从需求文件创建一个 Run。"""
    body = {
        "repository": {"url": repo, "revision": revision},
        "requirement": task.read_text(encoding="utf-8"),
        "acceptance_criteria": None,
        "budget": {
            "max_cost_usd": 5,
            "max_wall_time_seconds": 3600,
            "max_model_calls": 50,
            "max_sandbox_seconds": 1800,
        },
    }
    headers = _headers() | {"Idempotency-Key": idempotency_key or str(uuid4())}
    try:
        response = httpx.post(f"{_base_url()}/v1/runs", json=body, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@app.command()
def status(run_id: UUID) -> None:
    """查询 Run 当前状态。"""
    try:
        response = httpx.get(f"{_base_url()}/v1/runs/{run_id}", headers=_headers(), timeout=5.0)
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@app.command()
def events(run_id: UUID) -> None:
    """列出 Run 的审计事件。"""
    try:
        response = httpx.get(
            f"{_base_url()}/v1/runs/{run_id}/events", headers=_headers(), timeout=5.0
        )
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@app.command()
def artifacts(run_id: UUID) -> None:
    """列出 Run 的 Artifact 元数据。"""
    try:
        response = httpx.get(
            f"{_base_url()}/v1/runs/{run_id}/artifacts", headers=_headers(), timeout=5.0
        )
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@app.command()
def approve(
    run_id: UUID,
    kind: Annotated[
        str,
        typer.Option("--kind", help="requirements/plan/execution/test/delivery"),
    ],
    reason: Annotated[str, typer.Option("--reason")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
) -> None:
    """批准 Run 当前等待中的审批闸口。"""
    body = {"kind": kind, "decision": "approve", "actor_id": actor_id, "reason": reason}
    try:
        response = httpx.post(
            f"{_base_url()}/v1/runs/{run_id}/approvals",
            json=body,
            headers=_headers(),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@app.command()
def cancel(run_id: UUID) -> None:
    """取消一个 Run。"""
    try:
        response = httpx.post(
            f"{_base_url()}/v1/runs/{run_id}:cancel",
            headers=_headers(),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


def main() -> None:
    """`repopilot` console script 入口。"""
    app()


if __name__ == "__main__":
    main()
