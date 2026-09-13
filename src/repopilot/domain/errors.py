"""错误分类（实施设计第 20 节）。

`ErrorCode` 到"是否重试 / Run 最终行为"的映射表本身不是纯数据，
会随 Workflow 逻辑演进，因此只在这里放"是否可重试"这一静态属性，
具体的 Run 状态跳转规则由 workflows 层实现，避免 domain 反过来
依赖 workflow 概念。
"""

from __future__ import annotations

from enum import Enum

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef


class ErrorCode(str, Enum):
    PLATFORM_ERROR = "PLATFORM_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    REPOSITORY_NOT_FOUND = "REPOSITORY_NOT_FOUND"
    REVISION_NOT_FOUND = "REVISION_NOT_FOUND"
    UNSUPPORTED_REPOSITORY = "UNSUPPORTED_REPOSITORY"
    POLICY_DENIED = "POLICY_DENIED"
    FORBIDDEN_PATH_REQUIRED = "FORBIDDEN_PATH_REQUIRED"
    PLAN_INVALID = "PLAN_INVALID"
    TEST_COMMAND_NOT_FOUND = "TEST_COMMAND_NOT_FOUND"
    TEST_BUNDLE_INVALID = "TEST_BUNDLE_INVALID"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_COMPLETION_UNKNOWN = "MODEL_COMPLETION_UNKNOWN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PATCH_PATH_DENIED = "PATCH_PATH_DENIED"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    PATCH_CONFLICT = "PATCH_CONFLICT"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_OOM = "SANDBOX_OOM"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"


# 不可重试错误（实施设计 8.5 节）；不在此集合中的错误默认按可重试处理，
# 但 MODEL_RATE_LIMITED / MODEL_COMPLETION_UNKNOWN 等需要 Activity 层
# 按各自的退避与"未知结果不自动重发"规则单独处理，不能简单归为"可重试"。
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.POLICY_DENIED,
        ErrorCode.BUDGET_EXCEEDED,
        ErrorCode.UNSUPPORTED_REPOSITORY,
        ErrorCode.FORBIDDEN_PATH_REQUIRED,
        ErrorCode.APPROVAL_REJECTED,
    }
)


class ErrorInfo(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool
    source: str
    details_ref: ArtifactRef | None = None
