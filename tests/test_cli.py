from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
import yaml
from typer.testing import CliRunner

from agentlab.cli import app
from agentlab.models import RunResult

runner = CliRunner()


def test_validate_accepts_example() -> None:
    result = runner.invoke(
        app,
        ["validate", str(Path("experiments/examples/workflow-smoke.yaml"))],
    )

    assert result.exit_code == 0
    assert "valid ExperimentSpec" in result.stdout


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
