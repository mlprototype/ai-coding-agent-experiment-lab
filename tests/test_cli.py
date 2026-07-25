from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentlab.cli import app

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
