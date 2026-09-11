"""统一 JSON 日志配置（见实施设计第 22.3 节）。

要求：
- 所有日志为结构化 JSON，方便本地和未来生产环境统一采集。
- 绝不打印源码、Prompt、模型原始回复或任何 Secret；调用方必须只传入
  已脱敏的字段（例如 ArtifactRef 而不是 Artifact 内容）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(*, level: int = logging.INFO) -> None:
    """初始化 structlog + 标准库 logging，输出单行 JSON 到 stdout。"""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(**initial_values: Any) -> structlog.stdlib.BoundLogger:
    """获取带初始上下文（如 run_id / work_item_id）的 logger。"""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(**initial_values)
    return logger
