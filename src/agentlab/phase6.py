"""Phase 6 versioned contracts and read-only cross-artifact validation.

This module intentionally has no Provider, Gate, publication, or fixture
materialization entry point.  Slice 6A defines and validates persisted
contracts only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, NoReturn

import yaml
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.campaign import (
    CampaignEvent,
    CampaignFinishedEvent,
    CampaignRunEvent,
    CampaignRunStatus,
    CampaignStopReason,
    load_campaign,
)
from agentlab.models import (
    CodexCleanupState,
    CodexExecutionEvidence,
    CodexExecutionStage,
    CodexFailureStage,
    CommandEvidence,
    CommandStatus,
    ContractModel,
    DiffEvidence,
    ExecutionMode,
    GateKind,
    LiveFailureKind,
    LiveRunArtifact,
    Provider,
    ProviderExecutionStatus,
    ReasoningEffort,
    RunMetrics,
    RunnerSettings,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.recording import ReplayRecording, load_replay_recording
from agentlab.workflow import (
    LoadedWorkflowSpec,
    WorkflowExperimentSpec,
    WorkflowPlan,
    WorkflowSpecError,
    load_workflow_plan,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$|^[0-9a-f]{64}$"
CANONICAL_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


class Phase6ContractError(ValueError):
    """Raised when a Phase 6 contract or relationship is invalid."""


class Phase6PathError(Phase6ContractError):
    """Raised when a listed Phase 6 path crosses its fixed root."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    identity: FileIdentity
    content: bytes
    sha256: str


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class DirectorySnapshot:
    path: Path
    identity: DirectoryIdentity


class Language(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVA = "java"


class LanguageStatus(StrEnum):
    NOT_READY = "not_ready"
    READY_NOT_RUN = "ready_not_run"
    EVALUATED = "evaluated"
    BLOCKED = "blocked"


class SourceClass(StrEnum):
    PRIMARY = "primary"
    HISTORICAL = "historical"


class ProviderEvaluationStatus(StrEnum):
    EVALUATED = "evaluated"
    NOT_EVALUATED = "not_evaluated"


class Phase6OverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PROVIDER_ERROR = "provider_error"
    HARNESS_ERROR = "harness_error"
    REJECTED = "rejected"


class Phase6FailureKind(StrEnum):
    NONE = "none"
    PROVIDER_TURN_FAILED = "provider_turn_failed"
    PROVIDER_CLI_NONZERO = "provider_cli_nonzero"
    PROVIDER_SIGNAL_TERMINATION = "provider_signal_termination"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_SPAWN_ERROR = "provider_spawn_error"
    PROVIDER_INPUT_ERROR = "provider_input_error"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    PROVIDER_OUTPUT_LIMIT = "provider_output_limit"
    PROCESS_CLEANUP_ERROR = "process_cleanup_error"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    GATE_HARNESS_ERROR = "gate_harness_error"
    EVIDENCE_ERROR = "evidence_error"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


class Phase6CampaignOutcome(StrEnum):
    SUCCESS = "success"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    HARNESS_FAILURE = "harness_failure"
    CLEANUP_FAILURE = "cleanup_failure"
    HUMAN_INTERRUPTION = "human_interruption"
    STOP_CONDITION = "stop_condition"


class GateNotExecutedReason(StrEnum):
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    PRE_GATE_HARNESS_FAILURE = "pre_gate_harness_failure"
    INPUT_CHANGED = "input_changed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"


class ToolchainComponentRole(StrEnum):
    PYTHON_RUNTIME = "python_runtime"
    NODE_RUNTIME = "node_runtime"
    TYPESCRIPT_COMPILER = "typescript_compiler"
    JAVA_RUNTIME = "java_runtime"
    JAVA_COMPILER = "java_compiler"


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_canonical_timestamp(value: object, field_name: str) -> object:
    if not isinstance(value, str) or not CANONICAL_RFC3339_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must use canonical UTC RFC 3339 "
            "YYYY-MM-DDTHH:MM:SS.ffffffZ"
        )
    return value


def _relative_file(value: str, field_name: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or "\\" in value
    ):
        raise ValueError(f"{field_name} must be a canonical relative POSIX file path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value in {".", "./"}
        or ".." in posix.parts
        or posix.as_posix() != value
    ):
        raise ValueError(f"{field_name} must remain below the fixed root")
    return value


def _identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        link_count=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> DirectoryIdentity:
    return DirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _snapshot_directory(path: Path, label: str) -> DirectorySnapshot:
    """Open one directory without following a final symlink and snapshot it."""
    descriptor: int | None = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise Phase6PathError(f"{label} must be a real directory")
        descriptor = os.open(path, _directory_open_flags())
        opened = os.fstat(descriptor)
        after = path.lstat()
    except Phase6PathError:
        raise
    except OSError as error:
        raise Phase6PathError(f"{label} could not be opened safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    identities = {
        _directory_identity(before),
        _directory_identity(opened),
        _directory_identity(after),
    }
    if len(identities) != 1:
        raise Phase6PathError(f"{label} changed while being inspected")
    return DirectorySnapshot(
        path=path,
        identity=_directory_identity(opened),
    )


def _require_directory_snapshot_unchanged(
    snapshot: DirectorySnapshot,
    label: str,
) -> None:
    current = _snapshot_directory(snapshot.path, label)
    if current.identity != snapshot.identity:
        raise Phase6PathError(f"{label} changed after Manifest load")


def _read_file_below_root(
    *,
    root_snapshot: DirectorySnapshot,
    relative: str,
    label: str,
) -> tuple[FileSnapshot, tuple[DirectorySnapshot, ...]]:
    """Read a listed file from a stable root FD using no-follow component opens."""
    relative = _relative_file(relative, label)
    parts = PurePosixPath(relative).parts
    directory_descriptors: list[int] = []
    directory_snapshots: list[DirectorySnapshot] = [root_snapshot]
    file_descriptor: int | None = None
    try:
        root_descriptor = os.open(root_snapshot.path, _directory_open_flags())
        directory_descriptors.append(root_descriptor)
        if (
            _directory_identity(os.fstat(root_descriptor))
            != root_snapshot.identity
        ):
            raise Phase6PathError("Manifest root changed before listed file read")

        current_path = root_snapshot.path
        for component in parts[:-1]:
            parent_descriptor = directory_descriptors[-1]
            before = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise Phase6PathError(f"{label} path contains a non-directory link")
            child_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            directory_descriptors.append(child_descriptor)
            opened = os.fstat(child_descriptor)
            after = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            directory_identities = {
                _directory_identity(before),
                _directory_identity(opened),
                _directory_identity(after),
            }
            if len(directory_identities) != 1:
                raise Phase6PathError(f"{label} parent directory changed")
            current_path /= component
            directory_snapshots.append(
                DirectorySnapshot(
                    path=current_path,
                    identity=_directory_identity(opened),
                )
            )

        parent_descriptor = directory_descriptors[-1]
        filename = parts[-1]
        before_file = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(before_file.st_mode):
            raise Phase6PathError(f"{label} must not be a symlink")
        if not stat.S_ISREG(before_file.st_mode):
            raise Phase6PathError(f"{label} must be a regular file")
        if before_file.st_nlink != 1:
            raise Phase6PathError(f"{label} hardlink is not allowed")
        file_descriptor = os.open(
            filename,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened_file = os.fstat(file_descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_open_file = os.fstat(file_descriptor)
        after_file = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except Phase6PathError:
        raise
    except OSError as error:
        raise Phase6PathError(f"{label} could not be read safely") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)

    file_identities = {
        _identity(before_file),
        _identity(opened_file),
        _identity(after_open_file),
        _identity(after_file),
    }
    if len(file_identities) != 1:
        raise Phase6PathError(f"{label} changed while being read")
    content = b"".join(chunks)
    if len(content) != after_open_file.st_size:
        raise Phase6PathError(f"{label} size changed while being read")
    snapshot = FileSnapshot(
        path=root_snapshot.path.joinpath(*parts),
        identity=_identity(after_open_file),
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    for index, directory_snapshot in enumerate(directory_snapshots):
        _require_directory_snapshot_unchanged(
            directory_snapshot,
            f"{label} parent component {index}",
        )
    return snapshot, tuple(directory_snapshots)


def _read_stable_regular_file(path: Path, label: str) -> FileSnapshot:
    """Read one regular non-link file while detecting replacement or mutation."""
    try:
        before_path = path.lstat()
    except OSError as error:
        raise Phase6PathError(f"{label} is unavailable") from error
    if stat.S_ISLNK(before_path.st_mode):
        raise Phase6PathError(f"{label} must not be a symlink")
    if not stat.S_ISREG(before_path.st_mode):
        raise Phase6PathError(f"{label} must be a regular file")
    if before_path.st_nlink != 1:
        raise Phase6PathError(f"{label} hardlink is not allowed")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before_open = os.fstat(descriptor)
        if (
            before_open.st_dev,
            before_open.st_ino,
        ) != (
            before_path.st_dev,
            before_path.st_ino,
        ):
            raise Phase6PathError(f"{label} changed before reading")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_open = os.fstat(descriptor)
    except Phase6PathError:
        raise
    except OSError as error:
        raise Phase6PathError(f"{label} could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        after_path = path.lstat()
    except OSError as error:
        raise Phase6PathError(f"{label} changed after reading") from error
    identities = {
        _identity(before_path),
        _identity(before_open),
        _identity(after_open),
        _identity(after_path),
    }
    if len(identities) != 1:
        raise Phase6PathError(f"{label} changed while being read")
    content = b"".join(chunks)
    if len(content) != after_open.st_size:
        raise Phase6PathError(f"{label} size changed while being read")
    return FileSnapshot(
        path=path,
        identity=_identity(after_open),
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class ToolchainComponent(ContractModel):
    role: ToolchainComponentRole
    resolved_executable_path: StrictStr = Field(min_length=1)
    executable_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    version_argv: list[StrictStr] = Field(min_length=1)
    exact_version: StrictStr = Field(min_length=1)
    version_output_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    package_version: StrictStr | None = None
    package_fingerprint: StrictStr | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def component_contract_is_complete(self) -> ToolchainComponent:
        path = Path(self.resolved_executable_path)
        if not path.is_absolute() or "\x00" in self.resolved_executable_path:
            raise ValueError("resolved_executable_path must be an absolute path")
        if self.version_argv[0] != self.resolved_executable_path:
            raise ValueError("version argv must use the resolved executable path")
        compiler = self.role is ToolchainComponentRole.TYPESCRIPT_COMPILER
        if compiler is not (
            self.package_version is not None
            and self.package_fingerprint is not None
        ):
            raise ValueError(
                "typescript_compiler requires package version and package fingerprint"
            )
        return self


class ToolchainIdentity(ContractModel):
    os: StrictStr = Field(pattern=r"^[a-z0-9_]+$")
    architecture: StrictStr = Field(pattern=r"^[a-z0-9_]+$")
    gate_path_entries: list[StrictStr] = Field(min_length=1)
    workspace_executable_lookup_allowed: Literal[False]
    components: list[ToolchainComponent] = Field(min_length=1)
    fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def roles_and_fingerprint_are_canonical(self) -> ToolchainIdentity:
        if len(self.gate_path_entries) != len(set(self.gate_path_entries)):
            raise ValueError("Gate PATH entries must be unique")
        if any(
            not Path(entry).is_absolute() or "\x00" in entry
            for entry in self.gate_path_entries
        ):
            raise ValueError("Gate PATH entries must be absolute")
        roles = [component.role for component in self.components]
        if roles != sorted(set(roles), key=lambda role: role.value):
            raise ValueError("toolchain components must have unique sorted roles")
        expected = hashlib.sha256(
            canonical_json_bytes(
                {
                    "architecture": self.architecture,
                    "components": [
                        component.model_dump(mode="json")
                        for component in self.components
                    ],
                    "gate_path_entries": self.gate_path_entries,
                    "os": self.os,
                    "workspace_executable_lookup_allowed":
                    self.workspace_executable_lookup_allowed,
                }
            )
        ).hexdigest()
        if self.fingerprint != expected:
            raise ValueError("toolchain fingerprint does not match components")
        return self


class FixtureManifest(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain: ToolchainIdentity

    @model_validator(mode="after")
    def language_requires_exact_toolchain_roles(self) -> FixtureManifest:
        expected = {
            Language.PYTHON: {ToolchainComponentRole.PYTHON_RUNTIME},
            Language.TYPESCRIPT: {
                ToolchainComponentRole.NODE_RUNTIME,
                ToolchainComponentRole.TYPESCRIPT_COMPILER,
            },
            Language.JAVA: {
                ToolchainComponentRole.JAVA_RUNTIME,
                ToolchainComponentRole.JAVA_COMPILER,
            },
        }[self.language]
        actual = {component.role for component in self.toolchain.components}
        if actual != expected:
            raise ValueError(
                f"{self.language.value} Fixture requires its exact toolchain roles"
            )
        return self


class EditablePathPolicy(ContractModel):
    path: StrictStr
    allow_create: StrictBool
    allow_delete: StrictBool

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "editable path")


class ProtectedPathPolicy(ContractModel):
    path: StrictStr
    role: Literal[
        "acceptance",
        "regression",
        "lint",
        "typecheck",
        "gate_helper",
        "lockfile",
        "configuration",
        "fixture_manifest",
    ]

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "protected path")


class DiffPolicy(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    editable_paths: list[EditablePathPolicy] = Field(min_length=1)
    protected_paths: list[ProtectedPathPolicy] = Field(min_length=1)
    reject_unclassified_paths: Literal[True]
    reject_symlinks: Literal[True]
    reject_hardlinks: Literal[True]
    reject_special_files: Literal[True]

    @model_validator(mode="after")
    def paths_are_disjoint_and_sorted(self) -> DiffPolicy:
        editable = [item.path for item in self.editable_paths]
        protected = [item.path for item in self.protected_paths]
        if editable != sorted(set(editable)):
            raise ValueError("editable paths must be unique and sorted")
        if protected != sorted(set(protected)):
            raise ValueError("protected paths must be unique and sorted")
        if set(editable).intersection(protected):
            raise ValueError("editable and protected paths must be disjoint")
        return self


class GateAcceptanceSummary(ContractModel):
    acceptance_failed_as_expected: Literal[True]
    regression_passed: Literal[True]
    lint_passed: Literal[True]
    typecheck_passed: Literal[True]
    reference_all_gates_passed: Literal[True]
    source_unchanged: Literal[True]
    workspace_cleanup_succeeded: Literal[True]


class FixtureAcceptanceRecord(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    acceptance_agentlab_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_source_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_in_provider_workspace: Literal[False]
    toolchain: ToolchainIdentity
    result: GateAcceptanceSummary
    verified_at: datetime

    @field_validator("verified_at", mode="before")
    @classmethod
    def verified_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "verified_at")

    @field_validator("verified_at")
    @classmethod
    def verified_at_is_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("verified_at must use UTC")
        return value


class WorkflowExperimentSpecV2_1(WorkflowExperimentSpec):
    schema_version: Literal["2.1"]  # type: ignore[assignment]
    language: Language
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_manifest_path: StrictStr
    fixture_acceptance_path: StrictStr
    diff_policy_path: StrictStr

    @field_validator(
        "fixture_manifest_path",
        "fixture_acceptance_path",
        "diff_policy_path",
    )
    @classmethod
    def phase6_input_path_is_relative(
        cls,
        value: str,
        info: Any,
    ) -> str:
        return _relative_file(value, str(info.field_name))


class WorkflowPlanV1_2(WorkflowPlan):
    schema_version: Literal["1.2"]  # type: ignore[assignment]
    experiment_spec_schema_version: Literal["2.1"]  # type: ignore[assignment]
    language: Language
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)


WorkflowSpecContract = WorkflowExperimentSpec | WorkflowExperimentSpecV2_1
WorkflowPlanContract = WorkflowPlan | WorkflowPlanV1_2


@dataclass(frozen=True)
class LoadedWorkflowSpecContract:
    spec: WorkflowSpecContract
    sha256: str


class Phase6CampaignStartedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[0]
    event_type: Literal["campaign_started"]
    experiment_id: StrictStr
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    planned_run_count: StrictInt = Field(gt=0)
    planned_provider_call_count: StrictInt = Field(gt=0)
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


class Phase6CampaignRunEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["run_state"]
    run_id: StrictStr
    task_id: StrictStr
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    status: CampaignRunStatus
    outcome: Phase6CampaignOutcome | None
    stop_reason: CampaignStopReason | None
    provider_call_count: StrictInt | None = Field(default=None, ge=0, le=1)
    gate_executed: StrictBool
    counted_failure: StrictBool
    fail_fast_applies: StrictBool
    max_failures_applies: StrictBool
    failure_kind: Phase6FailureKind | None
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")

    @model_validator(mode="after")
    def state_transition_is_explicit(self) -> Phase6CampaignRunEvent:
        if self.status is CampaignRunStatus.PLANNED:
            raise ValueError("Campaign recording must not append planned states")
        if self.status is CampaignRunStatus.STARTED:
            if (
                self.outcome is not None
                or self.stop_reason is not None
                or self.provider_call_count is not None
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError("started state must not predict terminal observations")
            return self

        if self.stop_reason is CampaignStopReason.INPUT_CHANGED:
            if (
                self.status is not CampaignRunStatus.NOT_RUN
                or self.outcome is not Phase6CampaignOutcome.STOP_CONDITION
                or self.provider_call_count != 0
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError(
                    "input_changed is a zero-call, zero-Gate Harness safety stop "
                    "outside failure counting"
                )
            return self

        if self.outcome is Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.stop_reason is not None
                or self.provider_call_count != 1
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind
                is not Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            ):
                raise ValueError(
                    "output_contract_violation requires one Provider call, no Gate, "
                    "and normal failure counting"
                )
            return self

        if self.status is CampaignRunStatus.NOT_RUN:
            if (
                self.outcome is not Phase6CampaignOutcome.STOP_CONDITION
                or self.stop_reason in {None, CampaignStopReason.NONE}
                or self.provider_call_count != 0
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError("not_run requires a zero-call stop condition")
            return self

        if self.status is CampaignRunStatus.INTERRUPTED:
            if (
                self.outcome is not Phase6CampaignOutcome.HUMAN_INTERRUPTION
                or self.stop_reason is not CampaignStopReason.HUMAN_INTERRUPTION
                or self.provider_call_count not in {0, 1, None}
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError(
                    "interrupted state requires human_interruption, no Gate or "
                    "failure kind, and remains outside failure counting"
                )
            return self

        if (
            self.status not in {
                CampaignRunStatus.COMPLETED,
                CampaignRunStatus.FAILED,
            }
            or self.stop_reason is not None
            or self.provider_call_count is None
        ):
            raise ValueError("attempted terminal state is incomplete")

        if self.outcome is Phase6CampaignOutcome.SUCCESS:
            if (
                self.status is not CampaignRunStatus.COMPLETED
                or self.provider_call_count != 1
                or not self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.NONE
            ):
                raise ValueError("success requires one call, Gate completion, and no failure")
            return self

        if self.outcome is Phase6CampaignOutcome.QUALITY_GATE_FAILURE:
            if (
                self.status is not CampaignRunStatus.COMPLETED
                or self.provider_call_count != 1
                or not self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.QUALITY_GATE_FAILURE
            ):
                raise ValueError(
                    "quality_gate_failure requires one call, Gate completion, "
                    "and normal failure counting"
                )
            return self

        provider_failures = {
            Phase6FailureKind.PROVIDER_TURN_FAILED,
            Phase6FailureKind.PROVIDER_CLI_NONZERO,
            Phase6FailureKind.PROVIDER_SIGNAL_TERMINATION,
            Phase6FailureKind.PROVIDER_UNAVAILABLE,
            Phase6FailureKind.PROVIDER_SPAWN_ERROR,
            Phase6FailureKind.PROVIDER_INPUT_ERROR,
            Phase6FailureKind.PROVIDER_PROTOCOL_ERROR,
            Phase6FailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        if self.outcome is Phase6CampaignOutcome.PROVIDER_FAILURE:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind not in provider_failures
            ):
                raise ValueError(
                    "provider_failure requires no Gate and normal failure counting"
                )
            return self

        if self.outcome is Phase6CampaignOutcome.PROVIDER_TIMEOUT:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.provider_call_count != 1
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.PROVIDER_TIMEOUT
            ):
                raise ValueError(
                    "provider_timeout requires one call, no Gate, "
                    "and normal failure counting"
                )
            return self

        if self.outcome in {
            Phase6CampaignOutcome.HARNESS_FAILURE,
            Phase6CampaignOutcome.CLEANUP_FAILURE,
        }:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
            ):
                raise ValueError("Harness failures stop independently of failure counters")
            if (
                self.outcome is Phase6CampaignOutcome.CLEANUP_FAILURE
                and self.failure_kind
                is not Phase6FailureKind.PROCESS_CLEANUP_ERROR
            ):
                raise ValueError(
                    "cleanup failure has priority and requires process_cleanup_error"
                )
            if (
                self.outcome is Phase6CampaignOutcome.HARNESS_FAILURE
                and self.failure_kind
                not in {
                    Phase6FailureKind.GATE_HARNESS_ERROR,
                    Phase6FailureKind.EVIDENCE_ERROR,
                    Phase6FailureKind.UNSUPPORTED_PLATFORM,
                }
            ):
                raise ValueError("Harness failure requires a Harness failure kind")
            return self

        raise ValueError("terminal Campaign outcome is unsupported")


class Phase6CampaignFinishedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["campaign_finished"]
    experiment_id: StrictStr
    stop_reason: CampaignStopReason
    attempted_run_count: StrictInt = Field(ge=0)
    provider_call_count: StrictInt = Field(ge=0)
    provider_call_count_unknown_runs: StrictInt = Field(ge=0)
    counted_failure_count: StrictInt = Field(ge=0)
    retry_count: Literal[0]
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


Phase6CampaignEvent = Annotated[
    Phase6CampaignStartedEvent
    | Phase6CampaignRunEvent
    | Phase6CampaignFinishedEvent,
    Field(discriminator="event_type"),
]
_PHASE6_CAMPAIGN_ADAPTER: TypeAdapter[Phase6CampaignEvent] = TypeAdapter(
    Phase6CampaignEvent
)


@dataclass(frozen=True)
class LoadedPhase6Campaign:
    events: tuple[Phase6CampaignEvent, ...]

    @property
    def started(self) -> Phase6CampaignStartedEvent:
        event = self.events[0]
        assert isinstance(event, Phase6CampaignStartedEvent)
        return event

    @property
    def finished(self) -> Phase6CampaignFinishedEvent:
        event = self.events[-1]
        assert isinstance(event, Phase6CampaignFinishedEvent)
        return event


CampaignContract = list[CampaignEvent] | LoadedPhase6Campaign


def _validate_phase6_execution_observations(
    *,
    overall_status: Phase6OverallStatus,
    failure_kind: Phase6FailureKind,
    codex: CodexExecutionEvidence,
    gate_executed: bool,
    gate_not_executed_reason: GateNotExecutedReason | None,
    gate_commands: list[CommandEvidence],
    diff: DiffEvidence,
    metrics: RunMetrics | None,
    workspace_lifecycle: WorkspaceLifecycle,
    terminal_at: datetime,
) -> None:
    """Apply the shared Artifact/Recording quality and cleanup state contract."""
    if gate_executed != bool(gate_commands):
        raise ValueError("gate_executed must match Gate Evidence presence")
    if gate_executed is (gate_not_executed_reason is not None):
        raise ValueError(
            "Gate execution and gate_not_executed_reason must be complementary"
        )
    if (
        codex.execution_stage is CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
        and workspace_lifecycle is not WorkspaceLifecycle.NOT_CREATED
    ):
        raise ValueError(
            "preflight_not_completed requires a not_created Workspace"
        )
    if (
        codex.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
        and workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
    ):
        raise ValueError(
            "provider_invocation_attempted requires a created Workspace"
        )
    if (
        workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
        and gate_commands
    ):
        raise ValueError("not_created Workspace cannot contain Gate execution")
    gate_process_cleanup_failed = any(
        not command.termination.process_group_cleared
        for command in gate_commands
    )
    codex_process_cleanup_failed = (
        codex.cleanup_state is CodexCleanupState.FAILED
        or not codex.termination.process_group_cleared
        or codex.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    )
    cleanup_failed_observed = (
        workspace_lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
        or gate_process_cleanup_failed
        or codex_process_cleanup_failed
    )
    cleanup_terminal = (
        overall_status is Phase6OverallStatus.HARNESS_ERROR
        and failure_kind is Phase6FailureKind.PROCESS_CLEANUP_ERROR
    )
    if cleanup_failed_observed is not cleanup_terminal:
        raise ValueError(
            "observed cleanup failure and process_cleanup_error Harness status "
            "must be bidirectionally consistent and take priority"
        )
    if codex.status is ProviderExecutionStatus.FAILED and gate_commands:
        raise ValueError("quality Gates must not run after failed Codex execution")

    abnormal_gate_observed = bool(gate_commands) and any(
        command.status not in {CommandStatus.PASSED, CommandStatus.FAILED}
        or not command.termination.process_group_cleared
        for command in gate_commands
    )
    gate_harness_observed = (
        codex.status is ProviderExecutionStatus.SUCCEEDED
        and gate_executed
        and abnormal_gate_observed
        and not cleanup_failed_observed
    )
    gate_harness_terminal = (
        overall_status is Phase6OverallStatus.HARNESS_ERROR
        and failure_kind is Phase6FailureKind.GATE_HARNESS_ERROR
    )
    if gate_harness_observed is not gate_harness_terminal:
        raise ValueError(
            "gate_harness_error requires successful Codex and an abnormal "
            "Gate observation without a higher-priority cleanup failure"
        )

    unsupported_observed = (
        codex.status is ProviderExecutionStatus.FAILED
        and codex.failure_kind is LiveFailureKind.UNSUPPORTED_PLATFORM
        and codex.execution_stage is CodexExecutionStage.PREFLIGHT_COMPLETED
        and codex.failure_stage
        is CodexFailureStage.PROVIDER_RUNTIME_PRECHECK
        and not cleanup_failed_observed
    )
    unsupported_terminal = (
        overall_status is Phase6OverallStatus.HARNESS_ERROR
        and failure_kind is Phase6FailureKind.UNSUPPORTED_PLATFORM
    )
    if unsupported_observed is not unsupported_terminal:
        raise ValueError(
            "unsupported_platform must match Codex runtime-precheck Evidence"
        )

    evidence_error_observed = (
        (
            codex.status is ProviderExecutionStatus.FAILED
            and codex.failure_kind is LiveFailureKind.EVIDENCE_ERROR
        )
        or (
            codex.status is ProviderExecutionStatus.SUCCEEDED
            and diff.collection_error is not None
        )
    )
    evidence_error_expected = (
        evidence_error_observed
        and not cleanup_failed_observed
        and not gate_harness_observed
        and not unsupported_observed
    )
    evidence_error_terminal = (
        overall_status is Phase6OverallStatus.HARNESS_ERROR
        and failure_kind is Phase6FailureKind.EVIDENCE_ERROR
    )
    if evidence_error_expected is not evidence_error_terminal:
        raise ValueError(
            "evidence_error requires matching Codex Evidence failure or an "
            "explicit post-Codex Evidence collection error"
        )

    commands_completed_normally = bool(gate_commands) and all(
        command.status in {CommandStatus.PASSED, CommandStatus.FAILED}
        and command.termination.process_group_cleared
        for command in gate_commands
    )
    quality_status = overall_status in {
        Phase6OverallStatus.PASSED,
        Phase6OverallStatus.FAILED,
    }
    if quality_status and (
        codex.status is not ProviderExecutionStatus.SUCCEEDED
        or not commands_completed_normally
        or not diff.line_counts_complete
        or workspace_lifecycle is not WorkspaceLifecycle.REMOVED
    ):
        raise ValueError(
            "quality result requires complete Gate, diff, and Workspace Evidence"
        )
    if quality_status is not (metrics is not None):
        raise ValueError(
            "RunMetrics presence must match a complete quality result"
        )
    if gate_commands:
        if not any(command.gate is GateKind.ACCEPTANCE for command in gate_commands):
            raise ValueError("executed Gate requires acceptance commands")
        if any(
            command.started_at < codex.completed_at
            or command.completed_at > terminal_at
            for command in gate_commands
        ):
            raise ValueError(
                "Gate timestamps must follow Codex and remain inside terminal time"
            )
    if codex.completed_at > terminal_at:
        raise ValueError("Codex completion must not follow terminal time")

    if metrics is None:
        return
    acceptance = [
        command
        for command in gate_commands
        if command.gate is GateKind.ACCEPTANCE
    ]
    regression = [
        command
        for command in gate_commands
        if command.gate is GateKind.REGRESSION
    ]
    lint = [
        command for command in gate_commands if command.gate is GateKind.LINT
    ]
    typecheck = [
        command
        for command in gate_commands
        if command.gate is GateKind.TYPECHECK
    ]
    expected_quality_pass = all(
        command.status is CommandStatus.PASSED for command in gate_commands
    )
    expected_counts = (
        sum(command.status is CommandStatus.PASSED for command in acceptance),
        len(acceptance),
        sum(command.status is CommandStatus.FAILED for command in regression),
        sum(command.status is CommandStatus.FAILED for command in lint),
        sum(command.status is CommandStatus.FAILED for command in typecheck),
    )
    actual_counts = (
        metrics.acceptance_tests_passed,
        metrics.acceptance_tests_total,
        metrics.regression_failures,
        metrics.lint_errors,
        metrics.typecheck_errors,
    )
    if (
        metrics.quality_gate_pass is not expected_quality_pass
        or (
            overall_status is Phase6OverallStatus.PASSED
        )
        is not expected_quality_pass
        or actual_counts != expected_counts
        or metrics.agent_duration_ms != codex.duration_ms
        or metrics.total_duration_ms
        != metrics.agent_duration_ms + metrics.evaluation_duration_ms
        or metrics.agent_call_count != 1
        or metrics.retry_count != 0
        or metrics.changed_files != diff.changed_files
        or metrics.added_lines != diff.added_lines
        or metrics.deleted_lines != diff.deleted_lines
        or metrics.usage_metrics != codex.usage_metrics
    ):
        raise ValueError(
            "quality status, Metrics, Gate, Codex, and diff observations differ"
        )


class Phase6RecordingStartedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[0]
    event_type: Literal["run_started"]
    run_id: StrictStr
    experiment_id: StrictStr
    task_id: StrictStr
    language: Language
    workflow: Workflow
    provider: Literal[Provider.CODEX]
    repetition_index: StrictInt = Field(ge=0)
    execution_mode: Literal[ExecutionMode.LIVE]
    occurred_at: datetime
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: Literal[True]
    requested_model: StrictStr
    requested_reasoning_effort: ReasoningEffort
    cli_version: StrictStr

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


class Phase6RecordingTerminalEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[1]
    event_type: Literal["run_completed", "run_failed"]
    run_id: StrictStr
    experiment_id: StrictStr
    occurred_at: datetime
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    codex: CodexExecutionEvidence
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    gate_commands: list[CommandEvidence]
    diff: DiffEvidence
    metrics: RunMetrics | None
    workspace_lifecycle: WorkspaceLifecycle

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.5":
            raise ValueError(
                "Recording 1.2 requires unchanged CodexExecutionEvidence 1.5"
            )
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")

    @model_validator(mode="after")
    def terminal_state_is_coherent(self) -> Phase6RecordingTerminalEvent:
        if self.gate_not_executed_reason is GateNotExecutedReason.INPUT_CHANGED:
            raise ValueError("input_changed must not create a Recording")
        _validate_phase6_execution_observations(
            overall_status=self.overall_status,
            failure_kind=self.failure_kind,
            codex=self.codex,
            gate_executed=self.gate_executed,
            gate_not_executed_reason=self.gate_not_executed_reason,
            gate_commands=self.gate_commands,
            diff=self.diff,
            metrics=self.metrics,
            workspace_lifecycle=self.workspace_lifecycle,
            terminal_at=self.occurred_at,
        )
        if self.event_type == "run_completed":
            if (
                (
                    self.overall_status is Phase6OverallStatus.PASSED
                    and self.failure_kind is not Phase6FailureKind.NONE
                )
                or (
                    self.overall_status is Phase6OverallStatus.FAILED
                    and self.failure_kind
                    is not Phase6FailureKind.QUALITY_GATE_FAILURE
                )
                or self.overall_status
                not in {Phase6OverallStatus.PASSED, Phase6OverallStatus.FAILED}
                or not self.gate_executed
                or self.gate_not_executed_reason is not None
                or self.metrics is None
                or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
            ):
                raise ValueError("run_completed requires a complete quality result")
            return self
        if (
            self.overall_status
            not in {
                Phase6OverallStatus.PROVIDER_ERROR,
                Phase6OverallStatus.HARNESS_ERROR,
                Phase6OverallStatus.REJECTED,
            }
            or self.failure_kind
            in {
                Phase6FailureKind.NONE,
                Phase6FailureKind.QUALITY_GATE_FAILURE,
            }
            or self.metrics is not None
        ):
            raise ValueError("run_failed requires a non-quality terminal failure")
        if (
            self.overall_status is Phase6OverallStatus.REJECTED
        ) is not (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
        ):
            raise ValueError(
                "rejected status is reserved for output_contract_violation"
            )
        if (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            and (
                self.overall_status is not Phase6OverallStatus.REJECTED
                or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
                or self.gate_executed
                or self.gate_not_executed_reason
                is not GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
            )
        ):
            raise ValueError(
                "output_contract_violation requires successful Codex and no Gate"
            )
        provider_failures = {
            Phase6FailureKind.PROVIDER_TURN_FAILED,
            Phase6FailureKind.PROVIDER_CLI_NONZERO,
            Phase6FailureKind.PROVIDER_SIGNAL_TERMINATION,
            Phase6FailureKind.PROVIDER_TIMEOUT,
            Phase6FailureKind.PROVIDER_UNAVAILABLE,
            Phase6FailureKind.PROVIDER_SPAWN_ERROR,
            Phase6FailureKind.PROVIDER_INPUT_ERROR,
            Phase6FailureKind.PROVIDER_PROTOCOL_ERROR,
            Phase6FailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        harness_failures = {
            Phase6FailureKind.PROCESS_CLEANUP_ERROR,
            Phase6FailureKind.GATE_HARNESS_ERROR,
            Phase6FailureKind.EVIDENCE_ERROR,
            Phase6FailureKind.UNSUPPORTED_PLATFORM,
        }
        if (
            self.overall_status is Phase6OverallStatus.PROVIDER_ERROR
        ) is not (self.failure_kind in provider_failures):
            raise ValueError(
                "provider_error status must match a Provider failure kind"
            )
        if (
            self.overall_status is Phase6OverallStatus.HARNESS_ERROR
        ) is not (self.failure_kind in harness_failures):
            raise ValueError(
                "harness_error status must match a Harness failure kind"
            )
        if (
            self.overall_status is Phase6OverallStatus.PROVIDER_ERROR
            and self.codex.failure_kind.value != self.failure_kind.value
        ):
            raise ValueError(
                "Recording and Codex Provider failure kinds must match"
            )
        if self.overall_status is Phase6OverallStatus.PROVIDER_ERROR:
            expected_reason = (
                GateNotExecutedReason.PROVIDER_TIMEOUT
                if self.failure_kind is Phase6FailureKind.PROVIDER_TIMEOUT
                else GateNotExecutedReason.PROVIDER_FAILURE
            )
            if (
                self.codex.status is not ProviderExecutionStatus.FAILED
                or self.gate_executed
                or self.gate_not_executed_reason is not expected_reason
            ):
                raise ValueError(
                    "Provider failure requires failed Codex and its fixed Gate reason"
                )
        if (
            self.overall_status is Phase6OverallStatus.HARNESS_ERROR
            and not self.gate_executed
            and self.gate_not_executed_reason
            is not GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
        ):
            raise ValueError(
                "pre-Gate Harness failure requires its fixed Gate reason"
            )
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        return self


class Phase6RecordingStartedEventV1_3(Phase6RecordingStartedEvent):
    """Recording 1.3 starts the Codex Evidence 1.6 binding."""

    schema_version: Literal["1.3"]  # type: ignore[assignment]


class Phase6RecordingTerminalEventV1_3(Phase6RecordingTerminalEvent):
    schema_version: Literal["1.3"]  # type: ignore[assignment]

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.6":
            raise ValueError(
                "Recording 1.3 requires unchanged CodexExecutionEvidence 1.6"
            )
        return value


@dataclass(frozen=True)
class Phase6Recording:
    started: Phase6RecordingStartedEvent | Phase6RecordingStartedEventV1_3
    terminal: Phase6RecordingTerminalEvent | Phase6RecordingTerminalEventV1_3


RecordingContract = ReplayRecording | Phase6Recording


class LiveRunArtifactV1_2(ContractModel):
    """Phase 6 Live artifact; nested Codex Evidence remains version 1.5."""

    schema_version: Literal["1.2"]
    run_id: StrictStr
    experiment_id: StrictStr
    task_id: StrictStr
    language: Language
    repetition_index: StrictInt = Field(ge=0)
    workflow: Workflow
    provider: Literal[Provider.CODEX]
    execution_mode: Literal[ExecutionMode.LIVE]
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    started_at: datetime
    completed_at: datetime
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    spec_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: Literal[True]
    runner: RunnerSettings
    codex: CodexExecutionEvidence
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    gate_commands: list[CommandEvidence]
    diff: DiffEvidence
    metrics: RunMetrics | None
    workspace_lifecycle: WorkspaceLifecycle
    recording_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    raw_provider_output_persisted: Literal[False]

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.5":
            raise ValueError(
                "LiveRunArtifact 1.2 requires unchanged "
                "CodexExecutionEvidence 1.5"
            )
        return value

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def artifact_state_is_coherent(self) -> LiveRunArtifactV1_2:
        if self.gate_not_executed_reason is GateNotExecutedReason.INPUT_CHANGED:
            raise ValueError("input_changed must not create a LiveRunArtifact")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if not (
            self.started_at
            <= self.codex.started_at
            <= self.codex.completed_at
            <= self.completed_at
        ):
            raise ValueError(
                "Codex timestamps must remain inside Artifact timestamps"
            )
        if (
            self.codex.stdin_bytes_total is not None
            and self.codex.stdin_bytes_total != self.prompt_bytes
        ):
            raise ValueError(
                "Codex stdin byte total must match Prompt byte count"
            )
        _validate_phase6_execution_observations(
            overall_status=self.overall_status,
            failure_kind=self.failure_kind,
            codex=self.codex,
            gate_executed=self.gate_executed,
            gate_not_executed_reason=self.gate_not_executed_reason,
            gate_commands=self.gate_commands,
            diff=self.diff,
            metrics=self.metrics,
            workspace_lifecycle=self.workspace_lifecycle,
            terminal_at=self.completed_at,
        )
        if self.gate_executed != bool(self.gate_commands):
            raise ValueError("gate_executed must match Gate Evidence presence")
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        if (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            and (
                self.overall_status is not Phase6OverallStatus.REJECTED
                or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
                or self.gate_executed
                or self.gate_not_executed_reason
                is not GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
                or self.metrics is not None
            )
        ):
            raise ValueError(
                "output_contract_violation requires rejected status, "
                "successful Codex, no Gate, and no Metrics"
            )
        if (
            self.overall_status is Phase6OverallStatus.REJECTED
        ) is not (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
        ):
            raise ValueError(
                "rejected status is reserved for output_contract_violation"
            )
        if self.overall_status in {
            Phase6OverallStatus.PASSED,
            Phase6OverallStatus.FAILED,
        } and (
            not self.gate_executed
            or self.metrics is None
            or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
        ):
            raise ValueError(
                "quality result requires successful Codex, Gate Evidence, and Metrics"
            )
        if (
            self.overall_status is Phase6OverallStatus.PASSED
            and self.failure_kind is not Phase6FailureKind.NONE
        ) or (
            self.overall_status is Phase6OverallStatus.FAILED
            and self.failure_kind is not Phase6FailureKind.QUALITY_GATE_FAILURE
        ):
            raise ValueError("quality status and failure kind differ")
        if self.overall_status in {
            Phase6OverallStatus.PROVIDER_ERROR,
            Phase6OverallStatus.HARNESS_ERROR,
        } and (
            self.failure_kind
            in {
                Phase6FailureKind.NONE,
                Phase6FailureKind.QUALITY_GATE_FAILURE,
                Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION,
            }
            or self.metrics is not None
        ):
            raise ValueError("non-quality failures must not contain Metrics")
        provider_failures = {
            Phase6FailureKind.PROVIDER_TURN_FAILED,
            Phase6FailureKind.PROVIDER_CLI_NONZERO,
            Phase6FailureKind.PROVIDER_SIGNAL_TERMINATION,
            Phase6FailureKind.PROVIDER_TIMEOUT,
            Phase6FailureKind.PROVIDER_UNAVAILABLE,
            Phase6FailureKind.PROVIDER_SPAWN_ERROR,
            Phase6FailureKind.PROVIDER_INPUT_ERROR,
            Phase6FailureKind.PROVIDER_PROTOCOL_ERROR,
            Phase6FailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        harness_failures = {
            Phase6FailureKind.PROCESS_CLEANUP_ERROR,
            Phase6FailureKind.GATE_HARNESS_ERROR,
            Phase6FailureKind.EVIDENCE_ERROR,
            Phase6FailureKind.UNSUPPORTED_PLATFORM,
        }
        if (
            self.overall_status is Phase6OverallStatus.PROVIDER_ERROR
        ) is not (self.failure_kind in provider_failures):
            raise ValueError(
                "provider_error status must match a Provider failure kind"
            )
        if (
            self.overall_status is Phase6OverallStatus.HARNESS_ERROR
        ) is not (self.failure_kind in harness_failures):
            raise ValueError(
                "harness_error status must match a Harness failure kind"
            )
        if (
            self.overall_status is Phase6OverallStatus.PROVIDER_ERROR
            and self.codex.failure_kind.value != self.failure_kind.value
        ):
            raise ValueError(
                "Artifact and Codex Provider failure kinds must match"
            )
        if self.overall_status is Phase6OverallStatus.PROVIDER_ERROR:
            expected_reason = (
                GateNotExecutedReason.PROVIDER_TIMEOUT
                if self.failure_kind is Phase6FailureKind.PROVIDER_TIMEOUT
                else GateNotExecutedReason.PROVIDER_FAILURE
            )
            if (
                self.codex.status is not ProviderExecutionStatus.FAILED
                or self.gate_executed
                or self.gate_not_executed_reason is not expected_reason
            ):
                raise ValueError(
                    "Provider failure requires failed Codex and its fixed Gate reason"
                )
        if (
            self.overall_status is Phase6OverallStatus.HARNESS_ERROR
            and not self.gate_executed
            and self.gate_not_executed_reason
            is not GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
        ):
            raise ValueError(
                "pre-Gate Harness failure requires its fixed Gate reason"
            )
        if self.metrics is not None and (
            self.metrics.agent_duration_ms != self.codex.duration_ms
            or self.metrics.total_duration_ms
            != self.metrics.agent_duration_ms
            + self.metrics.evaluation_duration_ms
            or self.metrics.agent_call_count != 1
            or self.metrics.retry_count != 0
            or self.metrics.changed_files != self.diff.changed_files
            or self.metrics.added_lines != self.diff.added_lines
            or self.metrics.deleted_lines != self.diff.deleted_lines
            or self.metrics.usage_metrics != self.codex.usage_metrics
        ):
            raise ValueError(
                "Artifact Metrics must match Codex and diff observations"
            )
        if self.metrics is not None:
            acceptance = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.ACCEPTANCE
            ]
            regression = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.REGRESSION
            ]
            lint = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.LINT
            ]
            typecheck = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.TYPECHECK
            ]
            expected_counts = (
                sum(
                    command.status is CommandStatus.PASSED
                    for command in acceptance
                ),
                len(acceptance),
                sum(
                    command.status is CommandStatus.FAILED
                    for command in regression
                ),
                sum(
                    command.status is CommandStatus.FAILED
                    for command in lint
                ),
                sum(
                    command.status is CommandStatus.FAILED
                    for command in typecheck
                ),
            )
            actual_counts = (
                self.metrics.acceptance_tests_passed,
                self.metrics.acceptance_tests_total,
                self.metrics.regression_failures,
                self.metrics.lint_errors,
                self.metrics.typecheck_errors,
            )
            expected_quality_pass = all(
                command.status is CommandStatus.PASSED
                for command in self.gate_commands
            )
            if (
                actual_counts != expected_counts
                or self.metrics.quality_gate_pass is not expected_quality_pass
            ):
                raise ValueError(
                    "Artifact Metrics must match Gate command observations"
                )
        if self.gate_commands:
            if not any(
                command.gate.value == "acceptance"
                for command in self.gate_commands
            ):
                raise ValueError("executed Gate requires acceptance commands")
            if any(
                command.started_at < self.codex.completed_at
                or command.completed_at > self.completed_at
                for command in self.gate_commands
            ):
                raise ValueError(
                    "Gate timestamps must follow Codex and remain in the Artifact"
                )
        return self


class LiveRunArtifactV1_3(LiveRunArtifactV1_2):
    """Phase 6 Live artifact bound to Codex Execution Evidence 1.6."""

    schema_version: Literal["1.3"]  # type: ignore[assignment]

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.6":
            raise ValueError(
                "LiveRunArtifact 1.3 requires unchanged "
                "CodexExecutionEvidence 1.6"
            )
        return value


Phase6LiveRunArtifact = LiveRunArtifactV1_2 | LiveRunArtifactV1_3
LiveRunArtifactContract = LiveRunArtifact | Phase6LiveRunArtifact


class HistoricalVerificationRecord(ContractModel):
    schema_version: Literal["1.0"]
    source_class: Literal[SourceClass.HISTORICAL]
    language: Language
    experiment_id: StrictStr
    source_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    verification_agentlab_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    toolchain_version_status: Literal["unknown"]
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    campaign_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    report_json_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    report_markdown_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    strict_schema_validation_passed: Literal[True]
    cross_artifact_validation_passed: Literal[True]
    artifact_regenerated: Literal[False]
    campaign_reexecuted: Literal[False]
    validation_commands: list[list[StrictStr]] = Field(min_length=1)
    verified_at: datetime

    @field_validator("verified_at", mode="before")
    @classmethod
    def verified_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "verified_at")


class ArtifactReference(ContractModel):
    role: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]*$")
    path: StrictStr
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "Artifact path")


class PrimarySuiteSource(ContractModel):
    source_class: Literal[SourceClass.PRIMARY]
    language: Language
    expected_language_status: LanguageStatus
    blocker: StrictStr | None = None
    spec: ArtifactReference | None = None
    fixture_manifest: ArtifactReference | None = None
    fixture_acceptance: ArtifactReference | None = None
    diff_policy: ArtifactReference | None = None
    plan: ArtifactReference | None = None
    campaign: ArtifactReference | None = None
    evidence: list[ArtifactReference] = Field(default_factory=list)
    recordings: list[ArtifactReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def expected_status_has_required_inputs(self) -> PrimarySuiteSource:
        fixed_roles = (
            (self.spec, "spec"),
            (self.fixture_manifest, "fixture_manifest"),
            (self.fixture_acceptance, "fixture_acceptance"),
            (self.diff_policy, "diff_policy"),
            (self.plan, "plan"),
            (self.campaign, "campaign"),
        )
        if any(
            reference is not None and reference.role != role
            for reference, role in fixed_roles
        ):
            raise ValueError("Primary Artifact reference role does not match its field")
        if any(reference.role != "evidence" for reference in self.evidence):
            raise ValueError("Primary Evidence references must use role=evidence")
        if any(reference.role != "recording" for reference in self.recordings):
            raise ValueError("Primary Recording references must use role=recording")
        core = (
            self.spec,
            self.fixture_manifest,
            self.fixture_acceptance,
            self.diff_policy,
            self.plan,
        )
        if self.expected_language_status in {
            LanguageStatus.READY_NOT_RUN,
            LanguageStatus.EVALUATED,
        } and any(item is None for item in core):
            raise ValueError("ready/evaluated language requires all Plan-bound inputs")
        if self.expected_language_status is LanguageStatus.READY_NOT_RUN and (
            self.campaign is not None or self.evidence or self.recordings
        ):
            raise ValueError("ready_not_run must not claim Live Artifacts")
        if self.expected_language_status is LanguageStatus.EVALUATED and (
            self.campaign is None or not self.evidence or not self.recordings
        ):
            raise ValueError("evaluated language requires Campaign, Evidence, and Recording")
        if self.expected_language_status is LanguageStatus.BLOCKED:
            if self.blocker is None:
                raise ValueError("blocked language requires a blocker")
        elif self.blocker is not None:
            raise ValueError("only blocked language may contain a blocker")
        return self


class HistoricalSuiteSource(ContractModel):
    source_class: Literal[SourceClass.HISTORICAL]
    language: Language
    verification_record: ArtifactReference
    plan: ArtifactReference
    campaign: ArtifactReference
    report_json: ArtifactReference
    report_markdown: ArtifactReference
    included_in_primary_denominator: Literal[False]

    @model_validator(mode="after")
    def reference_roles_match_fields(self) -> HistoricalSuiteSource:
        fixed_roles = (
            (self.verification_record, "historical_verification"),
            (self.plan, "plan"),
            (self.campaign, "campaign"),
            (self.report_json, "report_json"),
            (self.report_markdown, "report_markdown"),
        )
        if any(reference.role != role for reference, role in fixed_roles):
            raise ValueError("Historical Artifact reference role does not match its field")
        return self


class ProviderCoverage(ContractModel):
    provider: Provider
    evaluation_status: ProviderEvaluationStatus
    evaluated_languages: list[Language]
    blocker: StrictStr | None = None

    @model_validator(mode="after")
    def coverage_state_is_explicit(self) -> ProviderCoverage:
        if self.provider is Provider.ANTIGRAVITY:
            if (
                self.evaluation_status
                is not ProviderEvaluationStatus.NOT_EVALUATED
                or self.evaluated_languages
                or self.blocker != "upstream_artifact_signature_invalid"
            ):
                raise ValueError(
                    "Antigravity must remain not_evaluated with its fixed blocker"
                )
        elif self.provider is Provider.CODEX:
            if self.evaluation_status is ProviderEvaluationStatus.EVALUATED:
                if not self.evaluated_languages or self.blocker is not None:
                    raise ValueError(
                        "evaluated Codex coverage requires languages and no blocker"
                    )
            elif self.evaluated_languages:
                raise ValueError("not_evaluated coverage must not list languages")
        else:
            raise ValueError("Public Suite coverage supports Codex and Antigravity only")
        return self


class PublicSuiteManifest(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    renderer_version: StrictStr = Field(min_length=1)
    data_cutoff_at: datetime
    primary_sources: list[PrimarySuiteSource] = Field(min_length=1)
    historical_sources: list[HistoricalSuiteSource]
    provider_coverage: list[ProviderCoverage]
    antigravity_blocker: Literal["upstream_artifact_signature_invalid"]
    zero_call_run_publication: Literal["aggregate_only_no_run_record"]
    planned_outputs: list[StrictStr] = Field(min_length=1)
    automatic_winner_selected: Literal[False]
    leaderboard_generated: Literal[False]
    statistical_significance_claimed: Literal[False]

    @field_validator("data_cutoff_at", mode="before")
    @classmethod
    def data_cutoff_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "data_cutoff_at")

    @field_validator("planned_outputs")
    @classmethod
    def output_paths_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = [_relative_file(value, "planned output") for value in values]
        if normalized != sorted(set(normalized)):
            raise ValueError("planned outputs must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def sources_and_paths_are_unique(self) -> PublicSuiteManifest:
        primary_languages = [source.language for source in self.primary_sources]
        if primary_languages != sorted(set(primary_languages), key=lambda item: item.value):
            raise ValueError("primary languages must be unique and sorted")
        references = list(_manifest_references(self))
        paths = [reference.path for reference in references]
        if len(paths) != len(set(paths)):
            raise ValueError("input Artifact paths must be unique after normalization")
        if set(paths).intersection(self.planned_outputs):
            raise ValueError("planned outputs must not alias input Artifacts")
        if not {"checksums.json", "release-metadata.json"}.issubset(
            self.planned_outputs
        ):
            raise ValueError(
                "planned outputs require checksums.json and release-metadata.json"
            )
        providers = [coverage.provider for coverage in self.provider_coverage]
        if providers != [Provider.CODEX, Provider.ANTIGRAVITY]:
            raise ValueError("Provider coverage must contain Codex then Antigravity")
        return self


class PublicRunRecord(ContractModel):
    """One allowlisted attempted run; zero-call runs remain aggregate-only."""

    schema_version: Literal["1.0"]
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    experiment_id: StrictStr
    run_id: StrictStr
    task_id: StrictStr
    language: Language
    provider: Literal[Provider.CODEX]
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    exact_model_id: StrictStr
    reasoning_effort: ReasoningEffort
    cli_profile: StrictStr
    cli_version: StrictStr
    os: StrictStr
    architecture: StrictStr
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    campaign_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    evidence_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    recording_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    provider_call_count: Literal[1]
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    run_metrics_available: StrictBool
    acceptance_passed: StrictInt = Field(ge=0)
    acceptance_total: StrictInt = Field(ge=0)
    regression_failures: StrictInt = Field(ge=0)
    lint_errors: StrictInt = Field(ge=0)
    typecheck_errors: StrictInt = Field(ge=0)
    agent_duration_ms: StrictInt | None = Field(default=None, ge=0)
    evaluation_duration_ms: StrictInt | None = Field(default=None, ge=0)
    total_duration_ms: StrictInt | None = Field(default=None, ge=0)
    changed_file_count: StrictInt | None = Field(default=None, ge=0)
    added_lines: StrictInt | None = Field(default=None, ge=0)
    deleted_lines: StrictInt | None = Field(default=None, ge=0)
    usage_status: Literal["observed", "missing"]
    usage_source: Literal["provider_reported", "estimated"] | None
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    cached_input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    reasoning_output_tokens: StrictInt | None = Field(default=None, ge=0)
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def public_allowlist_values_are_coherent(self) -> PublicRunRecord:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.acceptance_passed > self.acceptance_total:
            raise ValueError(
                "acceptance_passed must not exceed acceptance_total"
            )
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        if self.gate_not_executed_reason is GateNotExecutedReason.INPUT_CHANGED:
            raise ValueError(
                "input_changed has no Provider call and belongs only in aggregate counts"
            )
        gate_values = (
            self.acceptance_passed,
            self.acceptance_total,
            self.regression_failures,
            self.lint_errors,
            self.typecheck_errors,
        )
        if not self.gate_executed and any(gate_values):
            raise ValueError("unexecuted Gate requires zero Gate result counts")

        metric_values = (
            self.agent_duration_ms,
            self.evaluation_duration_ms,
            self.total_duration_ms,
            self.changed_file_count,
            self.added_lines,
            self.deleted_lines,
        )
        if self.run_metrics_available is not all(
            value is not None for value in metric_values
        ):
            raise ValueError(
                "run_metrics_available must match complete Metrics fields"
            )
        if not self.run_metrics_available and any(
            value is not None for value in metric_values
        ):
            raise ValueError("missing RunMetrics requires null Metrics fields")
        if self.run_metrics_available:
            assert self.agent_duration_ms is not None
            assert self.evaluation_duration_ms is not None
            assert self.total_duration_ms is not None
            if (
                self.total_duration_ms
                != self.agent_duration_ms + self.evaluation_duration_ms
            ):
                raise ValueError("total duration must equal Agent plus evaluation")

        provider_failures = {
            Phase6FailureKind.PROVIDER_TURN_FAILED,
            Phase6FailureKind.PROVIDER_CLI_NONZERO,
            Phase6FailureKind.PROVIDER_SIGNAL_TERMINATION,
            Phase6FailureKind.PROVIDER_TIMEOUT,
            Phase6FailureKind.PROVIDER_UNAVAILABLE,
            Phase6FailureKind.PROVIDER_SPAWN_ERROR,
            Phase6FailureKind.PROVIDER_INPUT_ERROR,
            Phase6FailureKind.PROVIDER_PROTOCOL_ERROR,
            Phase6FailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        harness_failures = {
            Phase6FailureKind.PROCESS_CLEANUP_ERROR,
            Phase6FailureKind.GATE_HARNESS_ERROR,
            Phase6FailureKind.EVIDENCE_ERROR,
            Phase6FailureKind.UNSUPPORTED_PLATFORM,
        }
        if self.overall_status in {
            Phase6OverallStatus.PASSED,
            Phase6OverallStatus.FAILED,
        }:
            expected_failure = (
                Phase6FailureKind.NONE
                if self.overall_status is Phase6OverallStatus.PASSED
                else Phase6FailureKind.QUALITY_GATE_FAILURE
            )
            if (
                self.failure_kind is not expected_failure
                or not self.gate_executed
                or not self.run_metrics_available
                or self.acceptance_total == 0
            ):
                raise ValueError(
                    "quality status requires matching failure, Gate, and Metrics"
                )
            quality_passed = (
                self.acceptance_passed == self.acceptance_total
                and self.regression_failures == 0
                and self.lint_errors == 0
                and self.typecheck_errors == 0
            )
            if (
                self.overall_status is Phase6OverallStatus.PASSED
            ) is not quality_passed:
                raise ValueError(
                    "quality status must match the published Gate counts"
                )
        elif self.overall_status is Phase6OverallStatus.REJECTED:
            if (
                self.failure_kind
                is not Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
                or self.gate_executed
                or self.gate_not_executed_reason
                is not GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
                or self.run_metrics_available
            ):
                raise ValueError(
                    "rejected status requires output_contract_violation and no Gate"
                )
        elif self.overall_status is Phase6OverallStatus.PROVIDER_ERROR:
            expected_reason = (
                GateNotExecutedReason.PROVIDER_TIMEOUT
                if self.failure_kind is Phase6FailureKind.PROVIDER_TIMEOUT
                else GateNotExecutedReason.PROVIDER_FAILURE
            )
            if (
                self.failure_kind not in provider_failures
                or self.gate_executed
                or self.gate_not_executed_reason is not expected_reason
                or self.run_metrics_available
            ):
                raise ValueError(
                    "provider_error requires a Provider failure and no Gate"
                )
        elif (
            self.overall_status is not Phase6OverallStatus.HARNESS_ERROR
            or self.failure_kind not in harness_failures
            or self.run_metrics_available
            or (
                not self.gate_executed
                and self.gate_not_executed_reason
                is not GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
            )
        ):
            raise ValueError(
                "harness_error requires a Harness failure and no RunMetrics"
            )
        usage_values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
        )
        if self.usage_status == "missing":
            if self.usage_source is not None or any(
                value is not None for value in usage_values
            ):
                raise ValueError("missing Usage must not be converted to zero")
        elif self.usage_source is None:
            raise ValueError("observed Usage requires a source")
        return self


class PublicLanguageReport(ContractModel):
    """Backward-compatible Public Language Report 1.0."""

    schema_version: Literal["1.0"]
    language: Language
    status: LanguageStatus
    scheduled_runs: StrictInt = Field(ge=0)
    attempted_runs: StrictInt = Field(ge=0)
    completed_runs: StrictInt = Field(ge=0)
    failed_runs: StrictInt = Field(ge=0)
    interrupted_runs: StrictInt = Field(ge=0)
    not_run_runs: StrictInt = Field(ge=0)
    output_rejected_runs: StrictInt = Field(ge=0)
    gate_not_executed_runs: StrictInt = Field(ge=0)
    gate_not_executed_reason: dict[GateNotExecutedReason, StrictInt]
    scheduled_pair_count: StrictInt = Field(ge=0)
    complete_pair_count: StrictInt = Field(ge=0)
    estimability: Literal["estimable", "not_estimable"]

    @model_validator(mode="after")
    def report_counts_are_coherent(self) -> PublicLanguageReport:
        if (
            self.completed_runs
            + self.failed_runs
            + self.interrupted_runs
            + self.not_run_runs
            != self.scheduled_runs
            or self.attempted_runs
            != self.completed_runs + self.failed_runs + self.interrupted_runs
        ):
            raise ValueError("language run counts do not match the terminal taxonomy")
        if sum(self.gate_not_executed_reason.values()) != self.gate_not_executed_runs:
            raise ValueError("Gate non-execution reasons must match their total")
        if (
            self.gate_not_executed_reason.get(
                GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION,
                0,
            )
            != self.output_rejected_runs
        ):
            raise ValueError(
                "output_rejected_runs must match output_contract_violation reasons"
            )
        if self.complete_pair_count > self.scheduled_pair_count:
            raise ValueError("complete pairs must not exceed scheduled pairs")
        if (self.estimability == "estimable") is not (
            self.complete_pair_count > 0
        ):
            raise ValueError("estimability must match complete pair count")
        return self


class PublicLanguageReportV1_1(PublicLanguageReport):
    """Phase 6 aggregate with explicit zero/unknown Provider call counts."""

    schema_version: Literal["1.1"]  # type: ignore[assignment]
    zero_call_runs: StrictInt = Field(ge=0)
    provider_call_count_unknown_runs: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def provider_call_aggregates_are_coherent(
        self,
    ) -> PublicLanguageReportV1_1:
        if self.output_rejected_runs > self.failed_runs:
            raise ValueError("output_rejected_runs must not exceed failed_runs")
        if self.gate_not_executed_runs > self.scheduled_runs:
            raise ValueError(
                "gate_not_executed_runs must not exceed scheduled_runs"
            )
        if (
            self.zero_call_runs > self.gate_not_executed_runs
            or self.provider_call_count_unknown_runs > self.interrupted_runs
            or self.zero_call_runs
            + self.provider_call_count_unknown_runs
            > self.scheduled_runs
        ):
            raise ValueError(
                "zero and unknown Provider call aggregates are inconsistent"
            )
        return self


@dataclass(frozen=True)
class DerivedPublicLanguageCounts:
    scheduled_runs: int
    attempted_runs: int
    completed_runs: int
    failed_runs: int
    interrupted_runs: int
    not_run_runs: int
    output_rejected_runs: int
    gate_not_executed_runs: int
    zero_call_runs: int
    provider_call_count_unknown_runs: int
    gate_not_executed_reason: dict[GateNotExecutedReason, int]
    scheduled_pair_count: int
    complete_pair_count: int
    estimability: Literal["estimable", "not_estimable"]


def _gate_not_executed_reason_from_campaign(
    event: Phase6CampaignRunEvent,
) -> GateNotExecutedReason:
    if event.gate_executed:
        raise Phase6ContractError(
            "executed Campaign Gate has no non-execution reason"
        )
    if event.stop_reason is CampaignStopReason.INPUT_CHANGED:
        return GateNotExecutedReason.INPUT_CHANGED
    if event.status is CampaignRunStatus.INTERRUPTED:
        return GateNotExecutedReason.INTERRUPTED
    if (
        event.outcome
        is Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION
    ):
        return GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
    if (
        event.outcome is Phase6CampaignOutcome.PROVIDER_TIMEOUT
        or event.failure_kind is Phase6FailureKind.PROVIDER_TIMEOUT
    ):
        return GateNotExecutedReason.PROVIDER_TIMEOUT
    if event.outcome is Phase6CampaignOutcome.PROVIDER_FAILURE:
        return GateNotExecutedReason.PROVIDER_FAILURE
    if event.outcome in {
        Phase6CampaignOutcome.HARNESS_FAILURE,
        Phase6CampaignOutcome.CLEANUP_FAILURE,
    }:
        return GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
    if event.status is CampaignRunStatus.NOT_RUN:
        return GateNotExecutedReason.NOT_RUN
    raise Phase6ContractError(
        "Campaign terminal state has no fixed Gate non-execution reason"
    )


def derive_public_language_counts(
    campaign: LoadedPhase6Campaign,
) -> DerivedPublicLanguageCounts:
    """Derive every publishable language aggregate from Campaign 1.2."""
    terminal = [
        event
        for event in campaign.events
        if isinstance(event, Phase6CampaignRunEvent)
        and event.status is not CampaignRunStatus.STARTED
    ]
    gate_reasons: dict[GateNotExecutedReason, int] = {}
    for event in terminal:
        if not event.gate_executed:
            reason = _gate_not_executed_reason_from_campaign(event)
            gate_reasons[reason] = gate_reasons.get(reason, 0) + 1
    scheduled_pairs: dict[tuple[str, int], set[Workflow]] = {}
    completed_pairs: dict[tuple[str, int], set[Workflow]] = {}
    for event in terminal:
        pair = (event.task_id, event.repetition_index)
        scheduled_pairs.setdefault(pair, set()).add(event.workflow)
        if event.status is CampaignRunStatus.COMPLETED:
            completed_pairs.setdefault(pair, set()).add(event.workflow)
    pair_workflows = {Workflow.ONE_SHOT, Workflow.STAGED}
    scheduled_pair_count = sum(
        workflows == pair_workflows
        for workflows in scheduled_pairs.values()
    )
    complete_pair_count = sum(
        workflows == pair_workflows
        for workflows in completed_pairs.values()
    )
    return DerivedPublicLanguageCounts(
        scheduled_runs=len(terminal),
        attempted_runs=sum(
            event.status
            in {
                CampaignRunStatus.COMPLETED,
                CampaignRunStatus.FAILED,
                CampaignRunStatus.INTERRUPTED,
            }
            for event in terminal
        ),
        completed_runs=sum(
            event.status is CampaignRunStatus.COMPLETED for event in terminal
        ),
        failed_runs=sum(
            event.status is CampaignRunStatus.FAILED for event in terminal
        ),
        interrupted_runs=sum(
            event.status is CampaignRunStatus.INTERRUPTED for event in terminal
        ),
        not_run_runs=sum(
            event.status is CampaignRunStatus.NOT_RUN for event in terminal
        ),
        output_rejected_runs=sum(
            event.outcome
            is Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION
            for event in terminal
        ),
        gate_not_executed_runs=sum(
            not event.gate_executed for event in terminal
        ),
        zero_call_runs=sum(
            event.provider_call_count == 0 for event in terminal
        ),
        provider_call_count_unknown_runs=sum(
            event.provider_call_count is None for event in terminal
        ),
        gate_not_executed_reason=gate_reasons,
        scheduled_pair_count=scheduled_pair_count,
        complete_pair_count=complete_pair_count,
        estimability=(
            "estimable" if complete_pair_count > 0 else "not_estimable"
        ),
    )


def validate_public_language_report_campaign(
    report: PublicLanguageReportV1_1,
    campaign: LoadedPhase6Campaign,
    *,
    source: PrimarySuiteSource,
    plan: WorkflowPlanV1_2,
    evidence_run_ids: set[str],
) -> None:
    """Bind a public aggregate to its Primary source, Plan, and Campaign."""
    derived_status = validate_expected_language_status(
        source,
        campaign=campaign,
        evidence_run_ids=evidence_run_ids,
    )
    if (
        report.language is not source.language
        or report.language is not plan.language
    ):
        raise Phase6ContractError(
            "Public Language Report language differs from Primary source or Plan"
        )
    if report.status is not derived_status:
        raise Phase6ContractError(
            "Public Language Report status differs from derived language status"
        )
    derived = derive_public_language_counts(campaign)
    for field_name in (
        "scheduled_runs",
        "attempted_runs",
        "completed_runs",
        "failed_runs",
        "interrupted_runs",
        "not_run_runs",
        "output_rejected_runs",
        "gate_not_executed_runs",
        "zero_call_runs",
        "provider_call_count_unknown_runs",
        "gate_not_executed_reason",
        "scheduled_pair_count",
        "complete_pair_count",
        "estimability",
    ):
        if getattr(report, field_name) != getattr(derived, field_name):
            raise Phase6ContractError(
                f"Public Language Report {field_name} differs from Campaign"
            )


class PublicSuiteReport(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    renderer_version: StrictStr
    generated_at: datetime
    data_cutoff_at: datetime
    languages: list[PublicLanguageReport | PublicLanguageReportV1_1]
    provider_coverage: list[ProviderCoverage]
    automatic_winner_selected: Literal[False]
    leaderboard_generated: Literal[False]
    statistical_significance_claimed: Literal[False]

    @field_validator("generated_at", "data_cutoff_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def report_uses_cutoff_as_generation_time(self) -> PublicSuiteReport:
        if self.generated_at != self.data_cutoff_at:
            raise ValueError("generated_at must equal deterministic data_cutoff_at")
        languages = [item.language for item in self.languages]
        if languages != sorted(set(languages), key=lambda item: item.value):
            raise ValueError("language reports must be unique and sorted")
        return self


class ChecksumEntry(ContractModel):
    path: StrictStr
    size_bytes: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "checksum path")


class PublicChecksums(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    entries: list[ChecksumEntry] = Field(min_length=1)
    excluded_paths: list[Literal["checksums.json"]]
    authenticity_claimed: Literal[False]

    @model_validator(mode="after")
    def entries_are_sorted_and_exclude_only_self(self) -> PublicChecksums:
        if self.excluded_paths != ["checksums.json"]:
            raise ValueError("checksums.json must be the only excluded path")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(set(paths)):
            raise ValueError("checksum entries must be unique and sorted")
        if "checksums.json" in paths:
            raise ValueError("checksums.json must exclude only itself")
        if "release-metadata.json" not in paths:
            raise ValueError("release-metadata.json must be checksum-protected")
        return self


class ReleaseMetadata(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    renderer_version: StrictStr
    data_cutoff_at: datetime
    checksum_manifest_path: Literal["checksums.json"]
    checksum_digest_anchored_externally: Literal[True]
    authenticity_claimed: Literal[False]

    @field_validator("data_cutoff_at", mode="before")
    @classmethod
    def data_cutoff_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "data_cutoff_at")


class ExternalChecksumAnchor(ContractModel):
    """Bundle-external integrity anchor; it is not an authenticity claim."""

    schema_version: Literal["1.0"]
    suite_id: StrictStr
    checksum_manifest_path: Literal["checksums.json"]
    checksum_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    authenticity_claimed: Literal[False]


def canonical_json_bytes(value: ContractModel | dict[str, Any]) -> bytes:
    """Serialize one Phase 6 contract with byte-stable JSON settings."""
    raw = value.model_dump(mode="python") if isinstance(value, ContractModel) else value
    return (
        json.dumps(
            _canonical_json_value(raw),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, dict):
        return {
            key: _canonical_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def _canonical_jsonl_line(event: ContractModel) -> bytes:
    return (
        json.dumps(
            _canonical_json_value(event.model_dump(mode="python")),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _load_canonical_model[TContract: ContractModel](
    path: Path,
    model: type[TContract],
    label: str,
) -> TContract:
    snapshot = _read_stable_regular_file(path, label)
    return _load_canonical_model_bytes(snapshot.content, model, label)


def _load_canonical_model_bytes[TContract: ContractModel](
    content: bytes,
    model: type[TContract],
    label: str,
) -> TContract:
    try:
        raw = _strict_json_bytes(content, label)
    except Phase6ContractError:
        raise
    try:
        validated = model.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid {label}: {error}") from error
    if content != canonical_json_bytes(validated):
        raise Phase6ContractError(f"{label} must use canonical JSON serialization")
    return validated


def load_fixture_manifest(path: Path) -> FixtureManifest:
    return _load_canonical_model(path, FixtureManifest, "Fixture Manifest")


def load_diff_policy(path: Path) -> DiffPolicy:
    return _load_canonical_model(path, DiffPolicy, "Diff Policy")


def load_fixture_acceptance(path: Path) -> FixtureAcceptanceRecord:
    return _load_canonical_model(
        path,
        FixtureAcceptanceRecord,
        "Fixture Acceptance Record",
    )


def load_historical_verification(path: Path) -> HistoricalVerificationRecord:
    return _load_canonical_model(
        path,
        HistoricalVerificationRecord,
        "Historical Verification Record",
    )


def load_public_suite_manifest(path: Path) -> PublicSuiteManifest:
    return _load_canonical_model(
        path,
        PublicSuiteManifest,
        "Public Suite Manifest",
    )


def load_public_run_record(path: Path) -> PublicRunRecord:
    return _load_canonical_model(path, PublicRunRecord, "Public Run Record")


def load_public_language_report(
    path: Path,
) -> PublicLanguageReport | PublicLanguageReportV1_1:
    snapshot = _read_stable_regular_file(path, "Public Language Report")
    raw = _strict_json_bytes(snapshot.content, "Public Language Report")
    version = raw.get("schema_version")
    if version == "1.0":
        model: type[PublicLanguageReport] | type[PublicLanguageReportV1_1] = (
            PublicLanguageReport
        )
    elif version == "1.1":
        model = PublicLanguageReportV1_1
    else:
        raise Phase6ContractError(
            "unsupported Public Language Report schema_version"
        )
    return _load_canonical_model_bytes(
        snapshot.content,
        model,
        "Public Language Report",
    )


def load_public_suite_report(path: Path) -> PublicSuiteReport:
    return _load_canonical_model(path, PublicSuiteReport, "Public Suite Report")


def load_public_checksums(path: Path) -> PublicChecksums:
    return _load_canonical_model(path, PublicChecksums, "Public checksums")


def load_release_metadata(path: Path) -> ReleaseMetadata:
    return _load_canonical_model(path, ReleaseMetadata, "release metadata")


def load_external_checksum_anchor(path: Path) -> ExternalChecksumAnchor:
    return _load_canonical_model(
        path,
        ExternalChecksumAnchor,
        "external checksum anchor",
    )


def load_workflow_spec_contract(path: Path) -> LoadedWorkflowSpecContract:
    snapshot = _read_stable_regular_file(path, "Workflow Spec")
    return _load_workflow_spec_contract_bytes(snapshot.content)


def _load_workflow_spec_contract_bytes(
    source: bytes,
) -> LoadedWorkflowSpecContract:
    try:
        raw: Any = yaml.safe_load(source.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise WorkflowSpecError(f"could not read Workflow Spec YAML: {error}") from error
    if not isinstance(raw, dict):
        raise WorkflowSpecError("Workflow Spec must be a YAML mapping")
    selected: type[WorkflowExperimentSpec] | type[WorkflowExperimentSpecV2_1]
    if raw.get("schema_version") == "2.0":
        selected = WorkflowExperimentSpec
    elif raw.get("schema_version") == "2.1":
        selected = WorkflowExperimentSpecV2_1
    else:
        raise WorkflowSpecError("unsupported Workflow Spec schema_version")
    try:
        spec = selected.model_validate(raw)
    except ValidationError as error:
        raise WorkflowSpecError(str(error)) from error
    return LoadedWorkflowSpecContract(
        spec=spec,
        sha256=hashlib.sha256(source).hexdigest(),
    )


def load_workflow_plan_contract(path: Path) -> WorkflowPlanContract:
    snapshot = _read_stable_regular_file(path, "Workflow Plan")
    raw = _strict_json_bytes(snapshot.content, "Workflow Plan")
    if raw.get("schema_version") == "1.1":
        return load_workflow_plan(path)
    return _load_workflow_plan_1_2_bytes(snapshot.content)


def _load_workflow_plan_1_2_bytes(content: bytes) -> WorkflowPlanV1_2:
    try:
        raw = _strict_json_bytes(content, "Workflow Plan")
    except Phase6ContractError:
        raise
    if raw.get("schema_version") != "1.2":
        raise Phase6ContractError("unsupported Workflow Plan schema_version")
    try:
        plan = WorkflowPlanV1_2.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid Workflow Plan: {error}") from error
    if content != canonical_json_bytes(plan):
        raise Phase6ContractError("Workflow Plan must use canonical JSON serialization")
    return plan


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def _strict_json_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError as error:
        raise Phase6ContractError(
            f"{label} contains duplicate key {error}"
        ) from error
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Phase6ContractError(f"could not read strict {label} JSON") from error
    if not isinstance(raw, dict):
        raise Phase6ContractError(f"{label} must be a JSON object")
    return raw


def _load_jsonl_objects_bytes(
    content: bytes,
    label: str,
) -> list[dict[str, Any]]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise Phase6ContractError(f"could not read {label}") from error
    if not lines or any(not line.strip() for line in lines):
        raise Phase6ContractError(f"{label} must be non-empty JSONL without blank lines")
    objects: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_finite,
            )
        except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as error:
            raise Phase6ContractError(
                f"invalid {label} JSON at line {index}"
            ) from error
        if not isinstance(raw, dict):
            raise Phase6ContractError(f"{label} line {index} must be an object")
        objects.append(raw)
    return objects


def load_campaign_contract(path: Path) -> CampaignContract:
    snapshot = _read_stable_regular_file(path, "Campaign")
    raw_events = _load_jsonl_objects_bytes(snapshot.content, "Campaign")
    version = raw_events[0].get("schema_version")
    if version == "1.1":
        return load_campaign(path)
    return _load_phase6_campaign_bytes(snapshot.content, raw_events)


def _load_phase6_campaign_bytes(
    content: bytes,
    raw_events: list[dict[str, Any]] | None = None,
) -> LoadedPhase6Campaign:
    if raw_events is None:
        raw_events = _load_jsonl_objects_bytes(content, "Campaign")
    version = raw_events[0].get("schema_version")
    if version != "1.2" or any(
        event.get("schema_version") != "1.2" for event in raw_events
    ):
        raise Phase6ContractError("Campaign must use one supported schema version")
    events: list[Phase6CampaignEvent] = []
    for raw in raw_events:
        try:
            events.append(_PHASE6_CAMPAIGN_ADAPTER.validate_python(raw))
        except ValidationError as error:
            raise Phase6ContractError(f"invalid Campaign 1.2 event: {error}") from error
    _validate_phase6_campaign(events)
    canonical = b"".join(_canonical_jsonl_line(event) for event in events)
    if content != canonical:
        raise Phase6ContractError("Campaign 1.2 must use canonical JSONL serialization")
    return LoadedPhase6Campaign(tuple(events))


def _validate_phase6_campaign(events: list[Phase6CampaignEvent]) -> None:
    if (
        not events
        or not isinstance(events[0], Phase6CampaignStartedEvent)
        or not isinstance(events[-1], Phase6CampaignFinishedEvent)
    ):
        raise Phase6ContractError("Campaign 1.2 requires started and finished boundaries")
    if [event.sequence for event in events] != list(range(len(events))):
        raise Phase6ContractError("Campaign 1.2 sequence must be contiguous")
    if [event.occurred_at for event in events] != sorted(
        event.occurred_at for event in events
    ):
        raise Phase6ContractError(
            "Campaign 1.2 timestamps must be non-decreasing"
        )
    started = events[0]
    finished = events[-1]
    assert isinstance(started, Phase6CampaignStartedEvent)
    assert isinstance(finished, Phase6CampaignFinishedEvent)
    if started.experiment_id != finished.experiment_id:
        raise Phase6ContractError("Campaign experiment ID must remain stable")
    runs = [event for event in events if isinstance(event, Phase6CampaignRunEvent)]
    started_ids = {
        event.run_id for event in runs if event.status is CampaignRunStatus.STARTED
    }
    started_events = [
        event for event in runs if event.status is CampaignRunStatus.STARTED
    ]
    started_by_id = {event.run_id: event for event in started_events}
    sequence_by_started_id = {
        event.run_id: event.sequence for event in started_events
    }
    terminal = [
        event for event in runs if event.status is not CampaignRunStatus.STARTED
    ]
    if (
        len(terminal) != started.planned_run_count
        or len(started_events) != len(started_ids)
        or len(started_ids) != finished.attempted_run_count
        or sum(event.provider_call_count or 0 for event in terminal)
        != finished.provider_call_count
        or sum(event.provider_call_count is None for event in terminal)
        != finished.provider_call_count_unknown_runs
        or sum(event.counted_failure for event in terminal)
        != finished.counted_failure_count
    ):
        raise Phase6ContractError("Campaign 1.2 header and terminal counts differ")
    terminal_ids = [event.run_id for event in terminal]
    if len(terminal_ids) != len(set(terminal_ids)):
        raise Phase6ContractError("Campaign 1.2 run may have only one terminal state")
    if any(
        event.status is not CampaignRunStatus.NOT_RUN
        and event.run_id not in started_ids
        for event in terminal
    ):
        raise Phase6ContractError("attempted terminal state requires a started event")
    for event in terminal:
        if event.status is CampaignRunStatus.NOT_RUN:
            if event.run_id in started_ids:
                raise Phase6ContractError(
                    "not_run state must not have a started event"
                )
            continue
        started_event = started_by_id[event.run_id]
        if sequence_by_started_id[event.run_id] >= event.sequence:
            raise Phase6ContractError(
                "run started event must precede its terminal event"
            )
        if (
            started_event.task_id != event.task_id
            or started_event.workflow is not event.workflow
            or started_event.repetition_index != event.repetition_index
        ):
            raise Phase6ContractError(
                "run started and terminal identities differ"
            )

    terminal_stop_reasons = {
        event.stop_reason
        for event in terminal
        if event.status
        in {CampaignRunStatus.NOT_RUN, CampaignRunStatus.INTERRUPTED}
    }
    if (
        terminal_stop_reasons
        and terminal_stop_reasons != {finished.stop_reason}
    ):
        raise Phase6ContractError(
            "Campaign finished stop reason differs from terminal stop reason"
        )
    if (
        finished.stop_reason is CampaignStopReason.INPUT_CHANGED
        and CampaignStopReason.INPUT_CHANGED not in terminal_stop_reasons
    ):
        raise Phase6ContractError(
            "input_changed Campaign finish requires an input_changed not_run state"
        )


def load_recording_contract(path: Path) -> RecordingContract:
    snapshot = _read_stable_regular_file(path, "Recording")
    raw_events = _load_jsonl_objects_bytes(snapshot.content, "Recording")
    version = raw_events[0].get("schema_version")
    if version in {"1.0", "1.1"}:
        return load_replay_recording(path)
    return _load_phase6_recording_bytes(snapshot.content, raw_events)


def _load_phase6_recording_bytes(
    content: bytes,
    raw_events: list[dict[str, Any]] | None = None,
) -> Phase6Recording:
    if raw_events is None:
        raw_events = _load_jsonl_objects_bytes(content, "Recording")
    version = raw_events[0].get("schema_version")
    if version not in {"1.2", "1.3"} or len(raw_events) != 2 or any(
        event.get("schema_version") != version for event in raw_events
    ):
        raise Phase6ContractError("Recording must use one supported schema version")
    started_model: (
        type[Phase6RecordingStartedEvent]
        | type[Phase6RecordingStartedEventV1_3]
    )
    terminal_model: (
        type[Phase6RecordingTerminalEvent]
        | type[Phase6RecordingTerminalEventV1_3]
    )
    if version == "1.2":
        started_model = Phase6RecordingStartedEvent
        terminal_model = Phase6RecordingTerminalEvent
    else:
        started_model = Phase6RecordingStartedEventV1_3
        terminal_model = Phase6RecordingTerminalEventV1_3
    try:
        started = started_model.model_validate(raw_events[0])
        terminal = terminal_model.model_validate(raw_events[1])
    except ValidationError as error:
        raise Phase6ContractError(f"invalid Recording {version}: {error}") from error
    if (
        started.run_id != terminal.run_id
        or started.experiment_id != terminal.experiment_id
        or not (
            started.occurred_at
            <= terminal.codex.started_at
            <= terminal.codex.completed_at
            <= terminal.occurred_at
        )
        or started.requested_model != terminal.codex.requested_model
        or started.requested_reasoning_effort
        is not terminal.codex.requested_reasoning_effort
        or started.cli_version != terminal.codex.cli_version
    ):
        raise Phase6ContractError(f"Recording {version} event identities differ")
    canonical = b"".join(
        _canonical_jsonl_line(event) for event in (started, terminal)
    )
    if content != canonical:
        raise Phase6ContractError(
            f"Recording {version} must use canonical JSONL serialization"
        )
    return Phase6Recording(started, terminal)


def load_live_run_artifact_contract(path: Path) -> LiveRunArtifactContract:
    snapshot = _read_stable_regular_file(path, "LiveRunArtifact")
    raw = _strict_json_bytes(snapshot.content, "LiveRunArtifact")
    version = raw.get("schema_version")
    if version in {"1.0", "1.1"}:
        try:
            return LiveRunArtifact.model_validate(raw)
        except ValidationError as error:
            raise Phase6ContractError(
                f"invalid LiveRunArtifact: {error}"
            ) from error
    return _load_phase6_live_run_artifact_bytes(snapshot.content, raw)


def _load_phase6_live_run_artifact_bytes(
    content: bytes,
    raw: dict[str, Any] | None = None,
) -> Phase6LiveRunArtifact:
    if raw is None:
        raw = _strict_json_bytes(content, "LiveRunArtifact")
    version = raw.get("schema_version")
    if version == "1.2":
        return _load_live_run_artifact_1_2_bytes(content, raw)
    if version == "1.3":
        return _load_live_run_artifact_1_3_bytes(content, raw)
    raise Phase6ContractError("unsupported LiveRunArtifact schema_version")


def _load_live_run_artifact_1_2_bytes(
    content: bytes,
    raw: dict[str, Any] | None = None,
) -> LiveRunArtifactV1_2:
    if raw is None:
        raw = _strict_json_bytes(content, "LiveRunArtifact")
    if raw.get("schema_version") != "1.2":
        raise Phase6ContractError("unsupported LiveRunArtifact schema_version")
    try:
        artifact = LiveRunArtifactV1_2.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid LiveRunArtifact: {error}") from error
    if content != canonical_json_bytes(artifact):
        raise Phase6ContractError(
            "LiveRunArtifact 1.2 must use canonical JSON serialization"
        )
    return artifact


def _load_live_run_artifact_1_3_bytes(
    content: bytes,
    raw: dict[str, Any] | None = None,
) -> LiveRunArtifactV1_3:
    if raw is None:
        raw = _strict_json_bytes(content, "LiveRunArtifact")
    if raw.get("schema_version") != "1.3":
        raise Phase6ContractError("unsupported LiveRunArtifact schema_version")
    try:
        artifact = LiveRunArtifactV1_3.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid LiveRunArtifact: {error}") from error
    if content != canonical_json_bytes(artifact):
        raise Phase6ContractError(
            "LiveRunArtifact 1.3 must use canonical JSON serialization"
        )
    return artifact


def validate_plan_bindings(
    *,
    loaded_spec: LoadedWorkflowSpecContract,
    plan: WorkflowPlanV1_2,
    fixture_manifest_bytes: bytes,
    fixture_manifest: FixtureManifest,
    fixture_acceptance_bytes: bytes,
    fixture_acceptance: FixtureAcceptanceRecord,
    diff_policy_bytes: bytes,
    diff_policy: DiffPolicy,
) -> None:
    """Cross-check every Slice 6A input bound by Workflow Plan 1.2."""
    expected = [
        plan.experiment_spec_sha256 == loaded_spec.sha256,
        plan.fixture_manifest_sha256
        == hashlib.sha256(fixture_manifest_bytes).hexdigest(),
        plan.fixture_acceptance_sha256
        == hashlib.sha256(fixture_acceptance_bytes).hexdigest(),
        plan.diff_policy_sha256 == hashlib.sha256(diff_policy_bytes).hexdigest(),
        plan.fixture_sha256 == fixture_manifest.fixture_sha256,
        plan.fixture_sha256 == fixture_acceptance.fixture_sha256,
        plan.gate_contract_sha256 == fixture_manifest.gate_contract_sha256,
        plan.gate_contract_sha256 == fixture_acceptance.gate_contract_sha256,
        plan.reference_solution_sha256
        == fixture_acceptance.reference_solution_sha256,
        plan.toolchain_fingerprint == fixture_manifest.toolchain.fingerprint,
        plan.toolchain_fingerprint == fixture_acceptance.toolchain.fingerprint,
        plan.reviewed_commit == fixture_acceptance.acceptance_agentlab_commit,
        plan.language is fixture_manifest.language,
        plan.language is fixture_acceptance.language,
        plan.language is diff_policy.language,
        plan.fixture_revision == fixture_manifest.fixture_revision,
        plan.fixture_revision == fixture_acceptance.fixture_revision,
        plan.fixture_revision == diff_policy.fixture_revision,
    ]
    if not isinstance(loaded_spec.spec, WorkflowExperimentSpecV2_1):
        raise Phase6ContractError("Workflow Plan 1.2 requires Workflow Spec 2.1")
    expected.extend(
        [
        plan.reviewed_commit == loaded_spec.spec.reviewed_commit,
        plan.language is loaded_spec.spec.language,
        fixture_acceptance.fixture_manifest_sha256
        == hashlib.sha256(fixture_manifest_bytes).hexdigest(),
        fixture_acceptance.diff_policy_sha256
        == hashlib.sha256(diff_policy_bytes).hexdigest(),
        ]
    )
    if not all(expected):
        raise Phase6ContractError("Workflow Plan 1.2 bindings are inconsistent")


def _manifest_references(
    manifest: PublicSuiteManifest,
) -> tuple[ArtifactReference, ...]:
    references: list[ArtifactReference] = []
    for primary in manifest.primary_sources:
        references.extend(
            item
            for item in (
                primary.spec,
                primary.fixture_manifest,
                primary.fixture_acceptance,
                primary.diff_policy,
                primary.plan,
                primary.campaign,
            )
            if item is not None
        )
        references.extend(primary.evidence)
        references.extend(primary.recordings)
    for historical in manifest.historical_sources:
        references.extend(
            (
                historical.verification_record,
                historical.plan,
                historical.campaign,
                historical.report_json,
                historical.report_markdown,
            )
        )
    return tuple(references)


def _safe_root(root: Path) -> Path:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise Phase6PathError("could not resolve fixed Manifest root") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase6PathError("Manifest root must be a real directory")
    return resolved


@dataclass(frozen=True)
class LoadedPublicSuiteInputs:
    manifest: PublicSuiteManifest
    root: Path
    directory_snapshots: tuple[DirectorySnapshot, ...]
    manifest_snapshot: FileSnapshot
    paths: dict[str, Path]
    bytes_by_path: dict[str, bytes]
    snapshots_by_path: dict[str, FileSnapshot]


@dataclass(frozen=True)
class ValidatedPublicSuiteInputs:
    loaded: LoadedPublicSuiteInputs
    derived_language_status: dict[Language, LanguageStatus]
    data_cutoff_at: datetime


@dataclass(frozen=True)
class Phase6PrimarySnapshotBinding:
    """Canonical primary binding facts derived from one byte snapshot."""

    language: Language
    campaign_path: str
    campaign_id: str
    planned_run_ids: frozenset[str]
    complete_pairs: frozenset[tuple[str, int]]
    planned_provider_call_count: int
    provider_call_count: int
    provider_call_count_unknown_runs: int


def load_public_suite_inputs(
    manifest_path: Path,
    *,
    root: Path,
) -> LoadedPublicSuiteInputs:
    """Load exactly the Manifest and its explicitly listed input files."""
    resolved_root = _safe_root(root)
    root_snapshot = _snapshot_directory(
        resolved_root,
        "Public Suite root",
    )
    try:
        lexical_manifest = Path(os.path.abspath(manifest_path))
        relative_manifest = lexical_manifest.relative_to(resolved_root).as_posix()
    except (OSError, ValueError) as error:
        raise Phase6PathError("Public Suite Manifest must remain below root") from error
    manifest_snapshot, manifest_directories = _read_file_below_root(
        root_snapshot=root_snapshot,
        relative=relative_manifest,
        label="Public Suite Manifest",
    )
    manifest = _load_canonical_model_bytes(
        manifest_snapshot.content,
        PublicSuiteManifest,
        "Public Suite Manifest",
    )
    paths: dict[str, Path] = {}
    bytes_by_path: dict[str, bytes] = {}
    snapshots_by_path: dict[str, FileSnapshot] = {}
    identities = {manifest_snapshot.identity}
    directory_snapshot_by_path = {
        snapshot.path: snapshot for snapshot in manifest_directories
    }
    for reference in _manifest_references(manifest):
        snapshot, parent_directories = _read_file_below_root(
            root_snapshot=root_snapshot,
            relative=reference.path,
            label=f"listed Artifact {reference.path}",
        )
        for directory_snapshot in parent_directories:
            previous = directory_snapshot_by_path.setdefault(
                directory_snapshot.path,
                directory_snapshot,
            )
            if previous.identity != directory_snapshot.identity:
                raise Phase6PathError(
                    "listed Artifact parent directory changed during load"
                )
        if snapshot.identity in identities:
            raise Phase6PathError(
                "Manifest and listed Artifacts must have distinct file identities"
            )
        identities.add(snapshot.identity)
        if snapshot.sha256 != reference.sha256:
            raise Phase6ContractError(
                f"listed Artifact hash differs: {reference.path}"
            )
        paths[reference.path] = snapshot.path
        bytes_by_path[reference.path] = snapshot.content
        snapshots_by_path[reference.path] = snapshot
    directory_snapshots = tuple(
        directory_snapshot_by_path[path]
        for path in sorted(
            directory_snapshot_by_path,
            key=lambda item: (len(item.parts), item.as_posix()),
        )
    )
    for index, directory_snapshot in enumerate(directory_snapshots):
        _require_directory_snapshot_unchanged(
            directory_snapshot,
            f"Public Suite directory component {index}",
        )
    return LoadedPublicSuiteInputs(
        manifest=manifest,
        root=resolved_root,
        directory_snapshots=directory_snapshots,
        manifest_snapshot=manifest_snapshot,
        paths=paths,
        bytes_by_path=bytes_by_path,
        snapshots_by_path=snapshots_by_path,
    )


def derive_language_status(
    source: PrimarySuiteSource,
    *,
    campaign: CampaignContract | None,
    evidence_run_ids: set[str],
) -> LanguageStatus:
    """Derive status without depending on Public Run Records."""
    if source.blocker is not None:
        return LanguageStatus.BLOCKED
    core = (
        source.spec,
        source.fixture_manifest,
        source.fixture_acceptance,
        source.diff_policy,
        source.plan,
    )
    if any(item is None for item in core):
        return LanguageStatus.NOT_READY
    if campaign is None:
        return LanguageStatus.READY_NOT_RUN
    if isinstance(campaign, list):
        terminal_observations = [
            (
                event.status,
                event.run_id,
                event.task_id,
                event.repetition_index,
                event.workflow,
            )
            for event in campaign
            if isinstance(event, CampaignRunEvent)
            and event.status is not CampaignRunStatus.STARTED
        ]
    else:
        terminal_observations = [
            (
                event.status,
                event.run_id,
                event.task_id,
                event.repetition_index,
                event.workflow,
            )
            for event in campaign.events
            if isinstance(event, Phase6CampaignRunEvent)
            and event.status is not CampaignRunStatus.STARTED
        ]
    blocks: dict[tuple[str, int], set[Workflow]] = {}
    for status, run_id, task_id, repetition_index, workflow in terminal_observations:
        if (
            status is CampaignRunStatus.COMPLETED
            and run_id in evidence_run_ids
        ):
            blocks.setdefault(
                (task_id, repetition_index),
                set(),
            ).add(workflow)
    if any(
        workflows == {Workflow.ONE_SHOT, Workflow.STAGED}
        for workflows in blocks.values()
    ):
        return LanguageStatus.EVALUATED
    return LanguageStatus.BLOCKED


def validate_expected_language_status(
    source: PrimarySuiteSource,
    *,
    campaign: CampaignContract | None,
    evidence_run_ids: set[str],
) -> LanguageStatus:
    derived = derive_language_status(
        source,
        campaign=campaign,
        evidence_run_ids=evidence_run_ids,
    )
    if source.expected_language_status is not derived:
        raise Phase6ContractError(
            f"expected language status {source.expected_language_status.value} "
            f"differs from derived status {derived.value}"
        )
    return derived


def validate_data_cutoff(
    manifest: PublicSuiteManifest,
    *,
    fixture_acceptances: list[FixtureAcceptanceRecord],
    campaigns: list[CampaignContract],
    historical_verifications: list[HistoricalVerificationRecord],
    live_artifacts: Sequence[Phase6LiveRunArtifact] = (),
    recordings: Sequence[Phase6Recording] = (),
) -> datetime:
    """Require data_cutoff_at to equal the maximum persisted terminal time."""
    timestamps = [record.verified_at for record in fixture_acceptances]
    timestamps.extend(record.verified_at for record in historical_verifications)
    timestamps.extend(artifact.completed_at for artifact in live_artifacts)
    timestamps.extend(recording.terminal.occurred_at for recording in recordings)
    for campaign in campaigns:
        if isinstance(campaign, LoadedPhase6Campaign):
            timestamps.append(campaign.finished.occurred_at)
        else:
            finished = campaign[-1]
            if not isinstance(finished, CampaignFinishedEvent):
                raise Phase6ContractError("Campaign 1.1 lacks a finished event")
            timestamps.append(finished.occurred_at)
    if not timestamps:
        raise Phase6ContractError(
            "data_cutoff_at requires a terminal or verification timestamp"
        )
    recomputed = max(timestamps)
    if (
        manifest.data_cutoff_at != recomputed
        or _canonical_timestamp(recomputed)
        != _canonical_timestamp(manifest.data_cutoff_at)
    ):
        raise Phase6ContractError(
            "Manifest data_cutoff_at differs from canonical input maximum"
        )
    return recomputed


def validate_checksum_contract(
    *,
    manifest: PublicSuiteManifest,
    checksums: PublicChecksums,
    release_metadata: ReleaseMetadata,
    external_anchor: ExternalChecksumAnchor | None,
    checksum_bytes: bytes | None = None,
) -> None:
    """Validate the one-way checksum trust model without claiming authenticity."""
    if (
        checksums.suite_id != manifest.suite_id
        or release_metadata.suite_id != manifest.suite_id
        or release_metadata.data_cutoff_at != manifest.data_cutoff_at
        or release_metadata.renderer_version != manifest.renderer_version
    ):
        raise Phase6ContractError("release contracts do not match the Suite Manifest")
    expected = set(manifest.planned_outputs) - {"checksums.json"}
    actual = {entry.path for entry in checksums.entries}
    if expected != actual or "release-metadata.json" not in actual:
        raise Phase6ContractError(
            "checksums must cover every planned output except checksums.json itself"
        )
    if external_anchor is None:
        if checksum_bytes is not None:
            raise Phase6ContractError(
                "checksum bytes require a bundle-external digest anchor"
            )
        return
    if (
        checksum_bytes is None
        or external_anchor.suite_id != manifest.suite_id
        or external_anchor.checksum_manifest_sha256
        != hashlib.sha256(checksum_bytes).hexdigest()
    ):
        raise Phase6ContractError("external checksum anchor does not match checksums.json")


def validate_public_suite_inputs(
    loaded: LoadedPublicSuiteInputs,
) -> ValidatedPublicSuiteInputs:
    """Strict-load and cross-check every explicitly listed Suite input."""
    _require_loaded_inputs_unchanged(loaded)
    statuses: dict[Language, LanguageStatus] = {}
    acceptances: list[FixtureAcceptanceRecord] = []
    campaigns: list[CampaignContract] = []
    historical_records: list[HistoricalVerificationRecord] = []
    live_artifacts: list[Phase6LiveRunArtifact] = []
    live_recordings: list[Phase6Recording] = []

    for source in loaded.manifest.primary_sources:
        spec = (
            _load_workflow_spec_contract_bytes(
                loaded.bytes_by_path[source.spec.path]
            )
            if source.spec is not None
            else None
        )
        fixture_manifest = (
            _load_canonical_model_bytes(
                loaded.bytes_by_path[source.fixture_manifest.path],
                FixtureManifest,
                "Fixture Manifest",
            )
            if source.fixture_manifest is not None
            else None
        )
        acceptance = (
            _load_canonical_model_bytes(
                loaded.bytes_by_path[source.fixture_acceptance.path],
                FixtureAcceptanceRecord,
                "Fixture Acceptance Record",
            )
            if source.fixture_acceptance is not None
            else None
        )
        policy = (
            _load_canonical_model_bytes(
                loaded.bytes_by_path[source.diff_policy.path],
                DiffPolicy,
                "Diff Policy",
            )
            if source.diff_policy is not None
            else None
        )
        plan = (
            _load_workflow_plan_1_2_bytes(
                loaded.bytes_by_path[source.plan.path]
            )
            if source.plan is not None
            else None
        )
        campaign = (
            _load_phase6_campaign_bytes(
                loaded.bytes_by_path[source.campaign.path]
            )
            if source.campaign is not None
            else None
        )
        if campaign is not None:
            campaigns.append(campaign)
        if acceptance is not None:
            acceptances.append(acceptance)

        core = (spec, fixture_manifest, acceptance, policy, plan)
        if all(item is not None for item in core):
            if (
                not isinstance(spec, LoadedWorkflowSpecContract)
                or not isinstance(spec.spec, WorkflowExperimentSpecV2_1)
                or not isinstance(fixture_manifest, FixtureManifest)
                or not isinstance(acceptance, FixtureAcceptanceRecord)
                or not isinstance(policy, DiffPolicy)
                or not isinstance(plan, WorkflowPlanV1_2)
            ):
                raise Phase6ContractError(
                    "Primary Phase 6 source requires Spec 2.1 and Plan 1.2 contracts"
                )
            assert source.fixture_manifest is not None
            assert source.fixture_acceptance is not None
            assert source.diff_policy is not None
            assert source.spec is not None
            _validate_spec_path_bindings(
                loaded=loaded,
                source=source,
                spec=spec.spec,
            )
            validate_plan_bindings(
                loaded_spec=spec,
                plan=plan,
                fixture_manifest_bytes=loaded.bytes_by_path[
                    source.fixture_manifest.path
                ],
                fixture_manifest=fixture_manifest,
                fixture_acceptance_bytes=loaded.bytes_by_path[
                    source.fixture_acceptance.path
                ],
                fixture_acceptance=acceptance,
                diff_policy_bytes=loaded.bytes_by_path[source.diff_policy.path],
                diff_policy=policy,
            )
        elif any(item is not None for item in core) and (
            campaign is not None or source.evidence or source.recordings
        ):
            raise Phase6ContractError(
                "Live inputs must not exist before every Plan-bound input is present"
            )

        evidence: list[Phase6LiveRunArtifact] = []
        for reference in source.evidence:
            artifact = _load_phase6_live_run_artifact_bytes(
                loaded.bytes_by_path[reference.path]
            )
            evidence.append(artifact)
            live_artifacts.append(artifact)
        recordings: list[Phase6Recording] = []
        for reference in source.recordings:
            recording = _load_phase6_recording_bytes(
                loaded.bytes_by_path[reference.path]
            )
            recordings.append(recording)
            live_recordings.append(recording)
        _validate_primary_live_bindings(
            source=source,
            spec=spec.spec if spec is not None else None,
            plan=plan,
            campaign=campaign,
            evidence=evidence,
            recordings=recordings,
        )
        status = validate_expected_language_status(
            source,
            campaign=campaign,
            evidence_run_ids={artifact.run_id for artifact in evidence},
        )
        statuses[source.language] = status

    for historical_source in loaded.manifest.historical_sources:
        record = _load_canonical_model_bytes(
            loaded.bytes_by_path[historical_source.verification_record.path],
            HistoricalVerificationRecord,
            "Historical Verification Record",
        )
        plan = _load_workflow_plan_1_2_bytes(
            loaded.bytes_by_path[historical_source.plan.path]
        )
        campaign = _load_phase6_campaign_bytes(
            loaded.bytes_by_path[historical_source.campaign.path]
        )
        report_raw = _strict_json_bytes(
            loaded.bytes_by_path[historical_source.report_json.path],
            "Historical Public Language Report",
        )
        report_model_type: type[PublicLanguageReport] | type[PublicLanguageReportV1_1]
        if report_raw.get("schema_version") == "1.0":
            report_model_type = PublicLanguageReport
        elif report_raw.get("schema_version") == "1.1":
            report_model_type = PublicLanguageReportV1_1
        else:
            raise Phase6ContractError("Historical Public Language Report schema is unsupported")
        report = _load_canonical_model_bytes(
            loaded.bytes_by_path[historical_source.report_json.path],
            report_model_type,
            "Historical Public Language Report",
        )
        if (
            record.language is not historical_source.language
            or plan.language is not historical_source.language
            or campaign.started.experiment_id != plan.experiment_id
            or report.language is not historical_source.language
            or record.plan_sha256 != historical_source.plan.sha256
            or record.campaign_sha256 != historical_source.campaign.sha256
            or record.report_json_sha256
            != historical_source.report_json.sha256
            or record.report_markdown_sha256
            != historical_source.report_markdown.sha256
        ):
            raise Phase6ContractError(
                "Historical Verification Record differs from Manifest references"
            )
        historical_records.append(record)

    _validate_provider_coverage(loaded.manifest, statuses)
    cutoff = validate_data_cutoff(
        loaded.manifest,
        fixture_acceptances=acceptances,
        campaigns=campaigns,
        historical_verifications=historical_records,
        live_artifacts=live_artifacts,
        recordings=live_recordings,
    )
    _require_loaded_inputs_unchanged(loaded)
    return ValidatedPublicSuiteInputs(
        loaded=loaded,
        derived_language_status=statuses,
        data_cutoff_at=cutoff,
    )


def validate_public_suite_snapshot(
    manifest: PublicSuiteManifest,
    bytes_by_path: Mapping[str, bytes],
) -> ValidatedPublicSuiteInputs:
    """Validate a Public Suite from caller-owned immutable bytes only.

    This facade deliberately performs no filesystem access.  It is the
    boundary used by read-only inventory tooling so Manifest, Plan, Campaign,
    Evidence, and Recording validation observes one already-captured byte
    snapshot.
    """
    references = _manifest_references(manifest)
    expected_paths = {reference.path for reference in references}
    if set(bytes_by_path) != expected_paths:
        raise Phase6ContractError("Public Suite snapshot paths differ from Manifest references")
    for reference in references:
        content = bytes_by_path[reference.path]
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise Phase6ContractError(
                f"Public Suite snapshot hash differs: {reference.path}"
            )
    manifest_bytes = canonical_json_bytes(manifest)
    loaded = LoadedPublicSuiteInputs(
        manifest=manifest,
        root=Path("."),
        directory_snapshots=(),
        manifest_snapshot=FileSnapshot(
            path=Path("<manifest>"),
            identity=FileIdentity(0, 0, 0, 1, len(manifest_bytes), 0, 0),
            content=manifest_bytes,
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        ),
        paths={path: Path(path) for path in expected_paths},
        bytes_by_path=dict(bytes_by_path),
        snapshots_by_path={},
    )
    return validate_public_suite_inputs(loaded)


def derive_primary_snapshot_binding(
    source: PrimarySuiteSource,
    bytes_by_path: Mapping[str, bytes],
) -> Phase6PrimarySnapshotBinding:
    """Validate one complete primary source and return canonical accounting facts."""
    if (
        source.spec is None
        or source.fixture_manifest is None
        or source.fixture_acceptance is None
        or source.diff_policy is None
        or source.plan is None
        or source.campaign is None
    ):
        raise Phase6ContractError("complete primary source is missing Plan-bound inputs")

    def content(reference: ArtifactReference) -> bytes:
        try:
            value = bytes_by_path[reference.path]
        except KeyError as error:
            raise Phase6ContractError(
                f"primary snapshot is missing {reference.path}"
            ) from error
        if hashlib.sha256(value).hexdigest() != reference.sha256:
            raise Phase6ContractError(
                f"primary snapshot hash differs: {reference.path}"
            )
        return value

    spec = _load_workflow_spec_contract_bytes(content(source.spec))
    fixture_manifest = _load_canonical_model_bytes(
        content(source.fixture_manifest), FixtureManifest, "Fixture Manifest"
    )
    fixture_acceptance = _load_canonical_model_bytes(
        content(source.fixture_acceptance),
        FixtureAcceptanceRecord,
        "Fixture Acceptance Record",
    )
    diff_policy = _load_canonical_model_bytes(
        content(source.diff_policy), DiffPolicy, "Diff Policy"
    )
    plan = _load_workflow_plan_1_2_bytes(content(source.plan))
    campaign = _load_phase6_campaign_bytes(content(source.campaign))
    evidence = [
        _load_phase6_live_run_artifact_bytes(content(reference))
        for reference in source.evidence
    ]
    recordings = [
        _load_phase6_recording_bytes(content(reference))
        for reference in source.recordings
    ]
    if not isinstance(spec.spec, WorkflowExperimentSpecV2_1):
        raise Phase6ContractError("complete primary source requires Spec 2.1")
    if not isinstance(plan, WorkflowPlanV1_2):
        raise Phase6ContractError("complete primary source requires Plan 1.2")
    for spec_relative, reference in (
        (spec.spec.fixture_manifest_path, source.fixture_manifest),
        (spec.spec.fixture_acceptance_path, source.fixture_acceptance),
        (spec.spec.diff_policy_path, source.diff_policy),
    ):
        bound = (
            PurePosixPath(source.spec.path).parent
            / PurePosixPath(spec_relative)
        ).as_posix()
        if bound != reference.path:
            raise Phase6ContractError(
                "Workflow Spec input path differs from Manifest reference"
            )
    validate_plan_bindings(
        loaded_spec=spec,
        plan=plan,
        fixture_manifest_bytes=content(source.fixture_manifest),
        fixture_manifest=fixture_manifest,
        fixture_acceptance_bytes=content(source.fixture_acceptance),
        fixture_acceptance=fixture_acceptance,
        diff_policy_bytes=content(source.diff_policy),
        diff_policy=diff_policy,
    )
    _validate_primary_live_bindings(
        source=source,
        spec=spec.spec,
        plan=plan,
        campaign=campaign,
        evidence=evidence,
        recordings=recordings,
    )
    complete_pairs: dict[tuple[str, int], set[Workflow]] = {}
    evidence_ids = {artifact.run_id for artifact in evidence}
    for event in campaign.events:
        if (
            isinstance(event, Phase6CampaignRunEvent)
            and event.status is CampaignRunStatus.COMPLETED
            and event.run_id in evidence_ids
        ):
            complete_pairs.setdefault(
                (event.task_id, event.repetition_index), set()
            ).add(event.workflow)
    return Phase6PrimarySnapshotBinding(
        language=source.language,
        campaign_path=source.campaign.path,
        campaign_id=campaign.started.experiment_id,
        planned_run_ids=frozenset(run.run_id for run in plan.runs),
        complete_pairs=frozenset(
            pair
            for pair, workflows in complete_pairs.items()
            if workflows == {Workflow.ONE_SHOT, Workflow.STAGED}
        ),
        planned_provider_call_count=plan.planned_provider_call_count,
        provider_call_count=campaign.finished.provider_call_count,
        provider_call_count_unknown_runs=campaign.finished.provider_call_count_unknown_runs,
    )


def _require_loaded_inputs_unchanged(
    loaded: LoadedPublicSuiteInputs,
) -> None:
    if not loaded.directory_snapshots:
        return
    root_snapshot = loaded.directory_snapshots[0]
    for index, directory_snapshot in enumerate(loaded.directory_snapshots):
        _require_directory_snapshot_unchanged(
            directory_snapshot,
            f"Public Suite directory component {index}",
        )
    manifest_relative = loaded.manifest_snapshot.path.relative_to(
        loaded.root
    ).as_posix()
    current_manifest, _ = _read_file_below_root(
        root_snapshot=root_snapshot,
        relative=manifest_relative,
        label="Public Suite Manifest",
    )
    if current_manifest != loaded.manifest_snapshot:
        raise Phase6PathError("Public Suite Manifest changed after Manifest load")
    for relative, file_snapshot in loaded.snapshots_by_path.items():
        current, _ = _read_file_below_root(
            root_snapshot=root_snapshot,
            relative=relative,
            label=f"listed Artifact {relative}",
        )
        if current != file_snapshot:
            raise Phase6PathError(
                f"listed Artifact {relative} changed after Manifest load"
            )
    for index, directory_snapshot in enumerate(loaded.directory_snapshots):
        _require_directory_snapshot_unchanged(
            directory_snapshot,
            f"Public Suite directory component {index}",
        )


def _validate_spec_path_bindings(
    *,
    loaded: LoadedPublicSuiteInputs,
    source: PrimarySuiteSource,
    spec: WorkflowExperimentSpecV2_1,
) -> None:
    assert source.spec is not None
    assert source.fixture_manifest is not None
    assert source.fixture_acceptance is not None
    assert source.diff_policy is not None
    spec_path = loaded.paths[source.spec.path]
    bindings = (
        (spec.fixture_manifest_path, source.fixture_manifest),
        (spec.fixture_acceptance_path, source.fixture_acceptance),
        (spec.diff_policy_path, source.diff_policy),
    )
    for spec_relative, reference in bindings:
        if not loaded.snapshots_by_path:
            relative = (
                PurePosixPath(source.spec.path).parent
                / PurePosixPath(spec_relative)
            ).as_posix()
            if relative != reference.path:
                raise Phase6ContractError(
                    "Workflow Spec input path differs from Suite Artifact reference"
                )
            continue
        try:
            lexical = Path(os.path.abspath(spec_path.parent / spec_relative))
            relative = lexical.relative_to(loaded.root).as_posix()
        except (OSError, ValueError) as error:
            raise Phase6PathError(
                "Workflow Spec input path escapes the fixed Suite root"
            ) from error
        suite_path = loaded.paths[reference.path]
        if (
            relative != reference.path
            or loaded.snapshots_by_path[reference.path].path != suite_path
        ):
            raise Phase6ContractError(
                "Workflow Spec input path differs from Suite Artifact reference"
            )


def _validate_primary_live_bindings(
    *,
    source: PrimarySuiteSource,
    spec: WorkflowSpecContract | None,
    plan: WorkflowPlanContract | None,
    campaign: CampaignContract | None,
    evidence: list[Phase6LiveRunArtifact],
    recordings: list[Phase6Recording],
) -> None:
    if campaign is None:
        if evidence or recordings:
            raise Phase6ContractError(
                "Evidence and Recording require a listed Campaign"
            )
        return
    if (
        not isinstance(spec, WorkflowExperimentSpecV2_1)
        or not isinstance(plan, WorkflowPlanV1_2)
        or not isinstance(campaign, LoadedPhase6Campaign)
    ):
        raise Phase6ContractError(
            "Primary Campaign requires Workflow Plan 1.2 and Campaign 1.2"
        )
    assert source.plan is not None
    started = campaign.started
    if (
        started.experiment_id != plan.experiment_id
        or started.plan_sha256 != source.plan.sha256
        or started.fixture_manifest_sha256 != plan.fixture_manifest_sha256
        or started.fixture_acceptance_sha256 != plan.fixture_acceptance_sha256
        or started.diff_policy_sha256 != plan.diff_policy_sha256
        or started.toolchain_fingerprint != plan.toolchain_fingerprint
        or started.planned_run_count != plan.planned_run_count
        or started.planned_provider_call_count
        != plan.planned_provider_call_count
    ):
        raise Phase6ContractError("Campaign header differs from Workflow Plan 1.2")

    planned = {run.run_id: run for run in plan.runs}
    terminal = {
        event.run_id: event
        for event in campaign.events
        if isinstance(event, Phase6CampaignRunEvent)
        and event.status is not CampaignRunStatus.STARTED
    }
    campaign_run_starts = {
        event.run_id: event
        for event in campaign.events
        if isinstance(event, Phase6CampaignRunEvent)
        and event.status is CampaignRunStatus.STARTED
    }
    if set(planned) != set(terminal):
        raise Phase6ContractError(
            "Workflow Plan run IDs and Campaign terminal run IDs differ"
        )
    required_artifact_ids = {
        run_id
        for run_id, event in terminal.items()
        if event.status
        in {CampaignRunStatus.COMPLETED, CampaignRunStatus.FAILED}
    }
    evidence_by_run = {artifact.run_id: artifact for artifact in evidence}
    recording_by_run = {
        recording.started.run_id: recording for recording in recordings
    }
    if (
        len(evidence_by_run) != len(evidence)
        or len(recording_by_run) != len(recordings)
        or set(evidence_by_run) != set(recording_by_run)
        or set(evidence_by_run) != required_artifact_ids
    ):
        raise Phase6ContractError(
            "Evidence and Recording run identities are incomplete or duplicated"
        )
    evidence_hash_by_run = {
        artifact.run_id: reference.sha256
        for artifact, reference in zip(
            evidence,
            source.evidence,
            strict=True,
        )
    }
    recording_hash_by_run = {
        recording.started.run_id: reference.sha256
        for recording, reference in zip(
            recordings,
            source.recordings,
            strict=True,
        )
    }
    derived_provider_calls: dict[str, int | None] = {}
    for run_id, event in terminal.items():
        artifact = evidence_by_run.get(run_id)
        if artifact is not None:
            derived_count = _provider_call_count_from_codex(artifact.codex)
            if event.provider_call_count != derived_count:
                raise Phase6ContractError(
                    "Campaign Provider call count differs from Codex Evidence"
                )
            derived_provider_calls[run_id] = derived_count
        elif event.status is CampaignRunStatus.NOT_RUN:
            derived_provider_calls[run_id] = 0
        elif event.status is CampaignRunStatus.INTERRUPTED:
            derived_provider_calls[run_id] = event.provider_call_count
        else:
            raise Phase6ContractError(
                "attempted terminal run lacks Provider call Evidence"
            )
    if (
        sum(value or 0 for value in derived_provider_calls.values())
        != campaign.finished.provider_call_count
        or sum(value is None for value in derived_provider_calls.values())
        != campaign.finished.provider_call_count_unknown_runs
    ):
        raise Phase6ContractError(
            "Campaign finished Provider call totals differ from run Evidence"
        )
    for run_id, artifact in evidence_by_run.items():
        run = planned.get(run_id)
        event = terminal[run_id]
        campaign_run_started = campaign_run_starts[run_id]
        recording = recording_by_run[run_id]
        try:
            _validate_phase6_execution_observations(
                overall_status=artifact.overall_status,
                failure_kind=artifact.failure_kind,
                codex=artifact.codex,
                gate_executed=artifact.gate_executed,
                gate_not_executed_reason=artifact.gate_not_executed_reason,
                gate_commands=artifact.gate_commands,
                diff=artifact.diff,
                metrics=artifact.metrics,
                workspace_lifecycle=artifact.workspace_lifecycle,
                terminal_at=artifact.completed_at,
            )
            _validate_phase6_execution_observations(
                overall_status=recording.terminal.overall_status,
                failure_kind=recording.terminal.failure_kind,
                codex=recording.terminal.codex,
                gate_executed=recording.terminal.gate_executed,
                gate_not_executed_reason=(
                    recording.terminal.gate_not_executed_reason
                ),
                gate_commands=recording.terminal.gate_commands,
                diff=recording.terminal.diff,
                metrics=recording.terminal.metrics,
                workspace_lifecycle=recording.terminal.workspace_lifecycle,
                terminal_at=recording.terminal.occurred_at,
            )
        except ValueError as error:
            raise Phase6ContractError(
                "Campaign Artifact or Recording observations are inconsistent"
            ) from error
        if run is None:
            raise Phase6ContractError("Evidence run is absent from Workflow Plan")
        expected_prompt_sha256 = (
            plan.one_shot_prompt_sha256
            if run.workflow is Workflow.ONE_SHOT
            else plan.staged_prompt_sha256
        )
        expected_prompt_bytes = (
            plan.one_shot_prompt_bytes
            if run.workflow is Workflow.ONE_SHOT
            else plan.staged_prompt_bytes
        )
        expected_state = _campaign_artifact_state(event)
        if (
            artifact.experiment_id != plan.experiment_id
            or artifact.language is not source.language
            or artifact.task_id != run.task_id
            or artifact.workflow is not run.workflow
            or artifact.repetition_index != run.repetition_index
            or artifact.reviewed_commit != plan.reviewed_commit
            or source.spec is None
            or artifact.spec_sha256 != source.spec.sha256
            or artifact.plan_sha256 != source.plan.sha256
            or artifact.fixture_sha256 != plan.fixture_sha256
            or artifact.fixture_manifest_sha256 != plan.fixture_manifest_sha256
            or artifact.fixture_acceptance_sha256
            != plan.fixture_acceptance_sha256
            or artifact.diff_policy_sha256 != plan.diff_policy_sha256
            or artifact.toolchain_fingerprint != plan.toolchain_fingerprint
            or artifact.prompt_sha256 != expected_prompt_sha256
            or artifact.prompt_bytes != expected_prompt_bytes
            or artifact.runner != spec.runner
            or artifact.codex.requested_model != plan.model
            or artifact.codex.requested_reasoning_effort
            is not plan.reasoning_effort
            or artifact.recording_sha256 != recording_hash_by_run.get(run_id)
            or artifact.overall_status is not expected_state[0]
            or artifact.failure_kind is not expected_state[1]
            or artifact.gate_executed is not event.gate_executed
            or recording.started.task_id != run.task_id
            or recording.started.experiment_id != plan.experiment_id
            or recording.started.language is not source.language
            or recording.started.workflow is not run.workflow
            or recording.started.repetition_index != run.repetition_index
            or recording.started.plan_sha256 != source.plan.sha256
            or recording.started.fixture_sha256 != plan.fixture_sha256
            or recording.started.fixture_manifest_sha256
            != plan.fixture_manifest_sha256
            or recording.started.fixture_acceptance_sha256
            != plan.fixture_acceptance_sha256
            or recording.started.diff_policy_sha256 != plan.diff_policy_sha256
            or recording.started.prompt_sha256 != expected_prompt_sha256
            or recording.started.prompt_bytes != expected_prompt_bytes
            or recording.started.requested_model != plan.model
            or recording.started.requested_reasoning_effort
            is not plan.reasoning_effort
            or recording.terminal.overall_status is not artifact.overall_status
            or recording.terminal.failure_kind is not artifact.failure_kind
            or recording.terminal.codex != artifact.codex
            or recording.terminal.metrics != artifact.metrics
            or recording.terminal.gate_executed is not artifact.gate_executed
            or recording.terminal.gate_not_executed_reason
            is not artifact.gate_not_executed_reason
            or recording.terminal.gate_commands != artifact.gate_commands
            or recording.terminal.diff != artifact.diff
            or recording.terminal.workspace_lifecycle
            is not artifact.workspace_lifecycle
            or recording.started.occurred_at != artifact.started_at
            or recording.terminal.occurred_at != artifact.completed_at
            or evidence_hash_by_run.get(run_id) is None
            or event.task_id != run.task_id
            or event.workflow is not run.workflow
            or event.repetition_index != run.repetition_index
            or event.failure_kind is not artifact.failure_kind
            or campaign_run_started.occurred_at > artifact.started_at
            or (
                (
                    event.status is CampaignRunStatus.COMPLETED
                    and recording.terminal.event_type != "run_completed"
                )
                or (
                    event.status is CampaignRunStatus.FAILED
                    and recording.terminal.event_type != "run_failed"
                )
            )
        ):
            raise Phase6ContractError(
                "Plan, Campaign, Evidence, and Recording identities differ"
            )
        if campaign.finished.occurred_at < artifact.completed_at:
            raise Phase6ContractError(
                "Campaign terminal timestamp precedes a run terminal timestamp"
            )
        if event.occurred_at < artifact.completed_at:
            raise Phase6ContractError(
                "Campaign run terminal timestamp precedes Artifact completion"
            )


def _provider_call_count_from_codex(
    codex: CodexExecutionEvidence,
) -> Literal[0, 1]:
    if (
        codex.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    ):
        return 1
    if codex.execution_stage in {
        CodexExecutionStage.PREFLIGHT_NOT_COMPLETED,
        CodexExecutionStage.PREFLIGHT_COMPLETED,
    }:
        return 0
    raise Phase6ContractError("Codex execution stage has no Provider call mapping")


def _campaign_artifact_state(
    event: Phase6CampaignRunEvent,
) -> tuple[Phase6OverallStatus, Phase6FailureKind]:
    if event.outcome is None:
        raise Phase6ContractError(
            "Campaign state does not permit a LiveRunArtifact"
        )
    expected_status = {
        Phase6CampaignOutcome.SUCCESS: Phase6OverallStatus.PASSED,
        Phase6CampaignOutcome.QUALITY_GATE_FAILURE: Phase6OverallStatus.FAILED,
        Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION:
        Phase6OverallStatus.REJECTED,
        Phase6CampaignOutcome.PROVIDER_FAILURE:
        Phase6OverallStatus.PROVIDER_ERROR,
        Phase6CampaignOutcome.PROVIDER_TIMEOUT:
        Phase6OverallStatus.PROVIDER_ERROR,
        Phase6CampaignOutcome.HARNESS_FAILURE:
        Phase6OverallStatus.HARNESS_ERROR,
        Phase6CampaignOutcome.CLEANUP_FAILURE:
        Phase6OverallStatus.HARNESS_ERROR,
    }.get(event.outcome)
    if expected_status is None or event.failure_kind is None:
        raise Phase6ContractError(
            "Campaign state does not permit a LiveRunArtifact"
        )
    return expected_status, event.failure_kind


def _validate_provider_coverage(
    manifest: PublicSuiteManifest,
    statuses: dict[Language, LanguageStatus],
) -> None:
    evaluated = sorted(
        (
            language
            for language, status in statuses.items()
            if status is LanguageStatus.EVALUATED
        ),
        key=lambda language: language.value,
    )
    codex = manifest.provider_coverage[0]
    if evaluated:
        expected_status = ProviderEvaluationStatus.EVALUATED
    else:
        expected_status = ProviderEvaluationStatus.NOT_EVALUATED
    if (
        codex.evaluation_status is not expected_status
        or codex.evaluated_languages != evaluated
    ):
        raise Phase6ContractError(
            "Codex Provider coverage differs from derived language status"
        )


def legacy_loaded_spec(
    loaded: LoadedWorkflowSpec,
) -> LoadedWorkflowSpecContract:
    """Adapt an existing 2.0 loader result without changing its runtime path."""
    return LoadedWorkflowSpecContract(spec=loaded.spec, sha256=loaded.sha256)


def legacy_plan(path: Path) -> WorkflowPlan:
    """Exercise the unchanged Plan 1.1 loader through the compatibility surface."""
    return load_workflow_plan(path)


def validate_phase6_snapshot_contract(role: str, content: bytes) -> None:
    """Validate one Phase 6 contract from a stable in-memory snapshot.

    This is the small public facade used by Phase 7. It keeps the existing
    byte-oriented strict loaders behind a stable API without publishing,
    regenerating, or executing any Artifact.
    """
    if role == "suite_manifest":
        _load_canonical_model_bytes(content, PublicSuiteManifest, "Public Suite Manifest")
    elif role == "checksums":
        _load_canonical_model_bytes(content, PublicChecksums, "Public checksums")
    elif role == "external_anchor":
        _load_canonical_model_bytes(content, ExternalChecksumAnchor, "External anchor")
    elif role == "release_metadata":
        _load_canonical_model_bytes(content, ReleaseMetadata, "Release metadata")
    elif role == "fixture_manifest":
        _load_canonical_model_bytes(content, FixtureManifest, "Fixture Manifest")
    elif role == "fixture_acceptance":
        _load_canonical_model_bytes(
            content,
            FixtureAcceptanceRecord,
            "Fixture Acceptance Record",
        )
    elif role == "historical_verification":
        _load_canonical_model_bytes(
            content,
            HistoricalVerificationRecord,
            "Historical Verification Record",
        )
    elif role == "plan":
        _load_workflow_plan_1_2_bytes(content)
    elif role == "campaign":
        _load_phase6_campaign_bytes(content)
    elif role == "recording":
        _load_phase6_recording_bytes(content)
    elif role == "evidence":
        _load_phase6_live_run_artifact_bytes(content)
    elif role == "spec":
        _load_workflow_spec_contract_bytes(content)
    else:
        raise ValueError(f"unsupported Phase 6 snapshot contract role: {role}")
