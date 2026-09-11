#!/usr/bin/env bash
# 本地开发常用命令的薄封装。所有真实逻辑都在 uv/docker compose 命令本身，
# 本脚本不重复实现任何配置逻辑（见设计文档第 26 节）。
set -euo pipefail

cmd="${1:-help}"
shift || true

case "$cmd" in
  sync)
    uv sync ;;
  up)
    docker compose up -d postgres minio temporal temporal-ui ;;
  down)
    docker compose down ;;
  migrate)
    uv run alembic upgrade head ;;
  api)
    uv run repopilot-api ;;
  worker)
    uv run repopilot-worker "$@" ;;
  test)
    uv run pytest "$@" ;;
  lint)
    uv run ruff format --check . && uv run ruff check . ;;
  typecheck)
    uv run mypy src ;;
  check)
    "$0" lint && "$0" typecheck && "$0" test tests/unit ;;
  *)
    cat <<EOF
用法: scripts/dev.sh <command>
  sync       uv sync
  up         启动本地依赖 (postgres/minio/temporal)
  down       停止本地依赖
  migrate    执行数据库迁移
  api        启动 FastAPI
  worker     启动指定 worker，如: scripts/dev.sh worker orchestration
  test       运行 pytest
  lint       运行 ruff
  typecheck  运行 mypy
  check      lint + typecheck + 单元测试
EOF
    ;;
esac
