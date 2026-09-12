"""Model Gateway 的确定性调用键和预算状态转换测试。"""

from __future__ import annotations

from uuid import UUID

from repopilot.services.model_gateway import build_logical_call_key


def _logical_key(parameters: dict[str, object]) -> str:
    return build_logical_call_key(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        work_item_id=UUID("00000000-0000-0000-0000-000000000002"),
        attempt=1,
        provider="fake",
        model="fake-model",
        model_parameters=parameters,
        prompt_version="1",
        tool_schema_version="1",
        ordered_input_artifact_hashes=("a" * 64, "b" * 64),
        policy_version="1",
    )


def test_logical_call_key_is_stable_across_mapping_order() -> None:
    assert _logical_key({"temperature": 0.0, "top_p": 1.0}) == _logical_key(
        {"top_p": 1.0, "temperature": 0.0}
    )


def test_logical_call_key_changes_with_semantic_input() -> None:
    assert _logical_key({"temperature": 0.0}) != _logical_key({"temperature": 0.1})
