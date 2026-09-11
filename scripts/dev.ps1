# 本地开发常用命令的薄封装（PowerShell 版本，与 dev.sh 行为一致）。
# 见设计文档第 26 节：脚本只封装命令，不复制配置逻辑。
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

switch ($Command) {
    "sync"      { uv sync }
    "up"        { docker compose up -d postgres minio temporal temporal-ui }
    "down"      { docker compose down }
    "migrate"   { uv run alembic upgrade head }
    "api"       { uv run repopilot-api }
    "worker"    { uv run repopilot-worker @Rest }
    "test"      { uv run pytest @Rest }
    "lint"      { uv run ruff format --check .; if ($?) { uv run ruff check . } }
    "typecheck" { uv run mypy src }
    "check"     {
        & $PSCommandPath lint
        if ($?) { & $PSCommandPath typecheck }
        if ($?) { & $PSCommandPath test tests/unit }
    }
    default {
        @"
用法: scripts/dev.ps1 <command>
  sync       uv sync
  up         启动本地依赖 (postgres/minio/temporal)
  down       停止本地依赖
  migrate    执行数据库迁移
  api        启动 FastAPI
  worker     启动指定 worker，如: scripts/dev.ps1 worker orchestration
  test       运行 pytest
  lint       运行 ruff
  typecheck  运行 mypy
  check      lint + typecheck + 单元测试
"@
    }
}
