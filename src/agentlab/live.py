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
from typing import Any, NoReturn

from pydantic import ValidationError

from agentlab.codex_provider import (
    CodexPreflight,
    CodexPreflightError,
    CodexProcessRunner,
    preflight_codex,
    preflight_failure_evidence,
    unsupported_platform_evidence,
)
from agentlab.gates import GateExecutionResult, execute_quality_gates_in_workspace
from agentlab.models import (
    CodexExecutionEvidence,
    CommandEvidence,
    CommandStatus,
    DiffEvidence,
    ExecutionMode,
    ExperimentSpec,
    GateKind,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    Provider,
    ProviderExecutionStatus,
    RunMetrics,
    Workflow,
)
from agentlab.recording import (
    LiveRunCompletedEvent,
    LiveRunFailedEvent,
    LiveRunStartedEvent,
    RecordingLoadError,
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

    def __init__(self, message: str, *, workspace_removed: bool | None = None) -> None:
        super().__init__(message)
        self.workspace_removed = workspace_removed


class LiveArtifactLoadError(ValueError):
    """A persisted Live Artifact is not strict UTF-8 JSON matching its contract."""


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
) -> None:
    """Protect Spec, Prompt, Fixture, and the two outputs from aliasing."""
    _ensure_existing_components_are_not_symlinks(recording_path, "Recording output")
    _ensure_existing_components_are_not_symlinks(output_path, "Evidence output")
    _ensure_output_is_not_symlink(recording_path, "Recording output")
    _ensure_output_is_not_symlink(output_path, "Evidence output")
    if paths_refer_to_same_file(recording_path, output_path):
        raise LiveCodexError("Recording and Evidence outputs must be different files")

    try:
        resolved_fixture = fixture_source.resolve(strict=True)
        resolved_outputs = (
            recording_path.resolve(strict=False),
            output_path.resolve(strict=False),
        )
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
    for output_label, output in (
        ("Recording", recording_path),
        ("Evidence", output_path),
    ):
        for input_label, input_path in protected_inputs:
            if paths_refer_to_same_file(output, input_path):
                raise LiveCodexError(
                    f"{output_label} output must not overwrite or alias {input_label}"
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
    workspace_removed: bool,
    overall_status: LiveOverallStatus,
    failure_kind: LiveFailureKind,
) -> tuple[bytes, LiveRunArtifact]:
    spec = loaded_spec.spec
    assert spec.live is not None
    assert spec.runner is not None
    assert spec.live.model is not None
    assert spec.live.reasoning_effort is not None
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
        terminal: LiveRunCompletedEvent | LiveRunFailedEvent = LiveRunCompletedEvent(
            schema_version="1.1",
            sequence=1,
            event_type="run_completed",
            run_id=run_id,
            experiment_id=spec.experiment_id,
            occurred_at=completed_at,
            metrics=metrics,
            codex=codex,
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
            metrics_included=False,
        )
    try:
        recording_bytes = live_recording_jsonl_bytes(started_event, terminal)
    except RecordingLoadError as error:
        raise LiveCodexError(str(error), workspace_removed=workspace_removed) from error
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
        workspace_removed=workspace_removed,
        recording_sha256=hashlib.sha256(recording_bytes).hexdigest(),
        raw_provider_output_persisted=False,
    )
    return recording_bytes, artifact


def _map_gate_harness(_gate_result: GateExecutionResult) -> LiveFailureKind:
    return LiveFailureKind.GATE_HARNESS_ERROR


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

    prompt = load_prompt(
        spec_path,
        spec.live.prompt_path,
        max_prompt_bytes=spec.live.max_prompt_bytes,
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
        spec.live.record_to,
        label="Recording output",
    )
    protect_live_inputs(
        spec_path=spec_path,
        prompt_path=prompt.path,
        fixture_source=source,
        fixture_snapshot=source_snapshot,
        recording_path=recording_path,
        output_path=output_path,
    )
    if not force:
        for label, path in (("Recording", recording_path), ("Evidence", output_path)):
            if os.path.lexists(path):
                raise LiveCodexError(
                    f"{label} output already exists: {path}; use --force to overwrite"
                )

    started_at = datetime.now(UTC)
    try:
        preflight_result = preflight(parent_environment=parent_environment)
    except CodexPreflightError as error:
        try:
            preflight_codex_evidence = preflight_failure_evidence(error, live=spec.live)
        except ValidationError as validation_error:
            raise LiveCodexError(
                "could not construct strict Codex preflight Evidence"
            ) from validation_error
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
                workspace_removed=True,
                overall_status=LiveOverallStatus.PROVIDER_ERROR,
                failure_kind=error.failure_kind,
            )
        except ValidationError as validation_error:
            raise LiveCodexError(
                "could not construct strict Live failure Evidence",
                workspace_removed=True,
            ) from validation_error
        protect_live_inputs(
            spec_path=spec_path,
            prompt_path=prompt.path,
            fixture_source=source,
            fixture_snapshot=source_snapshot,
            recording_path=recording_path,
            output_path=output_path,
        )
        write_live_outputs(
            recording_bytes=recording_bytes,
            artifact=artifact,
            recording_path=recording_path,
            output_path=output_path,
            force=force,
        )
        return LiveCodexOutcome(artifact, recording_path, output_path)

    commands: list[CommandEvidence] = []
    evaluation_duration_ms = 0
    metrics: RunMetrics | None = None
    diff = incomplete_diff_evidence("Live diff collection did not complete")
    workspace_removed = False
    workspace = None
    gate_result: GateExecutionResult | None = None
    codex: CodexExecutionEvidence
    failure_kind: LiveFailureKind | None = None
    try:
        workspace = prepare_disposable_workspace(source, source_snapshot)
        try:
            provider_environment_root = _prepare_live_environment_root(
                workspace.environment_root,
                "provider",
            )
            codex_result = CodexProcessRunner(
                live=spec.live,
                runner=spec.runner,
            ).run(
                preflight=preflight_result,
                prompt=prompt.content,
                workspace=workspace.workspace,
                environment_root=provider_environment_root,
                parent_environment=parent_environment,
            )
            codex = codex_result.evidence
        except UnsupportedRunnerPlatformError as error:
            codex = unsupported_platform_evidence(
                error,
                preflight=preflight_result,
                live=spec.live,
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
    except WorkspaceError as error:
        raise LiveCodexError(
            str(error),
            workspace_removed=None,
        ) from error
    except Exception:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
        if "codex" not in locals():
            synthetic_error = CodexPreflightError(
                LiveFailureKind.EVIDENCE_ERROR,
                "Live workspace execution failed",
                checked_at=preflight_result.checked_at,
                cli_version=preflight_result.cli_version,
                verified_flags=preflight_result.verified_flags,
            )
            codex = preflight_failure_evidence(synthetic_error, live=spec.live)
    finally:
        if workspace is not None:
            workspace_removed, _cleanup_error = remove_disposable_workspace(workspace)

    if not workspace_removed:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
    if codex.status is ProviderExecutionStatus.FAILED and failure_kind is None:
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
        if source_after.sha256 != source_snapshot.sha256:
            failure_kind = LiveFailureKind.EVIDENCE_ERROR
    except SnapshotError:
        failure_kind = LiveFailureKind.EVIDENCE_ERROR
    if (
        failure_kind is None
        and codex.status is ProviderExecutionStatus.SUCCEEDED
        and gate_result is not None
        and diff.line_counts_complete
        and workspace_removed
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
            workspace_removed=workspace_removed,
            overall_status=overall_status,
            failure_kind=final_failure,
        )
    except ValidationError as error:
        raise LiveCodexError(
            "could not construct strict Live Evidence",
            workspace_removed=workspace_removed,
        ) from error
    protect_live_inputs(
        spec_path=spec_path,
        prompt_path=prompt.path,
        fixture_source=source,
        fixture_snapshot=source_snapshot,
        recording_path=recording_path,
        output_path=output_path,
    )
    try:
        write_live_outputs(
            recording_bytes=recording_bytes,
            artifact=artifact,
            recording_path=recording_path,
            output_path=output_path,
            force=force,
        )
    except LiveCodexError as error:
        raise LiveCodexError(
            str(error),
            workspace_removed=workspace_removed,
        ) from error
    return LiveCodexOutcome(artifact, recording_path, output_path)
