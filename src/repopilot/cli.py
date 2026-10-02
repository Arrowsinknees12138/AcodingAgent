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
ci_app = typer.Typer(help="导入失败 CI、查看诊断并生成候选修复")
app.add_typer(ci_app, name="ci")


def _ci_http(method: str, path: str, body: dict[str, object] | None = None) -> None:
    try:
        response = httpx.request(
            method, f"{_base_url()}{path}", json=body, headers=_headers(), timeout=60
        )
    except httpx.HTTPError as exc:
        typer.secho(f"API 不可达：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    _print_response(response)


@ci_app.command("jobs")
def ci_jobs(run_url: str) -> None:
    """列出指定 run attempt 的 job；查看失败 job ID。"""
    from urllib.parse import urlencode

    _ci_http("GET", "/v1/ci/jobs?" + urlencode({"run_url": run_url}))


@ci_app.command("import")
def ci_import(
    run_url: str,
    job_id: Annotated[int | None, typer.Option("--job-id")] = None,
    repair: Annotated[bool, typer.Option("--repair")] = False,
    max_cost: Annotated[float, typer.Option("--max-cost")] = 5,
    max_calls: Annotated[int, typer.Option("--max-calls")] = 50,
    wall_seconds: Annotated[int, typer.Option("--wall-seconds")] = 3600,
    sandbox_seconds: Annotated[int, typer.Option("--sandbox-seconds")] = 1800,
) -> None:
    """默认仅诊断；--repair 在通过复现门禁后启动修复。"""
    _ci_http(
        "POST",
        "/v1/ci/import",
        {
            "run_url": run_url,
            "job_id": job_id,
            "mode": "repair" if repair else "diagnose",
            "budget": {
                "max_cost_usd": max_cost,
                "max_model_calls": max_calls,
                "max_wall_time_seconds": wall_seconds,
                "max_sandbox_seconds": sandbox_seconds,
            },
        },
    )


@ci_app.command("show")
def ci_show(run_id: UUID) -> None:
    """查看分类、两次复现、最终报告和产物引用。"""
    _ci_http("GET", f"/v1/ci/{run_id}")


@ci_app.command("repair")
def ci_repair(run_id: UUID) -> None:
    """为已完成且可修复的诊断创建修复 Run，沿用原预算/审批设置。"""
    _ci_http("POST", f"/v1/ci/{run_id}:repair")


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
) -> None:
    """批准 Run 当前等待中的审批闸口。"""
    body = {"kind": kind, "decision": "approve", "reason": reason}
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
