"""ErrorCode 分类的基本不变量（实施设计第 20、23.1 节）。"""

from __future__ import annotations

from repopilot.domain.errors import NON_RETRYABLE_ERROR_CODES, ErrorCode, ErrorInfo


def test_non_retryable_codes_are_a_subset_of_error_code() -> None:
    assert NON_RETRYABLE_ERROR_CODES.issubset(set(ErrorCode))


def test_error_info_defaults_details_ref_to_none() -> None:
    info = ErrorInfo(
        code=ErrorCode.VALIDATION_ERROR,
        message="requirement 不能为空",
        retryable=False,
        source="api",
    )
    assert info.details_ref is None
