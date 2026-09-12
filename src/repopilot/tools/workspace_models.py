"""文件读取、检索和写入工具的结构化参数/结果。"""

from __future__ import annotations

from pydantic import Field

from repopilot.domain import StrictModel


class ReadFileRequest(StrictModel):
    path: str
    start_line: int = Field(default=1, gt=0)
    end_line: int | None = Field(default=None, gt=0)


class ReadFileResult(StrictModel):
    path: str
    content: str
    truncated: bool


class SearchCodeRequest(StrictModel):
    query: str = Field(min_length=1, max_length=1_000)
    paths: tuple[str, ...] = ()
    max_results: int = Field(default=50, gt=0, le=200)


class SearchMatch(StrictModel):
    path: str
    line: int = Field(gt=0)
    text: str


class SearchCodeResult(StrictModel):
    matches: tuple[SearchMatch, ...]
    truncated: bool


class WriteFileRequest(StrictModel):
    path: str
    content: str


class WriteFileResult(StrictModel):
    path: str
    size_bytes: int = Field(ge=0)
