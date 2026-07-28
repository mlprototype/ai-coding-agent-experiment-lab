from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest
import yaml
from typer.testing import CliRunner

from agentlab.cli import app
from agentlab.gates import load_evidence_artifact
from agentlab.models import EvidenceOverallStatus, FailureKind, RunResult

runner = CliRunner()
SAMPLE_SPEC = Path("experiments/examples/workflow-smoke.yaml").resolve()


def _write_runner_spec(tmp_path: Path, command: list[str]) -> Path:
    case = tmp_path / "runner-case"
    fixture = case / "fixtures" / "task"
    fixture.mkdir(parents=True)
    (fixture / "input.txt").write_text("synthetic fixture\n", encoding="utf-8")
    spec = yaml.safe_load(
        Path("experiments/examples/workflow-smoke.yaml").read_text(encoding="utf-8")
    )
    spec["task_ids"] = ["task-1"]
    spec["quality_gate"] = {
        "acceptance": [command],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec["runner"] = {
        "fixture_path": "fixtures/task",
        "command_timeout_ms": 1000,
        "termination_grace_ms": 100,
        "max_output_bytes": 4096,
        "max_diff_bytes": 65536,
    }
    spec["replay"]["recording_path"] = str(
        Path("experiments/examples/recordings/workflow-smoke.jsonl").resolve()
    )
    spec_path = case / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return spec_path


def test_validate_accepts_example() -> None:
    result = runner.invoke(
        app,
        ["validate", str(Path("experiments/examples/workflow-smoke.yaml"))],
    )

    assert result.exit_code == 0
    assert "valid ExperimentSpec" in result.stdout


def test_validate_accepts_phase3_live_example() -> None:
    result = runner.invoke(
        app,
        ["validate", "experiments/examples/codex-live-smoke.yaml"],
    )

    assert result.exit_code == 0
    assert "codex-live-smoke" in result.stdout
    assert "mode=live" in result.stdout


def test_validate_rejects_invalid_spec_with_nonzero_exit(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.yaml"
    example = yaml.safe_load(
        Path("experiments/examples/workflow-smoke.yaml").read_text(encoding="utf-8")
    )
    example["repetitions"] = 0
    invalid_path.write_text(yaml.safe_dump(example), encoding="utf-8")

    result = runner.invoke(app, ["validate", str(invalid_path)])

    assert result.exit_code != 0
    assert "invalid ExperimentSpec" in result.stderr
    assert "repetitions" in result.stderr


def test_doctor_human_output_is_readable_when_commands_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentlab.capabilities.shutil.which", lambda _command: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "codex: not_available" in result.stdout
    assert "antigravity: not_available" in result.stdout
    assert "version: not_verified" in result.stdout
    assert "note:" in result.stdout


def test_replay_cli_saves_result_without_external_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Replay must not perform external calls")

    monkeypatch.setattr(subprocess, "run", blocked_call)
    monkeypatch.setattr(socket, "create_connection", blocked_call)
    output_path = tmp_path / "runs" / "result.json"

    result = runner.invoke(
        app,
        [
            "replay",
            "experiments/examples/workflow-smoke.yaml",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    assert "workflow-smoke-run-001" in result.stdout
    assert "workflow-smoke" in result.stdout
    assert str(output_path) in result.stdout
    assert "external AI executed: no" in result.stdout
    restored = RunResult.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert restored.run_id == "workflow-smoke-run-001"


def test_replay_cli_refuses_overwrite_then_force_is_deterministic(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"
    arguments = [
        "replay",
        "experiments/examples/workflow-smoke.yaml",
        "--output",
        str(output_path),
    ]
    first = runner.invoke(app, arguments)
    original_bytes = output_path.read_bytes()

    refused = runner.invoke(app, arguments)
    forced = runner.invoke(app, [*arguments, "--force"])

    assert first.exit_code == 0
    assert refused.exit_code != 0
    assert "already exists" in refused.stderr
    assert "Traceback" not in refused.stderr
    assert forced.exit_code == 0
    assert output_path.read_bytes() == original_bytes


def test_replay_cli_requires_output_option() -> None:
    result = runner.invoke(
        app,
        ["replay", "experiments/examples/workflow-smoke.yaml"],
    )

    assert result.exit_code != 0
    assert "--output" in result.output


def test_replay_cli_does_not_create_result_for_invalid_recording(tmp_path: Path) -> None:
    case_directory = tmp_path / "case"
    recording_directory = case_directory / "recordings"
    recording_directory.mkdir(parents=True)
    spec = yaml.safe_load(
        Path("experiments/examples/workflow-smoke.yaml").read_text(encoding="utf-8")
    )
    spec["replay"]["recording_path"] = "recordings/broken.jsonl"
    spec_path = case_directory / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    (recording_directory / "broken.jsonl").write_text("{broken\n", encoding="utf-8")
    output_path = tmp_path / "result.json"

    result = runner.invoke(
        app,
        ["replay", str(spec_path), "--output", str(output_path)],
    )

    assert result.exit_code != 0
    assert "invalid JSON" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Phase 2 runner is POSIX-only")
def test_run_gates_cli_saves_machine_readable_evidence(tmp_path: Path) -> None:
    output_path = tmp_path / "evidence.json"
    result = runner.invoke(
        app,
        [
            "run-gates",
            str(SAMPLE_SPEC),
            "--task-id",
            "smoke-task",
            "--run-id",
            "phase2-cli-smoke",
            "--output",
            str(output_path),
            "--confirm-execution",
        ],
    )

    assert result.exit_code == 0
    assert "quality gates: passed" in result.stdout
    assert "workspace removed: yes" in result.stdout
    assert "external AI executed: no" in result.stdout
    artifact = load_evidence_artifact(output_path)
    assert artifact.overall_status is EvidenceOverallStatus.PASSED
    assert len(artifact.commands) == 4


def test_run_gates_cli_requires_confirmation_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_runner_spec(tmp_path, [sys.executable, "-c", "print('ok')"])
    output = tmp_path / "evidence.json"

    def blocked_spawn(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", blocked_spawn)
    result = runner.invoke(
        app,
        [
            "run-gates",
            str(spec_path),
            "--task-id",
            "task-1",
            "--run-id",
            "phase2-no-confirm",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 2
    assert "confirm-execution" in result.stderr
    assert "Traceback" not in result.output
    assert not output.exists()


def test_live_codex_cli_requires_confirmation_before_any_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "live-evidence.json"

    def blocked_subprocess(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Live confirmation must be checked before preflight")

    monkeypatch.setattr(subprocess, "run", blocked_subprocess)
    monkeypatch.setattr(subprocess, "Popen", blocked_subprocess)
    result = runner.invoke(
        app,
        [
            "live-codex",
            "experiments/examples/codex-live-smoke.yaml",
            "--task-id",
            "codex-live-smoke",
            "--repetition-index",
            "0",
            "--run-id",
            "codex-live-smoke-001",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 2
    assert "confirm-live-codex" in result.stderr
    assert "Traceback" not in result.output
    assert "raw Prompt persisted: no" in result.stdout
    assert "raw Codex JSONL persisted: no" in result.stdout
    assert not output.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Phase 2 runner is POSIX-only")
def test_run_gates_cli_uses_exit_1_for_normal_gate_failure(tmp_path: Path) -> None:
    spec_path = _write_runner_spec(
        tmp_path,
        [sys.executable, "-c", "raise SystemExit(1)"],
    )
    output = tmp_path / "failed.json"

    result = runner.invoke(
        app,
        [
            "run-gates",
            str(spec_path),
            "--task-id",
            "task-1",
            "--run-id",
            "phase2-gate-failed",
            "--output",
            str(output),
            "--confirm-execution",
        ],
    )

    assert result.exit_code == 1
    assert "quality gates: failed" in result.stdout
    artifact = load_evidence_artifact(output)
    assert artifact.failure_kind is FailureKind.QUALITY_GATE_FAILURE
    assert artifact.metrics is not None


@pytest.mark.skipif(sys.platform == "win32", reason="Phase 2 runner is POSIX-only")
def test_run_gates_cli_uses_exit_2_for_harness_failure(tmp_path: Path) -> None:
    spec_path = _write_runner_spec(tmp_path, ["phase2-command-does-not-exist"])
    output = tmp_path / "harness-error.json"

    result = runner.invoke(
        app,
        [
            "run-gates",
            str(spec_path),
            "--task-id",
            "task-1",
            "--run-id",
            "phase2-harness-error",
            "--output",
            str(output),
            "--confirm-execution",
        ],
    )

    assert result.exit_code == 2
    assert "Harness failure: command_unavailable" in result.stdout
    assert "Traceback" not in result.output
    artifact = load_evidence_artifact(output)
    assert artifact.metrics is None


@pytest.mark.skipif(sys.platform == "win32", reason="Phase 2 runner is POSIX-only")
def test_run_gates_cli_refuses_existing_artifact_then_allows_force(tmp_path: Path) -> None:
    spec_path = _write_runner_spec(tmp_path, [sys.executable, "-c", "print('ok')"])
    output = tmp_path / "evidence.json"
    arguments = [
        "run-gates",
        str(spec_path),
        "--task-id",
        "task-1",
        "--run-id",
        "phase2-force",
        "--output",
        str(output),
        "--confirm-execution",
    ]

    first = runner.invoke(app, arguments)
    refused = runner.invoke(app, arguments)
    forced = runner.invoke(app, [*arguments, "--force"])

    assert first.exit_code == 0
    assert refused.exit_code == 2
    assert "already exists" in refused.stderr
    assert forced.exit_code == 0
