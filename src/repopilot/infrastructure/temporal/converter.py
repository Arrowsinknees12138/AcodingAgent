"""Temporal Data Converter：让 Workflow/Activity 之间可以直接传递本项目的
`StrictModel`（Pydantic v2）子类，而不用手写一层 dict 序列化。

客户端和所有 Worker 必须使用同一个 DataConverter，否则相同的 Payload
会被两端不同的方式解析，导致 Worker 启动后无法处理已经在跑的 Workflow。
"""

from __future__ import annotations

from temporalio.contrib.pydantic import pydantic_data_converter

data_converter = pydantic_data_converter
