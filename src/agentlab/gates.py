"""Phase 2 quality-gate orchestration and atomic Evidence persistence."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError

from agentlab.models import (
    CommandEvidence,
    CommandStatus,
    DiffEvidence,
    EvidenceArtifact,
    EvidenceOverallStatus,
    ExperimentSpec,
    FailureKind,
    GateKind,
    RunMetrics,
)
from agentlab.replay import resolve_recording_path
from agentlab.runner import (
    LocalCommandRunner,
    UnsupportedRunnerPlatformError,
    ensure_runner_platform_supported,
)
from agentlab.specs import load_experiment_spec_document
from agentlab.workspace import (
    DirectorySnapshot,
    SnapshotError,
    WorkspaceError,
    build_diff_evidence,
    incomplete_diff_evidence,
    prepare_disposable_workspace,
    protect_evidence_inputs,
    remove_disposable_workspace,
    snapshot_directory,
    validate_fixture_source,
)


class RunGatesError(ValueError):
    """Expected input, execution, or persistence failure at the CLI boundary."""

    def __init__(self, message: str, *, workspace_removed: bool | None = None) -> None:
        super().__init__(message)
        self.workspace_removed = workspace_removed


class EvidenceLoadError(ValueError):
    """Raised when persisted Evidence is not strict UTF-8 JSON matching the contract."""


@dataclass(frozen=True)
class GateRunOutcome:
    artifact: EvidenceArtifact
    output_path: Path


@dataclass(frozen=True)
class GateExecutionResult:
    commands: list[CommandEvidence]
    evaluation_duration_ms: int
    harness_failure: FailureKind | None
    harness_detail: str | None


class _DuplicateEvidenceKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateEvidenceKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def load_evidence_artifact(path: Path) -> EvidenceArtifact:
    """Strictly reload a Phase 2 Evidence Artifact."""
    try:
        text = path.read_bytes().decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateEvidenceKeyError as error:
        raise EvidenceLoadError(f"{path}: duplicate JSON key {error}") from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise EvidenceLoadError(f"{path}: could not read strict Evidence JSON: {error}") from error
    if not isinstance(raw, dict):
        raise EvidenceLoadError(f"{path}: Evidence must be a JSON object")
    try:
        return EvidenceArtifact.model_validate(raw)
    except ValidationError as error:
        raise EvidenceLoadError(f"{path}: invalid Evidence: {error}") from error


def _evidence_json_bytes(artifact: EvidenceArtifact) -> bytes:
    try:
        payload = json.dumps(
            artifact.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except ValueError as error:
        raise RunGatesError(f"Evidence contains a non-finite JSON number: {error}") from error
    return f"{payload}\n".encode()


def write_evidence_artifact(
    artifact: EvidenceArtifact,
    output_path: Path,
    *,
    force: bool = False,
) -> None:
    """Atomically create or explicitly replace a completed Evidence JSON file."""
    if os.path.lexists(output_path) and not force:
        raise RunGatesError(
            f"Evidence output already exists: {output_path}; use --force to overwrite"
        )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RunGatesError(
            f"could not create Evidence output directory: {type(error).__name__}"
        ) from error

    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(_evidence_json_bytes(artifact))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if force:
            os.replace(temporary_path, output_path)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, output_path)
            except FileExistsError as error:
                raise RunGatesError(
                    f"Evidence output already exists: {output_path}; "
                    "use --force to overwrite"
                ) from error
    except RunGatesError:
        raise
    except OSError as error:
        raise RunGatesError(
            f"could not write Evidence {output_path}: {type(error).__name__}"
        ) from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _append_harness_detail(existing: str | None, detail: str) -> str:
    return detail if existing is None else f"{existing}; {detail}"


def _metrics_from_evidence(
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    *,
    evaluation_duration_ms: int,
) -> RunMetrics:
    acceptance = [command for command in commands if command.gate is GateKind.ACCEPTANCE]
    regression = [command for command in commands if command.gate is GateKind.REGRESSION]
    lint = [command for command in commands if command.gate is GateKind.LINT]
    typecheck = [command for command in commands if command.gate is GateKind.TYPECHECK]
    assert diff.added_lines is not None
    assert diff.deleted_lines is not None
    return RunMetrics(
        quality_gate_pass=all(command.status is CommandStatus.PASSED for command in commands),
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
        agent_duration_ms=0,
        evaluation_duration_ms=evaluation_duration_ms,
        total_duration_ms=evaluation_duration_ms,
        agent_call_count=0,
        retry_count=0,
        changed_files=diff.changed_files,
        added_lines=diff.added_lines,
        deleted_lines=diff.deleted_lines,
        usage_metrics=None,
    )


def _command_groups(spec: ExperimentSpec) -> tuple[tuple[GateKind, list[list[str]]], ...]:
    return (
        (GateKind.ACCEPTANCE, spec.quality_gate.acceptance),
        (GateKind.REGRESSION, spec.quality_gate.regression),
        (GateKind.LINT, spec.quality_gate.lint),
        (GateKind.TYPECHECK, spec.quality_gate.typecheck),
    )


def execute_quality_gates_in_workspace(
    spec: ExperimentSpec,
    *,
    workspace: Path,
    environment_root: Path,
    temporary_root: Path,
) -> GateExecutionResult:
    """Run only Phase 2 Gate argv inside an already-prepared disposable Workspace."""
    assert spec.runner is not None
    ensure_runner_platform_supported()
    local_runner = LocalCommandRunner(spec.runner)
    commands: list[CommandEvidence] = []
    harness_failure: FailureKind | None = None
    harness_detail: str | None = None
    gate_started = time.monotonic()
    try:
        should_stop = False
        for gate, configured_commands in _command_groups(spec):
            for command_index, argv in enumerate(configured_commands):
                result = local_runner.run(
                    gate=gate,
                    command_index=command_index,
                    argv=argv,
                    workspace=workspace,
                    environment_root=environment_root,
                    temporary_root=temporary_root,
                )
                commands.append(result.evidence)
                if result.harness_failure is not None:
                    harness_failure = result.harness_failure
                    harness_detail = (
                        result.evidence.error
                        or result.evidence.termination.error
                        or result.harness_failure.value
                    )
                    should_stop = True
                    break
            if should_stop:
                break
    finally:
        evaluation_duration_ms = max(
            0,
            int((time.monotonic() - gate_started) * 1000),
        )
    return GateExecutionResult(
        commands=commands,
        evaluation_duration_ms=evaluation_duration_ms,
        harness_failure=harness_failure,
        harness_detail=harness_detail,
    )


def _execute_in_workspace(
    *,
    spec: ExperimentSpec,
    spec_hash: str,
    source: Path,
    source_snapshot: DirectorySnapshot,
    run_id: str,
    task_id: str,
) -> EvidenceArtifact:
    assert spec.runner is not None
    started_at = datetime.now(UTC)
    workspace = prepare_disposable_workspace(source, source_snapshot)
    commands: list[CommandEvidence] = []
    harness_failure: FailureKind | None = None
    harness_detail: str | None = None
    evaluation_duration_ms = 0
    diff = incomplete_diff_evidence("diff collection did not complete")
    workspace_removed = False
    cleanup_error: str | None = None

    try:
        try:
            ensure_runner_platform_supported()
        except UnsupportedRunnerPlatformError as error:
            harness_failure = FailureKind.UNSUPPORTED_PLATFORM
            harness_detail = str(error)
        else:
            gate_result = execute_quality_gates_in_workspace(
                spec,
                workspace=workspace.workspace,
                environment_root=workspace.environment_root,
                temporary_root=workspace.temporary_root,
            )
            commands = gate_result.commands
            evaluation_duration_ms = gate_result.evaluation_duration_ms
            harness_failure = gate_result.harness_failure
            harness_detail = gate_result.harness_detail

        try:
            final_snapshot = snapshot_directory(workspace.workspace)
            diff = build_diff_evidence(
                workspace.initial_snapshot,
                final_snapshot,
                max_diff_bytes=spec.runner.max_diff_bytes,
            )
        except SnapshotError as error:
            diff = incomplete_diff_evidence(f"diff collection failed: {error}")
            if harness_failure is None:
                harness_failure = FailureKind.EVIDENCE_ERROR
            harness_detail = _append_harness_detail(
                harness_detail,
                f"diff collection failed: {type(error).__name__}",
            )
    except Exception as error:
        diff = incomplete_diff_evidence(
            f"runner Evidence collection failed: {type(error).__name__}"
        )
        if harness_failure is None:
            harness_failure = FailureKind.EVIDENCE_ERROR
        harness_detail = _append_harness_detail(
            harness_detail,
            f"runner Evidence collection failed: {type(error).__name__}",
        )
    finally:
        workspace_removed, cleanup_error = remove_disposable_workspace(workspace)

    if not workspace_removed:
        if harness_failure is None:
            harness_failure = FailureKind.EVIDENCE_ERROR
        harness_detail = _append_harness_detail(
            harness_detail,
            cleanup_error or "temporary workspace cleanup failed",
        )

    if not diff.line_counts_complete and harness_failure is None:
        harness_failure = FailureKind.EVIDENCE_ERROR
        harness_detail = "changed binary or non-UTF-8 files made line counts incomplete"

    try:
        source_after = snapshot_directory(source)
        if source_after.sha256 != source_snapshot.sha256:
            if harness_failure is None:
                harness_failure = FailureKind.EVIDENCE_ERROR
            harness_detail = _append_harness_detail(
                harness_detail,
                "fixture source changed while gates were running",
            )
    except SnapshotError as error:
        if harness_failure is None:
            harness_failure = FailureKind.EVIDENCE_ERROR
        harness_detail = _append_harness_detail(
            harness_detail,
            f"could not verify unchanged fixture source: {type(error).__name__}",
        )

    if harness_failure is not None:
        overall_status = EvidenceOverallStatus.HARNESS_ERROR
        failure_kind = harness_failure
        metrics = None
    else:
        quality_gate_pass = all(
            command.status is CommandStatus.PASSED for command in commands
        )
        if quality_gate_pass:
            overall_status = EvidenceOverallStatus.PASSED
            failure_kind = FailureKind.NONE
        else:
            overall_status = EvidenceOverallStatus.FAILED
            failure_kind = FailureKind.QUALITY_GATE_FAILURE
        metrics = _metrics_from_evidence(
            commands,
            diff,
            evaluation_duration_ms=evaluation_duration_ms,
        )

    return EvidenceArtifact(
        schema_version="1.0",
        run_id=run_id,
        experiment_id=spec.experiment_id,
        task_id=task_id,
        overall_status=overall_status,
        failure_kind=failure_kind,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        spec_sha256=spec_hash,
        fixture_sha256=source_snapshot.sha256,
        runner=spec.runner,
        commands=commands,
        diff=diff,
        metrics=metrics,
        workspace_removed=workspace_removed,
        harness_error=harness_detail if harness_failure is not None else None,
    )


def run_gates(
    spec_path: Path,
    *,
    task_id: str,
    run_id: str,
    output_path: Path,
    confirm_execution: bool,
    force: bool = False,
) -> GateRunOutcome:
    """Run configured gates once and persist Evidence without invoking external AI."""
    if not confirm_execution:
        raise RunGatesError(
            "quality-gate execution requires --confirm-execution; no command was started"
        )
    if not task_id:
        raise RunGatesError("task_id must not be empty")
    if not run_id:
        raise RunGatesError("run_id must not be empty")

    loaded_spec = load_experiment_spec_document(spec_path)
    spec = loaded_spec.spec
    if spec.runner is None:
        raise RunGatesError("ExperimentSpec.runner is required for run-gates")
    if task_id not in spec.task_ids:
        raise RunGatesError(f"task_id {task_id!r} is not present in spec.task_ids")
    if os.path.lexists(output_path) and not force:
        raise RunGatesError(
            f"Evidence output already exists: {output_path}; use --force to overwrite"
        )

    try:
        source, source_snapshot = validate_fixture_source(
            spec_path,
            spec.runner.fixture_path,
        )
        recording_path = (
            resolve_recording_path(spec_path, spec.replay.recording_path)
            if spec.replay is not None
            else None
        )
        protect_evidence_inputs(
            output_path,
            spec_path=spec_path,
            recording_path=recording_path,
            fixture_source=source,
            fixture_snapshot=source_snapshot,
        )
    except WorkspaceError as error:
        raise RunGatesError(str(error)) from error

    try:
        artifact = _execute_in_workspace(
            spec=spec,
            spec_hash=loaded_spec.sha256,
            source=source,
            source_snapshot=source_snapshot,
            run_id=run_id,
            task_id=task_id,
        )
    except WorkspaceError as error:
        raise RunGatesError(str(error)) from error

    try:
        # Re-check after execution so --force cannot replace an input alias
        # introduced while a longer-running gate was in progress.
        protect_evidence_inputs(
            output_path,
            spec_path=spec_path,
            recording_path=recording_path,
            fixture_source=source,
            fixture_snapshot=source_snapshot,
        )
        write_evidence_artifact(artifact, output_path, force=force)
    except WorkspaceError as error:
        raise RunGatesError(
            str(error),
            workspace_removed=artifact.workspace_removed,
        ) from error
    except RunGatesError as error:
        raise RunGatesError(
            str(error),
            workspace_removed=artifact.workspace_removed,
        ) from error

    return GateRunOutcome(artifact=artifact, output_path=output_path)
