#!/usr/bin/env python3
"""Workflow Replay 测试（实施设计第 8.9、24 节）。

CI 用真实 Workflow 执行产生的历史样本重放，确保 Workflow 代码的后续改动
没有破坏确定性重放（例如改了参数顺序、引入了非确定性调用）。样本存放在
`tests/fixtures/histories/*.json`，用 `scripts/export_workflow_history.py`
生成。

用法：
    uv run python scripts/replay_workflows.py tests/fixtures/histories
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.workflows.code_repair import CodeRepairWorkflow


async def _replay_all(histories_dir: Path) -> int:
    history_files = sorted(histories_dir.glob("*.json"))
    if not history_files:
        print(f"没有在 {histories_dir} 找到任何历史样本（*.json）", file=sys.stderr)
        return 1

    replayer = Replayer(workflows=[CodeRepairWorkflow], data_converter=data_converter)
    failures = 0
    for path in history_files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        history = WorkflowHistory.from_json(
            workflow_id=raw.get("workflow_id", path.stem), history=raw["history"]
        )
        try:
            await replayer.replay_workflow(history)
            print(f"OK    {path.name}")
        except Exception as exc:  # noqa: BLE001 - 需要汇总所有失败样本再统一报告
            failures += 1
            print(f"FAIL  {path.name}: {exc}", file=sys.stderr)

    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("histories_dir", type=Path)
    args = parser.parse_args()

    exit_code = asyncio.run(_replay_all(args.histories_dir))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
