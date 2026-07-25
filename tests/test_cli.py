from __future__ import annotations

from pathlib import Path

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
