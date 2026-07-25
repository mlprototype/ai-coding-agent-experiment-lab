from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agentlab.models import (
    CapabilityReport,
    ExperimentSpec,
    RunMetrics,
    RunnerSettings,
    RunResult,
    UsageMetrics,
)


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


def test_rejects_duplicate_treatments(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["treatments"] = ["staged", "staged"]

    with pytest.raises(ValidationError, match="treatments must be unique"):
        ExperimentSpec.model_validate(data)


def test_rejects_control_in_treatments(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["treatments"] = ["one_shot"]

    with pytest.raises(ValidationError, match="control must not also appear"):
        ExperimentSpec.model_validate(data)


def test_rejects_workflow_that_does_not_equal_workflow_control(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["workflow"] = "staged"

    with pytest.raises(ValidationError, match="workflow must equal control"):
        ExperimentSpec.model_validate(data)


def test_rejects_provider_that_does_not_equal_provider_control(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        comparison_axis="provider",
        provider="antigravity",
        control="codex",
        treatments=["replay"],
    )

    with pytest.raises(ValidationError, match="provider must equal control"):
        ExperimentSpec.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("control", "codex"), ("treatments", ["codex"])],
)
def test_rejects_provider_values_on_workflow_axis(
    valid_spec_data: Callable[[], dict[str, Any]],
    field: str,
    value: object,
) -> None:
    data = valid_spec_data()
    data[field] = value

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("control", "one_shot"), ("treatments", ["staged"])],
)
def test_rejects_workflow_values_on_provider_axis(
    valid_spec_data: Callable[[], dict[str, Any]],
    field: str,
    value: object,
) -> None:
    data = valid_spec_data()
    data.update(
        comparison_axis="provider",
        provider="codex",
        control="codex",
        treatments=["antigravity"],
    )
    data[field] = value

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


@pytest.mark.parametrize(
    ("field", "value", "source"),
    [
        ("input_tokens", 100, "provider_reported"),
        ("estimated_api_cost", 1.25, "estimated"),
    ],
)
def test_usage_values_with_available_source_are_valid(
    field: str,
    value: int | float,
    source: str,
) -> None:
    metrics = UsageMetrics.model_validate({field: value, "source": source})

    assert getattr(metrics, field) == value


def test_usage_value_requires_source() -> None:
    with pytest.raises(ValidationError, match="require source"):
        UsageMetrics(input_tokens=100)


def test_not_available_source_rejects_usage_values() -> None:
    with pytest.raises(ValidationError, match="not_available"):
        UsageMetrics(input_tokens=100, source="not_available")


@pytest.mark.parametrize("field", ["estimated_api_cost", "quota_consumption"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_usage_float_values_must_be_finite(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        UsageMetrics.model_validate({field: value, "source": "estimated"})


def test_run_result_rejects_timezone_naive_recorded_at() -> None:
    metrics = RunMetrics(
        quality_gate_pass=True,
        acceptance_tests_passed=1,
        acceptance_tests_total=1,
        regression_failures=0,
        lint_errors=0,
        typecheck_errors=0,
        agent_duration_ms=1,
        evaluation_duration_ms=1,
        total_duration_ms=2,
        agent_call_count=1,
        retry_count=0,
        changed_files=[],
        added_lines=0,
        deleted_lines=0,
    )

    with pytest.raises(ValidationError, match="recorded_at must be timezone-aware"):
        RunResult(
            schema_version="1.0",
            run_id="run-001",
            experiment_id="workflow-smoke",
            task_id="smoke-task",
            workflow="one_shot",
            provider="replay",
            repetition_index=0,
            execution_mode="replay",
            recorded_at=datetime(2026, 7, 25, 9, 0, 0),
            metrics=metrics,
        )


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
        live={
            "record_to": "recordings/live.jsonl",
            "require_explicit_confirmation": True,
        },
    )

    spec = ExperimentSpec.model_validate(data)

    assert spec.live is not None
    assert spec.live.require_explicit_confirmation is True


def test_live_mode_rejects_omitted_explicit_confirmation(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        provider="codex",
        execution_mode="live",
        replay=None,
        live={"record_to": "recordings/live.jsonl"},
    )

    with pytest.raises(ValidationError, match="require_explicit_confirmation"):
        ExperimentSpec.model_validate(data)


def test_live_mode_rejects_false_explicit_confirmation(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        provider="codex",
        execution_mode="live",
        replay=None,
        live={
            "record_to": "recordings/live.jsonl",
            "require_explicit_confirmation": False,
        },
    )

    with pytest.raises(ValidationError, match="require_explicit_confirmation"):
        ExperimentSpec.model_validate(data)


def test_live_mode_rejects_missing_live_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(execution_mode="live", replay=None, live=None)

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


def test_replay_mode_rejects_missing_replay_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["replay"] = None

    with pytest.raises(ValidationError, match="replay settings are required"):
        ExperimentSpec.model_validate(data)


def test_replay_mode_rejects_live_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["live"] = {
        "record_to": "recordings/live.jsonl",
        "require_explicit_confirmation": True,
    }

    with pytest.raises(ValidationError, match="live settings must be absent"):
        ExperimentSpec.model_validate(data)


def test_live_mode_rejects_replay_settings(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data.update(
        provider="codex",
        execution_mode="live",
        live={
            "record_to": "recordings/live.jsonl",
            "require_explicit_confirmation": True,
        },
    )

    with pytest.raises(ValidationError, match="replay settings must be absent"):
        ExperimentSpec.model_validate(data)


def test_rejects_attempt_to_redefine_other_axis_as_fixed_factor(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["fixed_factors"]["provider"] = "codex"

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executable_path", "/fake/agy"),
        ("cli_version", "agy 1.0"),
        ("non_interactive_supported", True),
        ("structured_output_supported", True),
        ("usage_metrics_supported", True),
    ],
)
def test_unavailable_capability_rejects_reported_details(field: str, value: object) -> None:
    data: dict[str, object] = {
        "provider": "antigravity",
        "command_available": False,
        "executable_path": None,
        "cli_version": None,
        "non_interactive_supported": False,
        "structured_output_supported": False,
        "usage_metrics_supported": False,
        "checked_at": datetime.now(UTC),
        "notes": [],
    }
    data[field] = value

    with pytest.raises(ValidationError, match="unavailable command"):
        CapabilityReport.model_validate(data)


def test_existing_spec_without_runner_remains_valid(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    spec = ExperimentSpec.model_validate(valid_spec_data())

    assert spec.runner is None


def test_valid_runner_settings_are_loaded(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["runner"] = {
        "fixture_path": "fixtures/runner-smoke",
        "command_timeout_ms": 5000,
        "termination_grace_ms": 500,
        "max_output_bytes": 65536,
        "max_diff_bytes": 262144,
    }

    spec = ExperimentSpec.model_validate(data)

    assert spec.runner is not None
    assert spec.runner.fixture_path == "fixtures/runner-smoke"


@pytest.mark.parametrize(
    "fixture_path",
    ["", " ", "/absolute/fixture", "../fixture", "fixtures/../fixture", r"C:\fixture", "."],
)
def test_runner_rejects_unbounded_fixture_paths(fixture_path: str) -> None:
    with pytest.raises(ValidationError, match="fixture_path"):
        RunnerSettings(
            fixture_path=fixture_path,
            command_timeout_ms=5000,
            termination_grace_ms=500,
            max_output_bytes=65536,
            max_diff_bytes=262144,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_timeout_ms", 0),
        ("command_timeout_ms", 600_001),
        ("termination_grace_ms", 60_001),
        ("max_output_bytes", 16 * 1024 * 1024 + 1),
        ("max_diff_bytes", 64 * 1024 * 1024 + 1),
        ("command_timeout_ms", "5000"),
        ("termination_grace_ms", True),
    ],
)
def test_runner_limits_are_strict_and_bounded(field: str, value: object) -> None:
    data: dict[str, object] = {
        "fixture_path": "fixtures/runner-smoke",
        "command_timeout_ms": 5000,
        "termination_grace_ms": 500,
        "max_output_bytes": 65536,
        "max_diff_bytes": 262144,
    }
    data[field] = value

    with pytest.raises(ValidationError, match=field):
        RunnerSettings.model_validate(data)


def test_quality_gate_argv_rejects_non_string_argument(
    valid_spec_data: Callable[[], dict[str, Any]],
) -> None:
    data = valid_spec_data()
    data["quality_gate"]["acceptance"] = [["python3", 1]]

    with pytest.raises(ValidationError, match="quality_gate"):
        ExperimentSpec.model_validate(data)
