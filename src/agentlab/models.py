"""Versioned data contracts for experiments and their results."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """Shared strictness for persisted contracts."""

    model_config = ConfigDict(extra="forbid")


class ComparisonAxis(StrEnum):
    WORKFLOW = "workflow"
    PROVIDER = "provider"


class Workflow(StrEnum):
    ONE_SHOT = "one_shot"
    STAGED = "staged"


class Provider(StrEnum):
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    REPLAY = "replay"


class ExecutionMode(StrEnum):
    REPLAY = "replay"
    LIVE = "live"


class UsageMetricSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"
    NOT_AVAILABLE = "not_available"


class QualityGate(ContractModel):
    """Commands that a later runner will execute; Phase 0 only validates them."""

    acceptance: list[list[str]] = Field(min_length=1)
    regression: list[list[str]] = Field(default_factory=list)
    lint: list[list[str]] = Field(default_factory=list)
    typecheck: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def commands_must_have_argv(self) -> QualityGate:
        for group_name in ("acceptance", "regression", "lint", "typecheck"):
            commands = getattr(self, group_name)
            has_empty_argv = any(
                not command or any(not argument.strip() for argument in command)
                for command in commands
            )
            if has_empty_argv:
                raise ValueError(f"quality_gate.{group_name} commands must contain non-empty argv")
        return self


class StopConditions(ContractModel):
    max_failures: int | None = Field(default=None, gt=0)
    max_total_duration_ms: int | None = Field(default=None, gt=0)
    fail_fast: bool = False


class ReplaySettings(ContractModel):
    recording_path: str = Field(min_length=1)


class LiveSettings(ContractModel):
    """Contract only; Phase 0 has no live executor."""

    record_to: str = Field(min_length=1)
    require_explicit_confirmation: Literal[True] = True


class ExperimentSpec(ContractModel):
    """A single-axis experiment definition.

    ``control`` and ``treatments`` name values on ``comparison_axis``. The
    top-level workflow/provider value is the control value for its axis and the
    fixed value for the other axis. This makes simultaneous variation invalid.
    """

    schema_version: Literal["1.0"]
    experiment_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    research_question: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    comparison_axis: ComparisonAxis

    workflow: Workflow
    provider: Provider
    control: str = Field(min_length=1)
    treatments: list[str] = Field(min_length=1)
    fixed_factors: dict[str, Any]
    task_ids: list[str] = Field(min_length=1)
    repetitions: int = Field(gt=0)
    random_seed: int
    quality_gate: QualityGate
    stop_conditions: StopConditions
    execution_mode: ExecutionMode

    replay: ReplaySettings | None = None
    live: LiveSettings | None = None

    @model_validator(mode="after")
    def enforce_single_axis_and_execution_mode(self) -> ExperimentSpec:
        if len(set(self.treatments)) != len(self.treatments):
            raise ValueError("treatments must be unique")
        if self.control in self.treatments:
            raise ValueError("control must not also appear in treatments")

        forbidden_fixed_factors = {
            "comparison_axis",
            "workflow",
            "provider",
            "control",
            "treatments",
        }
        collisions = forbidden_fixed_factors.intersection(self.fixed_factors)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"fixed_factors must not redefine experiment dimensions: {names}")

        if self.comparison_axis is ComparisonAxis.WORKFLOW:
            allowed_values = {item.value for item in Workflow}
            if self.control != self.workflow.value:
                raise ValueError("workflow must equal control in a workflow comparison")
        else:
            allowed_values = {item.value for item in Provider}
            if self.control != self.provider.value:
                raise ValueError("provider must equal control in a provider comparison")

        invalid_values = ({self.control} | set(self.treatments)) - allowed_values
        if invalid_values:
            values = ", ".join(sorted(invalid_values))
            raise ValueError(f"control/treatments contain values outside comparison_axis: {values}")

        if self.execution_mode is ExecutionMode.REPLAY:
            if self.replay is None:
                raise ValueError("replay settings are required when execution_mode is replay")
            if self.live is not None:
                raise ValueError("live settings must be absent when execution_mode is replay")
        else:
            if self.live is None:
                raise ValueError("live settings are required when execution_mode is live")
            if self.replay is not None:
                raise ValueError("replay settings must be absent when execution_mode is live")
        return self


class UsageMetrics(ContractModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    estimated_api_cost: float | None = Field(default=None, ge=0)
    quota_consumption: float | None = Field(default=None, ge=0)
    source: UsageMetricSource | None = None


class RunMetrics(ContractModel):
    quality_gate_pass: bool
    acceptance_tests_passed: int = Field(ge=0)
    acceptance_tests_total: int = Field(ge=0)
    regression_failures: int = Field(ge=0)
    lint_errors: int = Field(ge=0)
    typecheck_errors: int = Field(ge=0)
    agent_duration_ms: int = Field(ge=0)
    evaluation_duration_ms: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    agent_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    changed_files: list[str]
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    usage_metrics: UsageMetrics | None = None

    @model_validator(mode="after")
    def passed_tests_cannot_exceed_total(self) -> RunMetrics:
        if self.acceptance_tests_passed > self.acceptance_tests_total:
            raise ValueError("acceptance_tests_passed must not exceed acceptance_tests_total")
        return self


class RunResult(ContractModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    recorded_at: datetime
    metrics: RunMetrics


class CapabilityReport(ContractModel):
    provider: Provider
    command_available: bool
    executable_path: str | None
    cli_version: str | None
    non_interactive_supported: bool
    structured_output_supported: bool
    usage_metrics_supported: bool
    checked_at: datetime
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unavailable_command_has_no_executable(self) -> CapabilityReport:
        if not self.command_available and self.executable_path is not None:
            raise ValueError("an unavailable command cannot have an executable_path")
        return self


class DoctorReport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: list[CapabilityReport]
