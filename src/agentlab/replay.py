"""Minimal Replay Provider and single-recording Phase 1 orchestration."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from agentlab.models import (
    ComparisonAxis,
    ExecutionMode,
    ExperimentSpec,
    RunResult,
)
from agentlab.recording import ReplayRecording, load_replay_recording
from agentlab.specs import load_experiment_spec


class ReplayError(ValueError):
    """Raised when Replay inputs do not match or a result cannot be saved."""


class ReplayProvider:
    """Reconstruct a result candidate from already-recorded data only."""

    def create_result(self, recording: ReplayRecording) -> RunResult:
        started = recording.started
        completed = recording.completed
        if completed is None:
            assert recording.failed is not None
            raise ReplayError(
                "run_failed Recording cannot produce a RunResult because Metrics are absent "
                f"({recording.failed.failure_kind.value})"
            )
        return RunResult(
            schema_version="1.0",
            run_id=started.run_id,
            experiment_id=started.experiment_id,
            task_id=started.task_id,
            workflow=started.workflow,
            provider=started.provider,
            repetition_index=started.repetition_index,
            execution_mode=ExecutionMode.REPLAY,
            recorded_at=completed.occurred_at,
            metrics=completed.metrics,
        )


def resolve_recording_path(spec_path: Path, recording_path: str) -> Path:
    configured_path = Path(recording_path)
    if configured_path.is_absolute():
        return configured_path
    return spec_path.parent / configured_path


def _paths_refer_to_same_file(first: Path, second: Path) -> bool:
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
    except (OSError, RuntimeError):
        pass

    try:
        return first.samefile(second)
    except OSError:
        return False


def protect_replay_inputs(
    output_path: Path,
    spec_path: Path,
    recording_path: Path,
) -> None:
    for input_name, input_path in (
        ("ExperimentSpec", spec_path),
        ("Replay Recording", recording_path),
    ):
        if _paths_refer_to_same_file(output_path, input_path):
            raise ReplayError(
                f"output must not overwrite or alias the input {input_name}: {input_path}"
            )


def validate_recording_against_spec(
    spec: ExperimentSpec,
    recording: ReplayRecording,
) -> None:
    started = recording.started
    if spec.execution_mode is not ExecutionMode.REPLAY:
        raise ReplayError("execution_mode must be replay")
    if spec.replay is None:
        raise ReplayError("replay settings are required")
    if started.experiment_id != spec.experiment_id:
        raise ReplayError(
            f"experiment_id mismatch: spec={spec.experiment_id}, "
            f"recording={started.experiment_id}"
        )
    if started.task_id not in spec.task_ids:
        raise ReplayError(f"task_id {started.task_id!r} is not present in spec.task_ids")
    if not 0 <= started.repetition_index < spec.repetitions:
        raise ReplayError(
            f"repetition_index {started.repetition_index} is outside "
            f"the range [0, {spec.repetitions})"
        )

    allowed_values = {spec.control, *spec.treatments}
    if spec.comparison_axis is ComparisonAxis.WORKFLOW:
        if started.workflow.value not in allowed_values:
            raise ReplayError(
                f"workflow mismatch: recording={started.workflow.value}, "
                f"allowed={sorted(allowed_values)}"
            )
        if started.provider is not spec.provider:
            raise ReplayError(
                f"provider mismatch: spec={spec.provider.value}, "
                f"recording={started.provider.value}"
            )
    else:
        if started.provider.value not in allowed_values:
            raise ReplayError(
                f"provider mismatch: recording={started.provider.value}, "
                f"allowed={sorted(allowed_values)}"
            )
        if started.workflow is not spec.workflow:
            raise ReplayError(
                f"workflow mismatch: spec={spec.workflow.value}, "
                f"recording={started.workflow.value}"
            )


def _deterministic_json(result: RunResult) -> bytes:
    try:
        payload = json.dumps(
            result.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except ValueError as error:
        raise ReplayError(f"result contains a non-finite JSON number: {error}") from error
    return f"{payload}\n".encode()


def write_run_result(result: RunResult, output_path: Path, *, force: bool = False) -> None:
    if output_path.exists() and not force:
        raise ReplayError(f"output already exists: {output_path}; use --force to overwrite")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReplayError(
            f"could not create output directory for {output_path}: {error}"
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
            temporary_file.write(_deterministic_json(result))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if force:
            os.replace(temporary_path, output_path)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, output_path)
            except FileExistsError as error:
                raise ReplayError(
                    f"output already exists: {output_path}; use --force to overwrite"
                ) from error
    except ReplayError:
        raise
    except OSError as error:
        raise ReplayError(f"could not write result {output_path}: {error}") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def run_replay(
    spec_path: Path,
    output_path: Path,
    *,
    force: bool = False,
    provider: ReplayProvider | None = None,
) -> RunResult:
    """Generate and persist one result from one recording."""
    spec = load_experiment_spec(spec_path)
    if spec.execution_mode is not ExecutionMode.REPLAY:
        raise ReplayError("execution_mode must be replay")
    if spec.replay is None:
        raise ReplayError("replay settings are required")

    recording_path = resolve_recording_path(spec_path, spec.replay.recording_path)
    protect_replay_inputs(output_path, spec_path, recording_path)
    recording = load_replay_recording(recording_path)
    validate_recording_against_spec(spec, recording)
    result = (provider or ReplayProvider()).create_result(recording)
    write_run_result(result, output_path, force=force)
    return result
