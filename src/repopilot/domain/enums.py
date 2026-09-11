"""跨领域枚举（实施设计第 7、7.2、7.3、8.6 节）。"""

from __future__ import annotations

from enum import Enum


class AgentRole(str, Enum):
    PLANNER = "planner"
    QA = "qa"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"


class WorkItemStatus(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactKind(str, Enum):
    CREATE_RUN_REQUEST = "create_run_request"
    TASK_SPEC = "task_spec"
    REPOSITORY_SNAPSHOT = "repository_snapshot"
    SYMBOL_INDEX = "symbol_index"
    CHANGE_PLAN = "change_plan"
    SOURCE_ARCHIVE = "source_archive"
    DEVELOPER_CONTEXT = "developer_context"
    PATCH = "patch"
    INTEGRATED_PATCH = "integrated_patch"
    EXPORTED_INTERFACE = "exported_interface"
    TEST_PLAN = "test_plan"
    TEST_BUNDLE = "test_bundle"
    BASELINE_REPORT = "baseline_report"
    VERIFICATION_REPORT = "verification_report"
    REVIEW_DECISION = "review_decision"
    FINAL_REPORT = "final_report"
    CLEANUP_REPORT = "cleanup_report"
    TRAJECTORY = "trajectory"
    MODEL_RESPONSE = "model_response"
    LOG = "log"


class RunStatus(str, Enum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    WAITING_REQUIREMENTS_APPROVAL = "waiting_requirements_approval"
    BASELINING = "baselining"
    PLANNING = "planning"
    WAITING_PLAN_APPROVAL = "waiting_plan_approval"
    DESIGNING_TESTS = "designing_tests"
    WAITING_TEST_APPROVAL = "waiting_test_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    REPLANNING = "replanning"
    WAITING_DELIVERY_APPROVAL = "waiting_delivery_approval"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskFlag(str, Enum):
    DEPENDENCY_CHANGE = "dependency_change"
    AUTH_CHANGE = "auth_change"
    MIGRATION = "migration"
    CI_CONFIG = "ci_config"
    LARGE_SCOPE = "large_scope"
    UNCLEAR_ACCEPTANCE_CRITERIA = "unclear_acceptance_criteria"
    PROMPT_INJECTION_SUSPECTED = "prompt_injection_suspected"


# Run 的终态集合；workflows 层用它判断是否需要继续调度。
TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.REJECTED, RunStatus.FAILED, RunStatus.CANCELLED}
)
