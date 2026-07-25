"""Strict JSONL contract and loader for Phase 1 Replay recordings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import Field, ValidationError, field_validator

from agentlab.models import ContractModel, Provider, RunMetrics, Workflow


class RecordingLoadError(ValueError):
    """Raised when a Replay recording cannot be read or validated."""


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
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


RecordingEvent = RunStartedEvent | RunCompletedEvent


@dataclass(frozen=True)
class ReplayRecording:
    """The only recording shape supported by Phase 1."""

    started: RunStartedEvent
    completed: RunCompletedEvent


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
    if event_type not in {"run_started", "run_completed"}:
        rendered_type = "missing" if event_type is None else repr(event_type)
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: unsupported event_type {rendered_type}"
        )

    try:
        if event_type == "run_started":
            return RunStartedEvent.model_validate(raw)
        return RunCompletedEvent.model_validate(raw)
    except ValidationError as error:
        raise RecordingLoadError(
            f"{path}: JSONL line {line_number}: invalid {event_type} event: {error}"
        ) from error


def _validate_recording(path: Path, events: list[RecordingEvent]) -> ReplayRecording:
    started_events = [event for event in events if isinstance(event, RunStartedEvent)]
    completed_events = [event for event in events if isinstance(event, RunCompletedEvent)]

    if len(started_events) != 1:
        raise RecordingLoadError(
            f"{path}: run_started must occur exactly once; found {len(started_events)}"
        )
    if len(completed_events) != 1:
        raise RecordingLoadError(
            f"{path}: run_completed must occur exactly once; found {len(completed_events)}"
        )
    if len(events) != 2:
        raise RecordingLoadError(f"{path}: Phase 1 recordings must contain exactly 2 events")
    if not isinstance(events[0], RunStartedEvent):
        raise RecordingLoadError(f"{path}: first event must be run_started")
    if not isinstance(events[-1], RunCompletedEvent):
        raise RecordingLoadError(f"{path}: last event must be run_completed")

    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise RecordingLoadError(
                f"{path}: sequence must be contiguous from 0; "
                f"expected {expected_sequence}, found {event.sequence}"
            )

    started = started_events[0]
    completed = completed_events[0]
    if started.run_id != completed.run_id:
        raise RecordingLoadError(f"{path}: run_id mismatch between recording events")
    if started.experiment_id != completed.experiment_id:
        raise RecordingLoadError(f"{path}: experiment_id mismatch between recording events")
    if completed.occurred_at < started.occurred_at:
        raise RecordingLoadError(f"{path}: run_completed occurred_at is before run_started")

    return ReplayRecording(started=started, completed=completed)


def load_replay_recording(path: Path) -> ReplayRecording:
    """Load and validate exactly one Phase 1 Replay recording."""
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
