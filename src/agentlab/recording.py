"""Strict JSONL contract and loader for Phase 1 Replay recordings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.models import (
    CodexExecutionEvidence,
    ContractModel,
    ExecutionMode,
    LiveEvaluationSummary,
    LiveFailureKind,
    Provider,
    ProviderExecutionStatus,
    ReasoningEffort,
    RunMetrics,
    Workflow,
    WorkspaceLifecycle,
)


class RecordingLoadError(ValueError):
    """Raised when a Replay recording cannot be read or validated."""


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _timezone_aware_utc(value: datetime, field_name: str) -> datetime:
    _timezone_aware(value, field_name)
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")
    return value


class RunStartedEvent(ContractModel):
    schema_version: Literal["1.0"]
    sequence: int = Field(ge=0, strict=True)
    event_type: Literal["run_started"]
    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    workflow: Workflow
    provider: Provider
    repetition_index: int = Field(strict=True)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_aware(value, "occurred_at")


class RunCompletedEvent(ContractModel):
    schema_version: Literal["1.0"]
    sequence: int = Field(ge=0, strict=True)
    event_type: Literal["run_completed"]
    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    occurred_at: datetime
    metrics: RunMetrics

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_aware(value, "occurred_at")


class LiveRunStartedEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: StrictInt = Field(ge=0)
    event_type: Literal["run_started"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    workflow: Literal[Workflow.ONE_SHOT]
    provider: Literal[Provider.CODEX]
    repetition_index: StrictInt = Field(ge=0)
    execution_mode: Literal[ExecutionMode.LIVE]
    occurred_at: datetime
    prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: StrictBool
    requested_model: StrictStr = Field(min_length=1)
    requested_reasoning_effort: ReasoningEffort
    cli_version: StrictStr | None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_must_be_datetime_or_iso_string(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("occurred_at must be an ISO string or datetime")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "occurred_at")

    @model_validator(mode="after")
    def prompt_must_be_redacted(self) -> LiveRunStartedEvent:
        if not self.prompt_redacted:
            raise ValueError("prompt_redacted must be true")
        return self


class LiveRunCompletedEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: StrictInt = Field(ge=0)
    event_type: Literal["run_completed"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    occurred_at: datetime
    metrics: RunMetrics
    codex: CodexExecutionEvidence
    evaluation: LiveEvaluationSummary

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_must_be_datetime_or_iso_string(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("occurred_at must be an ISO string or datetime")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "occurred_at")

    @model_validator(mode="after")
    def metrics_must_match_codex_summary(self) -> LiveRunCompletedEvent:
        if self.codex.status is not ProviderExecutionStatus.SUCCEEDED:
            raise ValueError("run_completed requires successful Codex Evidence")
        if (
            self.metrics.agent_duration_ms != self.codex.duration_ms
            or self.metrics.total_duration_ms
            != self.metrics.agent_duration_ms + self.metrics.evaluation_duration_ms
            or self.metrics.agent_call_count != 1
            or self.metrics.retry_count != 0
            or self.metrics.usage_metrics != self.codex.usage_metrics
        ):
            raise ValueError("run_completed Metrics do not match Codex Evidence")
        expected_counts = (
            self.evaluation.acceptance.passed_count,
            self.evaluation.acceptance.command_count,
            self.evaluation.regression.failed_count,
            self.evaluation.lint.failed_count,
            self.evaluation.typecheck.failed_count,
        )
        actual_counts = (
            self.metrics.acceptance_tests_passed,
            self.metrics.acceptance_tests_total,
            self.metrics.regression_failures,
            self.metrics.lint_errors,
            self.metrics.typecheck_errors,
        )
        quality_gate_pass = all(
            summary.failed_count == 0
            for summary in (
                self.evaluation.acceptance,
                self.evaluation.regression,
                self.evaluation.lint,
                self.evaluation.typecheck,
            )
        )
        if (
            not self.evaluation.all_commands_completed_normally
            or not self.evaluation.diff_line_counts_complete
            or self.evaluation.workspace_lifecycle is not WorkspaceLifecycle.REMOVED
            or self.metrics.quality_gate_pass is not quality_gate_pass
            or actual_counts != expected_counts
            or self.metrics.evaluation_duration_ms
            != self.evaluation.evaluation_duration_ms
            or self.metrics.changed_files != self.evaluation.changed_files
            or self.metrics.added_lines != self.evaluation.added_lines
            or self.metrics.deleted_lines != self.evaluation.deleted_lines
        ):
            raise ValueError("run_completed Metrics do not match evaluation summary")
        return self


class LiveRunFailedEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: StrictInt = Field(ge=0)
    event_type: Literal["run_failed"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    occurred_at: datetime
    failure_kind: LiveFailureKind
    codex: CodexExecutionEvidence
    evaluation: LiveEvaluationSummary
    metrics_included: StrictBool = False

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_must_be_datetime_or_iso_string(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("occurred_at must be an ISO string or datetime")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "occurred_at")

    @model_validator(mode="after")
    def failure_must_match_codex_summary(self) -> LiveRunFailedEvent:
        if self.metrics_included:
            raise ValueError("run_failed must not include Metrics")
        provider_failures = {
            LiveFailureKind.PROVIDER_TURN_FAILED,
            LiveFailureKind.PROVIDER_CLI_NONZERO,
            LiveFailureKind.PROVIDER_SIGNAL_TERMINATION,
            LiveFailureKind.PROVIDER_TIMEOUT,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            LiveFailureKind.PROVIDER_SPAWN_ERROR,
            LiveFailureKind.PROVIDER_INPUT_ERROR,
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        provider_harness_failures = {
            LiveFailureKind.PROCESS_CLEANUP_ERROR,
            LiveFailureKind.UNSUPPORTED_PLATFORM,
        }
        gate_command_count = sum(
            summary.command_count
            for summary in (
                self.evaluation.acceptance,
                self.evaluation.regression,
                self.evaluation.lint,
                self.evaluation.typecheck,
            )
        )
        if self.failure_kind in {
            LiveFailureKind.NONE,
            LiveFailureKind.QUALITY_GATE_FAILURE,
        }:
            raise ValueError("run_failed requires a non-quality failure kind")
        if self.failure_kind in provider_failures | provider_harness_failures and (
            self.codex.status is not ProviderExecutionStatus.FAILED
            or self.codex.failure_kind is not self.failure_kind
        ):
            raise ValueError("run_failed failure kind must match Codex Evidence")
        if self.failure_kind in provider_failures | provider_harness_failures and (
            gate_command_count != 0
        ):
            raise ValueError("Provider failure Recording must not contain Gate execution")
        if (
            self.failure_kind is LiveFailureKind.GATE_HARNESS_ERROR
            and self.codex.status is not ProviderExecutionStatus.SUCCEEDED
        ):
            raise ValueError("Gate Harness failure requires successful Codex Evidence")
        if (
            self.failure_kind is LiveFailureKind.GATE_HARNESS_ERROR
            and self.evaluation.all_commands_completed_normally
        ):
            raise ValueError("Gate Harness failure requires abnormal Gate summary")
        return self


RecordingEvent = (
    RunStartedEvent
    | RunCompletedEvent
    | LiveRunStartedEvent
    | LiveRunCompletedEvent
    | LiveRunFailedEvent
)
RecordingStartedEvent = RunStartedEvent | LiveRunStartedEvent
RecordingCompletedEvent = RunCompletedEvent | LiveRunCompletedEvent


@dataclass(frozen=True)
class ReplayRecording:
    """A strict two-event Recording supported by offline Replay."""

    started: RecordingStartedEvent
    completed: RecordingCompletedEvent | None
    failed: LiveRunFailedEvent | None = None


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _NonFiniteNumberError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> NoReturn:
    raise _NonFiniteNumberError(value)


def _parse_event(path: Path, line_number: int, line: str) -> RecordingEvent:
    try:
        raw = json.loads(
            line,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite_number,
        )
    except json.JSONDecodeError as error:
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: invalid JSON: {error.msg}"
        ) from error
    except _DuplicateKeyError as error:
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: duplicate JSON key {error.key!r}"
        ) from error
    except _NonFiniteNumberError as error:
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: non-finite JSON number {error}"
        ) from error

    if not isinstance(raw, dict):
        raise RecordingLoadError(f"{path}: JSONL line {line_number}: event must be a JSON object")

    event_type = raw.get("event_type")
    schema_version = raw.get("schema_version")
    if event_type not in {"run_started", "run_completed", "run_failed"}:
        rendered_type = "missing" if event_type is None else repr(event_type)
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: unsupported event_type {rendered_type}"
        )

    try:
        if schema_version == "1.0":
            if event_type == "run_started":
                return RunStartedEvent.model_validate(raw)
            if event_type == "run_completed":
                return RunCompletedEvent.model_validate(raw)
            raise RecordingLoadError(
                f"{path}: JSONL line {line_number}: run_failed requires schema_version 1.1"
            )
        if schema_version == "1.1":
            if event_type == "run_started":
                return LiveRunStartedEvent.model_validate(raw)
            if event_type == "run_completed":
                return LiveRunCompletedEvent.model_validate(raw)
            return LiveRunFailedEvent.model_validate(raw)
        rendered_version = "missing" if schema_version is None else repr(schema_version)
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: unsupported schema_version {rendered_version}"
        )
    except ValidationError as error:
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: invalid {event_type} event: {error}"
        ) from error


def _validate_recording(path: Path, events: list[RecordingEvent]) -> ReplayRecording:
    started_events = [
        event
        for event in events
        if isinstance(event, (RunStartedEvent, LiveRunStartedEvent))
    ]
    completed_events = [
        event
        for event in events
        if isinstance(event, (RunCompletedEvent, LiveRunCompletedEvent))
    ]
    failed_events = [event for event in events if isinstance(event, LiveRunFailedEvent)]

    if len(started_events) != 1:
        raise RecordingLoadError(
            f"{path}: run_started must occur exactly once; found {len(started_events)}"
        )
    if len(completed_events) + len(failed_events) != 1:
        raise RecordingLoadError(
            f"{path}: exactly one run_completed or run_failed event is required"
        )
    if len(events) != 2:
        raise RecordingLoadError(f"{path}: recordings must contain exactly 2 events")
    if not isinstance(events[0], (RunStartedEvent, LiveRunStartedEvent)):
        raise RecordingLoadError(f"{path}: first event must be run_started")
    if not isinstance(
        events[-1],
        (RunCompletedEvent, LiveRunCompletedEvent, LiveRunFailedEvent),
    ):
        raise RecordingLoadError(f"{path}: last event must be terminal")

    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise RecordingLoadError(
                f"{path}: sequence must be contiguous from 0; "
                f"expected {expected_sequence}, found {event.sequence}"
            )

    started = started_events[0]
    terminal = completed_events[0] if completed_events else failed_events[0]
    if started.run_id != terminal.run_id:
        raise RecordingLoadError(f"{path}: run_id mismatch between recording events")
    if started.experiment_id != terminal.experiment_id:
        raise RecordingLoadError(f"{path}: experiment_id mismatch between recording events")
    if terminal.occurred_at < started.occurred_at:
        raise RecordingLoadError(f"{path}: terminal occurred_at is before run_started")
    if isinstance(started, RunStartedEvent):
        if not isinstance(terminal, RunCompletedEvent):
            raise RecordingLoadError(f"{path}: schema 1.0 requires run_completed")
    elif not isinstance(terminal, (LiveRunCompletedEvent, LiveRunFailedEvent)):
        raise RecordingLoadError(f"{path}: schema 1.1 requires a Live terminal event")
    else:
        codex = terminal.codex
        if started.requested_model != codex.requested_model:
            raise RecordingLoadError(
                f"{path}: requested model mismatch between Live recording events"
            )
        if started.requested_reasoning_effort is not codex.requested_reasoning_effort:
            raise RecordingLoadError(
                f"{path}: reasoning effort mismatch between Live recording events"
            )
        if started.cli_version != codex.cli_version:
            raise RecordingLoadError(
                f"{path}: CLI version mismatch between Live recording events"
            )
        if codex.started_at < started.occurred_at:
            raise RecordingLoadError(
                f"{path}: Codex process started before the Live run"
            )
        if terminal.occurred_at < codex.completed_at:
            raise RecordingLoadError(
                f"{path}: Live terminal time precedes Codex completion"
            )

    completed = (
        terminal
        if isinstance(terminal, (RunCompletedEvent, LiveRunCompletedEvent))
        else None
    )
    failed = terminal if isinstance(terminal, LiveRunFailedEvent) else None
    return ReplayRecording(started=started, completed=completed, failed=failed)


def load_replay_recording(path: Path) -> ReplayRecording:
    """Load one strict Phase 1 or redacted Phase 3 Replay recording."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise RecordingLoadError(f"{path}: could not read UTF-8 recording: {error}") from error

    lines = text.splitlines()
    events: list[RecordingEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise RecordingLoadError(
                f"{path}: JSONL line {line_number}: empty lines are not allowed"
            )
        events.append(_parse_event(path, line_number, line))

    return _validate_recording(path, events)


def live_recording_jsonl_bytes(
    started: LiveRunStartedEvent,
    terminal: LiveRunCompletedEvent | LiveRunFailedEvent,
) -> bytes:
    """Serialize the two redacted Live events deterministically."""
    try:
        lines = [
            json.dumps(
                event.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for event in (started, terminal)
        ]
    except ValueError as error:
        raise RecordingLoadError(
            f"Live Recording contains a non-finite JSON number: {error}"
        ) from error
    return f"{lines[0]}\n{lines[1]}\n".encode()
