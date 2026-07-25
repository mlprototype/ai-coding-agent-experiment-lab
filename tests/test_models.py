from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from agentlab.models import ExperimentSpec, RunMetrics, UsageMetrics


def test_valid_experiment_spec_can_be_loaded(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    spec = ExperimentSpec.model_validate(valid_spec_data())

    assert spec.experiment_id == "workflow-smoke"
    assert spec.comparison_axis.value == "workflow"


def test_provider_comparison_can_be_loaded(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        comparison_axis="provider",
        workflow="staged",
        provider="codex",
        control="codex",
        treatments=["antigravity"],
    )

    spec = ExperimentSpec.model_validate(data)

    assert spec.workflow.value == "staged"
    assert spec.treatments == ["antigravity"]


@pytest.mark.parametrize("comparison_axis", [["workflow", "provider"], "workflow,provider", "both"])
def test_rejects_multiple_comparison_axes(
    valid_spec_data: Callable[[], dict[str, Any]],
    comparison_axis: object,
) -> None:
    data = valid_spec_data()
    data["comparison_axis"] = comparison_axis

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


@pytest.mark.parametrize("repetitions", [0, -1])
def test_rejects_non_positive_repetitions(
    valid_spec_data: Callable[[], dict[str, Any]],
    repetitions: int,
) -> None:
    data = valid_spec_data()
    data["repetitions"] = repetitions

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


def test_run_metrics_round_trip_with_null_usage_values() -> None:
    metrics = RunMetrics(
        quality_gate_pass=True,
        acceptance_tests_passed=2,
        acceptance_tests_total=2,
        regression_failures=0,
        lint_errors=0,
        typecheck_errors=0,
        agent_duration_ms=100,
        evaluation_duration_ms=50,
        total_duration_ms=150,
        agent_call_count=1,
        retry_count=0,
        changed_files=["src/example.py"],
        added_lines=5,
        deleted_lines=1,
        usage_metrics=UsageMetrics(),
    )

    restored = RunMetrics.model_validate_json(metrics.model_dump_json())

    assert restored.usage_metrics is not None
    assert restored.usage_metrics.input_tokens is None
    assert restored.usage_metrics.estimated_api_cost is None


def test_execution_mode_has_no_implicit_live_default(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    del data["execution_mode"]

    with pytest.raises(ValidationError) as error:
        ExperimentSpec.model_validate(data)

    assert "execution_mode" in str(error.value)


def test_live_mode_requires_matching_explicit_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        provider="codex",
        execution_mode="live",
        replay=None,
        live={"record_to": "recordings/live.jsonl"},
    )

    spec = ExperimentSpec.model_validate(data)

    assert spec.live is not None
    assert spec.live.require_explicit_confirmation is True


def test_live_mode_rejects_missing_live_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(execution_mode="live", replay=None, live=None)

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


def test_rejects_attempt_to_redefine_other_axis_as_fixed_factor(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["fixed_factors"]["provider"] = "codex"

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)
