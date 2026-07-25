from __future__ import annotations

from pathlib import Path

from agentlab.specs import load_experiment_spec


def test_example_yaml_loads() -> None:
    path = Path("experiments/examples/workflow-smoke.yaml")

    spec = load_experiment_spec(path)

    assert spec.experiment_id == "workflow-smoke"

