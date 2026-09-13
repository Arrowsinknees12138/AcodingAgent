"""`RepositoryService` 的真实实现（实施设计第 12、13 节）。

工作目录布局（第 12.1 节）：

```
{data_dir}/
├── bare/{repo_hash}.git                          # 每个仓库一个 mirror clone，跨 Run 复用
├── worktrees/                                     # 保留给未来按 WorkItem 拆分只读 worktree 使用
├── integration/{run_id}/                          # 该 Run 的主工作树，也是 cherry-pick 的落地分支
├── integration/{run_id}.base_revision             # 记录该 Run 的起始 base_revision
│                                                   # （sibling 文件，不放进 worktree，避免被打包进
│                                                   #   Source Archive）
└── staging/{run_id}/{work_item_id}-{attempt}-*/    # 单个 patch 的隔离校验/提交现场
```

Phase 0 简化点（明确写出来，不是漏掉）：
- bare repo 缓存用进程内 `asyncio.Lock` 序列化，不是跨进程文件锁——
  Phase 0 单个 repository-worker 进程即可满足要求；多进程共享同一个
  `REPOPILOT_DATA_DIR` 的场景等 Phase 1 引入多副本部署时再补文件锁。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    ExportedInterfacePayload,
    RepositorySnapshot,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import (
    CandidateSource,
    DeveloperContext,
    IntegratedPatch,
    PatchProposal,
    WorkItem,
)
from repopilot.domain.policies import dependency_manifest_paths
from repopilot.domain.tasks import TaskSpec
from repopilot.infrastructure.git.cli import GitCli, GitCommandError
from repopilot.logging import get_logger
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.repository_service import (
    DependencyChangeDeniedError,
    PatchApplyFailedError,
    PatchConflictError,
    PatchPathDeniedError,
    RevisionNotFoundError,
)
from repopilot.services.static_analyzer import (
    SymbolRecord,
    build_symbol_index,
    diff_exported_symbols,
    parse_symbols_from_source,
)

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NO_REPOSITORY_ACTIVITY_SENTINEL = "0" * 40  # 见 cleanup() 的说明
_BOT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "RepoPilot Bot",
    "GIT_AUTHOR_EMAIL": "repopilot-bot@localhost",
    "GIT_COMMITTER_NAME": "RepoPilot Bot",
    "GIT_COMMITTER_EMAIL": "repopilot-bot@localhost",
}

logger = get_logger(component="repository_service")


class GitRepositoryService:
    def __init__(
        self,
        *,
        data_dir: Path,
        artifact_store: ArtifactStore,
        tenant_id: UUID,
        max_patch_bytes: int = 5 * 1024 * 1024,
        max_touched_files: int = 20,
    ) -> None:
        self._data_dir = data_dir
        self._bare_dir = data_dir / "bare"
        self._worktrees_dir = data_dir / "worktrees"
        self._integration_dir = data_dir / "integration"
        self._staging_dir = data_dir / "staging"
        for directory in (
            self._bare_dir,
            self._worktrees_dir,
            self._integration_dir,
            self._staging_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._git = GitCli(data_dir)
        self._artifact_store = artifact_store
        self._tenant_id = tenant_id
        self._max_patch_bytes = max_patch_bytes
        self._max_touched_files = max_touched_files
        self._repo_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 仓库缓存
    # ------------------------------------------------------------------

    def _repo_hash(self, repo_url: str) -> str:
        return hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]

    def _bare_path(self, repo_url: str) -> Path:
        return self._bare_dir / f"{self._repo_hash(repo_url)}.git"

    def _lock_for(self, repo_url: str) -> asyncio.Lock:
        key = self._repo_hash(repo_url)
        if key not in self._repo_locks:
            self._repo_locks[key] = asyncio.Lock()
        return self._repo_locks[key]

    async def _ensure_bare_mirror(self, repo_url: str) -> Path:
        bare_path = self._bare_path(repo_url)
        async with self._lock_for(repo_url):
            if bare_path.exists():
                try:
                    await self._git.run(["fetch", "--prune"], cwd=bare_path, timeout_seconds=180)
                except GitCommandError as exc:
                    raise RevisionNotFoundError(f"无法更新仓库缓存 {repo_url}: {exc}") from exc
            else:
                bare_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await self._git.run(
                        ["clone", "--mirror", repo_url, str(bare_path)],
                        cwd=self._bare_dir,
                        timeout_seconds=300,
                    )
                except GitCommandError as exc:
                    raise RevisionNotFoundError(f"无法 clone 仓库 {repo_url}: {exc}") from exc
        return bare_path

    def _trusted_caller(self, run_id: UUID) -> ArtifactCaller:
        return ArtifactCaller(
            tenant_id=self._tenant_id, run_id=run_id, role=None, service="repository"
        )

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _archive_worktree(self, worktree_path: Path) -> bytes:
        """把工作树打包成不含 `.git` 的 tar.gz，作为 Source Archive 内容。"""
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for item in sorted(worktree_path.rglob("*")):
                relative = item.relative_to(worktree_path)
                if relative.parts and relative.parts[0] == ".git":
                    continue
                if item.is_file():
                    tar.add(item, arcname=relative.as_posix())
        return buffer.getvalue()

    async def _ensure_run_worktree(self, run_id: UUID, repo_url: str, base_revision: str) -> Path:
        integration_path = self._integration_dir / str(run_id)
        marker = self._integration_dir / f"{run_id}.base_revision"
        if integration_path.exists():
            return integration_path

        bare_path = await self._ensure_bare_mirror(repo_url)
        await self._git.run(
            ["worktree", "add", "--detach", str(integration_path), base_revision],
            cwd=bare_path,
        )
        marker.write_text(base_revision, encoding="utf-8")
        return integration_path

    async def _remove_worktree(self, repo_worktree_path: Path, worktree_to_remove: Path) -> None:
        resolved = worktree_to_remove.resolve()
        allowed_roots = (self._worktrees_dir.resolve(), self._staging_dir.resolve())
        if not any(self._is_relative_to(resolved, root) for root in allowed_roots):
            raise RuntimeError(f"拒绝删除不在受控目录（worktrees/staging）下的路径: {resolved}")
        try:
            await self._git.run(
                ["worktree", "remove", "--force", str(resolved)], cwd=repo_worktree_path
            )
        except GitCommandError:
            shutil.rmtree(resolved, ignore_errors=True)
            await self._git.run(["worktree", "prune"], cwd=repo_worktree_path)

    def _reject_unsafe_patch_content(self, patch_text: str) -> None:
        if "Binary files" in patch_text:
            raise PatchApplyFailedError("补丁包含二进制文件变更，Phase 0 不支持")
        for line in patch_text.splitlines():
            if line.startswith("diff --git"):
                parts = line.split()
                for token in parts[2:4]:
                    normalized = token[2:] if token.startswith(("a/", "b/")) else token
                    if normalized == ".git" or normalized.startswith(".git/"):
                        raise PatchApplyFailedError(f"补丁试图修改 .git 内部文件: {normalized}")
            if "new file mode 160000" in line or "new mode 160000" in line:
                raise PatchApplyFailedError("补丁包含 git submodule（gitlink）变更，Phase 0 不支持")
            if "new file mode 120000" in line or "new mode 120000" in line:
                raise PatchApplyFailedError("补丁包含符号链接变更，Phase 0 不支持")

    # ------------------------------------------------------------------
    # RepositoryService Protocol
    # ------------------------------------------------------------------

    async def resolve_revision(self, repo_url: str, revision: str | None) -> str:
        bare_path = await self._ensure_bare_mirror(repo_url)
        target = revision if revision is not None else "HEAD"
        try:
            resolved = await self._git.run(
                ["rev-parse", "--verify", f"{target}^{{commit}}"], cwd=bare_path
            )
        except GitCommandError as exc:
            raise RevisionNotFoundError(
                f"无法在 {repo_url} 解析 revision={revision!r}: {exc}"
            ) from exc
        sha = resolved.strip()
        if not _FULL_SHA_RE.match(sha):
            raise RevisionNotFoundError(f"解析结果不是合法的完整 commit SHA: {sha!r}")
        return sha

    async def snapshot(self, task: TaskSpec) -> ArtifactRef:
        integration_path = await self._ensure_run_worktree(
            task.run_id, task.repository_url, task.base_revision
        )

        symbol_records = build_symbol_index(integration_path)
        symbol_index_bytes = json.dumps(
            [record.model_dump(mode="json") for record in symbol_records]
        ).encode("utf-8")
        symbol_index_ref = await self._artifact_store.put_bytes(
            ArtifactKind.SYMBOL_INDEX,
            symbol_index_bytes,
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=task.run_id,
                base_revision=task.base_revision,
                schema_version="1",
            ),
        )

        source_archive_ref = await self._artifact_store.put_bytes(
            ArtifactKind.SOURCE_ARCHIVE,
            self._archive_worktree(integration_path),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=task.run_id,
                base_revision=task.base_revision,
                schema_version="1",
            ),
        )

        manifest_names = {"pyproject.toml"}
        test_config_names = {"pytest.ini", "tox.ini"}
        dependency_manifest_paths: list[str] = []
        test_config_paths: list[str] = []
        for entry in sorted(integration_path.iterdir()):
            if not entry.is_file():
                continue
            if entry.name in manifest_names or entry.name.startswith("requirements"):
                dependency_manifest_paths.append(entry.name)
            if entry.name in test_config_names:
                test_config_paths.append(entry.name)

        snapshot = RepositorySnapshot(
            repository_url=task.repository_url,
            base_revision=task.base_revision,
            source_archive_ref=source_archive_ref,
            primary_language="python",
            python_versions=(),
            dependency_manifest_paths=tuple(dependency_manifest_paths),
            test_config_paths=tuple(test_config_paths),
            forbidden_paths=(),
            symbol_index_ref=symbol_index_ref,
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.REPOSITORY_SNAPSHOT,
            snapshot.model_dump_json().encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=task.run_id,
                base_revision=task.base_revision,
                schema_version="1",
                input_artifact_ids=(source_archive_ref.artifact_id, symbol_index_ref.artifact_id),
            ),
        )

    async def create_developer_context(
        self,
        work_item: WorkItem,
        integrated_patches: tuple[ArtifactRef, ...],
    ) -> ArtifactRef:
        integration_path = self._integration_dir / str(work_item.run_id)
        if not integration_path.exists():
            raise RuntimeError(
                f"Run {work_item.run_id} 还没有初始化 integration worktree（需要先调用 snapshot）"
            )

        caller = self._trusted_caller(work_item.run_id)
        upstream_interface_refs: list[ArtifactRef] = []
        for patch_ref in integrated_patches:
            payload_bytes = await self._artifact_store.get_bytes(patch_ref, caller)
            integrated = IntegratedPatch.model_validate_json(payload_bytes)
            upstream_interface_refs.append(integrated.exported_interface_ref)

        current_revision = (
            await self._git.run(["rev-parse", "HEAD"], cwd=integration_path)
        ).strip()
        source_archive_ref = await self._artifact_store.put_bytes(
            ArtifactKind.SOURCE_ARCHIVE,
            self._archive_worktree(integration_path),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=work_item.run_id,
                base_revision=current_revision,
                schema_version="1",
            ),
        )

        context = DeveloperContext(
            work_item=work_item,
            input_revision=current_revision,
            source_archive_ref=source_archive_ref,
            upstream_interface_refs=tuple(upstream_interface_refs),
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.DEVELOPER_CONTEXT,
            context.model_dump_json().encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=work_item.run_id,
                base_revision=current_revision,
                schema_version="1",
                input_artifact_ids=(source_archive_ref.artifact_id,),
            ),
        )

    async def integrate_patch(
        self,
        proposal_ref: ArtifactRef,
        *,
        allowed_write_paths: tuple[str, ...],
        allow_dependency_changes: bool = False,
    ) -> ArtifactRef:
        # `ArtifactKind` 没有单独给 `PatchProposal` 这个小型包装对象分配
        # 种类（第 7.2 节的枚举里只有承载原始 diff 内容的 PATCH 和承载
        # 集成结果的 INTEGRATED_PATCH）；这里复用 PATCH 这个 kind 存储
        # 序列化后的 PatchProposal JSON——它本身足够小，且核心内容
        # （真正的 diff）已经通过 `patch_ref` 这个字段间接引用另一个
        # PATCH Artifact，符合"大内容都走 Artifact 引用"的精神。
        caller = self._trusted_caller(proposal_ref.run_id)
        proposal = PatchProposal.model_validate_json(
            await self._artifact_store.get_bytes(proposal_ref, caller)
        )

        denied = set(proposal.touched_paths) - set(allowed_write_paths)
        if denied or len(proposal.touched_paths) > self._max_touched_files:
            raise PatchPathDeniedError(tuple(sorted(denied)) or proposal.touched_paths)
        changed_manifests = dependency_manifest_paths(proposal.touched_paths)
        if changed_manifests and not allow_dependency_changes:
            raise DependencyChangeDeniedError(changed_manifests)

        patch_bytes = await self._artifact_store.get_bytes(proposal.patch_ref, caller)
        if len(patch_bytes) > self._max_patch_bytes:
            raise PatchApplyFailedError(
                f"patch 大小 {len(patch_bytes)} 字节超过上限 {self._max_patch_bytes}"
            )
        patch_text = patch_bytes.decode("utf-8", errors="strict")
        self._reject_unsafe_patch_content(patch_text)

        integration_path = self._integration_dir / str(proposal_ref.run_id)
        if not integration_path.exists():
            raise RuntimeError(f"Run {proposal_ref.run_id} 还没有初始化 integration worktree")

        staging_root = self._staging_dir / str(proposal_ref.run_id)
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = (
            staging_root / f"{proposal.work_item_id}-{proposal.attempt}-{uuid4().hex[:8]}"
        )
        patch_file = staging_root / f"{staging_path.name}.patch"

        await self._git.run(
            ["worktree", "add", "--detach", str(staging_path), proposal.input_revision],
            cwd=integration_path,
        )
        try:
            # Text-mode writes turn LF into CRLF on Windows and break git apply's context match.
            patch_file.write_bytes(patch_bytes)
            try:
                await self._git.run(["apply", "--check", str(patch_file)], cwd=staging_path)
            except GitCommandError as exc:
                raise PatchApplyFailedError(f"git apply --check 未通过: {exc}") from exc

            before_records: list[SymbolRecord] = []
            for rel_path in proposal.touched_paths:
                if not rel_path.endswith(".py"):
                    continue
                try:
                    original_source = await self._git.run(
                        ["show", f"{proposal.input_revision}:{rel_path}"], cwd=integration_path
                    )
                except GitCommandError:
                    continue  # 新建文件，补丁之前不存在
                before_records.extend(parse_symbols_from_source(original_source, rel_path))

            await self._git.run(["apply", str(patch_file)], cwd=staging_path)

            changed_output = await self._git.run(["diff", "--name-only", "HEAD"], cwd=staging_path)
            actual_changed = {line.strip() for line in changed_output.splitlines() if line.strip()}
            if actual_changed != set(proposal.touched_paths):
                raise PatchPathDeniedError(tuple(sorted(actual_changed - set(allowed_write_paths))))

            after_records: list[SymbolRecord] = []
            for rel_path in proposal.touched_paths:
                if not rel_path.endswith(".py"):
                    continue
                file_path = staging_path / rel_path
                if not file_path.exists():
                    continue  # 本次 patch 删除了这个文件
                try:
                    source = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise PatchApplyFailedError(f"{rel_path} 不是合法 UTF-8 文本文件") from exc
                try:
                    compile(source, rel_path, "exec")
                except SyntaxError as exc:
                    raise PatchApplyFailedError(f"{rel_path} AST 解析失败: {exc}") from exc
                after_records.extend(parse_symbols_from_source(source, rel_path))

            exported_symbols = diff_exported_symbols(tuple(before_records), tuple(after_records))

            await self._git.run(["add", "-A"], cwd=staging_path)
            commit_message = f"WorkItem {proposal.work_item_id} attempt {proposal.attempt}"
            await self._git.run(
                ["commit", "-m", commit_message], cwd=staging_path, env_overrides=_BOT_IDENTITY_ENV
            )
            staging_commit_sha = (
                await self._git.run(["rev-parse", "HEAD"], cwd=staging_path)
            ).strip()
        finally:
            patch_file.unlink(missing_ok=True)
            await self._remove_worktree(integration_path, staging_path)

        try:
            await self._git.run(
                ["cherry-pick", "--allow-empty", staging_commit_sha],
                cwd=integration_path,
                env_overrides=_BOT_IDENTITY_ENV,
            )
        except GitCommandError as exc:
            await self._git.run(["cherry-pick", "--abort"], cwd=integration_path)
            raise PatchConflictError(
                f"集成 WorkItem {proposal.work_item_id} 的补丁时发生冲突: {exc}"
            ) from exc

        integrated_commit_sha = (
            await self._git.run(["rev-parse", "HEAD"], cwd=integration_path)
        ).strip()

        interface_payload = ExportedInterfacePayload(
            work_item_id=proposal.work_item_id,
            commit_sha=integrated_commit_sha,
            symbols=exported_symbols,
            unresolved_references=(),
        )
        exported_interface_ref = await self._artifact_store.put_bytes(
            ArtifactKind.EXPORTED_INTERFACE,
            interface_payload.model_dump_json().encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=proposal_ref.run_id,
                base_revision=integrated_commit_sha,
                schema_version="1",
                input_artifact_ids=(proposal_ref.artifact_id,),
            ),
        )

        integrated = IntegratedPatch(
            work_item_id=proposal.work_item_id,
            proposal_ref=proposal_ref,
            input_revision=proposal.input_revision,
            commit_sha=integrated_commit_sha,
            exported_interface_ref=exported_interface_ref,
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.INTEGRATED_PATCH,
            integrated.model_dump_json().encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=proposal_ref.run_id,
                base_revision=integrated_commit_sha,
                schema_version="1",
                input_artifact_ids=(proposal_ref.artifact_id, exported_interface_ref.artifact_id),
            ),
        )

    async def export_candidate(self, run_id: UUID) -> CandidateSource:
        integration_path = self._integration_dir / str(run_id)
        if not integration_path.exists():
            raise RuntimeError(f"Run {run_id} has no integration worktree")
        revision = (await self._git.run(["rev-parse", "HEAD"], cwd=integration_path)).strip()
        source_ref = await self._artifact_store.put_bytes(
            ArtifactKind.SOURCE_ARCHIVE,
            self._archive_worktree(integration_path),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=run_id,
                base_revision=revision,
                schema_version="1",
            ),
        )
        return CandidateSource(source_archive_ref=source_ref, revision=revision)

    async def final_diff(self, run_id: UUID) -> ArtifactRef:
        integration_path = self._integration_dir / str(run_id)
        base_marker = self._integration_dir / f"{run_id}.base_revision"
        if not integration_path.exists() or not base_marker.exists():
            raise RuntimeError(f"Run {run_id} 没有可用的 integration worktree")

        base_revision = base_marker.read_text(encoding="utf-8").strip()
        diff_text = await self._git.run(["diff", base_revision, "HEAD"], cwd=integration_path)
        current_revision = (
            await self._git.run(["rev-parse", "HEAD"], cwd=integration_path)
        ).strip()
        return await self._artifact_store.put_bytes(
            ArtifactKind.PATCH,
            diff_text.encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=run_id,
                base_revision=current_revision,
                schema_version="1",
            ),
        )

    async def cleanup(self, run_id: UUID) -> ArtifactRef:
        integration_path = self._integration_dir / str(run_id)
        base_marker = self._integration_dir / f"{run_id}.base_revision"
        staging_root = self._staging_dir / str(run_id)

        # cleanup 报告本身也是一个 Artifact，必须携带完整 40 位 base_revision
        # （第 7.2 节）。正常情况下用这次 Run 的起始 base_revision；如果
        # Run 在 Repository Service 做任何事之前就失败了（例如 Ingest 阶段
        # 就被拒绝），从没写过这个 marker，用全零 sha 作为"这个 Run 从未
        # 实际拉取过仓库"的显式哨兵值，而不是伪造一个看起来真实的 commit。
        base_revision = (
            base_marker.read_text(encoding="utf-8").strip()
            if base_marker.exists()
            else _NO_REPOSITORY_ACTIVITY_SENTINEL
        )

        removed_paths: list[str] = []
        for path in (integration_path, staging_root):
            resolved = path.resolve()
            if not self._is_relative_to(
                resolved, self._integration_dir.resolve()
            ) and not self._is_relative_to(resolved, self._staging_dir.resolve()):
                continue
            if resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)
                removed_paths.append(str(resolved))
        base_marker.unlink(missing_ok=True)

        report = {
            "run_id": str(run_id),
            "removed_paths": removed_paths,
            "cleaned_at": datetime.now(UTC).isoformat(),
        }
        return await self._artifact_store.put_bytes(
            ArtifactKind.CLEANUP_REPORT,
            json.dumps(report).encode("utf-8"),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=run_id,
                base_revision=base_revision,
                schema_version="1",
            ),
        )
