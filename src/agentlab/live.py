"""Phase 3 single-run Codex orchestration and paired Artifact persistence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn

from pydantic import ValidationError

from agentlab.codex_provider import (
    CodexLifecycleTracker,
    CodexPreflight,
    CodexPreflightError,
    CodexProcessRunner,
    CodexRunnerError,
    lifecycle_failure_evidence,
    post_preflight_failure_evidence,
    preflight_codex,
    preflight_failure_evidence,
    resolve_codex_home,
    unsupported_platform_evidence,
)
from agentlab.gates import (
    GateExecutionResult,
    GateExecutionTracker,
    execute_quality_gates_in_workspace,
)
from agentlab.models import (
    CodexExecutionEvidence,
    CodexFailureStage,
    CommandEvidence,
    CommandStatus,
    DiagnosticCleanupState,
    DiagnosticFailureStage,
    DiagnosticInvocationState,
    DiagnosticRunnerState,
    DiffEvidence,
    ExecutionMode,
    ExperimentSpec,
    GateKind,
    GateKindSummary,
    LiveDiagnosticCode,
    LiveEvaluationSummary,
    LiveFailureDiagnostic,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    Provider,
    ProviderActivityDetermination,
    ProviderExecutionStatus,
    RunMetrics,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.recording import (
    LiveRunCompletedEvent,
    LiveRunFailedEvent,
    LiveRunStartedEvent,
    live_recording_jsonl_bytes,
)
from agentlab.runner import UnsupportedRunnerPlatformError
from agentlab.specs import LoadedExperimentSpec, load_experiment_spec_document
from agentlab.workspace import (
    DirectorySnapshot,
    SnapshotError,
    WorkspaceError,
    build_diff_evidence,
    incomplete_diff_evidence,
    paths_refer_to_same_file,
    prepare_disposable_workspace,
    remove_disposable_workspace,
    snapshot_directory,
    validate_fixture_source,
)


class LiveCodexError(ValueError):
    """A safe CLI-boundary error that never contains Prompt or raw Provider output."""

    def __init__(
        self,
        message: str,
        *,
        workspace_lifecycle: WorkspaceLifecycle | None = None,
    ) -> None:
        super().__init__(message)
        self.workspace_lifecycle = workspace_lifecycle
        self.workspace_removed = (
            None
            if workspace_lifecycle is None
            else workspace_lifecycle is WorkspaceLifecycle.REMOVED
        )


class LiveArtifactLoadError(ValueError):
    """A persisted Live Artifact is not strict UTF-8 JSON matching its contract."""


class LiveDiagnosticLoadError(ValueError):
    """A Failure Diagnostic is not strict UTF-8 JSON matching its contract."""


class LiveDiagnosticPublicationError(LiveCodexError):
    """A safe fixed-code failure while publishing a Failure Diagnostic."""

    def __init__(
        self,
        *,
        workspace_lifecycle: WorkspaceLifecycle,
    ) -> None:
        super().__init__(
            "Live failure diagnostic publication failed",
            workspace_lifecycle=workspace_lifecycle,
        )
        self.diagnostic_code = LiveDiagnosticCode.DIAGNOSTIC_PUBLICATION_FAILED
        self.diagnostic_published = False


class LiveDiagnosticCreatedError(LiveCodexError):
    """Strict paired outputs were withheld and a safe Diagnostic was published."""

    def __init__(
        self,
        diagnostic_code: LiveDiagnosticCode,
        *,
        workspace_lifecycle: WorkspaceLifecycle,
    ) -> None:
        super().__init__(
            f"strict Live outputs were withheld; diagnostic={diagnostic_code.value}",
            workspace_lifecycle=workspace_lifecycle,
        )
        self.diagnostic_code = diagnostic_code
        self.diagnostic_published = True


class _StrictConstructionFailure(Exception):
    """Internal fixed-code signal; never persist its exception cause."""

    def __init__(
        self,
        diagnostic_code: LiveDiagnosticCode,
        lifecycle: CodexLifecycleTracker,
    ) -> None:
        super().__init__(diagnostic_code.value)
        self.diagnostic_code = diagnostic_code
        self.lifecycle = lifecycle


@dataclass(frozen=True)
class PromptInput:
    path: Path
    content: bytes
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class LiveCodexOutcome:
    artifact: LiveRunArtifact
    recording_path: Path
    output_path: Path


class _DuplicateKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def load_live_artifact(path: Path) -> LiveRunArtifact:
    try:
        text = path.read_bytes().decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError as error:
        raise LiveArtifactLoadError(f"{path}: duplicate JSON key {error}") from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LiveArtifactLoadError(
            f"{path}: could not read strict Live Evidence JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise LiveArtifactLoadError(f"{path}: Live Evidence must be a JSON object")
    try:
        return LiveRunArtifact.model_validate(raw)
    except ValidationError as error:
        raise LiveArtifactLoadError(f"{path}: invalid Live Evidence: {error}") from error


def load_failure_diagnostic(path: Path) -> LiveFailureDiagnostic:
    """Read one strict standalone Failure Diagnostic."""
    try:
        text = path.read_bytes().decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError as error:
        raise LiveDiagnosticLoadError(f"{path}: duplicate JSON key {error}") from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LiveDiagnosticLoadError(
            f"{path}: could not read strict Failure Diagnostic JSON"
        ) from error
    if not isinstance(raw, dict):
        raise LiveDiagnosticLoadError(
            f"{path}: Failure Diagnostic must be a JSON object"
        )
    try:
        return LiveFailureDiagnostic.model_validate(raw)
    except ValidationError as error:
        raise LiveDiagnosticLoadError(
            f"{path}: invalid Failure Diagnostic"
        ) from error


def _resolve_relative_path(spec_path: Path, configured_path: str) -> Path:
    return spec_path.parent / Path(configured_path)


def _reject_symlink_components(
    spec_path: Path,
    configured_path: str,
    *,
    label: str,
) -> Path:
    current = spec_path.parent
    for component in PurePosixPath(configured_path).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise LiveCodexError(
                f"{label} path is unavailable at {component!r}: {type(error).__name__}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise LiveCodexError(f"{label} path component is a symlink: {component}")
    return _resolve_relative_path(spec_path, configured_path)


def load_prompt(
    spec_path: Path,
    configured_path: str,
    *,
    max_prompt_bytes: int,
) -> PromptInput:
    """Read one regular Prompt without following symlinks and validate it structurally."""
    prompt_path = _reject_symlink_components(
        spec_path,
        configured_path,
        label="Prompt",
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(prompt_path, flags)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LiveCodexError("Prompt must be a regular file")
        with os.fdopen(file_descriptor, "rb") as prompt_file:
            file_descriptor = None
            content = prompt_file.read(max_prompt_bytes + 1)
    except LiveCodexError:
        raise
    except OSError as error:
        raise LiveCodexError(
            f"could not read Prompt: {type(error).__name__}"
        ) from error
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)

    if len(content) > max_prompt_bytes:
        raise LiveCodexError("Prompt exceeds live.max_prompt_bytes")
    if not content:
        raise LiveCodexError("Prompt must not be empty")
    if b"\x00" in content:
        raise LiveCodexError("Prompt must not contain NUL")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LiveCodexError("Prompt must be valid UTF-8") from error
    if not decoded.strip():
        raise LiveCodexError("Prompt must contain non-whitespace text")
    return PromptInput(
        path=prompt_path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )


def _validate_phase3_request(
    spec: ExperimentSpec,
    *,
    task_id: str,
    repetition_index: int,
) -> None:
    if spec.execution_mode is not ExecutionMode.LIVE:
        raise LiveCodexError("execution_mode must be live")
    if spec.provider is not Provider.CODEX:
        raise LiveCodexError("Phase 3 live-codex requires provider=codex")
    if spec.workflow is not Workflow.ONE_SHOT:
        raise LiveCodexError("Phase 3 live-codex supports workflow=one_shot only")
    if spec.live is None:
        raise LiveCodexError("Live settings are required")
    if spec.runner is None:
        raise LiveCodexError("Runner settings are required")
    missing = [
        name
        for name in (
            "prompt_path",
            "model",
            "reasoning_effort",
            "provider_timeout_ms",
            "max_prompt_bytes",
            "max_event_line_bytes",
            "max_provider_output_bytes",
        )
        if getattr(spec.live, name) is None
    ]
    if missing:
        raise LiveCodexError(
            f"Phase 3 Live settings are incomplete: {', '.join(missing)}"
        )
    if task_id not in spec.task_ids:
        raise LiveCodexError(f"task_id {task_id!r} is not present in spec.task_ids")
    if isinstance(repetition_index, bool) or not 0 <= repetition_index < spec.repetitions:
        raise LiveCodexError(
            f"repetition_index {repetition_index} is outside [0, {spec.repetitions})"
        )


def _ensure_output_is_not_symlink(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise LiveCodexError(
            f"could not inspect {label}: {type(error).__name__}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise LiveCodexError(f"{label} must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise LiveCodexError(f"{label} must be a regular file when it already exists")


def _ensure_existing_components_are_not_symlinks(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise LiveCodexError(
                f"could not inspect {label} path: {type(error).__name__}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise LiveCodexError(f"{label} path must not contain symlinks")


def protect_live_inputs(
    *,
    spec_path: Path,
    prompt_path: Path,
    fixture_source: Path,
    fixture_snapshot: DirectorySnapshot,
    recording_path: Path,
    output_path: Path,
    diagnostic_path: Path | None = None,
) -> None:
    """Protect inputs and all configured outputs from aliasing."""
    _ensure_existing_components_are_not_symlinks(recording_path, "Recording output")
    _ensure_existing_components_are_not_symlinks(output_path, "Evidence output")
    _ensure_output_is_not_symlink(recording_path, "Recording output")
    _ensure_output_is_not_symlink(output_path, "Evidence output")
    if diagnostic_path is not None:
        _ensure_existing_components_are_not_symlinks(
            diagnostic_path,
            "Failure Diagnostic output",
        )
        _ensure_output_is_not_symlink(
            diagnostic_path,
            "Failure Diagnostic output",
        )
    if paths_refer_to_same_file(recording_path, output_path):
        raise LiveCodexError("Recording and Evidence outputs must be different files")
    if diagnostic_path is not None and (
        paths_refer_to_same_file(diagnostic_path, recording_path)
        or paths_refer_to_same_file(diagnostic_path, output_path)
    ):
        raise LiveCodexError(
            "Failure Diagnostic, Recording, and Evidence outputs must be different files"
        )

    try:
        resolved_fixture = fixture_source.resolve(strict=True)
        resolved_outputs = [
            recording_path.resolve(strict=False),
            output_path.resolve(strict=False),
        ]
        if diagnostic_path is not None:
            resolved_outputs.append(diagnostic_path.resolve(strict=False))
    except (OSError, RuntimeError) as error:
        raise LiveCodexError(
            f"could not resolve Live output paths: {type(error).__name__}"
        ) from error
    for resolved_output in resolved_outputs:
        if resolved_output == resolved_fixture or resolved_output.is_relative_to(
            resolved_fixture
        ):
            raise LiveCodexError("Live outputs must not be inside the Fixture source")

    protected_inputs: list[tuple[str, Path]] = [
        ("ExperimentSpec", spec_path),
        ("Prompt", prompt_path),
    ]
    protected_inputs.extend(
        ("Fixture source", fixture_source / relative)
        for relative in fixture_snapshot.files
    )
    outputs: list[tuple[str, Path]] = [
        ("Recording", recording_path),
        ("Evidence", output_path),
    ]
    if diagnostic_path is not None:
        outputs.append(("Failure Diagnostic", diagnostic_path))
    for output_label, output in outputs:
        for input_label, input_path in protected_inputs:
            if paths_refer_to_same_file(output, input_path):
                raise LiveCodexError(
                    f"{output_label} output must not overwrite or alias {input_label}"
                )


def _resolve_diagnostic_path(
    *,
    spec_path: Path,
    spec: ExperimentSpec,
    output_path: Path,
) -> Path:
    assert spec.live is not None
    if spec.live.diagnostic_to is not None:
        return _reject_symlink_components(
            spec_path,
            spec.live.diagnostic_to,
            label="Failure Diagnostic output",
        )
    suffix = output_path.suffix or ".json"
    return output_path.with_name(f"{output_path.stem}.diagnostic{suffix}")


def _diagnostic_from_lifecycle(
    *,
    run_id: str,
    experiment_id: str,
    task_id: str,
    diagnostic_code: LiveDiagnosticCode,
    lifecycle: CodexLifecycleTracker | None,
    workspace_lifecycle: WorkspaceLifecycle,
    gate_executed: bool,
) -> LiveFailureDiagnostic:
    if lifecycle is None:
        runner_state = DiagnosticRunnerState.UNKNOWN
        invocation_state = DiagnosticInvocationState.UNKNOWN
        cleanup_state = DiagnosticCleanupState.UNKNOWN
        failure_stage = DiagnosticFailureStage.UNKNOWN
    else:
        try:
            runner_state = DiagnosticRunnerState(lifecycle.runner_state.value)
        except (AttributeError, ValueError):
            runner_state = DiagnosticRunnerState.UNKNOWN
        try:
            invocation_state = DiagnosticInvocationState(
                lifecycle.invocation_state.value
            )
        except (AttributeError, ValueError):
            invocation_state = DiagnosticInvocationState.UNKNOWN
        try:
            cleanup_state = (
                DiagnosticCleanupState.UNKNOWN
                if lifecycle.cleanup_state is None
                else DiagnosticCleanupState(lifecycle.cleanup_state.value)
            )
        except (AttributeError, ValueError):
            cleanup_state = DiagnosticCleanupState.UNKNOWN
        try:
            failure_stage = DiagnosticFailureStage(lifecycle.failure_stage.value)
        except (AttributeError, ValueError):
            failure_stage = DiagnosticFailureStage.UNKNOWN

    determination = (
        ProviderActivityDetermination.UNKNOWN
        if "unknown"
        in {
            runner_state.value,
            invocation_state.value,
            cleanup_state.value,
            failure_stage.value,
        }
        else ProviderActivityDetermination.DETERMINED
    )
    failure_kind: Literal[
        LiveFailureKind.EVIDENCE_ERROR,
        LiveFailureKind.PROCESS_CLEANUP_ERROR,
    ] = (
        LiveFailureKind.PROCESS_CLEANUP_ERROR
        if cleanup_state is DiagnosticCleanupState.FAILED
        else LiveFailureKind.EVIDENCE_ERROR
    )
    return LiveFailureDiagnostic(
        schema_version="1.0",
        run_id=run_id,
        experiment_id=experiment_id,
        task_id=task_id,
        failure_kind=failure_kind,
        diagnostic_code=diagnostic_code,
        failure_stage=failure_stage,
        runner_state=runner_state,
        invocation_state=invocation_state,
        cleanup_state=cleanup_state,
        workspace_lifecycle=workspace_lifecycle,
        paired_artifacts_published=False,
        gate_executed=gate_executed,
        provider_activity_determined=determination,
        created_at=datetime.now(UTC),
    )


def _publish_failure_diagnostic_and_raise(
    *,
    diagnostic_path: Path,
    recording_path: Path,
    output_path: Path,
    run_id: str,
    experiment_id: str,
    task_id: str,
    diagnostic_code: LiveDiagnosticCode,
    lifecycle: CodexLifecycleTracker | None,
    workspace_lifecycle: WorkspaceLifecycle,
    gate_executed: bool,
) -> NoReturn:
    if os.path.lexists(recording_path) or os.path.lexists(output_path):
        raise LiveDiagnosticPublicationError(
            workspace_lifecycle=workspace_lifecycle,
        )
    try:
        diagnostic = _diagnostic_from_lifecycle(
            run_id=run_id,
            experiment_id=experiment_id,
            task_id=task_id,
            diagnostic_code=diagnostic_code,
            lifecycle=lifecycle,
            workspace_lifecycle=workspace_lifecycle,
            gate_executed=gate_executed,
        )
    except Exception as error:
        raise LiveDiagnosticPublicationError(
            workspace_lifecycle=workspace_lifecycle,
        ) from error
    write_failure_diagnostic(diagnostic, diagnostic_path)
    raise LiveDiagnosticCreatedError(
        diagnostic_code,
        workspace_lifecycle=workspace_lifecycle,
    )


def _json_bytes(artifact: LiveRunArtifact) -> bytes:
    try:
        payload = json.dumps(
            artifact.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except ValueError as error:
        raise LiveCodexError(
            f"Live Evidence contains a non-finite number: {error}"
        ) from error
    return f"{payload}\n".encode()


def _diagnostic_json_bytes(diagnostic: LiveFailureDiagnostic) -> bytes:
    try:
        payload = json.dumps(
            diagnostic.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except ValueError as error:
        raise LiveCodexError(
            "Failure Diagnostic contains a non-finite number"
        ) from error
    return f"{payload}\n".encode()


def _stage_bytes(path: Path, content: bytes) -> Path:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        return temporary_path
    except OSError as error:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise LiveCodexError(
            f"could not stage Live output: {type(error).__name__}"
        ) from error


def write_failure_diagnostic(
    diagnostic: LiveFailureDiagnostic,
    path: Path,
) -> None:
    """Atomically create one standalone Diagnostic without replacing any file."""
    temporary_path: Path | None = None
    try:
        _ensure_existing_components_are_not_symlinks(
            path,
            "Failure Diagnostic output",
        )
        _ensure_output_is_not_symlink(path, "Failure Diagnostic output")
        if os.path.lexists(path):
            raise FileExistsError
        temporary_path = _stage_bytes(path, _diagnostic_json_bytes(diagnostic))
        os.link(temporary_path, path)
    except Exception as error:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise LiveDiagnosticPublicationError(
            workspace_lifecycle=diagnostic.workspace_lifecycle,
        ) from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _backup_existing(path: Path) -> Path | None:
    if not os.path.lexists(path):
        return None
    file_descriptor: int | None = None
    backup: Path | None = None
    try:
        file_descriptor, backup_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".backup",
        )
        backup = Path(backup_name)
        os.close(file_descriptor)
        file_descriptor = None
        backup.unlink()
        os.link(path, backup)
        return backup
    except OSError:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
        if backup is not None:
            with suppress(OSError):
                backup.unlink(missing_ok=True)
        raise


def write_live_outputs(
    *,
    recording_bytes: bytes,
    artifact: LiveRunArtifact,
    recording_path: Path,
    output_path: Path,
    force: bool,
) -> None:
    """Stage both outputs and roll back the first publication if the second fails."""
    if not force:
        for label, path in (("Recording", recording_path), ("Evidence", output_path)):
            if os.path.lexists(path):
                raise LiveCodexError(
                    f"{label} output already exists: {path}; use --force to overwrite"
                )
    recording_temporary: Path | None = None
    evidence_temporary: Path | None = None
    recording_backup: Path | None = None
    recording_was_published = False
    evidence_was_published = False
    try:
        recording_temporary = _stage_bytes(recording_path, recording_bytes)
        evidence_temporary = _stage_bytes(output_path, _json_bytes(artifact))
        if force:
            recording_backup = _backup_existing(recording_path)
            os.replace(recording_temporary, recording_path)
            recording_temporary = None
            recording_was_published = True
            os.replace(evidence_temporary, output_path)
            evidence_temporary = None
            evidence_was_published = True
        else:
            try:
                os.link(recording_temporary, recording_path)
                recording_was_published = True
                os.link(evidence_temporary, output_path)
                evidence_was_published = True
            except FileExistsError as error:
                raise LiveCodexError(
                    "Live output appeared concurrently; no existing output was replaced"
                ) from error
    except (LiveCodexError, OSError) as error:
        if recording_was_published and not evidence_was_published:
            try:
                if recording_backup is None:
                    recording_path.unlink(missing_ok=True)
                else:
                    os.replace(recording_backup, recording_path)
                    recording_backup = None
                recording_was_published = False
            except OSError as rollback_error:
                backup_status = (
                    "the original Recording backup was retained"
                    if recording_backup is not None
                    else "manual inspection of the Recording path is required"
                )
                raise LiveCodexError(
                    "could not roll back a partial Live output publication; "
                    f"{backup_status}"
                ) from rollback_error
        if isinstance(error, LiveCodexError):
            raise error
        raise LiveCodexError(
            f"could not publish Live outputs: {type(error).__name__}"
        ) from error
    finally:
        if evidence_temporary is not None:
            with suppress(OSError):
                evidence_temporary.unlink(missing_ok=True)
        if recording_temporary is not None:
            with suppress(OSError):
                recording_temporary.unlink(missing_ok=True)
        if recording_backup is not None and (
            evidence_was_published or not recording_was_published
        ):
            with suppress(OSError):
                recording_backup.unlink(missing_ok=True)


def _gate_metrics(
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    *,
    agent_duration_ms: int,
    evaluation_duration_ms: int,
    codex: CodexExecutionEvidence,
) -> RunMetrics:
    assert diff.added_lines is not None
    assert diff.deleted_lines is not None
    acceptance = [command for command in commands if command.gate is GateKind.ACCEPTANCE]
    regression = [command for command in commands if command.gate is GateKind.REGRESSION]
    lint = [command for command in commands if command.gate is GateKind.LINT]
    typecheck = [command for command in commands if command.gate is GateKind.TYPECHECK]
    return RunMetrics(
        quality_gate_pass=all(
            command.status is CommandStatus.PASSED for command in commands
        ),
        acceptance_tests_passed=sum(
            command.status is CommandStatus.PASSED for command in acceptance
        ),
        acceptance_tests_total=len(acceptance),
        regression_failures=sum(
            command.status is CommandStatus.FAILED for command in regression
        ),
        lint_errors=sum(command.status is CommandStatus.FAILED for command in lint),
        typecheck_errors=sum(
            command.status is CommandStatus.FAILED for command in typecheck
        ),
        agent_duration_ms=agent_duration_ms,
        evaluation_duration_ms=evaluation_duration_ms,
        total_duration_ms=agent_duration_ms + evaluation_duration_ms,
        agent_call_count=1,
        retry_count=0,
        changed_files=diff.changed_files,
        added_lines=diff.added_lines,
        deleted_lines=diff.deleted_lines,
        usage_metrics=codex.usage_metrics,
    )


def _empty_diff(snapshot: DirectorySnapshot, *, max_diff_bytes: int) -> DiffEvidence:
    return build_diff_evidence(snapshot, snapshot, max_diff_bytes=max_diff_bytes)


def _evaluation_summary(
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    *,
    evaluation_duration_ms: int,
    workspace_lifecycle: WorkspaceLifecycle,
) -> LiveEvaluationSummary:
    def gate_summary(gate: GateKind) -> GateKindSummary:
        selected = [command for command in commands if command.gate is gate]
        return GateKindSummary(
            command_count=len(selected),
            passed_count=sum(
                command.status is CommandStatus.PASSED for command in selected
            ),
            failed_count=sum(
                command.status is CommandStatus.FAILED for command in selected
            ),
        )

    return LiveEvaluationSummary(
        acceptance=gate_summary(GateKind.ACCEPTANCE),
        regression=gate_summary(GateKind.REGRESSION),
        lint=gate_summary(GateKind.LINT),
        typecheck=gate_summary(GateKind.TYPECHECK),
        all_commands_completed_normally=all(
            command.status in {CommandStatus.PASSED, CommandStatus.FAILED}
            and command.termination.process_group_cleared
            for command in commands
        ),
        evaluation_duration_ms=evaluation_duration_ms,
        changed_files=diff.changed_files,
        added_lines=diff.added_lines,
        deleted_lines=diff.deleted_lines,
        diff_line_counts_complete=diff.line_counts_complete,
        workspace_lifecycle=workspace_lifecycle,
    )


def _prepare_live_environment_root(parent: Path, name: str) -> Path:
    root = parent / name
    for directory in ("home", "tmp", "cache"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    return root


def _recording_and_artifact(
    *,
    loaded_spec: LoadedExperimentSpec,
    task_id: str,
    repetition_index: int,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    source_snapshot: DirectorySnapshot,
    prompt: PromptInput,
    codex: CodexExecutionEvidence,
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    metrics: RunMetrics | None,
    workspace_lifecycle: WorkspaceLifecycle,
    evaluation_duration_ms: int,
    overall_status: LiveOverallStatus,
    failure_kind: LiveFailureKind,
    lifecycle: CodexLifecycleTracker,
) -> tuple[bytes, LiveRunArtifact]:
    spec = loaded_spec.spec
    assert spec.live is not None
    assert spec.runner is not None
    assert spec.live.model is not None
    assert spec.live.reasoning_effort is not None
    try:
        evaluation = _evaluation_summary(
            commands,
            diff,
            evaluation_duration_ms=evaluation_duration_ms,
            workspace_lifecycle=workspace_lifecycle,
        )
        started_event = LiveRunStartedEvent(
            schema_version="1.1",
            sequence=0,
            event_type="run_started",
            run_id=run_id,
            experiment_id=spec.experiment_id,
            task_id=task_id,
            workflow=Workflow.ONE_SHOT,
            provider=Provider.CODEX,
            repetition_index=repetition_index,
            execution_mode=ExecutionMode.LIVE,
            occurred_at=started_at,
            prompt_sha256=prompt.sha256,
            prompt_bytes=prompt.byte_count,
            prompt_redacted=True,
            requested_model=spec.live.model,
            requested_reasoning_effort=spec.live.reasoning_effort,
            cli_version=codex.cli_version,
        )
        if metrics is not None:
            terminal: LiveRunCompletedEvent | LiveRunFailedEvent = (
                LiveRunCompletedEvent(
                    schema_version="1.1",
                    sequence=1,
                    event_type="run_completed",
                    run_id=run_id,
                    experiment_id=spec.experiment_id,
                    occurred_at=completed_at,
                    metrics=metrics,
                    codex=codex,
                    evaluation=evaluation,
                )
            )
        else:
            terminal = LiveRunFailedEvent(
                schema_version="1.1",
                sequence=1,
                event_type="run_failed",
                run_id=run_id,
                experiment_id=spec.experiment_id,
                occurred_at=completed_at,
                failure_kind=failure_kind,
                codex=codex,
                evaluation=evaluation,
                metrics_included=False,
            )
        recording_bytes = live_recording_jsonl_bytes(started_event, terminal)
    except Exception as error:
        raise _StrictConstructionFailure(
            LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED,
            lifecycle,
        ) from error
    try:
        artifact = LiveRunArtifact(
            schema_version="1.0",
            run_id=run_id,
            experiment_id=spec.experiment_id,
            task_id=task_id,
            repetition_index=repetition_index,
            workflow=Workflow.ONE_SHOT,
            provider=Provider.CODEX,
            execution_mode=ExecutionMode.LIVE,
            overall_status=overall_status,
            failure_kind=failure_kind,
            started_at=started_at,
            completed_at=completed_at,
            spec_sha256=loaded_spec.sha256,
            fixture_sha256=source_snapshot.sha256,
            prompt_sha256=prompt.sha256,
            prompt_bytes=prompt.byte_count,
            prompt_redacted=True,
            runner=spec.runner,
            codex=codex,
            gate_commands=commands,
            diff=diff,
            metrics=metrics,
            workspace_lifecycle=workspace_lifecycle,
            recording_sha256=hashlib.sha256(recording_bytes).hexdigest(),
            raw_provider_output_persisted=False,
        )
    except Exception as error:
        raise _StrictConstructionFailure(
            LiveDiagnosticCode.LIVE_ARTIFACT_CONSTRUCTION_FAILED,
            lifecycle,
        ) from error
    return recording_bytes, artifact


def _map_gate_harness(_gate_result: GateExecutionResult) -> LiveFailureKind:
    return LiveFailureKind.GATE_HARNESS_ERROR


def _strict_lifecycle_failure_evidence(
    preflight: CodexPreflight,
    *,
    spec: ExperimentSpec,
    lifecycle: CodexLifecycleTracker,
) -> CodexExecutionEvidence:
    assert spec.live is not None
    try:
        return lifecycle_failure_evidence(
            preflight,
            live=spec.live,
            lifecycle=lifecycle,
        )
    except Exception as error:
        raise _StrictConstructionFailure(
            LiveDiagnosticCode.LIFECYCLE_FALLBACK_EVIDENCE_VALIDATION_FAILED,
            lifecycle,
        ) from error


def _strict_codex_evidence(
    build: Callable[[], CodexExecutionEvidence],
    *,
    lifecycle: CodexLifecycleTracker,
) -> CodexExecutionEvidence:
    try:
        return build()
    except Exception as error:
        lifecycle.failure_stage = CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION
        raise _StrictConstructionFailure(
            LiveDiagnosticCode.CODEX_EVIDENCE_VALIDATION_FAILED,
            lifecycle,
        ) from error


def run_live_codex(
    spec_path: Path,
    *,
    task_id: str,
    repetition_index: int,
    run_id: str,
    output_path: Path,
    confirm_live_codex: bool,
    force: bool = False,
    parent_environment: Mapping[str, str] | None = None,
    preflight: Callable[..., CodexPreflight] = preflight_codex,
) -> LiveCodexOutcome:
    """Execute one explicit Codex run; never call this from normal tests with real PATH."""
    if not confirm_live_codex:
        raise LiveCodexError(
            "Live Codex requires --confirm-live-codex; no subprocess was started"
        )
    if not task_id:
        raise LiveCodexError("task_id must not be empty")
    if not run_id:
        raise LiveCodexError("run_id must not be empty")

    loaded_spec = load_experiment_spec_document(spec_path)
    spec = loaded_spec.spec
    _validate_phase3_request(
        spec,
        task_id=task_id,
        repetition_index=repetition_index,
    )
    assert spec.live is not None
    assert spec.runner is not None
    assert spec.live.prompt_path is not None
    assert spec.live.max_prompt_bytes is not None
    live = spec.live
    assert live.prompt_path is not None
    assert live.max_prompt_bytes is not None

    prompt = load_prompt(
        spec_path,
        live.prompt_path,
        max_prompt_bytes=live.max_prompt_bytes,
    )
    try:
        source, source_snapshot = validate_fixture_source(
            spec_path,
            spec.runner.fixture_path,
        )
    except WorkspaceError as error:
        raise LiveCodexError(str(error)) from error
    recording_path = _reject_symlink_components(
        spec_path,
        live.record_to,
        label="Recording output",
    )
    diagnostic_path = _resolve_diagnostic_path(
        spec_path=spec_path,
        spec=spec,
        output_path=output_path,
    )
    protect_live_inputs(
        spec_path=spec_path,
        prompt_path=prompt.path,
        fixture_source=source,
        fixture_snapshot=source_snapshot,
        recording_path=recording_path,
        output_path=output_path,
        diagnostic_path=diagnostic_path,
    )
    if not force:
        for label, path in (("Recording", recording_path), ("Evidence", output_path)):
            if os.path.lexists(path):
                raise LiveCodexError(
                    f"{label} output already exists: {path}; use --force to overwrite"
                )
    if os.path.lexists(diagnostic_path):
        raise LiveCodexError(
            "Failure Diagnostic output already exists; choose a new output set"
        )

    parent = os.environ if parent_environment is None else parent_environment
    try:
        resolve_codex_home(parent)
    except ValueError as error:
        raise LiveCodexError(str(error)) from error

    lifecycle = CodexLifecycleTracker()
    lifecycle.failure_stage = CodexFailureStage.PREFLIGHT
    gate_tracker = GateExecutionTracker()
    started_at = datetime.now(UTC)
    try:
        preflight_result = preflight(parent_environment=parent_environment)
    except CodexPreflightError as error:
        preflight_error = error
        try:
            preflight_codex_evidence = _strict_codex_evidence(
                lambda: preflight_failure_evidence(
                    preflight_error,
                    live=live,
                ),
                lifecycle=lifecycle,
            )
        except _StrictConstructionFailure as failure:
            _publish_failure_diagnostic_and_raise(
                diagnostic_path=diagnostic_path,
                recording_path=recording_path,
                output_path=output_path,
                run_id=run_id,
                experiment_id=spec.experiment_id,
                task_id=task_id,
                diagnostic_code=failure.diagnostic_code,
                lifecycle=failure.lifecycle,
                workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
                gate_executed=gate_tracker.gate_executed,
            )
        diff = _empty_diff(
            source_snapshot,
            max_diff_bytes=spec.runner.max_diff_bytes,
        )
        completed_at = datetime.now(UTC)
        try:
            recording_bytes, artifact = _recording_and_artifact(
                loaded_spec=loaded_spec,
                task_id=task_id,
                repetition_index=repetition_index,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                source_snapshot=source_snapshot,
                prompt=prompt,
                codex=preflight_codex_evidence,
                commands=[],
                diff=diff,
                metrics=None,
                workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
                evaluation_duration_ms=0,
                overall_status=(
                    LiveOverallStatus.HARNESS_ERROR
                    if error.failure_kind
                    in {
                        LiveFailureKind.PROCESS_CLEANUP_ERROR,
                        LiveFailureKind.EVIDENCE_ERROR,
                        LiveFailureKind.UNSUPPORTED_PLATFORM,
                    }
                    else LiveOverallStatus.PROVIDER_ERROR
                ),
                failure_kind=error.failure_kind,
                lifecycle=lifecycle,
            )
        except _StrictConstructionFailure as failure:
            _publish_failure_diagnostic_and_raise(
                diagnostic_path=diagnostic_path,
                recording_path=recording_path,
                output_path=output_path,
                run_id=run_id,
                experiment_id=spec.experiment_id,
                task_id=task_id,
                diagnostic_code=failure.diagnostic_code,
                lifecycle=failure.lifecycle,
                workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
                gate_executed=gate_tracker.gate_executed,
            )
        protect_live_inputs(
            spec_path=spec_path,
            prompt_path=prompt.path,
            fixture_source=source,
            fixture_snapshot=source_snapshot,
            recording_path=recording_path,
            output_path=output_path,
            diagnostic_path=diagnostic_path,
        )
        try:
            write_live_outputs(
                recording_bytes=recording_bytes,
                artifact=artifact,
                recording_path=recording_path,
                output_path=output_path,
                force=force,
            )
        except LiveCodexError:
            _publish_failure_diagnostic_and_raise(
                diagnostic_path=diagnostic_path,
                recording_path=recording_path,
                output_path=output_path,
                run_id=run_id,
                experiment_id=spec.experiment_id,
                task_id=task_id,
                diagnostic_code=(
                    LiveDiagnosticCode.PAIRED_OUTPUT_PUBLICATION_FAILED
                ),
                lifecycle=lifecycle,
                workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
                gate_executed=gate_tracker.gate_executed,
            )
        return LiveCodexOutcome(artifact, recording_path, output_path)

    commands: list[CommandEvidence] = []
    evaluation_duration_ms = 0
    metrics: RunMetrics | None = None
    diff = incomplete_diff_evidence("Live diff collection did not complete")
    workspace_lifecycle = WorkspaceLifecycle.NOT_CREATED
    workspace = None
    gate_result: GateExecutionResult | None = None
    codex: CodexExecutionEvidence | None = None
    failure_kind: LiveFailureKind | None = None
    lifecycle.failure_stage = CodexFailureStage.WORKSPACE_PREPARATION
    diagnostic_failure: _StrictConstructionFailure | None = None
    try:
        workspace = prepare_disposable_workspace(source, source_snapshot)
        try:
            provider_environment_root = _prepare_live_environment_root(
                workspace.environment_root,
                "provider",
            )
        except Exception:
            lifecycle.failure_stage = (
                CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION
            )
            codex = _strict_codex_evidence(
                lambda: post_preflight_failure_evidence(
                    preflight_result,
                    live=live,
                    failure_stage=(
                        CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION
                    ),
                ),
                lifecycle=lifecycle,
            )
        else:
            try:
                lifecycle.failure_stage = (
                    CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION
                )
                runner = CodexProcessRunner(
                    live=spec.live,
                    runner=spec.runner,
                    lifecycle=lifecycle,
                )
                lifecycle.mark_runner_started()
                codex_result = runner.run(
                    preflight=preflight_result,
                    prompt=prompt.content,
                    workspace=workspace.workspace,
                    environment_root=provider_environment_root,
                    parent_environment=parent_environment,
                )
                lifecycle.failure_stage = (
                    CodexFailureStage.PROVIDER_RUNNER_RESULT_EXTRACTION
                )
                codex = codex_result.evidence
            except UnsupportedRunnerPlatformError as error:
                platform_error = error
                codex = _strict_codex_evidence(
                    lambda: unsupported_platform_evidence(
                        platform_error,
                        preflight=preflight_result,
                        live=live,
                    ),
                    lifecycle=lifecycle,
                )
            except CodexRunnerError as error:
                if (
                    error.lifecycle.failure_stage
                    is CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION
                ):
                    raise _StrictConstructionFailure(
                        LiveDiagnosticCode.CODEX_EVIDENCE_VALIDATION_FAILED,
                        error.lifecycle,
                    ) from error
                codex = _strict_lifecycle_failure_evidence(
                    preflight_result,
                    spec=spec,
                    lifecycle=error.lifecycle,
                )
            except Exception:
                codex = _strict_lifecycle_failure_evidence(
                    preflight_result,
                    spec=spec,
                    lifecycle=lifecycle,
                )
        if codex.status is ProviderExecutionStatus.SUCCEEDED:
            gate_environment_root = _prepare_live_environment_root(
                workspace.environment_root,
                "gates",
            )
            gate_result = execute_quality_gates_in_workspace(
                spec,
                workspace=workspace.workspace,
                environment_root=gate_environment_root,
                temporary_root=workspace.temporary_root,
                execution_tracker=gate_tracker,
            )
            commands = gate_result.commands
            evaluation_duration_ms = gate_result.evaluation_duration_ms
        try:
            final_snapshot = snapshot_directory(workspace.workspace)
            diff = build_diff_evidence(
                workspace.initial_snapshot,
                final_snapshot,
                max_diff_bytes=spec.runner.max_diff_bytes,
            )
        except SnapshotError:
            diff = incomplete_diff_evidence("Live diff collection failed")
            failure_kind = LiveFailureKind.EVIDENCE_ERROR
    except _StrictConstructionFailure as error:
        diagnostic_failure = error
    except WorkspaceError as error:
        workspace_lifecycle = error.lifecycle or WorkspaceLifecycle.NOT_CREATED
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
        if codex is None:
            lifecycle.failure_stage = CodexFailureStage.WORKSPACE_PREPARATION
            try:
                codex = _strict_codex_evidence(
                    lambda: post_preflight_failure_evidence(
                        preflight_result,
                        live=live,
                        failure_stage=CodexFailureStage.WORKSPACE_PREPARATION,
                    ),
                    lifecycle=lifecycle,
                )
            except _StrictConstructionFailure as construction_error:
                diagnostic_failure = construction_error
    except LiveCodexError:
        raise
    except Exception:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
        if codex is None:
            lifecycle.failure_stage = CodexFailureStage.PROVIDER_ORCHESTRATION
            try:
                codex = _strict_lifecycle_failure_evidence(
                    preflight_result,
                    spec=spec,
                    lifecycle=lifecycle,
                )
            except _StrictConstructionFailure as construction_error:
                diagnostic_failure = construction_error
    finally:
        if workspace is not None:
            workspace_removed, _cleanup_error = remove_disposable_workspace(workspace)
            workspace_lifecycle = (
                WorkspaceLifecycle.REMOVED
                if workspace_removed
                else WorkspaceLifecycle.CLEANUP_FAILED
            )

    if diagnostic_failure is not None:
        _publish_failure_diagnostic_and_raise(
            diagnostic_path=diagnostic_path,
            recording_path=recording_path,
            output_path=output_path,
            run_id=run_id,
            experiment_id=spec.experiment_id,
            task_id=task_id,
            diagnostic_code=diagnostic_failure.diagnostic_code,
            lifecycle=diagnostic_failure.lifecycle,
            workspace_lifecycle=workspace_lifecycle,
            gate_executed=gate_tracker.gate_executed,
        )

    assert codex is not None
    provider_cleanup_failed = (
        codex.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    )
    if provider_cleanup_failed:
        failure_kind = LiveFailureKind.PROCESS_CLEANUP_ERROR
    elif workspace_lifecycle is WorkspaceLifecycle.CLEANUP_FAILED:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
    elif codex.status is ProviderExecutionStatus.FAILED and failure_kind is None:
        failure_kind = codex.failure_kind
    if (
        gate_result is not None
        and gate_result.harness_failure is not None
        and failure_kind is None
    ):
        failure_kind = _map_gate_harness(gate_result)
    if not diff.line_counts_complete and failure_kind is None:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR

    try:
        source_after = snapshot_directory(source)
        if (
            source_after.sha256 != source_snapshot.sha256
            and not provider_cleanup_failed
        ):
            failure_kind = LiveFailureKind.EVIDENCE_ERROR
    except SnapshotError:
        if not provider_cleanup_failed:
            failure_kind = LiveFailureKind.EVIDENCE_ERROR
    if (
        failure_kind is None
        and codex.status is ProviderExecutionStatus.SUCCEEDED
        and gate_result is not None
        and diff.line_counts_complete
        and workspace_lifecycle is WorkspaceLifecycle.REMOVED
    ):
        metrics = _gate_metrics(
            commands,
            diff,
            agent_duration_ms=codex.duration_ms,
            evaluation_duration_ms=evaluation_duration_ms,
            codex=codex,
        )
        if metrics.quality_gate_pass:
            overall_status = LiveOverallStatus.PASSED
            final_failure = LiveFailureKind.NONE
        else:
            overall_status = LiveOverallStatus.FAILED
            final_failure = LiveFailureKind.QUALITY_GATE_FAILURE
    else:
        final_failure = failure_kind or LiveFailureKind.EVIDENCE_ERROR
        if final_failure in {
            LiveFailureKind.PROVIDER_TURN_FAILED,
            LiveFailureKind.PROVIDER_CLI_NONZERO,
            LiveFailureKind.PROVIDER_SIGNAL_TERMINATION,
            LiveFailureKind.PROVIDER_TIMEOUT,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            LiveFailureKind.PROVIDER_SPAWN_ERROR,
            LiveFailureKind.PROVIDER_INPUT_ERROR,
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
        }:
            overall_status = LiveOverallStatus.PROVIDER_ERROR
        else:
            overall_status = LiveOverallStatus.HARNESS_ERROR

    completed_at = datetime.now(UTC)
    try:
        recording_bytes, artifact = _recording_and_artifact(
            loaded_spec=loaded_spec,
            task_id=task_id,
            repetition_index=repetition_index,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            source_snapshot=source_snapshot,
            prompt=prompt,
            codex=codex,
            commands=commands,
            diff=diff,
            metrics=metrics,
            workspace_lifecycle=workspace_lifecycle,
            evaluation_duration_ms=evaluation_duration_ms,
            overall_status=overall_status,
            failure_kind=final_failure,
            lifecycle=lifecycle,
        )
    except _StrictConstructionFailure as construction_error:
        _publish_failure_diagnostic_and_raise(
            diagnostic_path=diagnostic_path,
            recording_path=recording_path,
            output_path=output_path,
            run_id=run_id,
            experiment_id=spec.experiment_id,
            task_id=task_id,
            diagnostic_code=construction_error.diagnostic_code,
            lifecycle=construction_error.lifecycle,
            workspace_lifecycle=workspace_lifecycle,
            gate_executed=gate_tracker.gate_executed,
        )
    protect_live_inputs(
        spec_path=spec_path,
        prompt_path=prompt.path,
        fixture_source=source,
        fixture_snapshot=source_snapshot,
        recording_path=recording_path,
        output_path=output_path,
        diagnostic_path=diagnostic_path,
    )
    try:
        write_live_outputs(
            recording_bytes=recording_bytes,
            artifact=artifact,
            recording_path=recording_path,
            output_path=output_path,
            force=force,
        )
    except LiveCodexError:
        _publish_failure_diagnostic_and_raise(
            diagnostic_path=diagnostic_path,
            recording_path=recording_path,
            output_path=output_path,
            run_id=run_id,
            experiment_id=spec.experiment_id,
            task_id=task_id,
            diagnostic_code=LiveDiagnosticCode.PAIRED_OUTPUT_PUBLICATION_FAILED,
            lifecycle=lifecycle,
            workspace_lifecycle=workspace_lifecycle,
            gate_executed=gate_tracker.gate_executed,
        )
    return LiveCodexOutcome(artifact, recording_path, output_path)
