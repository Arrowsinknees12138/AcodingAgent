"""Docker Sandbox 安全边界测试（实施设计第 14、23.4 节）。

覆盖 Milestone 5 验收标准列出的场景："恶意路径、超时、网络访问、Fork
Bomb 和隐藏测试 ACL 场景通过"。这些测试会真的创建/销毁 Docker 容器，
需要本机 Docker Engine 可用。
"""

from __future__ import annotations

import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from tests.security.conftest import SANDBOX_IMAGE, TENANT_ID, make_tar_gz

from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.policies import DependencyPolicy
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore
from repopilot.infrastructure.sandbox.docker import DockerSandboxService
from repopilot.services.sandbox_service import RunCommandRequest, SandboxSpec

pytestmark = pytest.mark.security


async def test_cached_dependency_layer_is_available_in_offline_runtime(
    sandbox_service: DockerSandboxService,
    source_archive_ref: tuple,
) -> None:
    run_id, ref = source_archive_ref
    layer_key = await sandbox_service.prepare_dependencies(ref, DependencyPolicy())
    assert await sandbox_service.prepare_dependencies(ref, DependencyPolicy()) == layer_key

    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            dependency_layer_key=layer_key,
            network_enabled=False,
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-m", "pytest", "--version"),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
    finally:
        await sandbox_service.destroy(sandbox_id)

    assert result.exit_code == 0


async def test_network_access_is_blocked(
    sandbox_service: DockerSandboxService, source_archive_ref: tuple
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            network_enabled=False,
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=(
                    "-c",
                    "import socket; socket.create_connection(('8.8.8.8', 53), timeout=3)",
                ),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
    finally:
        await sandbox_service.destroy(sandbox_id)

    assert result.exit_code != 0
    assert not result.timed_out  # 应该是立刻连接失败，不是等到超时


async def test_cleanup_run_removes_lingering_runtime_sandbox(
    sandbox_service: DockerSandboxService,
    source_archive_ref: tuple,
    artifact_store: MinioArtifactStore,
    tmp_path: Path,
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            network_enabled=False,
            wall_time_seconds=30,
        )
    )

    # A new worker has no in-memory handles; Docker labels must locate the container.
    restarted_service = DockerSandboxService(
        data_dir=tmp_path / "repopilot-data",
        artifact_store=artifact_store,
        tenant_id=TENANT_ID,
    )
    cleanup_ref = await restarted_service.cleanup_run(run_id)

    assert cleanup_ref.kind is ArtifactKind.CLEANUP_REPORT
    assert not (tmp_path / "repopilot-data" / "sandboxes" / str(sandbox_id)).exists()
    await sandbox_service.destroy(sandbox_id)  # idempotent when prior worker retained stale handle


async def test_fork_bomb_is_contained(
    sandbox_service: DockerSandboxService, source_archive_ref: tuple
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            pids_limit=32,
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "import os\nwhile True:\n    os.fork()\n"),
                cwd="/workspace",
                timeout_seconds=15,
            ),
        )
        # 关键断言是"被限制住了、没有拖垮宿主机"，而不是"这个已经被灌满
        # 僵尸/孤儿进程的容器立刻还能正常干活"——fork 出来的孤儿进程被
        # container init 收养后，即使我们这次 exec 已经返回，它们仍然会
        # 在 pids_limit 的 cgroup 里占着位置一段时间，在同一个容器里立刻
        # 再跑一个命令不一定马上成功，这不代表"没控制住"。
        assert result.exit_code != 0
    finally:
        await sandbox_service.destroy(sandbox_id)

    # 真正要证明的是 Docker daemon / 宿主机本身没有被拖垮：能正常再开一个
    # 全新的沙箱并执行命令。
    new_run_id, new_ref = run_id, ref
    new_sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=new_run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=new_ref,
            test_bundle_ref=None,
            wall_time_seconds=30,
        )
    )
    try:
        sanity = await sandbox_service.execute(
            new_sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "print('still alive')"),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
    finally:
        await sandbox_service.destroy(new_sandbox_id)
    assert sanity.exit_code == 0

    assert sanity.exit_code == 0


async def test_timeout_is_enforced(
    sandbox_service: DockerSandboxService, source_archive_ref: tuple
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "import time; time.sleep(30)"),
                cwd="/workspace",
                timeout_seconds=2,
            ),
        )
    finally:
        await sandbox_service.destroy(sandbox_id)

    assert result.timed_out is True
    assert result.duration_ms < 15_000  # 明显小于命令本身要求的 30 秒


async def test_malicious_cwd_is_rejected(
    sandbox_service: DockerSandboxService, source_archive_ref: tuple
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            wall_time_seconds=30,
        )
    )
    try:
        with pytest.raises(ValueError, match="workspace"):
            await sandbox_service.execute(
                sandbox_id,
                RunCommandRequest(
                    executable="python", args=("-c", "pass"), cwd="/etc", timeout_seconds=5
                ),
            )
        with pytest.raises(ValueError, match=r"\.\."):
            await sandbox_service.execute(
                sandbox_id,
                RunCommandRequest(
                    executable="python",
                    args=("-c", "pass"),
                    cwd="/workspace/../secrets",
                    timeout_seconds=5,
                ),
            )
    finally:
        await sandbox_service.destroy(sandbox_id)


async def test_sealed_test_bundle_hidden_unless_explicitly_provided(
    sandbox_service: DockerSandboxService,
    source_archive_ref: tuple,
    artifact_store: MinioArtifactStore,
) -> None:
    run_id, ref = source_archive_ref

    developer_sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,  # Developer 场景：没有测试 Bundle
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            developer_sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "import os; assert not os.path.exists('tests/test_secret.py')"),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
    finally:
        await sandbox_service.destroy(developer_sandbox_id)
    assert result.exit_code == 0

    test_bundle_bytes = make_tar_gz(
        {"tests/test_secret.py": "def test_secret():\n    assert True\n"}
    )
    test_bundle_metadata = ArtifactMetadata(
        tenant_id=TENANT_ID, run_id=run_id, base_revision="a" * 40, schema_version="1"
    )
    test_bundle_ref = await artifact_store.put_bytes(
        ArtifactKind.TEST_BUNDLE, test_bundle_bytes, test_bundle_metadata
    )

    verification_sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=test_bundle_ref,  # Verification 场景：注入密封测试
            wall_time_seconds=30,
        )
    )
    try:
        result = await sandbox_service.execute(
            verification_sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "import os; assert os.path.exists('tests/test_secret.py')"),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
        assert result.exit_code == 0

        # 密封测试文件本身不应该出现在 export_changes 的 diff 里——它是
        # 注入的验证素材，不是 Agent 产出的变更。
        diff_ref = await sandbox_service.export_changes(verification_sandbox_id)
        from repopilot.domain.artifacts import ArtifactCaller

        caller = ArtifactCaller(tenant_id=TENANT_ID, run_id=run_id, role=None, service="sandbox")
        diff_bytes = await artifact_store.get_bytes(diff_ref, caller)
        assert b"test_secret" not in diff_bytes
    finally:
        await sandbox_service.destroy(verification_sandbox_id)


async def test_export_changes_produces_git_apply_compatible_diff(
    sandbox_service: DockerSandboxService,
    source_archive_ref: tuple,
    artifact_store: MinioArtifactStore,
    tmp_path: Path,
) -> None:
    run_id, ref = source_archive_ref
    sandbox_id = await sandbox_service.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image=SANDBOX_IMAGE,
            source_archive_ref=ref,
            test_bundle_ref=None,
            wall_time_seconds=30,
        )
    )
    try:
        await sandbox_service.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-c", "open('new_module.py', 'w').write('def helper():\\n    return 1\\n')"),
                cwd="/workspace",
                timeout_seconds=10,
            ),
        )
        diff_ref = await sandbox_service.export_changes(sandbox_id)
    finally:
        await sandbox_service.destroy(sandbox_id)

    from repopilot.domain.artifacts import ArtifactCaller

    caller = ArtifactCaller(tenant_id=TENANT_ID, run_id=run_id, role=None, service="sandbox")
    diff_bytes = await artifact_store.get_bytes(diff_ref, caller)
    assert b"new_module.py" in diff_bytes

    repo_path = tmp_path / "apply-target"
    repo_path.mkdir()
    with tarfile.open(
        fileobj=BytesIO(make_tar_gz({"main.py": "print('hello')\n"})), mode="r:gz"
    ) as tar:
        tar.extractall(repo_path, filter="data")

    patch_file = tmp_path / "changes.patch"
    patch_file.write_bytes(diff_bytes)
    subprocess.run(  # noqa: S603 - 测试内部调用受信任的本地 git
        ["git", "apply", "--check", str(patch_file)], cwd=repo_path, check=True
    )
