"""Versioned data contracts for experiments and their results."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


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


class GateKind(StrEnum):
    ACCEPTANCE = "acceptance"
    REGRESSION = "regression"
    LINT = "lint"
    TYPECHECK = "typecheck"


class CommandStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SIGNAL_TERMINATED = "signal_terminated"
    SPAWN_ERROR = "spawn_error"
    COLLECTION_ERROR = "collection_error"


class TerminationReason(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    RESIDUAL_PROCESS = "residual_process"
    EMERGENCY_CLEANUP = "emergency_cleanup"


class EvidenceOverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    HARNESS_ERROR = "harness_error"


class FailureKind(StrEnum):
    NONE = "none"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    TIMEOUT = "timeout"
    SIGNAL_TERMINATION = "signal_termination"
    COMMAND_UNAVAILABLE = "command_unavailable"
    SPAWN_ERROR = "spawn_error"
    PROCESS_CLEANUP_ERROR = "process_cleanup_error"
    EVIDENCE_ERROR = "evidence_error"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


class QualityGate(ContractModel):
    """The only argv sequences that the local quality-gate runner may execute."""

    acceptance: list[list[StrictStr]] = Field(min_length=1)
    regression: list[list[StrictStr]] = Field(default_factory=list)
    lint: list[list[StrictStr]] = Field(default_factory=list)
    typecheck: list[list[StrictStr]] = Field(default_factory=list)

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
    require_explicit_confirmation: Literal[True]


class RunnerSettings(ContractModel):
    """Bounded settings for the trusted-fixture Phase 2 local runner."""

    fixture_path: StrictStr = Field(min_length=1)
    command_timeout_ms: StrictInt = Field(gt=0, le=600_000)
    termination_grace_ms: StrictInt = Field(gt=0, le=60_000)
    max_output_bytes: StrictInt = Field(gt=0, le=16 * 1024 * 1024)
    max_diff_bytes: StrictInt = Field(gt=0, le=64 * 1024 * 1024)

    @field_validator("fixture_path")
    @classmethod
    def fixture_path_must_be_bounded_relative_posix_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fixture_path must not be empty")
        if "\x00" in value:
            raise ValueError("fixture_path must not contain NUL")
        if "\\" in value:
            raise ValueError("fixture_path must use POSIX separators")

        posix_path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ValueError("fixture_path must be relative")
        if value in {".", "./"}:
            raise ValueError("fixture_path must name a fixture directory")
        if ".." in posix_path.parts:
            raise ValueError("fixture_path must not contain parent-directory references")
        return value


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
    runner: RunnerSettings | None = None

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
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    cached_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    reasoning_output_tokens: int | None = Field(default=None, ge=0, strict=True)
    estimated_api_cost: float | None = Field(
        default=None,
        ge=0,
        strict=True,
        allow_inf_nan=False,
    )
    quota_consumption: float | None = Field(
        default=None,
        ge=0,
        strict=True,
        allow_inf_nan=False,
    )
    source: UsageMetricSource | None = None

    @model_validator(mode="after")
    def values_must_have_an_available_source(self) -> UsageMetrics:
        numeric_values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.estimated_api_cost,
            self.quota_consumption,
        )
        has_value = any(value is not None for value in numeric_values)
        if has_value and self.source is None:
            raise ValueError("usage metric values require source")
        if has_value and self.source is UsageMetricSource.NOT_AVAILABLE:
            raise ValueError("source not_available requires all usage metric values to be null")
        return self


class RunMetrics(ContractModel):
    quality_gate_pass: bool = Field(strict=True)
    acceptance_tests_passed: int = Field(ge=0, strict=True)
    acceptance_tests_total: int = Field(ge=0, strict=True)
    regression_failures: int = Field(ge=0, strict=True)
    lint_errors: int = Field(ge=0, strict=True)
    typecheck_errors: int = Field(ge=0, strict=True)
    agent_duration_ms: int = Field(ge=0, strict=True)
    evaluation_duration_ms: int = Field(ge=0, strict=True)
    total_duration_ms: int = Field(ge=0, strict=True)
    agent_call_count: int = Field(ge=0, strict=True)
    retry_count: int = Field(ge=0, strict=True)
    changed_files: list[str]
    added_lines: int = Field(ge=0, strict=True)
    deleted_lines: int = Field(ge=0, strict=True)
    usage_metrics: UsageMetrics | None = None

    @model_validator(mode="after")
    def passed_tests_cannot_exceed_total(self) -> RunMetrics:
        if self.acceptance_tests_passed > self.acceptance_tests_total:
            raise ValueError("acceptance_tests_passed must not exceed acceptance_tests_total")
        return self


class TerminationEvidence(ContractModel):
    reason: TerminationReason
    sigterm_sent: StrictBool
    sigkill_sent: StrictBool
    process_group_cleared: StrictBool
    error: StrictStr | None = None

    @model_validator(mode="after")
    def signal_and_cleanup_state_must_be_consistent(self) -> TerminationEvidence:
        if self.sigkill_sent and not self.sigterm_sent:
            raise ValueError("sigkill_sent requires sigterm_sent")
        if self.reason is TerminationReason.NONE:
            if self.sigterm_sent or self.sigkill_sent:
                raise ValueError("termination reason none must not contain sent signals")
            if not self.process_group_cleared:
                raise ValueError("termination reason none requires a cleared process group")
        if self.process_group_cleared and self.error is not None:
            raise ValueError("cleared process group must not contain a cleanup error")
        if not self.process_group_cleared and self.error is None:
            raise ValueError("uncleared process group requires a cleanup error")
        return self


class CommandEvidence(ContractModel):
    gate: GateKind
    command_index: StrictInt = Field(ge=0)
    argv: list[StrictStr] = Field(min_length=1)
    status: CommandStatus
    return_code: StrictInt | None
    started_at: datetime
    completed_at: datetime
    duration_ms: StrictInt = Field(ge=0)
    stdout: StrictStr
    stderr: StrictStr
    stdout_truncated: StrictBool
    stderr_truncated: StrictBool
    stdout_decode_replaced: StrictBool
    stderr_decode_replaced: StrictBool
    termination: TerminationEvidence
    error: StrictStr | None = None

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def timestamps_must_use_datetime_or_iso_string(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("command timestamps must be ISO strings or datetime values")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("command timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def status_must_match_return_code(self) -> CommandEvidence:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        if self.status is CommandStatus.PASSED and self.return_code != 0:
            raise ValueError("passed command must have return_code 0")
        if self.status is CommandStatus.FAILED and (
            self.return_code is None or self.return_code <= 0
        ):
            raise ValueError("failed command must have a positive non-zero return_code")
        if self.status is CommandStatus.SIGNAL_TERMINATED and (
            self.return_code is None or self.return_code >= 0
        ):
            raise ValueError("signal_terminated command must have a negative return_code")
        if self.status is CommandStatus.SPAWN_ERROR:
            if self.return_code is not None:
                raise ValueError("spawn_error command must not have a return_code")
            if self.error is None:
                raise ValueError("spawn_error command requires an error reason")
        if self.status is CommandStatus.COLLECTION_ERROR and self.error is None:
            raise ValueError("collection_error command requires an error reason")
        if (
            self.status not in {CommandStatus.SPAWN_ERROR, CommandStatus.COLLECTION_ERROR}
            and self.error is not None
        ):
            raise ValueError("only spawn_error or collection_error may contain an error")
        if self.status is CommandStatus.TIMED_OUT:
            if self.termination.reason is not TerminationReason.TIMEOUT:
                raise ValueError("timed_out command requires termination reason timeout")
        elif (
            self.status is not CommandStatus.COLLECTION_ERROR
            and self.termination.reason is TerminationReason.TIMEOUT
        ):
            raise ValueError("termination reason timeout requires timed_out status")
        if (
            self.status in {CommandStatus.PASSED, CommandStatus.FAILED}
            and self.termination.reason
            not in {TerminationReason.NONE, TerminationReason.RESIDUAL_PROCESS}
        ):
            raise ValueError("normally completed command has an invalid termination reason")
        if (
            self.status is CommandStatus.SPAWN_ERROR
            and self.termination.reason is not TerminationReason.NONE
        ):
            raise ValueError("spawn_error command must not contain process termination")
        return self


class DiffEvidence(ContractModel):
    changed_files: list[StrictStr]
    binary_files: list[StrictStr]
    added_lines: StrictInt | None = Field(default=None, ge=0)
    deleted_lines: StrictInt | None = Field(default=None, ge=0)
    unified_diff: StrictStr
    diff_truncated: StrictBool
    line_counts_complete: StrictBool
    collection_error: StrictStr | None = None

    @model_validator(mode="after")
    def complete_line_counts_must_have_values(self) -> DiffEvidence:
        if self.changed_files != sorted(set(self.changed_files)):
            raise ValueError("changed_files must be unique and sorted")
        if self.binary_files != sorted(set(self.binary_files)):
            raise ValueError("binary_files must be unique and sorted")
        if not set(self.binary_files).issubset(self.changed_files):
            raise ValueError("binary_files must be a subset of changed_files")
        if self.binary_files and self.line_counts_complete:
            raise ValueError("binary file changes require incomplete line counts")
        if self.line_counts_complete:
            if self.added_lines is None or self.deleted_lines is None:
                raise ValueError("complete line counts require added_lines and deleted_lines")
            if self.collection_error is not None:
                raise ValueError("complete diff must not have a collection_error")
        elif self.added_lines is not None or self.deleted_lines is not None:
            raise ValueError("incomplete line counts must not contain estimated values")
        return self


class EvidenceArtifact(ContractModel):
    schema_version: Literal["1.0"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    overall_status: EvidenceOverallStatus
    failure_kind: FailureKind
    started_at: datetime
    completed_at: datetime
    spec_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    runner: RunnerSettings
    commands: list[CommandEvidence]
    diff: DiffEvidence
    metrics: RunMetrics | None
    workspace_removed: StrictBool
    harness_error: StrictStr | None = None

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def artifact_timestamps_must_use_datetime_or_iso_string(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("artifact timestamps must be ISO strings or datetime values")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def artifact_timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def status_failure_and_metrics_must_be_consistent(self) -> EvidenceArtifact:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")

        expected = {
            EvidenceOverallStatus.PASSED: FailureKind.NONE,
            EvidenceOverallStatus.FAILED: FailureKind.QUALITY_GATE_FAILURE,
        }
        if (
            self.overall_status in expected
            and self.failure_kind is not expected[self.overall_status]
        ):
            raise ValueError("overall_status and failure_kind are inconsistent")
        if (
            self.overall_status is EvidenceOverallStatus.HARNESS_ERROR
            and self.failure_kind
            in {FailureKind.NONE, FailureKind.QUALITY_GATE_FAILURE}
        ):
            raise ValueError("harness_error requires a Harness failure kind")
        if self.overall_status is EvidenceOverallStatus.HARNESS_ERROR:
            if self.harness_error is None:
                raise ValueError("harness_error status requires a reason")
        elif self.harness_error is not None:
            raise ValueError("non-Harness result must not contain harness_error")

        required_command_status = {
            FailureKind.TIMEOUT: CommandStatus.TIMED_OUT,
            FailureKind.SIGNAL_TERMINATION: CommandStatus.SIGNAL_TERMINATED,
            FailureKind.COMMAND_UNAVAILABLE: CommandStatus.SPAWN_ERROR,
            FailureKind.SPAWN_ERROR: CommandStatus.SPAWN_ERROR,
        }.get(self.failure_kind)
        if required_command_status is not None and not any(
            command.status is required_command_status for command in self.commands
        ):
            raise ValueError("failure_kind does not match any command status")
        if (
            self.failure_kind is FailureKind.PROCESS_CLEANUP_ERROR
            and not any(
                not command.termination.process_group_cleared
                for command in self.commands
            )
        ):
            raise ValueError("process_cleanup_error requires an uncleared command group")
        if (
            self.failure_kind is FailureKind.UNSUPPORTED_PLATFORM
            and self.commands
        ):
            raise ValueError("unsupported_platform Evidence must not contain commands")

        commands_completed_normally = all(
            command.status in {CommandStatus.PASSED, CommandStatus.FAILED}
            and command.termination.process_group_cleared
            for command in self.commands
        )
        metrics_permitted = (
            self.failure_kind in {FailureKind.NONE, FailureKind.QUALITY_GATE_FAILURE}
            and commands_completed_normally
            and self.diff.line_counts_complete
            and self.workspace_removed
        )
        if metrics_permitted != (self.metrics is not None):
            raise ValueError("metrics presence is inconsistent with Evidence completeness")
        if self.overall_status is not EvidenceOverallStatus.HARNESS_ERROR:
            if not self.commands:
                raise ValueError("non-Harness Evidence requires at least one command")
            if not commands_completed_normally:
                raise ValueError("non-Harness Evidence requires normally completed commands")
            if not self.diff.line_counts_complete or not self.workspace_removed:
                raise ValueError("non-Harness Evidence must be complete and cleaned up")
            all_commands_passed = all(
                command.status is CommandStatus.PASSED for command in self.commands
            )
            if (
                self.overall_status is EvidenceOverallStatus.PASSED
            ) is not all_commands_passed:
                raise ValueError("overall_status does not match command statuses")

        if self.metrics is not None:
            acceptance = [
                command for command in self.commands if command.gate is GateKind.ACCEPTANCE
            ]
            regression = [
                command for command in self.commands if command.gate is GateKind.REGRESSION
            ]
            lint = [command for command in self.commands if command.gate is GateKind.LINT]
            typecheck = [
                command for command in self.commands if command.gate is GateKind.TYPECHECK
            ]
            expected_quality_pass = all(
                command.status is CommandStatus.PASSED for command in self.commands
            )
            expected_counts = (
                sum(command.status is CommandStatus.PASSED for command in acceptance),
                len(acceptance),
                sum(command.status is CommandStatus.FAILED for command in regression),
                sum(command.status is CommandStatus.FAILED for command in lint),
                sum(command.status is CommandStatus.FAILED for command in typecheck),
            )
            actual_counts = (
                self.metrics.acceptance_tests_passed,
                self.metrics.acceptance_tests_total,
                self.metrics.regression_failures,
                self.metrics.lint_errors,
                self.metrics.typecheck_errors,
            )
            metrics_are_phase2_consistent = (
                self.metrics.quality_gate_pass is expected_quality_pass
                and actual_counts == expected_counts
                and self.metrics.agent_duration_ms == 0
                and self.metrics.agent_call_count == 0
                and self.metrics.retry_count == 0
                and self.metrics.total_duration_ms
                == self.metrics.evaluation_duration_ms
                and self.metrics.changed_files == self.diff.changed_files
                and self.metrics.added_lines == self.diff.added_lines
                and self.metrics.deleted_lines == self.diff.deleted_lines
                and self.metrics.usage_metrics is None
            )
            if not metrics_are_phase2_consistent:
                raise ValueError("RunMetrics do not match command and diff Evidence")
        return self


class RunResult(ContractModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    workflow: Workflow
    provider: Provider
    repetition_index: int = Field(ge=0)
    execution_mode: ExecutionMode
    recorded_at: datetime
    metrics: RunMetrics

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value


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
    def unavailable_command_has_no_reported_capabilities(self) -> CapabilityReport:
        has_unavailable_details = (
            self.executable_path is not None
            or self.cli_version is not None
            or self.non_interactive_supported
            or self.structured_output_supported
            or self.usage_metrics_supported
        )
        if not self.command_available and has_unavailable_details:
            raise ValueError(
                "an unavailable command cannot report an executable, version, "
                "or supported capabilities"
            )
        return self


class DoctorReport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: list[CapabilityReport]
