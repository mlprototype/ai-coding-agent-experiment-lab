from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentlab.models import ExecutionMode, ExperimentSpec, RunResult, Workflow
from agentlab.recording import ReplayRecording, load_replay_recording
from agentlab.replay import (
    ReplayError,
    resolve_recording_path,
    run_replay,
    validate_recording_against_spec,
    write_run_result,
)

SAMPLE_SPEC = Path("experiments/examples/workflow-smoke.yaml")
SAMPLE_RECORDING = Path("experiments/examples/recordings/workflow-smoke.jsonl")


def _spec_data() -> dict[str, Any]:
    loaded = yaml.safe_load(SAMPLE_SPEC.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _recording_events() -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for line in SAMPLE_RECORDING.read_text(encoding="utf-8").splitlines()
    ]
    assert all(isinstance(event, dict) for event in events)
    return events


def _write_case(
    tmp_path: Path,
    *,
    spec_data: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> Path:
    case_directory = tmp_path / "case"
    recording_directory = case_directory / "recordings"
    recording_directory.mkdir(parents=True)
    spec = deepcopy(spec_data or _spec_data())
    spec["replay"]["recording_path"] = "recordings/input.jsonl"
    spec_path = case_directory / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    recording_path = recording_directory / "input.jsonl"
    recording_lines = "\n".join(json.dumps(event) for event in (events or _recording_events()))
    recording_path.write_text(f"{recording_lines}\n", encoding="utf-8")
    return spec_path


def test_replay_generates_and_saves_expected_run_result(tmp_path: Path) -> None:
    spec_path = _write_case(tmp_path)
    output_path = tmp_path / "results" / "result.json"

    result = run_replay(spec_path, output_path)
    restored = RunResult.model_validate_json(output_path.read_text(encoding="utf-8"))

    assert result == restored
    assert restored.run_id == "workflow-smoke-run-001"
    assert restored.experiment_id == "workflow-smoke"
    assert restored.task_id == "smoke-task"
    assert restored.workflow.value == "one_shot"
    assert restored.provider.value == "replay"
    assert restored.repetition_index == 0
    assert restored.execution_mode.value == "replay"
    assert restored.recorded_at.isoformat() == "2026-07-25T09:00:01+00:00"
    assert restored.metrics.usage_metrics is not None
    assert restored.metrics.usage_metrics.input_tokens is None


def test_relative_recording_path_is_resolved_from_spec_directory(tmp_path: Path) -> None:
    spec_path = _write_case(tmp_path)
    output_path = tmp_path / "elsewhere" / "result.json"

    run_replay(spec_path, output_path)

    assert output_path.is_file()


def test_absolute_recording_path_is_preserved(tmp_path: Path) -> None:
    absolute_path = tmp_path / "recording.jsonl"

    resolved = resolve_recording_path(Path("specs/experiment.yaml"), str(absolute_path))

    assert resolved == absolute_path


def test_force_replay_produces_byte_identical_json(tmp_path: Path) -> None:
    spec_path = _write_case(tmp_path)
    output_path = tmp_path / "result.json"

    run_replay(spec_path, output_path)
    first_bytes = output_path.read_bytes()
    run_replay(spec_path, output_path, force=True)

    assert output_path.read_bytes() == first_bytes


def test_existing_output_is_not_overwritten_without_force(tmp_path: Path) -> None:
    spec_path = _write_case(tmp_path)
    output_path = tmp_path / "result.json"
    output_path.write_text("original", encoding="utf-8")

    with pytest.raises(ReplayError, match="already exists"):
        run_replay(spec_path, output_path)

    assert output_path.read_text(encoding="utf-8") == "original"


def test_write_failure_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_case(tmp_path)
    initial_output = tmp_path / "initial.json"
    result = run_replay(spec_path, initial_output)
    failing_output = tmp_path / "nested" / "result.json"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("agentlab.replay.os.replace", fail_replace)

    with pytest.raises(ReplayError, match="could not write result"):
        write_run_result(result, failing_output, force=True)

    assert not failing_output.exists()
    assert list(failing_output.parent.iterdir()) == []


def test_output_directory_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_case(tmp_path)
    initial_output = tmp_path / "initial.json"
    result = run_replay(spec_path, initial_output)

    def fail_mkdir(_path: Path, *_args: object, **_kwargs: object) -> None:
        raise OSError("mkdir failed")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(ReplayError, match="create output directory"):
        write_run_result(result, tmp_path / "unavailable" / "result.json")


def test_atomic_create_reports_concurrent_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_case(tmp_path)
    initial_output = tmp_path / "initial.json"
    result = run_replay(spec_path, initial_output)
    output_path = tmp_path / "concurrent" / "result.json"

    monkeypatch.setattr(Path, "exists", lambda _path: False)

    def fail_link(_source: Path, _destination: Path) -> None:
        raise FileExistsError("created concurrently")

    monkeypatch.setattr("agentlab.replay.os.link", fail_link)

    with pytest.raises(ReplayError, match="already exists"):
        write_run_result(result, output_path)

    assert list(output_path.parent.iterdir()) == []


def test_invalid_recording_does_not_create_result_file(tmp_path: Path) -> None:
    spec_path = _write_case(tmp_path)
    recording_path = spec_path.parent / "recordings" / "input.jsonl"
    recording_path.write_text("{broken\n", encoding="utf-8")
    output_path = tmp_path / "results" / "result.json"

    with pytest.raises(ValueError, match="invalid JSON"):
        run_replay(spec_path, output_path)

    assert not output_path.exists()


def test_workflow_treatment_recording_matches_workflow_spec(tmp_path: Path) -> None:
    events = _recording_events()
    events[0]["workflow"] = "staged"
    spec_path = _write_case(tmp_path, events=events)

    result = run_replay(spec_path, tmp_path / "result.json")

    assert result.workflow.value == "staged"


def test_provider_treatment_recording_matches_provider_spec(tmp_path: Path) -> None:
    spec = _spec_data()
    spec.update(
        comparison_axis="provider",
        workflow="one_shot",
        provider="codex",
        control="codex",
        treatments=["antigravity"],
    )
    events = _recording_events()
    events[0]["provider"] = "antigravity"
    spec_path = _write_case(tmp_path, spec_data=spec, events=events)

    result = run_replay(spec_path, tmp_path / "result.json")

    assert result.provider.value == "antigravity"


def test_rejects_recording_experiment_id_not_in_spec(tmp_path: Path) -> None:
    spec = _spec_data()
    spec["experiment_id"] = "different-experiment"
    spec_path = _write_case(tmp_path, spec_data=spec)

    with pytest.raises(ReplayError, match="experiment_id"):
        run_replay(spec_path, tmp_path / "result.json")


def test_rejects_task_id_not_in_spec(tmp_path: Path) -> None:
    spec = _spec_data()
    spec["task_ids"] = ["different-task"]
    spec_path = _write_case(tmp_path, spec_data=spec)

    with pytest.raises(ReplayError, match="task_id"):
        run_replay(spec_path, tmp_path / "result.json")


@pytest.mark.parametrize("repetition_index", [-1, 3])
def test_rejects_repetition_index_outside_spec(
    tmp_path: Path,
    repetition_index: int,
) -> None:
    events = _recording_events()
    events[0]["repetition_index"] = repetition_index
    spec_path = _write_case(tmp_path, events=events)

    with pytest.raises(ReplayError, match="repetition_index"):
        run_replay(spec_path, tmp_path / "result.json")


def test_rejects_fixed_provider_mismatch_in_workflow_comparison(tmp_path: Path) -> None:
    events = _recording_events()
    events[0]["provider"] = "codex"
    spec_path = _write_case(tmp_path, events=events)

    with pytest.raises(ReplayError, match="provider mismatch"):
        run_replay(spec_path, tmp_path / "result.json")


def test_rejects_provider_outside_provider_comparison(tmp_path: Path) -> None:
    spec = _spec_data()
    spec.update(
        comparison_axis="provider",
        workflow="one_shot",
        provider="codex",
        control="codex",
        treatments=["antigravity"],
    )
    spec_path = _write_case(tmp_path, spec_data=spec)

    with pytest.raises(ReplayError, match="provider mismatch"):
        run_replay(spec_path, tmp_path / "result.json")


def test_rejects_fixed_workflow_mismatch_in_provider_comparison(tmp_path: Path) -> None:
    spec = _spec_data()
    spec.update(
        comparison_axis="provider",
        workflow="one_shot",
        provider="codex",
        control="codex",
        treatments=["antigravity"],
    )
    events = _recording_events()
    events[0].update(provider="codex", workflow="staged")
    spec_path = _write_case(tmp_path, spec_data=spec, events=events)

    with pytest.raises(ReplayError, match="workflow mismatch"):
        run_replay(spec_path, tmp_path / "result.json")


def test_rejects_non_replay_experiment_spec(tmp_path: Path) -> None:
    spec = _spec_data()
    spec.update(
        provider="codex",
        execution_mode="live",
        replay=None,
        live={
            "record_to": "recordings/live.jsonl",
            "require_explicit_confirmation": True,
        },
    )
    case_directory = tmp_path / "live"
    case_directory.mkdir()
    spec_path = case_directory / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    with pytest.raises(ReplayError, match="execution_mode"):
        run_replay(spec_path, tmp_path / "result.json")


def test_spec_matcher_rejects_non_replay_mode_defensively() -> None:
    spec = ExperimentSpec.model_validate(_spec_data())
    recording = load_replay_recording(SAMPLE_RECORDING)
    live_spec = spec.model_copy(
        update={"execution_mode": ExecutionMode.LIVE, "replay": None}
    )

    with pytest.raises(ReplayError, match="execution_mode"):
        validate_recording_against_spec(live_spec, recording)


def test_spec_matcher_rejects_missing_replay_settings_defensively() -> None:
    spec = ExperimentSpec.model_validate(_spec_data())
    recording = load_replay_recording(SAMPLE_RECORDING)
    missing_replay_spec = spec.model_copy(update={"replay": None})

    with pytest.raises(ReplayError, match="replay settings"):
        validate_recording_against_spec(missing_replay_spec, recording)


def test_spec_matcher_reports_workflow_outside_allowed_values() -> None:
    spec = ExperimentSpec.model_validate(_spec_data())
    recording = load_replay_recording(SAMPLE_RECORDING)
    restricted_spec = spec.model_copy(update={"treatments": []})
    staged_started = recording.started.model_copy(update={"workflow": Workflow.STAGED})
    staged_recording = ReplayRecording(
        started=staged_started,
        completed=recording.completed,
    )

    with pytest.raises(ReplayError, match="workflow mismatch"):
        validate_recording_against_spec(restricted_spec, staged_recording)
