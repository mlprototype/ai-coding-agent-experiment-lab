from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agentlab.recording import RecordingLoadError, load_replay_recording


def _metrics() -> dict[str, Any]:
    return {
        "quality_gate_pass": True,
        "acceptance_tests_passed": 1,
        "acceptance_tests_total": 1,
        "regression_failures": 0,
        "lint_errors": 0,
        "typecheck_errors": 0,
        "agent_duration_ms": 750,
        "evaluation_duration_ms": 250,
        "total_duration_ms": 1000,
        "agent_call_count": 1,
        "retry_count": 0,
        "changed_files": ["src/example.py"],
        "added_lines": 5,
        "deleted_lines": 1,
        "usage_metrics": {
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "estimated_api_cost": None,
            "quota_consumption": None,
            "source": None,
        },
    }


def _events() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": "1.0",
            "sequence": 0,
            "event_type": "run_started",
            "run_id": "run-001",
            "experiment_id": "workflow-smoke",
            "task_id": "smoke-task",
            "workflow": "one_shot",
            "provider": "replay",
            "repetition_index": 0,
            "occurred_at": "2026-07-25T09:00:00Z",
        },
        {
            "schema_version": "1.0",
            "sequence": 1,
            "event_type": "run_completed",
            "run_id": "run-001",
            "experiment_id": "workflow-smoke",
            "occurred_at": "2026-07-25T09:00:01Z",
            "metrics": _metrics(),
        },
    ]


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    content = "\n".join(json.dumps(event) for event in events)
    path.write_text(f"{content}\n", encoding="utf-8")


def test_loads_valid_jsonl_recording_with_null_usage_metrics(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    _write_events(path, _events())

    recording = load_replay_recording(path)

    assert recording.started.run_id == "run-001"
    assert recording.completed.metrics.usage_metrics is not None
    assert recording.completed.metrics.usage_metrics.input_tokens is None
    assert recording.completed.metrics.usage_metrics.estimated_api_cost is None


def test_invalid_json_reports_filename_and_line_number(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    first_event = json.dumps(_events()[0])
    path.write_text(f"{first_event}\n{{broken\n", encoding="utf-8")

    with pytest.raises(RecordingLoadError) as error:
        load_replay_recording(path)

    message = str(error.value)
    assert path.name in message
    assert "line 2" in message
    assert "invalid JSON" in message


def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.jsonl"
    path.write_bytes(b"\xff")

    with pytest.raises(RecordingLoadError, match="UTF-8"):
        load_replay_recording(path)


def test_rejects_empty_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "empty-line.jsonl"
    events = _events()
    path.write_text(
        f"{json.dumps(events[0])}\n\n{json.dumps(events[1])}\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="empty lines"):
        load_replay_recording(path)


@pytest.mark.parametrize("json_value", [[], "text", 1, None])
def test_rejects_json_values_that_are_not_objects(tmp_path: Path, json_value: object) -> None:
    path = tmp_path / "non-object.jsonl"
    path.write_text(f"{json.dumps(json_value)}\n", encoding="utf-8")

    with pytest.raises(RecordingLoadError, match="JSON object"):
        load_replay_recording(path)


def test_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "schema.jsonl"
    events = _events()
    events[0]["schema_version"] = "2.0"
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="schema_version"):
        load_replay_recording(path)


def test_rejects_unknown_event_field(tmp_path: Path) -> None:
    path = tmp_path / "unknown-field.jsonl"
    events = _events()
    events[0]["future_field"] = True
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="future_field"):
        load_replay_recording(path)


def test_rejects_unsupported_event_type(tmp_path: Path) -> None:
    path = tmp_path / "event-type.jsonl"
    events = _events()
    events[0]["event_type"] = "tool_called"
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="unsupported event_type"):
        load_replay_recording(path)


@pytest.mark.parametrize("sequences", [(0, 0), (0, 2), (1, 0)])
def test_rejects_non_contiguous_sequences(
    tmp_path: Path,
    sequences: tuple[int, int],
) -> None:
    path = tmp_path / "sequence.jsonl"
    events = _events()
    events[0]["sequence"], events[1]["sequence"] = sequences
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="sequence"):
        load_replay_recording(path)


def test_rejects_missing_run_started(tmp_path: Path) -> None:
    path = tmp_path / "missing-start.jsonl"
    completed = _events()[1]
    completed["sequence"] = 0
    _write_events(path, [completed])

    with pytest.raises(RecordingLoadError, match="run_started"):
        load_replay_recording(path)


def test_rejects_missing_run_completed(tmp_path: Path) -> None:
    path = tmp_path / "missing-complete.jsonl"
    _write_events(path, [_events()[0]])

    with pytest.raises(RecordingLoadError, match="run_completed"):
        load_replay_recording(path)


def test_rejects_multiple_run_started_events(tmp_path: Path) -> None:
    path = tmp_path / "multiple-start.jsonl"
    started = _events()[0]
    second_started = deepcopy(started)
    second_started["sequence"] = 1
    _write_events(path, [started, second_started])

    with pytest.raises(RecordingLoadError, match="run_started"):
        load_replay_recording(path)


def test_rejects_multiple_run_completed_events(tmp_path: Path) -> None:
    path = tmp_path / "multiple-complete.jsonl"
    started, completed = _events()
    second_completed = deepcopy(completed)
    second_completed["sequence"] = 2
    _write_events(path, [started, completed, second_completed])

    with pytest.raises(RecordingLoadError, match="run_completed"):
        load_replay_recording(path)


def test_rejects_completed_event_before_started_event(tmp_path: Path) -> None:
    path = tmp_path / "reversed-events.jsonl"
    started, completed = _events()
    completed["sequence"] = 0
    started["sequence"] = 1
    _write_events(path, [completed, started])

    with pytest.raises(RecordingLoadError, match="first event"):
        load_replay_recording(path)


@pytest.mark.parametrize("field", ["run_id", "experiment_id"])
def test_rejects_identifier_mismatch(tmp_path: Path, field: str) -> None:
    path = tmp_path / "identifier.jsonl"
    events = _events()
    events[1][field] = "different"
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match=field):
        load_replay_recording(path)


def test_rejects_completion_before_start_time(tmp_path: Path) -> None:
    path = tmp_path / "time-order.jsonl"
    events = _events()
    events[1]["occurred_at"] = "2026-07-25T08:59:59Z"
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="before run_started"):
        load_replay_recording(path)


@pytest.mark.parametrize("event_index", [0, 1])
def test_rejects_timezone_naive_datetime(tmp_path: Path, event_index: int) -> None:
    path = tmp_path / "naive-time.jsonl"
    events = _events()
    events[event_index]["occurred_at"] = "2026-07-25T09:00:00"
    _write_events(path, events)

    with pytest.raises(RecordingLoadError, match="timezone-aware"):
        load_replay_recording(path)

