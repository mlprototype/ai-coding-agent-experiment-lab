from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml

from agentlab.gates import (
    EvidenceLoadError,
    RunGatesError,
    load_evidence_artifact,
    run_gates,
    write_evidence_artifact,
)
from agentlab.models import (
    CommandStatus,
    EvidenceOverallStatus,
    FailureKind,
    UsageMetrics,
    UsageMetricSource,
)
from agentlab.runner import UnsupportedRunnerPlatformError
from agentlab.workspace import SnapshotError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Phase 2 runner is POSIX-only")

SAMPLE_SPEC = Path("experiments/examples/workflow-smoke.yaml")
SAMPLE_RECORDING = Path("experiments/examples/recordings/workflow-smoke.jsonl")


def _base_spec() -> dict[str, Any]:
    loaded = yaml.safe_load(SAMPLE_SPEC.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_case(
    tmp_path: Path,
    *,
    quality_gate: dict[str, list[list[str]]] | None = None,
    timeout_ms: int = 1000,
    max_diff_bytes: int = 65536,
) -> tuple[Path, Path, Path]:
    case = tmp_path / "case"
    fixture = case / "fixtures" / "task"
    recording_directory = case / "recordings"
    fixture.mkdir(parents=True)
    recording_directory.mkdir()
    (fixture / "input.txt").write_text("source remains unchanged\n", encoding="utf-8")
    recording_path = recording_directory / "input.jsonl"
    recording_path.write_bytes(SAMPLE_RECORDING.read_bytes())

    spec = deepcopy(_base_spec())
    spec["task_ids"] = ["task-1"]
    spec["replay"]["recording_path"] = "recordings/input.jsonl"
    spec["runner"] = {
        "fixture_path": "fixtures/task",
        "command_timeout_ms": timeout_ms,
        "termination_grace_ms": 100,
        "max_output_bytes": 4096,
        "max_diff_bytes": max_diff_bytes,
    }
    spec["quality_gate"] = quality_gate or {
        "acceptance": [[sys.executable, "-c", "print('ok')"]],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec_path = case / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return spec_path, fixture, recording_path


def _run(
    spec_path: Path,
    output_path: Path,
    *,
    force: bool = False,
):
    return run_gates(
        spec_path,
        task_id="task-1",
        run_id="phase2-test-run",
        output_path=output_path,
        confirm_execution=True,
        force=force,
    )


def test_all_gates_run_in_group_order_and_generate_metrics(tmp_path: Path) -> None:
    code = (
        "import pathlib,sys;"
        "pathlib.Path('order.txt').open('a',encoding='utf-8').write(sys.argv[1]+'\\n')"
    )
    quality_gate = {
        "acceptance": [[sys.executable, "-c", code, "acceptance"]],
        "regression": [[sys.executable, "-c", code, "regression"]],
        "lint": [[sys.executable, "-c", code, "lint"]],
        "typecheck": [[sys.executable, "-c", code, "typecheck"]],
    }
    spec_path, fixture, _recording = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    output = tmp_path / "evidence.json"

    outcome = _run(spec_path, output)
    restored = load_evidence_artifact(output)

    assert outcome.artifact == restored
    assert restored.overall_status is EvidenceOverallStatus.PASSED
    assert [command.gate.value for command in restored.commands] == [
        "acceptance",
        "regression",
        "lint",
        "typecheck",
    ]
    assert restored.metrics is not None
    assert restored.metrics.quality_gate_pass is True
    assert restored.metrics.acceptance_tests_total == 1
    assert restored.metrics.acceptance_tests_passed == 1
    assert restored.metrics.agent_duration_ms == 0
    assert restored.metrics.agent_call_count == 0
    assert restored.metrics.usage_metrics is None
    assert restored.diff.changed_files == ["order.txt"]
    assert restored.diff.added_lines == 4
    assert restored.workspace_removed is True
    assert not (fixture / "order.txt").exists()


def test_normal_failures_continue_and_use_command_level_metrics(tmp_path: Path) -> None:
    code = (
        "import pathlib,sys;"
        "pathlib.Path('order.txt').open('a',encoding='utf-8').write(sys.argv[1]+'\\n');"
        "raise SystemExit(1 if sys.argv[1].endswith('fail') else 0)"
    )
    quality_gate = {
        "acceptance": [
            [sys.executable, "-c", code, "acceptance-pass"],
            [sys.executable, "-c", code, "acceptance-fail"],
        ],
        "regression": [[sys.executable, "-c", code, "regression-fail"]],
        "lint": [[sys.executable, "-c", code, "lint-fail"]],
        "typecheck": [[sys.executable, "-c", code, "typecheck-fail"]],
    }
    spec_path, _fixture, _recording = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )

    artifact = _run(spec_path, tmp_path / "evidence.json").artifact

    assert artifact.overall_status is EvidenceOverallStatus.FAILED
    assert artifact.failure_kind is FailureKind.QUALITY_GATE_FAILURE
    assert len(artifact.commands) == 5
    assert [command.status for command in artifact.commands] == [
        CommandStatus.PASSED,
        CommandStatus.FAILED,
        CommandStatus.FAILED,
        CommandStatus.FAILED,
        CommandStatus.FAILED,
    ]
    assert artifact.metrics is not None
    assert artifact.metrics.quality_gate_pass is False
    assert artifact.metrics.acceptance_tests_total == 2
    assert artifact.metrics.acceptance_tests_passed == 1
    assert artifact.metrics.regression_failures == 1
    assert artifact.metrics.lint_errors == 1
    assert artifact.metrics.typecheck_errors == 1


def test_timeout_stops_later_gates_and_does_not_generate_metrics(tmp_path: Path) -> None:
    quality_gate = {
        "acceptance": [[sys.executable, "-c", "import signal; signal.pause()"]],
        "regression": [
            [sys.executable, "-c", "open('must-not-run','w',encoding='utf-8').write('x')"]
        ],
        "lint": [],
        "typecheck": [],
    }
    spec_path, fixture, _recording = _write_case(
        tmp_path,
        quality_gate=quality_gate,
        timeout_ms=150,
    )
    output = tmp_path / "timeout.json"

    artifact = _run(spec_path, output).artifact

    assert artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR
    assert artifact.failure_kind is FailureKind.TIMEOUT
    assert artifact.metrics is None
    assert len(artifact.commands) == 1
    assert artifact.commands[0].status is CommandStatus.TIMED_OUT
    assert artifact.workspace_removed is True
    assert not (fixture / "must-not-run").exists()
    assert load_evidence_artifact(output) == artifact


def test_signal_termination_stops_later_gates_and_has_null_metrics(
    tmp_path: Path,
) -> None:
    quality_gate = {
        "acceptance": [
            [
                sys.executable,
                "-c",
                "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
            ]
        ],
        "regression": [
            [sys.executable, "-c", "open('must-not-run','w',encoding='utf-8').write('x')"]
        ],
        "lint": [],
        "typecheck": [],
    }
    spec_path, fixture, _recording = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )

    artifact = _run(spec_path, tmp_path / "signal.json").artifact

    assert artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR
    assert artifact.failure_kind is FailureKind.SIGNAL_TERMINATION
    assert artifact.metrics is None
    assert len(artifact.commands) == 1
    assert artifact.commands[0].status is CommandStatus.SIGNAL_TERMINATED
    assert artifact.commands[0].return_code == -signal.SIGTERM
    assert not (fixture / "must-not-run").exists()


def test_spec_model_and_hash_come_from_the_same_single_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    spec_a = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec_a["quality_gate"]["acceptance"] = [
        [sys.executable, "-c", "print('spec-a')"]
    ]
    spec_b = deepcopy(spec_a)
    spec_b["quality_gate"]["acceptance"] = [
        [sys.executable, "-c", "print('spec-b')"]
    ]
    bytes_a = yaml.safe_dump(spec_a, sort_keys=False).encode()
    bytes_b = yaml.safe_dump(spec_b, sort_keys=False).encode()
    spec_path.write_bytes(bytes_a)
    original_read_bytes = Path.read_bytes
    spec_read_count = 0

    def replace_after_read(path: Path) -> bytes:
        nonlocal spec_read_count
        content = original_read_bytes(path)
        if path == spec_path:
            spec_read_count += 1
            spec_path.write_bytes(bytes_b)
        return content

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    artifact = _run(spec_path, tmp_path / "single-read.json").artifact

    assert spec_read_count == 1
    assert artifact.spec_sha256 == hashlib.sha256(bytes_a).hexdigest()
    assert artifact.commands[0].argv[-1] == "print('spec-a')"
    assert artifact.commands[0].stdout == "spec-a\n"


def test_unavailable_command_is_harness_failure_with_saved_evidence(tmp_path: Path) -> None:
    spec_path, _fixture, _recording = _write_case(
        tmp_path,
        quality_gate={
            "acceptance": [["phase2-command-does-not-exist"]],
            "regression": [],
            "lint": [],
            "typecheck": [],
        },
    )
    output = tmp_path / "unavailable.json"

    artifact = _run(spec_path, output).artifact

    assert artifact.failure_kind is FailureKind.COMMAND_UNAVAILABLE
    assert artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR
    assert artifact.metrics is None
    assert output.is_file()


def test_output_collection_error_is_not_recorded_as_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)

    def fail_select(_selector: object, _timeout: float | None = None) -> NoReturn:
        raise OSError("synthetic selector failure")

    monkeypatch.setattr("agentlab.runner.selectors.DefaultSelector.select", fail_select)
    artifact = _run(spec_path, tmp_path / "collection-error.json").artifact

    assert artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR
    assert artifact.failure_kind is FailureKind.EVIDENCE_ERROR
    assert artifact.metrics is None
    assert artifact.commands[0].status is CommandStatus.COLLECTION_ERROR
    assert artifact.commands[0].status is not CommandStatus.SPAWN_ERROR


def test_binary_change_is_evidence_error_without_estimated_metrics(tmp_path: Path) -> None:
    script = "open('binary.dat','wb').write(b'\\x00changed')"
    spec_path, _fixture, _recording = _write_case(
        tmp_path,
        quality_gate={
            "acceptance": [[sys.executable, "-c", script]],
            "regression": [],
            "lint": [],
            "typecheck": [],
        },
    )

    artifact = _run(spec_path, tmp_path / "binary.json").artifact

    assert artifact.failure_kind is FailureKind.EVIDENCE_ERROR
    assert artifact.metrics is None
    assert artifact.diff.changed_files == ["binary.dat"]
    assert artifact.diff.binary_files == ["binary.dat"]
    assert artifact.diff.line_counts_complete is False


def test_parent_secret_and_temporary_paths_are_absent_from_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "phase2-parent-secret"
    monkeypatch.setenv("AGENTLAB_SYNTHETIC_SECRET", secret)
    script = (
        "import os,pathlib;"
        "print(os.environ.get('AGENTLAB_SYNTHETIC_SECRET','not_present'));"
        "print(pathlib.Path.cwd());"
        "print(os.environ['HOME'])"
    )
    spec_path, _fixture, _recording = _write_case(
        tmp_path,
        quality_gate={
            "acceptance": [[sys.executable, "-c", script]],
            "regression": [],
            "lint": [],
            "typecheck": [],
        },
    )
    output = tmp_path / "clean.json"

    _run(spec_path, output)
    content = output.read_text(encoding="utf-8")

    assert secret not in content
    assert "agentlab-run-" not in content
    assert "<WORKSPACE>" in content
    assert "<TEMP_HOME>" in content


def test_missing_confirmation_does_not_spawn_or_create_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "not-created.json"

    def blocked_spawn(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("subprocess must not start without confirmation")

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", blocked_spawn)

    with pytest.raises(RunGatesError, match="confirm-execution"):
        run_gates(
            spec_path,
            task_id="task-1",
            run_id="phase2-test-run",
            output_path=output,
            confirm_execution=False,
        )

    assert not output.exists()


def test_invalid_task_id_is_rejected_before_execution(tmp_path: Path) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)

    with pytest.raises(RunGatesError, match="task_id"):
        run_gates(
            spec_path,
            task_id="unknown-task",
            run_id="phase2-test-run",
            output_path=tmp_path / "evidence.json",
            confirm_execution=True,
        )


def test_runner_settings_are_required(tmp_path: Path) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    del spec["runner"]
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    with pytest.raises(RunGatesError, match="runner"):
        _run(spec_path, tmp_path / "evidence.json")


def test_existing_artifact_requires_force_before_any_new_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "evidence.json"
    _run(spec_path, output)

    def blocked_spawn(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("existing output must be rejected before execution")

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", blocked_spawn)
    with pytest.raises(RunGatesError, match="already exists"):
        _run(spec_path, output)

    monkeypatch.undo()
    forced = _run(spec_path, output, force=True)
    assert forced.artifact.overall_status is EvidenceOverallStatus.PASSED


@pytest.mark.parametrize("protected_input", ["spec", "recording"])
def test_force_cannot_overwrite_spec_or_recording(
    tmp_path: Path,
    protected_input: str,
) -> None:
    spec_path, _fixture, recording_path = _write_case(tmp_path)
    output = spec_path if protected_input == "spec" else recording_path
    original = output.read_bytes()

    with pytest.raises(RunGatesError, match="must not overwrite"):
        _run(spec_path, output, force=True)

    assert output.read_bytes() == original


@pytest.mark.parametrize("protected_input", ["spec", "recording"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_force_cannot_overwrite_spec_or_recording_through_alias(
    tmp_path: Path,
    protected_input: str,
    alias_kind: str,
) -> None:
    spec_path, _fixture, recording_path = _write_case(tmp_path)
    protected_path = spec_path if protected_input == "spec" else recording_path
    output = tmp_path / f"{protected_input}-{alias_kind}.json"
    if alias_kind == "symlink":
        output.symlink_to(protected_path)
    else:
        output.hardlink_to(protected_path)
    original = protected_path.read_bytes()

    with pytest.raises(RunGatesError, match="must not overwrite"):
        _run(spec_path, output, force=True)

    assert protected_path.read_bytes() == original


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_force_cannot_overwrite_fixture_through_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    spec_path, fixture, _recording = _write_case(tmp_path)
    fixture_file = fixture / "input.txt"
    output = tmp_path / "fixture-alias.json"
    if alias_kind == "symlink":
        output.symlink_to(fixture_file)
    else:
        output.hardlink_to(fixture_file)
    original = fixture_file.read_bytes()

    with pytest.raises(RunGatesError, match="fixture source"):
        _run(spec_path, output, force=True)

    assert fixture_file.read_bytes() == original


def test_artifact_inside_fixture_is_rejected_even_with_force(tmp_path: Path) -> None:
    spec_path, fixture, _recording = _write_case(tmp_path)

    with pytest.raises(RunGatesError, match="inside the fixture"):
        _run(spec_path, fixture / "evidence.json", force=True)


def test_unsupported_platform_is_saved_as_harness_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)

    def unsupported() -> NoReturn:
        raise UnsupportedRunnerPlatformError("synthetic unsupported platform")

    monkeypatch.setattr("agentlab.gates.ensure_runner_platform_supported", unsupported)
    output = tmp_path / "unsupported.json"
    artifact = _run(spec_path, output).artifact

    assert artifact.failure_kind is FailureKind.UNSUPPORTED_PLATFORM
    assert artifact.commands == []
    assert artifact.metrics is None
    assert artifact.workspace_removed is True
    assert load_evidence_artifact(output) == artifact


def test_unexpected_runner_error_still_removes_workspace_and_saves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)

    def fail_runner(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic runner failure")

    monkeypatch.setattr("agentlab.gates.LocalCommandRunner.run", fail_runner)
    artifact = _run(spec_path, tmp_path / "runner-error.json").artifact

    assert artifact.failure_kind is FailureKind.EVIDENCE_ERROR
    assert artifact.metrics is None
    assert artifact.workspace_removed is True


def test_diff_collection_error_has_null_metrics_and_no_workspace_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)

    def fail_snapshot(_path: Path) -> NoReturn:
        raise SnapshotError("synthetic diff failure")

    monkeypatch.setattr("agentlab.gates.snapshot_directory", fail_snapshot)
    artifact = _run(spec_path, tmp_path / "diff-error.json").artifact

    assert artifact.failure_kind is FailureKind.EVIDENCE_ERROR
    assert artifact.diff.line_counts_complete is False
    assert artifact.metrics is None
    assert artifact.workspace_removed is True


def test_writer_failure_removes_temporary_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    artifact = _run(spec_path, tmp_path / "initial.json").artifact
    output = tmp_path / "nested" / "evidence.json"

    def fail_replace(_source: Path, _destination: Path) -> NoReturn:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("agentlab.gates.os.replace", fail_replace)
    with pytest.raises(RunGatesError, match="could not write Evidence"):
        write_evidence_artifact(artifact, output, force=True)

    assert not output.exists()
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize(
    ("operation", "error", "match"),
    [
        ("fsync", OSError("synthetic fsync failure"), "could not write Evidence"),
        ("link", OSError("synthetic link failure"), "could not write Evidence"),
        ("link", FileExistsError("synthetic concurrent create"), "already exists"),
    ],
)
def test_writer_io_failures_leave_no_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error: OSError,
    match: str,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    artifact = _run(spec_path, tmp_path / "initial.json").artifact
    output = tmp_path / operation / "evidence.json"

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    monkeypatch.setattr(f"agentlab.gates.os.{operation}", fail)
    with pytest.raises(RunGatesError, match=match):
        write_evidence_artifact(artifact, output)

    assert not output.exists()
    assert list(output.parent.iterdir()) == []


def test_evidence_writer_rejects_non_finite_json(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    artifact = _run(spec_path, tmp_path / "initial.json").artifact
    assert artifact.metrics is not None
    invalid_usage = UsageMetrics().model_copy(
        update={
            "estimated_api_cost": float("inf"),
            "source": UsageMetricSource.ESTIMATED,
        }
    )
    invalid_metrics = artifact.metrics.model_copy(
        update={"usage_metrics": invalid_usage}
    )
    invalid_artifact = artifact.model_copy(update={"metrics": invalid_metrics})
    output = tmp_path / "non-finite" / "evidence.json"

    with pytest.raises(RunGatesError, match="non-finite"):
        write_evidence_artifact(invalid_artifact, output)

    assert not output.exists()
    assert list(output.parent.iterdir()) == []


def test_evidence_loader_rejects_unknown_field_and_duplicate_key(tmp_path: Path) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "evidence.json"
    _run(spec_path, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["unknown"] = True
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLoadError, match="unknown"):
        load_evidence_artifact(output)

    output.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(EvidenceLoadError, match="duplicate"):
        load_evidence_artifact(output)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("command", "command_index", "0"),
        ("command", "stdout_truncated", "false"),
        ("artifact", "workspace_removed", 1),
        ("command", "started_at", "2026-07-25T09:00:00"),
        ("artifact", "started_at", 1721900000),
    ],
)
def test_evidence_loader_rejects_type_coercion_and_naive_time(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "evidence.json"
    _run(spec_path, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    target = payload["commands"][0] if section == "command" else payload
    target[field] = value
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLoadError):
        load_evidence_artifact(output)


def test_evidence_loader_rejects_non_finite_json_number(tmp_path: Path) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "evidence.json"
    _run(spec_path, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["metrics"]["usage_metrics"] = {
        "estimated_api_cost": float("inf"),
        "source": "estimated",
    }
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLoadError, match="non-finite"):
        load_evidence_artifact(output)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["commands"][0].update(
            {"status": "failed", "return_code": 1}
        ),
        lambda payload: payload.update(
            {
                "overall_status": "failed",
                "failure_kind": "quality_gate_failure",
            }
        ),
        lambda payload: payload["metrics"].update(
            {"acceptance_tests_passed": 0}
        ),
        lambda payload: payload["metrics"].update(
            {"quality_gate_pass": False}
        ),
        lambda payload: payload["commands"][0]["termination"].update(
            {"sigterm_sent": False, "sigkill_sent": True}
        ),
        lambda payload: payload["commands"][0]["termination"].update(
            {
                "process_group_cleared": False,
                "error": "synthetic uncleared group",
            }
        ),
        lambda payload: payload["commands"][0].update(
            {
                "status": "timed_out",
                "return_code": -signal.SIGTERM,
            }
        ),
        lambda payload: payload["commands"][0].update(
            {
                "status": "spawn_error",
                "return_code": None,
                "error": None,
            }
        ),
        lambda payload: payload.update(
            {
                "overall_status": "harness_error",
                "failure_kind": "signal_termination",
                "metrics": None,
                "harness_error": "synthetic mismatch",
            }
        ),
    ],
    ids=[
        "passed-artifact-with-failed-command",
        "failed-artifact-with-all-passed-commands",
        "metrics-command-count-mismatch",
        "metrics-quality-status-mismatch",
        "sigkill-without-sigterm",
        "none-reason-with-uncleared-group",
        "timed-out-without-timeout-reason",
        "spawn-error-without-reason",
        "signal-failure-kind-without-signal-command",
    ],
)
def test_evidence_loader_rejects_semantic_contradictions(
    tmp_path: Path,
    mutate: Any,
) -> None:
    spec_path, _fixture, _recording = _write_case(tmp_path)
    output = tmp_path / "evidence.json"
    _run(spec_path, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    mutate(payload)
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceLoadError):
        load_evidence_artifact(output)
