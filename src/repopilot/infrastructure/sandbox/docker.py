"""`SandboxService` 的 Docker 实现（实施设计第 14 节）。

安全边界（第 14.2 节）都在这里落地：非 root 用户、`--network none`
（除非显式要求联网）、只读根文件系统 + 受限的 `/workspace` `/tmp`、
`--cap-drop ALL`、`no-new-privileges`、CPU/内存/PID/磁盘限制、不挂载
Docker Socket、不挂载宿主 Git/HOME/SSH/云凭证。

当前简化点（明确写出来）：
- 基础镜像用固定 tag（`python:3.12-slim`）而不是文档建议的 digest 锁定；
  锁一个具体 digest 又不做镜像更新流程，只会在镜像下线时变成"完全跑不
  起来"，比浮动 tag 更脆。真正的 digest 锁定和镜像更新策略留给有镜像
  管理流程的阶段再做。
- Build Sandbox 仅通过受控 PyPI 网络创建内容寻址的依赖层；运行沙箱只读
  挂载该层并保持断网。当前固定安装平台 pytest/uv 版本，基础镜像 digest
  与依赖层生命周期治理留给后续镜像管理流程。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import io
import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.policies import DependencyPolicy
from repopilot.logging import get_logger
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.dependency_service import DependencyInstallPlan, plan_dependency_install
from repopilot.services.sandbox_service import CommandResult, RunCommandRequest, SandboxSpec

logger = get_logger(component="sandbox_service")

_TIMEOUT_EXIT_CODE = 124  # GNU coreutils `timeout` 在它自己触发超时时的固定退出码
_SIGKILL_EXIT_CODE = 137  # 128 + SIGKILL；超时之外收到这个退出码，最可能是 OOM
_SANDBOX_PYTEST_REQUIREMENT = "pytest==8.4.2"
_SANDBOX_UV_REQUIREMENT = "uv==0.8.15"


@dataclass
class _SandboxHandle:
    container: Container
    workspace_dir: Path
    snapshot_dir: Path
    run_id: UUID
    excluded_paths: frozenset[str]  # sealed test bundle 的路径，diff 时要排除


class DockerSandboxService:
    def __init__(
        self,
        *,
        data_dir: Path,
        artifact_store: ArtifactStore,
        tenant_id: UUID,
        docker_client: docker.DockerClient | None = None,
        pypi_proxy_url: str = "http://pypi-proxy:3128",
        pypi_egress_network: str = "repopilot-pypi-egress",
    ) -> None:
        self._sandboxes_dir = data_dir / "sandboxes"
        self._dependency_layers_dir = data_dir / "dependency-layers"
        self._package_cache_dir = data_dir / "package-cache"
        self._sandboxes_dir.mkdir(parents=True, exist_ok=True)
        self._dependency_layers_dir.mkdir(parents=True, exist_ok=True)
        self._package_cache_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_store = artifact_store
        self._tenant_id = tenant_id
        self._client = docker_client or docker.from_env()
        self._pypi_proxy_url = pypi_proxy_url
        self._pypi_egress_network = pypi_egress_network
        self._handles: dict[UUID, _SandboxHandle] = {}
        self._dependency_locks: dict[str, asyncio.Lock] = {}

    def _caller(self, run_id: UUID) -> ArtifactCaller:
        return ArtifactCaller(
            tenant_id=self._tenant_id, run_id=run_id, role=None, service="sandbox"
        )

    async def prepare_dependencies(
        self, source_archive_ref: ArtifactRef, policy: DependencyPolicy
    ) -> str:
        caller = self._caller(source_archive_ref.run_id)
        source = await self._artifact_store.get_bytes(source_archive_ref, caller)
        plan = plan_dependency_install(source, policy)
        key = hashlib.sha256(
            json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()
            + source_archive_ref.sha256.encode()
            + _SANDBOX_PYTEST_REQUIREMENT.encode()
        ).hexdigest()
        layer_dir = self._dependency_layers_dir / key
        complete_marker = layer_dir / ".complete"
        if complete_marker.exists():
            return key
        lock = self._dependency_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if complete_marker.exists():
                return key
            await asyncio.to_thread(self._build_dependency_layer, key, source, plan)
        return key

    def _build_dependency_layer(
        self,
        key: str,
        source: bytes,
        plan: DependencyInstallPlan,
    ) -> None:
        build_root = self._dependency_layers_dir / f".build-{key}-{uuid4().hex[:8]}"
        workspace_dir = build_root / "workspace"
        deps_dir = build_root / "deps"
        workspace_dir.mkdir(parents=True)
        deps_dir.mkdir()
        self._extract_tar_gz(source, workspace_dir)
        self._chmod_recursive(deps_dir, 0o777)
        self._chmod_recursive(self._package_cache_dir, 0o777)
        network_mode, environment = _sandbox_network_settings(
            network_enabled=True,
            proxy_url=self._pypi_proxy_url,
            egress_network=self._pypi_egress_network,
        )
        environment.update(
            {
                "PIP_CACHE_DIR": "/package-cache/pip",
                "UV_CACHE_DIR": "/package-cache/uv",
                "UV_PROJECT_ENVIRONMENT": "/deps",
            }
        )
        container = self._client.containers.run(
            "python:3.12-slim",
            command=["sleep", "infinity"],
            detach=True,
            read_only=True,
            network_mode=network_mode,
            mem_limit="1024m",
            nano_cpus=1_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            user="65534:65534",
            volumes={
                str(workspace_dir.resolve()): {"bind": "/workspace", "mode": "ro"},
                str(deps_dir.resolve()): {"bind": "/deps", "mode": "rw"},
                str(self._package_cache_dir.resolve()): {
                    "bind": "/package-cache",
                    "mode": "rw",
                },
            },
            tmpfs={"/tmp": "size=2048m"},  # noqa: S108 - container-only tmpfs
            working_dir="/workspace",
            environment=environment,
            labels={"repopilot.dependency_layer": key},
            auto_remove=False,
        )
        build_error: Exception | None = None
        try:
            self._run_build_command(container, ["python", "-m", "venv", "/deps"])
            self._run_build_command(
                container,
                [
                    "/deps/bin/python",
                    "-m",
                    "pip",
                    "install",
                    "--only-binary=:all:",
                    "--index-url",
                    plan.index_url,
                    _SANDBOX_PYTEST_REQUIREMENT,
                ],
            )
            if plan.manager == "uv":
                self._run_build_command(
                    container,
                    [
                        "/deps/bin/python",
                        "-m",
                        "pip",
                        "install",
                        "--only-binary=:all:",
                        "--index-url",
                        plan.index_url,
                        _SANDBOX_UV_REQUIREMENT,
                    ],
                )
                assert plan.command is not None
                self._run_build_command(container, ["/deps/bin/uv", *plan.command.args])
            elif plan.command is not None:
                self._run_build_command(
                    container,
                    ["/deps/bin/python", *plan.command.args],
                )
            self._run_build_command(container, ["rm", "-f", "/deps/lib64"])
        except Exception as exc:  # cleanup must happen after the container releases bind mounts
            build_error = exc
        finally:
            try:
                container.stop(timeout=3)
            except NotFound:
                pass
            try:
                container.remove(force=True)
            except NotFound:
                pass

        if build_error is not None:
            shutil.rmtree(build_root, ignore_errors=True)
            raise build_error

        final_dir = self._dependency_layers_dir / key
        if final_dir.exists():
            shutil.rmtree(build_root, ignore_errors=True)
            return
        deps_dir.replace(final_dir)
        (final_dir / ".complete").write_text("ready\n", encoding="utf-8")
        shutil.rmtree(build_root, ignore_errors=True)

    @staticmethod
    def _run_build_command(container: Container, command: list[str]) -> None:
        result = container.exec_run(
            ["timeout", "--kill-after=5s", "900s", *command],
            workdir="/workspace",
            demux=True,
            user="65534:65534",
        )
        if result.exit_code != 0:
            stdout, stderr = result.output
            details = ((stderr or stdout) or b"").decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"dependency build failed ({result.exit_code}): {details}")

    async def create(self, spec: SandboxSpec) -> UUID:
        sandbox_id = uuid4()
        sandbox_root = self._sandboxes_dir / str(sandbox_id)
        workspace_dir = sandbox_root / "workspace"
        snapshot_dir = sandbox_root / "snapshot"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        caller = self._caller(spec.run_id)
        source_bytes = await self._artifact_store.get_bytes(spec.source_archive_ref, caller)
        await asyncio.to_thread(self._extract_tar_gz, source_bytes, workspace_dir)
        await asyncio.to_thread(self._extract_tar_gz, source_bytes, snapshot_dir)

        excluded_paths: frozenset[str] = frozenset()
        if spec.test_bundle_ref is not None:
            test_bundle_bytes = await self._artifact_store.get_bytes(spec.test_bundle_ref, caller)
            excluded_paths = await asyncio.to_thread(
                self._extract_tar_gz_tracked, test_bundle_bytes, workspace_dir
            )

        # Phase 0 简化：把 workspace 整体设成可读写，不精确模拟容器内非 root
        # 用户与宿主卷的 uid/gid 映射——bind mount 场景下这套映射本来就因
        # 平台而异（尤其是 Docker Desktop 的虚拟机后端），用宽松权限换取
        # 跨平台一致性。
        await asyncio.to_thread(self._chmod_recursive, workspace_dir, 0o777)

        network_mode, environment = _sandbox_network_settings(
            network_enabled=spec.network_enabled,
            proxy_url=self._pypi_proxy_url,
            egress_network=self._pypi_egress_network,
        )
        volumes = {str(workspace_dir.resolve()): {"bind": "/workspace", "mode": "rw"}}
        if spec.dependency_layer_key is not None:
            key = spec.dependency_layer_key
            if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
                raise ValueError("invalid dependency layer key")
            dependency_dir = self._dependency_layers_dir / key
            if not (dependency_dir / ".complete").exists():
                raise ValueError(f"dependency layer is not ready: {key}")
            volumes[str(dependency_dir.resolve())] = {"bind": "/deps", "mode": "ro"}
            environment.update(
                {
                    "VIRTUAL_ENV": "/deps",
                    "PATH": "/deps/bin:/usr/local/bin:/usr/bin:/bin",
                }
            )
        container = await asyncio.to_thread(
            self._client.containers.run,
            spec.image,
            command=["sleep", "infinity"],
            detach=True,
            read_only=True,
            network_mode=network_mode,
            mem_limit=f"{spec.memory_mb}m",
            nano_cpus=int(spec.cpu_limit * 1_000_000_000),
            pids_limit=spec.pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            user="65534:65534",
            volumes=volumes,
            tmpfs={"/tmp": f"size={spec.disk_mb}m"},  # noqa: S108 - 容器内路径，不是宿主临时文件
            working_dir="/workspace",
            environment=environment,
            labels={
                "repopilot.run_id": str(spec.run_id),
                "repopilot.sandbox_id": str(sandbox_id),
            },
            auto_remove=False,
        )

        self._handles[sandbox_id] = _SandboxHandle(
            container=container,
            workspace_dir=workspace_dir,
            snapshot_dir=snapshot_dir,
            run_id=spec.run_id,
            excluded_paths=excluded_paths,
        )
        logger.info("sandbox.create", sandbox_id=str(sandbox_id), run_id=str(spec.run_id))
        return sandbox_id

    async def execute(self, sandbox_id: UUID, request: RunCommandRequest) -> CommandResult:
        handle = self._handles[sandbox_id]
        self._validate_cwd(request.cwd)

        # 用容器自带的 `timeout` 工具强制杀掉真正的子进程；仅靠 Python 侧
        # `asyncio.wait_for` 取消 await 并不能杀死已经在容器里跑的进程。
        #
        # 用默认信号（SIGTERM）+ `--kill-after` 兜底，而不是直接
        # `--signal=KILL`：实测（GNU coreutils 9.7）显示，明确指定
        # `--signal=KILL` 时 timeout 会把子进程收到 SIGKILL 的退出状态
        # （137）原样返回，而不是它自己的"我把它杀了"哨兵值 124；只有
        # 用默认信号，或者用 --kill-after 走"先 TERM、不听话再 KILL"的
        # 两段式，才能稳定拿到 124 来判断"是不是我们的超时导致的"。
        wrapped_cmd = [
            "timeout",
            f"--kill-after={min(5, max(1, request.timeout_seconds))}s",
            f"{request.timeout_seconds}s",
            request.executable,
            *request.args,
        ]

        started_at = datetime.now(UTC)
        exit_code, stdout_bytes, stderr_bytes = await asyncio.to_thread(
            self._exec_run, handle.container, wrapped_cmd, request.cwd
        )
        duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)

        timed_out = exit_code == _TIMEOUT_EXIT_CODE
        oom_killed = exit_code == _SIGKILL_EXIT_CODE and not timed_out

        # LOG 类 Artifact 也必须带完整 base_revision（见 ArtifactRef 的
        # model_validator）；沙箱执行结果本身不天然关联某个 commit，这里
        # 复用全零哨兵，跟 repository_service.cleanup() 的做法一致。
        metadata = ArtifactMetadata(
            tenant_id=self._tenant_id,
            run_id=handle.run_id,
            base_revision="0" * 40,
            schema_version="1",
        )
        stdout_ref = await self._artifact_store.put_bytes(ArtifactKind.LOG, stdout_bytes, metadata)
        stderr_ref = await self._artifact_store.put_bytes(ArtifactKind.LOG, stderr_bytes, metadata)

        logger.info(
            "sandbox.execute",
            sandbox_id=str(sandbox_id),
            executable=request.executable,
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
        )
        return CommandResult(
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            duration_ms=duration_ms,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
        )

    async def export_changes(self, sandbox_id: UUID) -> ArtifactRef:
        handle = self._handles[sandbox_id]
        diff_bytes = await asyncio.to_thread(
            _build_unified_diff, handle.snapshot_dir, handle.workspace_dir, handle.excluded_paths
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.PATCH,
            diff_bytes,
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=handle.run_id,
                base_revision="0" * 40,
                schema_version="1",
            ),
        )

    async def destroy(self, sandbox_id: UUID) -> None:
        handle = self._handles.pop(sandbox_id, None)
        if handle is None:
            return  # 幂等：容器已经被销毁过

        try:
            await asyncio.to_thread(handle.container.stop, timeout=3)
        except NotFound:
            pass
        try:
            await asyncio.to_thread(handle.container.remove, force=True)
        except NotFound:
            pass

        sandbox_root = handle.workspace_dir.parent
        allowed_root = self._sandboxes_dir.resolve()
        resolved = sandbox_root.resolve()
        if resolved.parent == allowed_root or resolved.is_relative_to(allowed_root):
            shutil.rmtree(resolved, ignore_errors=True)
        logger.info("sandbox.destroy", sandbox_id=str(sandbox_id))

    async def cleanup_run(self, run_id: UUID) -> ArtifactRef:
        """Remove every runtime sandbox carrying this exact run label.

        Docker label discovery covers worker restarts where the in-memory handle map was
        lost. Content-addressed dependency layers are deliberately retained as cache.
        """
        containers_by_id: dict[str, Container] = {}
        sandbox_ids: set[UUID] = set()
        for sandbox_id, handle in tuple(self._handles.items()):
            if handle.run_id == run_id:
                containers_by_id[handle.container.id] = handle.container
                sandbox_ids.add(sandbox_id)
                self._handles.pop(sandbox_id, None)

        discovered = await asyncio.to_thread(
            self._client.containers.list,
            all=True,
            filters={"label": f"repopilot.run_id={run_id}"},
        )
        for container in discovered:
            containers_by_id[container.id] = container
            raw_sandbox_id = container.labels.get("repopilot.sandbox_id")
            if raw_sandbox_id is not None:
                try:
                    sandbox_ids.add(UUID(raw_sandbox_id))
                except ValueError:
                    logger.warning(
                        "sandbox.cleanup.invalid_label",
                        run_id=str(run_id),
                        sandbox_id=raw_sandbox_id,
                    )

        removed_containers: list[str] = []
        for container in containers_by_id.values():
            try:
                await asyncio.to_thread(container.stop, timeout=3)
            except NotFound:
                pass
            try:
                await asyncio.to_thread(container.remove, force=True)
            except NotFound:
                pass
            removed_containers.append(container.id)

        removed_paths: list[str] = []
        allowed_root = self._sandboxes_dir.resolve()
        for sandbox_id in sandbox_ids:
            sandbox_root = (self._sandboxes_dir / str(sandbox_id)).resolve()
            if sandbox_root.parent != allowed_root:
                continue
            if sandbox_root.exists():
                shutil.rmtree(sandbox_root, ignore_errors=True)
                removed_paths.append(str(sandbox_root))

        report = json.dumps(
            {
                "run_id": str(run_id),
                "removed_container_ids": sorted(removed_containers),
                "removed_paths": sorted(removed_paths),
                "cleaned_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        ).encode()
        return await self._artifact_store.put_bytes(
            ArtifactKind.CLEANUP_REPORT,
            report,
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=run_id,
                base_revision="0" * 40,
                schema_version="1",
            ),
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cwd(cwd: str) -> None:
        normalized = cwd.replace("\\", "/")
        if normalized.startswith("/") and not normalized.startswith("/workspace"):
            raise ValueError(f"cwd 必须在 /workspace 之内: {cwd!r}")
        if ".." in Path(normalized).parts:
            raise ValueError(f"cwd 不能包含 '..': {cwd!r}")

    @staticmethod
    def _extract_tar_gz(content: bytes, destination: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            tar.extractall(destination, filter="data")

    @staticmethod
    def _extract_tar_gz_tracked(content: bytes, destination: Path) -> frozenset[str]:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            names = frozenset(member.name for member in tar.getmembers() if member.isfile())
            tar.extractall(destination, filter="data")
        return names

    @staticmethod
    def _chmod_recursive(root: Path, mode: int) -> None:
        for path in root.rglob("*"):
            path.chmod(mode)
        root.chmod(mode)

    @staticmethod
    def _exec_run(container: Container, cmd: list[str], workdir: str) -> tuple[int, bytes, bytes]:
        result = container.exec_run(cmd, workdir=workdir, demux=True, user="65534:65534")
        stdout, stderr = result.output
        return result.exit_code, stdout or b"", stderr or b""


def _sandbox_network_settings(
    *, network_enabled: bool, proxy_url: str, egress_network: str
) -> tuple[str, dict[str, str]]:
    """Return the only two supported sandbox network profiles.

    Runtime sandboxes have no network. Build sandboxes join an internal-only
    Docker network and receive proxy variables; the proxy is the sole container
    on that network with a second, Internet-routed interface.
    """
    if not network_enabled:
        return "none", {}
    return egress_network, {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "",
        "no_proxy": "",
        "PIP_INDEX_URL": "https://pypi.org/simple",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _build_unified_diff(before_dir: Path, after_dir: Path, exclude: frozenset[str]) -> bytes:
    """比较 `before_dir`（不可变起始副本）与 `after_dir`（容器写回的当前
    workspace），只为新增/修改/删除的文本文件生成 unified diff（第 14.1
    节）。产出格式兼容 `git apply`（RepositoryService.integrate_patch
    随后会独立解析和复验，Sandbox Service 这里的比较结果不能替代那层
    校验）。
    """

    def relative_files(root: Path) -> set[str]:
        if not root.exists():
            return set()
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    before_paths = relative_files(before_dir) - exclude
    after_paths = relative_files(after_dir) - exclude
    chunks: list[str] = []

    for rel_path in sorted(before_paths | after_paths):
        before_file = before_dir / rel_path
        after_file = after_dir / rel_path
        before_lines = _read_lines(before_file)
        after_lines = _read_lines(after_file)
        if before_lines == after_lines:
            continue

        from_label = "/dev/null" if before_lines is None else f"a/{rel_path}"
        to_label = "/dev/null" if after_lines is None else f"b/{rel_path}"
        diff_lines = list(
            difflib.unified_diff(
                before_lines or [], after_lines or [], fromfile=from_label, tofile=to_label
            )
        )
        if not diff_lines:
            continue

        chunks.append(f"diff --git a/{rel_path} b/{rel_path}\n")
        if before_lines is None:
            chunks.append("new file mode 100644\n")
        elif after_lines is None:
            chunks.append("deleted file mode 100644\n")
        chunks.extend(diff_lines)

    return "".join(chunks).encode("utf-8")


def _read_lines(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None  # 二进制文件：Phase 0 不追踪二进制变更（跟 patch 集成规则一致）
