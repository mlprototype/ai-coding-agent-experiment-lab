"""Slice 6B local toolchain audit and trusted Fixture Acceptance.

This module never invokes a Provider, transmits a Prompt, or accesses the
network.  Every subprocess is a fixed local toolchain executable or a trusted
Fixture Gate executed through the existing process-group runner.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from agentlab.models import (
    CommandEvidence,
    CommandStatus,
    GateKind,
    RunnerSettings,
)
from agentlab.phase6 import (
    DiffPolicy,
    FixtureAcceptanceRecord,
    FixtureManifest,
    GateAcceptanceSummary,
    Language,
    ToolchainComponent,
    ToolchainComponentRole,
    ToolchainIdentity,
    canonical_json_bytes,
    load_diff_policy,
    load_fixture_acceptance,
    load_fixture_manifest,
)
from agentlab.runner import LocalCommandRunner
from agentlab.workspace import (
    SnapshotError,
    prepare_disposable_workspace,
    remove_disposable_workspace,
    remove_temporary_root,
    snapshot_directory,
)

_VERSION_TIMEOUT_MS = 5_000
_GATE_TIMEOUT_MS = 15_000
_TERMINATION_GRACE_MS = 500
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_DIFF_BYTES = 256 * 1024
_TREE_DOMAIN = b"agentlab-phase6-tree-v1\0"
_VERSION_DOMAIN = "agentlab-phase6-version-output-v1"
_TSC_PACKAGE_DOMAIN = "agentlab-phase6-typescript-package-v2"
_GATE_CONTRACT_DOMAIN = "agentlab-phase6-gate-contract-v1"


class FixtureAcceptanceError(ValueError):
    """A fail-closed Slice 6B audit or acceptance failure."""


@dataclass(frozen=True)
class ToolchainCandidates:
    python: Path | None = None
    node: Path | None = None
    tsc: Path | None = None
    java: Path | None = None
    javac: Path | None = None


@dataclass(frozen=True)
class SecureTreeSnapshot:
    files: dict[str, bytes]
    directories: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class FixtureDefinition:
    language: Language
    fixture_revision: str
    fixture_root: Path
    reference_root: Path
    policy_path: Path
    output_root: Path


@dataclass(frozen=True)
class FixtureAcceptanceOutcome:
    language: Language
    status: Literal["accepted", "blocked"]
    blocker: str | None
    detail: str | None
    manifest: FixtureManifest | None
    acceptance: FixtureAcceptanceRecord | None
    manifest_path: Path | None
    acceptance_path: Path | None
    manifest_sha256: str | None
    acceptance_sha256: str | None
    baseline_commands: tuple[CommandEvidence, ...]
    reference_commands: tuple[CommandEvidence, ...]


@dataclass(frozen=True)
class FixtureAcceptanceSuiteOutcome:
    commit_sha: str
    results: tuple[FixtureAcceptanceOutcome, ...]

    @property
    def accepted_count(self) -> int:
        return sum(result.status == "accepted" for result in self.results)

    @property
    def engineering_minimum_met(self) -> bool:
        return self.accepted_count >= 2

    @property
    def full_target_met(self) -> bool:
        return self.accepted_count == 3


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str


@dataclass(frozen=True)
class _VersionObservation:
    exact_version: str
    output_sha256: str


@dataclass(frozen=True)
class _ComponentBinding:
    role: ToolchainComponentRole
    resolved_path: Path
    identity: _FileIdentity


@dataclass(frozen=True)
class _ToolchainBindingSnapshot:
    language: Language
    toolchain_fingerprint: str
    components: tuple[_ComponentBinding, ...]
    typescript_package_root: Path | None = None
    typescript_package: SecureTreeSnapshot | None = None
    typescript_compiler_js: Path | None = None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_hash(value: dict[str, object]) -> str:
    return _sha256(canonical_json_bytes(value))


def _relative_path(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if (
        not relative
        or relative in {".", ".."}
        or ".." in PurePosixPath(relative).parts
        or any(character in relative for character in ("\x00", "\n", "\r"))
    ):
        raise FixtureAcceptanceError("tree contains an unsafe relative path")
    return relative


def _tree_hash(files: Mapping[str, bytes], directories: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(_TREE_DOMAIN)
    for relative in sorted(directories):
        path_bytes = relative.encode("utf-8")
        digest.update(b"D\0")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
    for relative, content in sorted(files.items()):
        path_bytes = relative.encode("utf-8")
        digest.update(b"F\0")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def secure_tree_snapshot(root: Path) -> SecureTreeSnapshot:
    """Snapshot a small trusted tree while rejecting links and special files."""
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise FixtureAcceptanceError("tree root is unavailable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise FixtureAcceptanceError("tree root must be a real directory")

    files: dict[str, bytes] = {}
    directories: list[str] = []
    directory_identities: dict[Path, tuple[int, int, int]] = {
        root: (root_metadata.st_dev, root_metadata.st_ino, root_metadata.st_mode)
    }

    def visit(directory: Path) -> None:
        try:
            directory_before = directory.lstat()
        except OSError as error:
            raise FixtureAcceptanceError("tree directory is unavailable") from error
        if stat.S_ISLNK(directory_before.st_mode) or not stat.S_ISDIR(
            directory_before.st_mode
        ):
            raise FixtureAcceptanceError("tree directory must remain a real directory")
        identity = (
            directory_before.st_dev,
            directory_before.st_ino,
            directory_before.st_mode,
        )
        expected_identity = directory_identities.setdefault(directory, identity)
        if identity != expected_identity:
            raise FixtureAcceptanceError("tree directory changed while inspected")
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise FixtureAcceptanceError("tree could not be scanned") from error
        for entry in entries:
            path = Path(entry.path)
            relative = _relative_path(root, path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise FixtureAcceptanceError("tree entry could not be inspected") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise FixtureAcceptanceError(f"symlink is not allowed: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
                directory_identities[path] = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                )
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise FixtureAcceptanceError(f"special file is not allowed: {relative}")
            if metadata.st_nlink != 1:
                raise FixtureAcceptanceError(f"hardlink is not allowed: {relative}")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor: int | None = None
            try:
                descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
                if (
                    opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                    or opened.st_nlink != 1
                    or not stat.S_ISREG(opened.st_mode)
                ):
                    raise FixtureAcceptanceError(f"tree entry changed: {relative}")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.fstat(descriptor)
            except FixtureAcceptanceError:
                raise
            except OSError as error:
                raise FixtureAcceptanceError(f"tree entry could not be read: {relative}") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_nlink,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise FixtureAcceptanceError(f"tree entry changed while read: {relative}")
            content = b"".join(chunks)
            if len(content) != after.st_size:
                raise FixtureAcceptanceError(f"tree entry size changed: {relative}")
            files[relative] = content
        try:
            directory_after = directory.lstat()
        except OSError as error:
            raise FixtureAcceptanceError("tree directory changed after scan") from error
        if (
            directory_after.st_dev,
            directory_after.st_ino,
            directory_after.st_mode,
        ) != expected_identity:
            raise FixtureAcceptanceError("tree directory changed during scan")

    visit(root)
    for directory, expected_identity in directory_identities.items():
        try:
            current = directory.lstat()
        except OSError as error:
            raise FixtureAcceptanceError("tree directory changed after snapshot") from error
        if stat.S_ISLNK(current.st_mode) or (
            current.st_dev,
            current.st_ino,
            current.st_mode,
        ) != expected_identity:
            raise FixtureAcceptanceError("tree directory changed after snapshot")
    sorted_directories = tuple(sorted(directories))
    return SecureTreeSnapshot(
        files=files,
        directories=sorted_directories,
        sha256=_tree_hash(files, sorted_directories),
    )


def _snapshot_executable(
    path: Path,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> tuple[Path, _FileIdentity]:
    if not path.is_absolute():
        raise FixtureAcceptanceError("toolchain executable path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError) as error:
        raise FixtureAcceptanceError("toolchain executable is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FixtureAcceptanceError("resolved toolchain executable must be a regular file")
    if any(_is_relative_to(resolved, root.resolve(strict=False)) for root in forbidden_roots):
        raise FixtureAcceptanceError("toolchain executable must remain outside Workspace roots")
    if metadata.st_nlink != 1:
        raise FixtureAcceptanceError("toolchain executable hardlink is not allowed")
    if not os.access(resolved, os.X_OK):
        raise FixtureAcceptanceError("toolchain executable is not executable")
    try:
        content = resolved.read_bytes()
        after = resolved.lstat()
    except OSError as error:
        raise FixtureAcceptanceError("toolchain executable could not be read") from error
    identity = _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        link_count=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        sha256=_sha256(content),
    )
    current = _FileIdentity(
        device=after.st_dev,
        inode=after.st_ino,
        mode=after.st_mode,
        link_count=after.st_nlink,
        size=after.st_size,
        modified_ns=after.st_mtime_ns,
        changed_ns=after.st_ctime_ns,
        sha256=_sha256(content),
    )
    if current != identity:
        raise FixtureAcceptanceError("toolchain executable changed while hashed")
    return resolved, identity


def _minimal_environment(path_entries: Sequence[str]) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(path_entries),
        "LANG": "C",
        "LC_ALL": "C",
    }


def _normalize_version_stream(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _run_version_command(
    argv: Sequence[str],
    *,
    path_entries: Sequence[str],
) -> _VersionObservation:
    temporary_root = Path(tempfile.mkdtemp(prefix="agentlab-phase6-version-"))
    workspace = temporary_root / "workspace"
    environment_root = temporary_root / "environment"
    try:
        workspace.mkdir()
        for name in ("home", "tmp", "cache"):
            (environment_root / name).mkdir(parents=True, exist_ok=True)
        settings = RunnerSettings(
            fixture_path="phase6-version-audit",
            command_timeout_ms=_VERSION_TIMEOUT_MS,
            termination_grace_ms=_TERMINATION_GRACE_MS,
            max_output_bytes=_MAX_OUTPUT_BYTES,
            max_diff_bytes=_MAX_DIFF_BYTES,
        )
        result = LocalCommandRunner(settings).run(
            gate=GateKind.TYPECHECK,
            command_index=0,
            argv=list(argv),
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
            parent_environment=_minimal_environment(path_entries),
        )
        evidence = result.evidence
        if (
            result.harness_failure is not None
            or evidence.status is not CommandStatus.PASSED
            or evidence.return_code != 0
            or evidence.stdout_truncated
            or evidence.stderr_truncated
            or evidence.stdout_decode_replaced
            or evidence.stderr_decode_replaced
            or not evidence.termination.process_group_cleared
        ):
            raise FixtureAcceptanceError("toolchain version command failed strict validation")
        stdout = _normalize_version_stream(evidence.stdout)
        stderr = _normalize_version_stream(evidence.stderr)
        if not stdout and not stderr:
            raise FixtureAcceptanceError("toolchain version command returned no version text")
        nonempty = [stream.removesuffix("\n") for stream in (stdout, stderr) if stream]
        exact_version = "\n".join(nonempty)
        output_sha256 = _canonical_hash(
            {
                "domain": _VERSION_DOMAIN,
                "exit_code": evidence.return_code,
                "stderr": stderr,
                "stdout": stdout,
            }
        )
        return _VersionObservation(exact_version, output_sha256)
    finally:
        removed, detail = remove_temporary_root(temporary_root)
        if not removed:
            raise FixtureAcceptanceError(detail or "version workspace cleanup failed")


def _audit_basic_component(
    role: ToolchainComponentRole,
    candidate: Path | None,
    version_arguments: Sequence[str],
    *,
    path_entries: Sequence[str],
    forbidden_roots: Sequence[Path],
) -> ToolchainComponent:
    if candidate is None:
        raise FixtureAcceptanceError(f"missing toolchain component: {role.value}")
    resolved, before = _snapshot_executable(
        candidate,
        forbidden_roots=forbidden_roots,
    )
    argv = [str(resolved), *version_arguments]
    observation = _run_version_command(argv, path_entries=path_entries)
    after_resolved, after = _snapshot_executable(
        resolved,
        forbidden_roots=forbidden_roots,
    )
    if after_resolved != resolved or after != before:
        raise FixtureAcceptanceError("toolchain executable changed during capability audit")
    return ToolchainComponent(
        role=role,
        resolved_executable_path=str(resolved),
        executable_sha256=before.sha256,
        version_argv=argv,
        exact_version=observation.exact_version,
        version_output_sha256=observation.output_sha256,
        package_version=None,
        package_fingerprint=None,
    )


def _find_typescript_package(
    tsc: Path,
) -> tuple[Path, dict[str, object], bytes, SecureTreeSnapshot]:
    for parent in (tsc.parent, *tsc.parents):
        package_json = parent / "package.json"
        if not os.path.lexists(package_json):
            continue
        try:
            package_snapshot = secure_tree_snapshot(parent)
            content = package_snapshot.files["package.json"]
            raw = json.loads(content.decode("utf-8"))
        except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as error:
            raise FixtureAcceptanceError("TypeScript package.json is invalid") from error
        if isinstance(raw, dict) and raw.get("name") == "typescript":
            return parent, raw, content, package_snapshot
    raise FixtureAcceptanceError("TypeScript package root could not be determined")


def _typescript_package_fingerprint(
    *,
    package_root: Path,
    package_version: str,
    package_json_bytes: bytes,
    package_snapshot: SecureTreeSnapshot,
    compiler_js_bytes: bytes,
    launcher_sha256: str,
    node_component: ToolchainComponent,
    compiler_version_output_sha256: str,
) -> str:
    compiler_js = package_root / "lib" / "tsc.js"
    return _canonical_hash(
        {
            "compiler_js_path": "lib/tsc.js",
            "compiler_js_sha256": _sha256(compiler_js_bytes),
            "compiler_version_argv": [
                node_component.resolved_executable_path,
                str(compiler_js),
                "--version",
            ],
            "compiler_version_output_sha256": compiler_version_output_sha256,
            "domain": _TSC_PACKAGE_DOMAIN,
            "node_component": node_component.model_dump(mode="json"),
            "package_json_sha256": _sha256(package_json_bytes),
            "package_tree_sha256": package_snapshot.sha256,
            "package_version": package_version,
            "tsc_launcher_sha256": launcher_sha256,
        }
    )


def _audit_typescript_component(
    candidate: Path | None,
    node_component: ToolchainComponent,
    *,
    path_entries: Sequence[str],
    forbidden_roots: Sequence[Path],
) -> ToolchainComponent:
    if candidate is None:
        raise FixtureAcceptanceError("missing toolchain component: typescript_compiler")
    resolved, before = _snapshot_executable(
        candidate,
        forbidden_roots=forbidden_roots,
    )
    package_root, package_raw, package_json_bytes, package_snapshot = (
        _find_typescript_package(resolved)
    )
    package_version = package_raw.get("version")
    if not isinstance(package_version, str) or not package_version:
        raise FixtureAcceptanceError("TypeScript package version is missing")
    try:
        compiler_js_bytes = package_snapshot.files["lib/tsc.js"]
    except KeyError as error:
        raise FixtureAcceptanceError("TypeScript compiler JS is unavailable") from error
    launcher_observation = _run_version_command(
        [str(resolved), "--version"],
        path_entries=path_entries,
    )
    compiler_js = package_root / "lib" / "tsc.js"
    compiler_observation = _run_version_command(
        [node_component.resolved_executable_path, str(compiler_js), "--version"],
        path_entries=path_entries,
    )
    if (
        launcher_observation.exact_version != f"Version {package_version}"
        or compiler_observation.exact_version != launcher_observation.exact_version
        or compiler_observation.output_sha256 != launcher_observation.output_sha256
    ):
        raise FixtureAcceptanceError("TypeScript package and tsc versions differ")
    after_resolved, after = _snapshot_executable(
        resolved,
        forbidden_roots=forbidden_roots,
    )
    if after_resolved != resolved or after != before:
        raise FixtureAcceptanceError("TypeScript launcher changed during audit")
    after_package_snapshot = secure_tree_snapshot(package_root)
    if _sha256(after_package_snapshot.files.get("lib/tsc.js", b"")) != _sha256(
        compiler_js_bytes
    ):
        raise FixtureAcceptanceError("TypeScript compiler JS changed during audit")
    if after_package_snapshot.sha256 != package_snapshot.sha256:
        raise FixtureAcceptanceError("TypeScript package tree changed during audit")
    package_fingerprint = _typescript_package_fingerprint(
        package_root=package_root,
        package_version=package_version,
        package_json_bytes=package_json_bytes,
        package_snapshot=package_snapshot,
        compiler_js_bytes=compiler_js_bytes,
        launcher_sha256=before.sha256,
        node_component=node_component,
        compiler_version_output_sha256=compiler_observation.output_sha256,
    )
    return ToolchainComponent(
        role=ToolchainComponentRole.TYPESCRIPT_COMPILER,
        resolved_executable_path=str(resolved),
        executable_sha256=before.sha256,
        version_argv=[str(resolved), "--version"],
        exact_version=launcher_observation.exact_version,
        version_output_sha256=launcher_observation.output_sha256,
        package_version=package_version,
        package_fingerprint=package_fingerprint,
    )


def _toolchain_identity(components: Sequence[ToolchainComponent]) -> ToolchainIdentity:
    ordered = sorted(components, key=lambda component: component.role.value)
    path_entries = sorted(
        {str(Path(component.resolved_executable_path).parent) for component in ordered}
    )
    system = platform.system().lower()
    architecture = platform.machine().lower()
    payload: dict[str, object] = {
        "architecture": architecture,
        "components": [component.model_dump(mode="json") for component in ordered],
        "gate_path_entries": path_entries,
        "os": system,
        "workspace_executable_lookup_allowed": False,
    }
    return ToolchainIdentity(
        os=system,
        architecture=architecture,
        gate_path_entries=path_entries,
        workspace_executable_lookup_allowed=False,
        components=ordered,
        fingerprint=_canonical_hash(payload),
    )


def audit_toolchain(
    language: Language,
    candidates: ToolchainCandidates,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> ToolchainIdentity:
    """Measure one exact local toolchain without running workspace executables."""
    selected_candidates = {
        Language.PYTHON: (candidates.python,),
        Language.TYPESCRIPT: (candidates.node, candidates.tsc),
        Language.JAVA: (candidates.java, candidates.javac),
    }[language]
    candidate_paths = {
        candidate.resolve(strict=False)
        for candidate in selected_candidates
        if candidate is not None
    }
    path_entries = sorted({str(path.parent) for path in candidate_paths})
    if not path_entries:
        raise FixtureAcceptanceError("no toolchain candidate was provided")
    if language is Language.PYTHON:
        component = _audit_basic_component(
            ToolchainComponentRole.PYTHON_RUNTIME,
            candidates.python,
            ["--version"],
            path_entries=path_entries,
            forbidden_roots=forbidden_roots,
        )
        return _toolchain_identity([component])
    if language is Language.TYPESCRIPT:
        node = _audit_basic_component(
            ToolchainComponentRole.NODE_RUNTIME,
            candidates.node,
            ["--version"],
            path_entries=path_entries,
            forbidden_roots=forbidden_roots,
        )
        tsc = _audit_typescript_component(
            candidates.tsc,
            node,
            path_entries=path_entries,
            forbidden_roots=forbidden_roots,
        )
        return _toolchain_identity([node, tsc])
    if language is Language.JAVA:
        java = _audit_basic_component(
            ToolchainComponentRole.JAVA_RUNTIME,
            candidates.java,
            ["-version"],
            path_entries=path_entries,
            forbidden_roots=forbidden_roots,
        )
        javac = _audit_basic_component(
            ToolchainComponentRole.JAVA_COMPILER,
            candidates.javac,
            ["-version"],
            path_entries=path_entries,
            forbidden_roots=forbidden_roots,
        )
        java_root = Path(java.resolved_executable_path).parent.parent
        javac_root = Path(javac.resolved_executable_path).parent.parent
        if java_root != javac_root:
            raise FixtureAcceptanceError("java and javac must belong to the same JDK root")
        return _toolchain_identity([java, javac])
    raise FixtureAcceptanceError(f"unsupported Fixture language: {language.value}")


def discover_toolchain_candidates() -> ToolchainCandidates:
    """Resolve candidates only; execution happens later after explicit confirmation."""
    return ToolchainCandidates(
        python=Path(sys.executable) if Path(sys.executable).is_absolute() else None,
        node=Path(found) if (found := shutil.which("node")) else None,
        tsc=Path(found) if (found := shutil.which("tsc")) else None,
        java=Path(found) if (found := shutil.which("java")) else None,
        javac=Path(found) if (found := shutil.which("javac")) else None,
    )


def _capture_toolchain_binding(
    language: Language,
    toolchain: ToolchainIdentity,
) -> _ToolchainBindingSnapshot:
    components: list[_ComponentBinding] = []
    by_role = {component.role: component for component in toolchain.components}
    for component in toolchain.components:
        recorded_path = Path(component.resolved_executable_path)
        resolved, identity = _snapshot_executable(recorded_path)
        if resolved != recorded_path or identity.sha256 != component.executable_sha256:
            raise FixtureAcceptanceError(
                f"recorded {component.role.value} executable identity changed"
            )
        components.append(
            _ComponentBinding(
                role=component.role,
                resolved_path=resolved,
                identity=identity,
            )
        )

    package_root: Path | None = None
    package_snapshot: SecureTreeSnapshot | None = None
    compiler_js: Path | None = None
    if language is Language.TYPESCRIPT:
        node = by_role[ToolchainComponentRole.NODE_RUNTIME]
        compiler = by_role[ToolchainComponentRole.TYPESCRIPT_COMPILER]
        package_root, package_raw, package_json_bytes, package_snapshot = (
            _find_typescript_package(Path(compiler.resolved_executable_path))
        )
        package_version = package_raw.get("version")
        if not isinstance(package_version, str):
            raise FixtureAcceptanceError("TypeScript package version is invalid")
        try:
            compiler_js_bytes = package_snapshot.files["lib/tsc.js"]
        except KeyError as error:
            raise FixtureAcceptanceError(
                "TypeScript compiler JS is unavailable"
            ) from error
        expected_fingerprint = _typescript_package_fingerprint(
            package_root=package_root,
            package_version=package_version,
            package_json_bytes=package_json_bytes,
            package_snapshot=package_snapshot,
            compiler_js_bytes=compiler_js_bytes,
            launcher_sha256=compiler.executable_sha256,
            node_component=node,
            compiler_version_output_sha256=compiler.version_output_sha256,
        )
        if (
            compiler.package_version != package_version
            or compiler.exact_version != f"Version {package_version}"
            or compiler.package_fingerprint != expected_fingerprint
        ):
            raise FixtureAcceptanceError(
                "TypeScript package no longer matches its recorded fingerprint"
            )
        compiler_js = package_root / "lib" / "tsc.js"

    return _ToolchainBindingSnapshot(
        language=language,
        toolchain_fingerprint=toolchain.fingerprint,
        components=tuple(components),
        typescript_package_root=package_root,
        typescript_package=package_snapshot,
        typescript_compiler_js=compiler_js,
    )


def _verify_toolchain_binding(
    expected: _ToolchainBindingSnapshot,
    toolchain: ToolchainIdentity,
) -> None:
    current = _capture_toolchain_binding(expected.language, toolchain)
    if current != expected:
        raise FixtureAcceptanceError("toolchain changed during Fixture Acceptance")


def _commands_for(
    language: Language,
    toolchain: ToolchainIdentity,
    binding: _ToolchainBindingSnapshot,
) -> tuple[tuple[GateKind, list[str]], ...]:
    by_role = {component.role: component for component in toolchain.components}
    if language is Language.PYTHON:
        executable = by_role[ToolchainComponentRole.PYTHON_RUNTIME].resolved_executable_path
        return tuple(
            (gate, [executable, "gate_helper.py", gate.value])
            for gate in GateKind
        )
    if language is Language.TYPESCRIPT:
        node = by_role[ToolchainComponentRole.NODE_RUNTIME].resolved_executable_path
        compiler_js = binding.typescript_compiler_js
        if compiler_js is None:
            raise FixtureAcceptanceError("TypeScript compiler binding is unavailable")
        return tuple(
            (
                gate,
                [node, "gate_helper.mjs", gate.value, node, str(compiler_js)],
            )
            for gate in GateKind
        )
    java = by_role[ToolchainComponentRole.JAVA_RUNTIME].resolved_executable_path
    javac = by_role[ToolchainComponentRole.JAVA_COMPILER].resolved_executable_path
    return tuple(
        (gate, [java, "GateHelper.java", gate.value, javac])
        for gate in GateKind
    )


def _gate_contract_hash(
    language: Language,
    toolchain: ToolchainIdentity,
    commands: Sequence[tuple[GateKind, list[str]]],
) -> str:
    return _canonical_hash(
        {
            "commands": [
                {"argv": argv, "gate": gate.value, "order": index}
                for index, (gate, argv) in enumerate(commands)
            ],
            "domain": _GATE_CONTRACT_DOMAIN,
            "environment": {
                "gate_path_entries": toolchain.gate_path_entries,
                "workspace_executable_lookup_allowed": False,
            },
            "language": language.value,
            "runner": {
                "command_timeout_ms": _GATE_TIMEOUT_MS,
                "max_diff_bytes": _MAX_DIFF_BYTES,
                "max_output_bytes": _MAX_OUTPUT_BYTES,
                "termination_grace_ms": _TERMINATION_GRACE_MS,
            },
        }
    )


def _validate_policy_tree(
    policy: DiffPolicy,
    snapshot: SecureTreeSnapshot,
) -> None:
    classified = {
        item.path for item in policy.editable_paths
    } | {item.path for item in policy.protected_paths}
    if set(snapshot.files) != classified or snapshot.directories:
        raise FixtureAcceptanceError(
            "Fixture tree must contain exactly its classified regular files"
        )


def _validate_workspace_policy(
    policy: DiffPolicy,
    before: SecureTreeSnapshot,
    after: SecureTreeSnapshot,
) -> None:
    editable = {item.path: item for item in policy.editable_paths}
    protected = {item.path for item in policy.protected_paths}
    before_paths = set(before.files)
    after_paths = set(after.files)
    created = after_paths - before_paths
    deleted = before_paths - after_paths
    if after.directories != before.directories:
        raise FixtureAcceptanceError("Workspace directory topology changed")
    if any(path not in editable or not editable[path].allow_create for path in created):
        raise FixtureAcceptanceError("reference created an unclassified or forbidden file")
    if any(path not in editable or not editable[path].allow_delete for path in deleted):
        raise FixtureAcceptanceError("reference deleted an unclassified or forbidden file")
    if any(before.files[path] != after.files.get(path) for path in protected):
        raise FixtureAcceptanceError("reference changed a protected file")
    changed = {
        path for path in before_paths | after_paths
        if before.files.get(path) != after.files.get(path)
    }
    if any(path not in editable for path in changed):
        raise FixtureAcceptanceError("Workspace contains an unclassified change")


def _run_gate_commands(
    commands: Sequence[tuple[GateKind, list[str]]],
    *,
    workspace: Path,
    environment_root: Path,
    temporary_root: Path,
    toolchain: ToolchainIdentity,
    toolchain_binding: _ToolchainBindingSnapshot | None = None,
) -> tuple[CommandEvidence, ...]:
    settings = RunnerSettings(
        fixture_path="phase6-fixture",
        command_timeout_ms=_GATE_TIMEOUT_MS,
        termination_grace_ms=_TERMINATION_GRACE_MS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        max_diff_bytes=_MAX_DIFF_BYTES,
    )
    runner = LocalCommandRunner(settings)
    evidence: list[CommandEvidence] = []
    environment = _minimal_environment(toolchain.gate_path_entries)
    for index, (gate, argv) in enumerate(commands):
        if toolchain_binding is not None:
            _verify_toolchain_binding(toolchain_binding, toolchain)
        result = runner.run(
            gate=gate,
            command_index=index,
            argv=argv,
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
            parent_environment=environment,
        )
        if toolchain_binding is not None:
            _verify_toolchain_binding(toolchain_binding, toolchain)
        command = result.evidence
        if (
            result.harness_failure is not None
            or command.status
            not in {CommandStatus.PASSED, CommandStatus.FAILED}
            or command.stdout_truncated
            or command.stderr_truncated
            or command.stdout_decode_replaced
            or command.stderr_decode_replaced
            or not command.termination.process_group_cleared
        ):
            raise FixtureAcceptanceError(
                f"{gate.value} Gate did not complete as a normal quality result"
            )
        evidence.append(command)
    return tuple(evidence)


def _assert_gate_expectations(
    commands: Sequence[CommandEvidence],
    *,
    baseline: bool,
) -> None:
    status = {command.gate: command.status for command in commands}
    expected = {
        GateKind.ACCEPTANCE: (
            CommandStatus.FAILED if baseline else CommandStatus.PASSED
        ),
        GateKind.REGRESSION: CommandStatus.PASSED,
        GateKind.LINT: CommandStatus.PASSED,
        GateKind.TYPECHECK: CommandStatus.PASSED,
    }
    if status != expected:
        label = "baseline" if baseline else "reference"
        raise FixtureAcceptanceError(f"{label} Gate results differ from acceptance contract")


def _apply_reference(
    policy: DiffPolicy,
    reference: SecureTreeSnapshot,
    workspace: Path,
) -> None:
    editable = {item.path for item in policy.editable_paths}
    if set(reference.files) != editable or reference.directories:
        raise FixtureAcceptanceError(
            "reference overlay must contain exactly the editable regular files"
        )
    for relative, content in reference.files.items():
        destination = workspace / relative
        try:
            metadata = destination.lstat()
        except OSError as error:
            raise FixtureAcceptanceError("editable destination is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FixtureAcceptanceError("editable destination must remain a regular file")
        destination.write_bytes(content)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_definition_roots(
    definition: FixtureDefinition,
    *,
    repository_root: Path,
) -> None:
    try:
        fixture = definition.fixture_root.resolve(strict=True)
        reference = definition.reference_root.resolve(strict=True)
        policy = definition.policy_path.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise FixtureAcceptanceError("Fixture input root is unavailable") from error
    prompt_root = repository / "experiments" / "phase6" / "prompts"
    output = definition.output_root.resolve(strict=False)
    if not _is_relative_to(fixture, repository) or not _is_relative_to(policy, repository):
        raise FixtureAcceptanceError("Fixture and Policy must remain inside repository")
    if not _is_relative_to(reference, repository):
        raise FixtureAcceptanceError("reference root must remain inside repository")
    for forbidden in (fixture, prompt_root.resolve(strict=False), output):
        if _is_relative_to(reference, forbidden) or _is_relative_to(forbidden, reference):
            raise FixtureAcceptanceError(
                "reference root must be outside Fixture, Prompt, and Artifact roots"
            )
    for protected_root in (fixture, reference, prompt_root.resolve(strict=False)):
        if _is_relative_to(output, protected_root):
            raise FixtureAcceptanceError(
                "Artifact root must remain outside Fixture, reference, and Prompt roots"
            )
    if os.path.lexists(definition.output_root):
        raise FixtureAcceptanceError("Fixture Acceptance output already exists")
    _validate_output_path_components(definition.output_root, repository)


def _validate_output_path_components(output: Path, repository: Path) -> None:
    normalized_output = Path(os.path.abspath(output))
    normalized_repository = Path(os.path.abspath(repository))
    try:
        relative = normalized_output.relative_to(normalized_repository)
    except ValueError as error:
        raise FixtureAcceptanceError("Artifact output must remain below repository") from error
    current = normalized_repository
    for component in relative.parts:
        current /= component
        if not os.path.lexists(current):
            continue
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise FixtureAcceptanceError(
                "Artifact output path contains a link or non-directory"
            )


def fixture_definitions(
    repository_root: Path,
    output_root: Path,
) -> tuple[FixtureDefinition, ...]:
    base = repository_root / "experiments" / "phase6" / "fixtures"
    return tuple(
        FixtureDefinition(
            language=language,
            fixture_revision=f"tag-normalizer-{language.value}-v1",
            fixture_root=base / language.value / "baseline",
            reference_root=base / language.value / "reference",
            policy_path=base / language.value / "diff-policy.json",
            output_root=output_root / language.value,
        )
        for language in (Language.PYTHON, Language.TYPESCRIPT, Language.JAVA)
    )


def verify_repository_provenance(
    repository_root: Path,
) -> str:
    """Require a clean repository at one exact full commit."""
    try:
        status = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            shell=False,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            shell=False,
        ).stdout.strip()
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise FixtureAcceptanceError("Git provenance could not be verified") from error
    if status.stdout:
        raise FixtureAcceptanceError("Fixture Acceptance requires a clean worktree")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise FixtureAcceptanceError("Fixture Acceptance requires a full commit SHA")
    return commit


def verify_fixture_sources_committed(
    repository_root: Path,
    definition: FixtureDefinition,
) -> None:
    """Verify one language's Fixture, Policy, and reference are tracked."""
    tracked_paths: list[str] = []
    snapshot = secure_tree_snapshot(definition.fixture_root)
    reference = secure_tree_snapshot(definition.reference_root)
    for root, paths in (
        (definition.fixture_root, snapshot.files),
        (definition.reference_root, reference.files),
    ):
        try:
            tracked_paths.extend(
                (root / relative).relative_to(repository_root).as_posix()
                for relative in paths
            )
        except ValueError as error:
            raise FixtureAcceptanceError("Fixture source escaped repository") from error
    try:
        tracked_paths.append(
            definition.policy_path.relative_to(repository_root).as_posix()
        )
    except ValueError as error:
        raise FixtureAcceptanceError("Diff Policy escaped repository") from error
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", *sorted(tracked_paths)],
            cwd=repository_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FixtureAcceptanceError("Fixture source is not fully committed") from error


def _blocked_outcome(
    language: Language,
    *,
    blocker: str,
    detail: str,
) -> FixtureAcceptanceOutcome:
    return FixtureAcceptanceOutcome(
        language=language,
        status="blocked",
        blocker=blocker,
        detail=detail,
        manifest=None,
        acceptance=None,
        manifest_path=None,
        acceptance_path=None,
        manifest_sha256=None,
        acceptance_sha256=None,
        baseline_commands=(),
        reference_commands=(),
    )


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a directory while refusing every existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(source_bytes, destination_bytes, 0x00000004)
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            result = rename(
                -100,
                source_bytes,
                -100,
                destination_bytes,
                0x00000001,
            )
        else:
            raise FixtureAcceptanceError(
                "atomic no-replace publication is unsupported on this platform"
            )
    except AttributeError as error:
        raise FixtureAcceptanceError(
            "atomic no-replace publication is unavailable"
        ) from error
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FixtureAcceptanceError("Fixture Acceptance output already exists")
    raise FixtureAcceptanceError(
        f"atomic no-replace publication failed: {os.strerror(error_number)}"
    )


def _write_acceptance_outputs(
    definition: FixtureDefinition,
    manifest: FixtureManifest,
    acceptance: FixtureAcceptanceRecord,
    *,
    repository_root: Path,
) -> tuple[Path, Path, str, str]:
    manifest_bytes = canonical_json_bytes(manifest)
    acceptance_bytes = canonical_json_bytes(acceptance)
    output_parent = definition.output_root.parent
    _validate_output_path_components(definition.output_root, repository_root)
    output_parent.mkdir(parents=True, exist_ok=True)
    _validate_output_path_components(definition.output_root, repository_root)
    staging = Path(tempfile.mkdtemp(prefix=f".{definition.language.value}-", dir=output_parent))
    manifest_path = staging / "fixture-manifest.json"
    acceptance_path = staging / "fixture-acceptance.json"
    try:
        manifest_path.write_bytes(manifest_bytes)
        acceptance_path.write_bytes(acceptance_bytes)
        if load_fixture_manifest(manifest_path) != manifest:
            raise FixtureAcceptanceError("canonical Fixture Manifest reload failed")
        if load_fixture_acceptance(acceptance_path) != acceptance:
            raise FixtureAcceptanceError("canonical Fixture Acceptance reload failed")
        _rename_directory_no_replace(staging, definition.output_root)
    except FileExistsError as error:
        raise FixtureAcceptanceError("Fixture Acceptance output already exists") from error
    except Exception:
        with suppress(OSError):
            shutil.rmtree(staging)
        raise
    return (
        definition.output_root / manifest_path.name,
        definition.output_root / acceptance_path.name,
        _sha256(manifest_bytes),
        _sha256(acceptance_bytes),
    )


def accept_fixture(
    definition: FixtureDefinition,
    *,
    repository_root: Path,
    commit_sha: str,
    candidates: ToolchainCandidates,
) -> FixtureAcceptanceOutcome:
    """Audit and accept one language independently; never calls a Provider."""
    baseline_commands: tuple[CommandEvidence, ...] = ()
    reference_commands: tuple[CommandEvidence, ...] = ()
    try:
        _validate_definition_roots(definition, repository_root=repository_root)
        policy_bytes = definition.policy_path.read_bytes()
        policy = load_diff_policy(definition.policy_path)
        canonical_policy_bytes = canonical_json_bytes(policy)
        if policy_bytes != canonical_policy_bytes:
            raise FixtureAcceptanceError("Diff Policy must use canonical JSON bytes")
        if (
            policy.language is not definition.language
            or policy.fixture_revision != definition.fixture_revision
        ):
            raise FixtureAcceptanceError("Diff Policy identity differs from Fixture")
        source_before = secure_tree_snapshot(definition.fixture_root)
        reference_before = secure_tree_snapshot(definition.reference_root)
        _validate_policy_tree(policy, source_before)
        toolchain = audit_toolchain(
            definition.language,
            candidates,
            forbidden_roots=(
                repository_root,
                definition.fixture_root,
                definition.reference_root,
                definition.output_root,
            ),
        )
        toolchain_binding = _capture_toolchain_binding(
            definition.language,
            toolchain,
        )
        commands = _commands_for(
            definition.language,
            toolchain,
            toolchain_binding,
        )
        gate_contract_sha256 = _gate_contract_hash(
            definition.language,
            toolchain,
            commands,
        )
        manifest = FixtureManifest(
            schema_version="1.0",
            language=definition.language,
            fixture_revision=definition.fixture_revision,
            fixture_sha256=source_before.sha256,
            gate_contract_sha256=gate_contract_sha256,
            toolchain=toolchain,
        )

        source_workspace_snapshot = snapshot_directory(definition.fixture_root)
        baseline_workspace = prepare_disposable_workspace(
            definition.fixture_root,
            source_workspace_snapshot,
        )
        baseline_removed = False
        try:
            baseline_commands = _run_gate_commands(
                commands,
                workspace=baseline_workspace.workspace,
                environment_root=baseline_workspace.environment_root,
                temporary_root=baseline_workspace.temporary_root,
                toolchain=toolchain,
                toolchain_binding=toolchain_binding,
            )
            _assert_gate_expectations(baseline_commands, baseline=True)
            baseline_after = secure_tree_snapshot(baseline_workspace.workspace)
            if baseline_after.sha256 != source_before.sha256:
                raise FixtureAcceptanceError("baseline Gates changed their Workspace")
        finally:
            baseline_removed, detail = remove_disposable_workspace(baseline_workspace)
            if not baseline_removed:
                raise FixtureAcceptanceError(detail or "baseline Workspace cleanup failed")

        reference_workspace = prepare_disposable_workspace(
            definition.fixture_root,
            source_workspace_snapshot,
        )
        reference_removed = False
        try:
            reference_now = secure_tree_snapshot(definition.reference_root)
            if reference_now.sha256 != reference_before.sha256:
                raise FixtureAcceptanceError("reference solution changed before apply")
            _apply_reference(policy, reference_now, reference_workspace.workspace)
            applied = secure_tree_snapshot(reference_workspace.workspace)
            _validate_workspace_policy(policy, source_before, applied)
            reference_commands = _run_gate_commands(
                commands,
                workspace=reference_workspace.workspace,
                environment_root=reference_workspace.environment_root,
                temporary_root=reference_workspace.temporary_root,
                toolchain=toolchain,
                toolchain_binding=toolchain_binding,
            )
            _assert_gate_expectations(reference_commands, baseline=False)
            reference_after = secure_tree_snapshot(reference_workspace.workspace)
            _validate_workspace_policy(policy, source_before, reference_after)
        finally:
            reference_removed, detail = remove_disposable_workspace(reference_workspace)
            if not reference_removed:
                raise FixtureAcceptanceError(detail or "reference Workspace cleanup failed")

        source_after = secure_tree_snapshot(definition.fixture_root)
        if source_after != source_before:
            raise FixtureAcceptanceError("source Fixture changed during Acceptance")
        _verify_toolchain_binding(toolchain_binding, toolchain)
        if (repository_root / ".git").exists():
            current_commit = verify_repository_provenance(repository_root)
            if current_commit != commit_sha:
                raise FixtureAcceptanceError(
                    "repository commit changed during Fixture Acceptance"
                )
        manifest_bytes = canonical_json_bytes(manifest)
        acceptance = FixtureAcceptanceRecord(
            schema_version="1.0",
            language=definition.language,
            fixture_revision=definition.fixture_revision,
            acceptance_agentlab_commit=commit_sha,
            fixture_source_commit=commit_sha,
            fixture_sha256=source_before.sha256,
            fixture_manifest_sha256=_sha256(manifest_bytes),
            diff_policy_sha256=_sha256(canonical_policy_bytes),
            gate_contract_sha256=gate_contract_sha256,
            reference_solution_sha256=reference_before.sha256,
            reference_solution_in_provider_workspace=False,
            toolchain=toolchain,
            result=GateAcceptanceSummary(
                acceptance_failed_as_expected=True,
                regression_passed=True,
                lint_passed=True,
                typecheck_passed=True,
                reference_all_gates_passed=True,
                source_unchanged=True,
                workspace_cleanup_succeeded=baseline_removed and reference_removed,
            ),
            verified_at=cast(
                datetime,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        manifest_path, acceptance_path, manifest_hash, acceptance_hash = (
            _write_acceptance_outputs(
                definition,
                manifest,
                acceptance,
                repository_root=repository_root,
            )
        )
        return FixtureAcceptanceOutcome(
            language=definition.language,
            status="accepted",
            blocker=None,
            detail=None,
            manifest=manifest,
            acceptance=acceptance,
            manifest_path=manifest_path,
            acceptance_path=acceptance_path,
            manifest_sha256=manifest_hash,
            acceptance_sha256=acceptance_hash,
            baseline_commands=baseline_commands,
            reference_commands=reference_commands,
        )
    except (
        FixtureAcceptanceError,
        SnapshotError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        return FixtureAcceptanceOutcome(
            language=definition.language,
            status="blocked",
            blocker=(
                "toolchain_or_fixture_acceptance_failed"
                if not isinstance(error, SnapshotError)
                else "fixture_snapshot_invalid"
            ),
            detail=str(error),
            manifest=None,
            acceptance=None,
            manifest_path=None,
            acceptance_path=None,
            manifest_sha256=None,
            acceptance_sha256=None,
            baseline_commands=baseline_commands,
            reference_commands=reference_commands,
        )


def accept_phase6_fixtures(
    repository_root: Path,
    output_root: Path,
    *,
    confirm_local_execution: bool,
    candidates: ToolchainCandidates | None = None,
) -> FixtureAcceptanceSuiteOutcome:
    """Accept all languages independently after an explicit local-execution opt-in."""
    if not confirm_local_execution:
        raise FixtureAcceptanceError(
            "--confirm-local-execution is required before toolchain or Gate subprocesses"
        )
    repository = repository_root.resolve(strict=True)
    requested_output = Path(os.path.abspath(
        output_root
        if output_root.is_absolute()
        else repository / output_root
    ))
    required_artifact_root = repository / ".artifacts" / "phase6" / "fixture-acceptance"
    if not _is_relative_to(requested_output, required_artifact_root):
        raise FixtureAcceptanceError(
            "Fixture Acceptance output must remain below "
            ".artifacts/phase6/fixture-acceptance"
        )
    definitions = fixture_definitions(repository, requested_output)
    commit_sha = verify_repository_provenance(repository)
    selected = discover_toolchain_candidates() if candidates is None else candidates
    results: list[FixtureAcceptanceOutcome] = []
    for definition in definitions:
        try:
            verify_fixture_sources_committed(repository, definition)
        except (FixtureAcceptanceError, OSError, ValueError) as error:
            results.append(
                _blocked_outcome(
                    definition.language,
                    blocker="fixture_provenance_invalid",
                    detail=str(error),
                )
            )
            continue
        results.append(
            accept_fixture(
                definition,
                repository_root=repository,
                commit_sha=commit_sha,
                candidates=selected,
            )
        )
    return FixtureAcceptanceSuiteOutcome(commit_sha=commit_sha, results=tuple(results))
