#!/usr/bin/env python3
"""导出一个真实 Workflow 执行的 History，供 `scripts/replay_workflows.py`
在 CI 里做确定性重放测试（实施设计第 8.9、24 节）。

用法：
    uv run python scripts/export_workflow_history.py <workflow_id> \
        tests/fixtures/histories/<name>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from repopilot.config import get_settings
from repopilot.infrastructure.temporal.client import connect


async def _export(workflow_id: str, output_path: Path) -> None:
    settings = get_settings()
    client = await connect(settings)
    handle = client.get_workflow_handle(workflow_id)
    history = await handle.fetch_history()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"workflow_id": workflow_id, "history": history.to_json_dict()}, indent=2),
        encoding="utf-8",
    )
    print(f"已导出到 {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id")
    parser.add_argument("output_path", type=Path)
    args = parser.parse_args()

    asyncio.run(_export(args.workflow_id, args.output_path))


if __name__ == "__main__":
    main()
