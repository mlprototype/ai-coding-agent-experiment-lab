"""Versioned data contracts for experiments and their results."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

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


CODEX_REQUIRED_EXEC_FLAGS = (
    "--config",
    "--ephemeral",
    "--ignore-rules",
    "--ignore-user-config",
    "--json",
    "--model",
    "--sandbox",
    "--skip-git-repo-check",
    "--strict-config",
)

CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS = frozenset(
    {
        "codex-cli 0.146.0-alpha.3.1",
    }
)

NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]


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


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


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


class ProviderExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CodexCliProfile(StrEnum):
    """Versioned CLI contract selected by preflight."""

    NOT_SELECTED = "not_selected"
    HEADLESS_EXEC_EXPLICIT_NEVER_V2 = "headless_exec_explicit_never_v2"


class CodexApprovalBasis(StrEnum):
    """Why the normalized approval policy is recorded as never."""

    EXPLICIT_CONFIG_NEVER = "explicit_config_never"


class CodexExecutionStage(StrEnum):
    """Furthest completed step at the Codex Provider boundary."""

    PREFLIGHT_NOT_COMPLETED = "preflight_not_completed"
    PREFLIGHT_COMPLETED = "preflight_completed"
    PROVIDER_INVOCATION_ATTEMPTED = "provider_invocation_attempted"


class CodexFailureStage(StrEnum):
    """Safe fixed location for a failed Codex Provider lifecycle."""

    PREFLIGHT = "preflight"
    WORKSPACE_PREPARATION = "workspace_preparation"
    PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION = (
        "provider_environment_directory_preparation"
    )
    PROVIDER_RUNNER_CONSTRUCTION = "provider_runner_construction"
    PROVIDER_RUNNER_ENTRY = "provider_runner_entry"
    PROVIDER_RUNTIME_PRECHECK = "provider_runtime_precheck"
    JSONL_PARSER_INITIALIZATION = "jsonl_parser_initialization"
    PROVIDER_ARGV_CONSTRUCTION = "provider_argv_construction"
    PROVIDER_ENVIRONMENT_CONSTRUCTION = "provider_environment_construction"
    PROVIDER_PROCESS_SPAWN = "provider_process_spawn"
    PROVIDER_PIPE_SELECTOR_INITIALIZATION = "provider_pipe_selector_initialization"
    PROVIDER_PROCESS_COLLECTION = "provider_process_collection"
    CODEX_EVIDENCE_CONSTRUCTION = "codex_evidence_construction"
    PROVIDER_RUNNER_RESULT_CONSTRUCTION = "provider_runner_result_construction"
    PROVIDER_RUNNER_RESULT_EXTRACTION = "provider_runner_result_extraction"
    PROVIDER_ORCHESTRATION = "provider_orchestration"


class CodexRunnerState(StrEnum):
    """Whether the Provider runner entry boundary was reached."""

    NOT_STARTED = "not_started"
    STARTED = "started"


class CodexInvocationState(StrEnum):
    """Observed subprocess creation state without storing process identifiers."""

    NOT_ATTEMPTED = "not_attempted"
    SPAWN_ATTEMPTED = "spawn_attempted"
    PROCESS_STARTED = "process_started"


class CodexCleanupState(StrEnum):
    """Observed Provider process-group cleanup result."""

    NOT_APPLICABLE = "not_applicable"
    CLEARED = "cleared"
    FAILED = "failed"


class DiagnosticRunnerState(StrEnum):
    """Runner observation retained when strict paired Evidence cannot be built."""

    NOT_STARTED = "not_started"
    STARTED = "started"
    UNKNOWN = "unknown"


class DiagnosticInvocationState(StrEnum):
    """Provider invocation observation with an explicit unknown state."""

    NOT_ATTEMPTED = "not_attempted"
    SPAWN_ATTEMPTED = "spawn_attempted"
    PROCESS_STARTED = "process_started"
    UNKNOWN = "unknown"


class DiagnosticCleanupState(StrEnum):
    """Provider cleanup observation with an explicit unknown state."""

    NOT_APPLICABLE = "not_applicable"
    CLEARED = "cleared"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DiagnosticFailureStage(StrEnum):
    """Safe failure location copied from the shared lifecycle tracker."""

    PREFLIGHT = "preflight"
    WORKSPACE_PREPARATION = "workspace_preparation"
    PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION = (
        "provider_environment_directory_preparation"
    )
    PROVIDER_RUNNER_CONSTRUCTION = "provider_runner_construction"
    PROVIDER_RUNNER_ENTRY = "provider_runner_entry"
    PROVIDER_RUNTIME_PRECHECK = "provider_runtime_precheck"
    JSONL_PARSER_INITIALIZATION = "jsonl_parser_initialization"
    PROVIDER_ARGV_CONSTRUCTION = "provider_argv_construction"
    PROVIDER_ENVIRONMENT_CONSTRUCTION = "provider_environment_construction"
    PROVIDER_PROCESS_SPAWN = "provider_process_spawn"
    PROVIDER_PIPE_SELECTOR_INITIALIZATION = "provider_pipe_selector_initialization"
    PROVIDER_PROCESS_COLLECTION = "provider_process_collection"
    CODEX_EVIDENCE_CONSTRUCTION = "codex_evidence_construction"
    PROVIDER_RUNNER_RESULT_CONSTRUCTION = "provider_runner_result_construction"
    PROVIDER_RUNNER_RESULT_EXTRACTION = "provider_runner_result_extraction"
    PROVIDER_ORCHESTRATION = "provider_orchestration"
    UNKNOWN = "unknown"


class LiveDiagnosticCode(StrEnum):
    """Fixed reason why paired strict Live outputs were not published."""

    CODEX_EVIDENCE_VALIDATION_FAILED = "codex_evidence_validation_failed"
    LIFECYCLE_FALLBACK_EVIDENCE_VALIDATION_FAILED = (
        "lifecycle_fallback_evidence_validation_failed"
    )
    RECORDING_CONSTRUCTION_FAILED = "recording_construction_failed"
    LIVE_ARTIFACT_CONSTRUCTION_FAILED = "live_artifact_construction_failed"
    PAIRED_OUTPUT_PUBLICATION_FAILED = "paired_output_publication_failed"
    DIAGNOSTIC_PUBLICATION_FAILED = "diagnostic_publication_failed"


class ProviderActivityDetermination(StrEnum):
    """Whether all Provider lifecycle observations needed for diagnosis are known."""

    DETERMINED = "determined"
    UNKNOWN = "unknown"


class CodexTerminalEvent(StrEnum):
    NONE = "none"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    ERROR = "error"


class CodexItemType(StrEnum):
    AGENT_MESSAGE = "agent_message"
    COMMAND = "command"
    COMMAND_EXECUTION = "command_execution"
    ERROR = "error"
    FILE = "file"
    FILE_CHANGE = "file_change"
    MCP_TOOL_CALL = "mcp_tool_call"
    MESSAGE = "message"
    REASONING = "reasoning"
    TODO_LIST = "todo_list"
    WEB_SEARCH = "web_search"
    UNKNOWN = "unknown"


class WorkspaceLifecycle(StrEnum):
    NOT_CREATED = "not_created"
    REMOVED = "removed"
    CLEANUP_FAILED = "cleanup_failed"


class LiveOverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PROVIDER_ERROR = "provider_error"
    HARNESS_ERROR = "harness_error"


class LiveFailureKind(StrEnum):
    NONE = "none"
    PROVIDER_TURN_FAILED = "provider_turn_failed"
    PROVIDER_CLI_NONZERO = "provider_cli_nonzero"
    PROVIDER_SIGNAL_TERMINATION = "provider_signal_termination"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_SPAWN_ERROR = "provider_spawn_error"
    PROVIDER_INPUT_ERROR = "provider_input_error"
    PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"
    PROVIDER_OUTPUT_LIMIT = "provider_output_limit"
    PROCESS_CLEANUP_ERROR = "process_cleanup_error"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    GATE_HARNESS_ERROR = "gate_harness_error"
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
    """Backward-compatible Live contract; Phase 3 fields opt into Codex execution."""

    record_to: str = Field(min_length=1)
    diagnostic_to: StrictStr | None = Field(default=None, min_length=1)
    prompt_path: StrictStr | None = Field(default=None, min_length=1)
    model: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: ReasoningEffort | None = None
    provider_timeout_ms: StrictInt | None = Field(
        default=None,
        gt=0,
        le=1_800_000,
    )
    max_prompt_bytes: StrictInt | None = Field(
        default=None,
        gt=0,
        le=1024 * 1024,
    )
    max_event_line_bytes: StrictInt | None = Field(
        default=None,
        gt=0,
        le=4 * 1024 * 1024,
    )
    max_provider_output_bytes: StrictInt | None = Field(
        default=None,
        gt=0,
        le=64 * 1024 * 1024,
    )
    require_explicit_confirmation: StrictBool

    @field_validator("model")
    @classmethod
    def model_must_be_an_explicit_condition(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or value != value.strip() or "\x00" in value:
            raise ValueError("live.model must be a non-empty explicit model identifier")
        mutable_aliases = {"latest", "default", "auto"}
        normalized_parts = set(re.split(r"[-_/.]", value.casefold()))
        if mutable_aliases.intersection(normalized_parts):
            raise ValueError("live.model must not use a mutable alias")
        return value

    @model_validator(mode="after")
    def phase3_fields_are_all_present_or_all_absent(self) -> LiveSettings:
        if not self.require_explicit_confirmation:
            raise ValueError("live.require_explicit_confirmation must be true")
        phase3_values = (
            self.prompt_path,
            self.model,
            self.reasoning_effort,
            self.provider_timeout_ms,
            self.max_prompt_bytes,
            self.max_event_line_bytes,
            self.max_provider_output_bytes,
        )
        if any(value is not None for value in phase3_values) and not all(
            value is not None for value in phase3_values
        ):
            raise ValueError("Phase 3 live settings must be configured as a complete set")
        if (
            self.max_event_line_bytes is not None
            and self.max_provider_output_bytes is not None
            and self.max_event_line_bytes > self.max_provider_output_bytes
        ):
            raise ValueError(
                "live.max_event_line_bytes must not exceed max_provider_output_bytes"
            )
        if self.prompt_path is not None:
            _validate_relative_posix_file_path(self.prompt_path, "live.prompt_path")
            _validate_relative_posix_file_path(self.record_to, "live.record_to")
        if self.diagnostic_to is not None:
            _validate_relative_posix_file_path(
                self.diagnostic_to,
                "live.diagnostic_to",
            )
        return self


def _validate_relative_posix_file_path(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if "\\" in value:
        raise ValueError(f"{field_name} must use POSIX separators")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{field_name} must be relative")
    if value in {".", "./"} or ".." in posix_path.parts:
        raise ValueError(f"{field_name} must name a file below the Spec directory")
    return value


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


class GateKindSummary(ContractModel):
    """Redacted count-only summary for one quality Gate kind."""

    command_count: StrictInt = Field(ge=0)
    passed_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def counts_must_fit_command_count(self) -> GateKindSummary:
        if self.passed_count + self.failed_count > self.command_count:
            raise ValueError("Gate passed/failed counts must not exceed command_count")
        return self


class LiveEvaluationSummary(ContractModel):
    """Redacted Gate/diff/workspace facts retained by Recording 1.1."""

    acceptance: GateKindSummary
    regression: GateKindSummary
    lint: GateKindSummary
    typecheck: GateKindSummary
    all_commands_completed_normally: StrictBool
    evaluation_duration_ms: StrictInt = Field(ge=0)
    changed_files: list[StrictStr]
    added_lines: StrictInt | None = Field(default=None, ge=0)
    deleted_lines: StrictInt | None = Field(default=None, ge=0)
    diff_line_counts_complete: StrictBool
    workspace_lifecycle: WorkspaceLifecycle

    @model_validator(mode="after")
    def summary_must_be_internally_consistent(self) -> LiveEvaluationSummary:
        if self.changed_files != sorted(set(self.changed_files)):
            raise ValueError("changed_files must be unique and sorted")
        gate_summaries = (
            self.acceptance,
            self.regression,
            self.lint,
            self.typecheck,
        )
        counts_complete = all(
            gate.command_count == gate.passed_count + gate.failed_count
            for gate in gate_summaries
        )
        if self.all_commands_completed_normally is not counts_complete:
            raise ValueError(
                "all_commands_completed_normally must match Gate command counts"
            )
        if self.diff_line_counts_complete:
            if self.added_lines is None or self.deleted_lines is None:
                raise ValueError("complete diff summary requires line counts")
        elif self.added_lines is not None or self.deleted_lines is not None:
            raise ValueError("incomplete diff summary must omit line counts")
        return self


class CodexExecutionEvidence(ContractModel):
    """Redacted summary of one Codex CLI process; raw events are never persisted."""

    schema_version: Literal["1.1", "1.2", "1.3", "1.4"]
    provider: Literal[Provider.CODEX]
    cli_version: StrictStr | None
    cli_profile: CodexCliProfile
    execution_stage: CodexExecutionStage
    failure_stage: CodexFailureStage | None = None
    runner_state: CodexRunnerState | None = None
    invocation_state: CodexInvocationState | None = None
    cleanup_state: CodexCleanupState | None = None
    preflight_checked_at: datetime
    verified_flags: list[StrictStr]
    requested_model: StrictStr
    requested_reasoning_effort: ReasoningEffort
    sandbox_mode: Literal["workspace-write"]
    approval_policy: Literal["never"] | None
    approval_basis: Literal[CodexApprovalBasis.EXPLICIT_CONFIG_NEVER] | None
    web_search_disabled: StrictBool
    command_network_disabled: StrictBool
    raw_stream_persisted: StrictBool
    process_started: StrictBool
    status: ProviderExecutionStatus
    failure_kind: LiveFailureKind
    exit_code: StrictInt | None
    started_at: datetime
    completed_at: datetime
    duration_ms: StrictInt = Field(ge=0)
    event_count: StrictInt = Field(ge=0)
    unknown_event_count: StrictInt = Field(ge=0)
    thread_started_count: StrictInt = Field(ge=0)
    turn_started_count: StrictInt = Field(ge=0)
    terminal_event: CodexTerminalEvent
    turn_completed_count: StrictInt = Field(ge=0)
    turn_failed_count: StrictInt = Field(ge=0)
    error_event_count: StrictInt = Field(ge=0)
    item_type_counts: dict[CodexItemType, NonNegativeStrictInt]
    usage_metrics: UsageMetrics
    stdout_bytes: StrictInt = Field(ge=0)
    stderr_bytes: StrictInt = Field(ge=0)
    stdout_limit_exceeded: StrictBool
    stderr_truncated: StrictBool
    termination: TerminationEvidence

    @field_validator("preflight_checked_at", "started_at", "completed_at", mode="before")
    @classmethod
    def codex_timestamps_must_use_datetime_or_iso_string(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("Codex timestamps must be ISO strings or datetime values")
        return value

    @field_validator("preflight_checked_at", "started_at", "completed_at")
    @classmethod
    def codex_timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Codex timestamps must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def codex_summary_must_be_semantically_consistent(self) -> CodexExecutionEvidence:
        if self.schema_version == "1.1":
            if self.failure_stage is not None:
                raise ValueError("Codex Evidence 1.1 must not contain failure_stage")
        elif self.status is ProviderExecutionStatus.SUCCEEDED:
            if self.failure_stage is not None:
                raise ValueError("successful Codex Evidence must not contain failure_stage")
        elif self.failure_stage is None:
            raise ValueError("failed Codex Evidence 1.2+ requires failure_stage")

        lifecycle_values = (
            self.runner_state,
            self.invocation_state,
            self.cleanup_state,
        )
        if self.schema_version in {"1.1", "1.2"}:
            if any(value is not None for value in lifecycle_values):
                raise ValueError(
                    "Codex Evidence 1.1/1.2 must not contain 1.3 lifecycle state"
                )
        else:
            if any(value is None for value in lifecycle_values):
                raise ValueError(
                    "Codex Evidence 1.3+ requires complete lifecycle state"
                )
            assert self.runner_state is not None
            assert self.invocation_state is not None
            assert self.cleanup_state is not None
            invocation_attempted = (
                self.invocation_state is not CodexInvocationState.NOT_ATTEMPTED
            )
            process_started = (
                self.invocation_state is CodexInvocationState.PROCESS_STARTED
            )
            if process_started is not self.process_started:
                raise ValueError(
                    "process_started must match the observed invocation state"
                )
            if (
                self.runner_state is CodexRunnerState.NOT_STARTED
                and invocation_attempted
            ):
                raise ValueError(
                    "a runner that was not started cannot attempt Provider invocation"
                )
            if (
                self.execution_stage
                is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
            ) is not invocation_attempted:
                raise ValueError(
                    "execution_stage must match the observed invocation state"
                )
            if process_started:
                expected_cleanup = (
                    CodexCleanupState.CLEARED
                    if self.termination.process_group_cleared
                    else CodexCleanupState.FAILED
                )
                if self.cleanup_state is not expected_cleanup:
                    raise ValueError(
                        "cleanup_state must match Provider termination Evidence"
                    )
            elif self.cleanup_state is not CodexCleanupState.NOT_APPLICABLE:
                raise ValueError(
                    "cleanup_state must be not_applicable without a process"
                )
            runner_started_stages = {
                CodexFailureStage.PROVIDER_RUNNER_ENTRY,
                CodexFailureStage.PROVIDER_RUNTIME_PRECHECK,
                CodexFailureStage.JSONL_PARSER_INITIALIZATION,
                CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
                CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
                CodexFailureStage.PROVIDER_PROCESS_SPAWN,
                CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION,
                CodexFailureStage.PROVIDER_PROCESS_COLLECTION,
            }
            if (
                self.failure_stage is CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION
                and self.runner_state is not CodexRunnerState.NOT_STARTED
            ):
                raise ValueError(
                    "runner construction failure requires a not_started runner"
                )
            if (
                self.failure_stage in runner_started_stages
                and self.runner_state is not CodexRunnerState.STARTED
            ):
                raise ValueError(
                    "runner-internal failure stage requires a started runner"
                )
            if (
                self.failure_stage
                in {
                    CodexFailureStage.PROVIDER_RUNNER_ENTRY,
                    CodexFailureStage.PROVIDER_RUNTIME_PRECHECK,
                    CodexFailureStage.JSONL_PARSER_INITIALIZATION,
                    CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
                    CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
                }
                and self.invocation_state
                is not CodexInvocationState.NOT_ATTEMPTED
            ):
                raise ValueError(
                    "pre-spawn runner failure cannot record an invocation attempt"
                )
            if (
                self.failure_stage is CodexFailureStage.PROVIDER_PROCESS_SPAWN
                and self.invocation_state
                is not CodexInvocationState.SPAWN_ATTEMPTED
            ):
                raise ValueError(
                    "process spawn failure requires a spawn attempt without a process"
                )
            if (
                self.failure_stage
                in {
                    CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION,
                    CodexFailureStage.PROVIDER_PROCESS_COLLECTION,
                }
                and self.invocation_state
                is not CodexInvocationState.PROCESS_STARTED
            ):
                raise ValueError(
                    "post-spawn runner failure requires a started process"
                )

        preflight_failure_stages = {
            CodexFailureStage.PREFLIGHT,
        }
        pre_invocation_failure_stages = {
            CodexFailureStage.WORKSPACE_PREPARATION,
            CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION,
            CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION,
            CodexFailureStage.PROVIDER_RUNNER_ENTRY,
            CodexFailureStage.PROVIDER_RUNTIME_PRECHECK,
            CodexFailureStage.JSONL_PARSER_INITIALIZATION,
            CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
            CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
            CodexFailureStage.PROVIDER_ORCHESTRATION,
        }
        invocation_failure_stages = {
            CodexFailureStage.PROVIDER_PROCESS_SPAWN,
            CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION,
            CodexFailureStage.PROVIDER_PROCESS_COLLECTION,
        }
        lifecycle_dependent_failure_stages = {
            CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION,
            CodexFailureStage.PROVIDER_RUNNER_RESULT_CONSTRUCTION,
            CodexFailureStage.PROVIDER_RUNNER_RESULT_EXTRACTION,
        }
        if (
            self.failure_stage in preflight_failure_stages
            and self.execution_stage
            is not CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
        ):
            raise ValueError("preflight failure_stage requires incomplete preflight")
        if (
            self.failure_stage in pre_invocation_failure_stages
            and self.execution_stage is not CodexExecutionStage.PREFLIGHT_COMPLETED
        ):
            raise ValueError(
                "pre-invocation failure_stage requires completed preflight"
            )
        if (
            self.failure_stage in invocation_failure_stages
            and self.execution_stage
            is not CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
        ):
            raise ValueError(
                "Provider invocation failure_stage requires an invocation attempt"
            )
        if (
            self.failure_stage in lifecycle_dependent_failure_stages
            and self.schema_version not in {"1.3", "1.4"}
        ):
            raise ValueError(
                "runner handoff failure stages require Codex Evidence 1.3+"
            )
        if self.failure_stage in {
            CodexFailureStage.PROVIDER_PROCESS_SPAWN,
            *pre_invocation_failure_stages,
            *preflight_failure_stages,
        } and self.process_started:
            raise ValueError("pre-spawn failure_stage cannot have a started process")
        if (
            self.failure_stage
            in {
                CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION,
                CodexFailureStage.PROVIDER_PROCESS_COLLECTION,
            }
            and not self.process_started
        ):
            raise ValueError("post-spawn failure_stage requires a started process")

        evidence_error_only_stages = {
            CodexFailureStage.WORKSPACE_PREPARATION,
            CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION,
            CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION,
            CodexFailureStage.PROVIDER_RUNNER_ENTRY,
            CodexFailureStage.JSONL_PARSER_INITIALIZATION,
            CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
            CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
            CodexFailureStage.PROVIDER_ORCHESTRATION,
        }
        if (
            self.failure_stage in evidence_error_only_stages
            and self.failure_kind is not LiveFailureKind.EVIDENCE_ERROR
        ):
            raise ValueError(
                "initialization failure_stage requires evidence_error"
            )
        if (
            self.failure_stage in lifecycle_dependent_failure_stages
            and self.failure_kind
            not in {
                LiveFailureKind.EVIDENCE_ERROR,
                LiveFailureKind.PROCESS_CLEANUP_ERROR,
            }
        ):
            raise ValueError(
                "runner handoff failure stage requires a Harness failure kind"
            )
        if (
            self.failure_stage is CodexFailureStage.PROVIDER_RUNTIME_PRECHECK
            and self.failure_kind
            not in {
                LiveFailureKind.UNSUPPORTED_PLATFORM,
                LiveFailureKind.EVIDENCE_ERROR,
            }
        ):
            raise ValueError(
                "provider_runtime_precheck requires a runtime Harness failure"
            )
        if (
            self.failure_stage is CodexFailureStage.PROVIDER_PROCESS_SPAWN
            and self.failure_kind
            not in {
                LiveFailureKind.PROVIDER_UNAVAILABLE,
                LiveFailureKind.PROVIDER_SPAWN_ERROR,
            }
        ):
            raise ValueError("provider_process_spawn requires a spawn failure kind")
        if (
            self.failure_stage
            is CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION
            and self.failure_kind
            not in {
                LiveFailureKind.EVIDENCE_ERROR,
                LiveFailureKind.PROCESS_CLEANUP_ERROR,
            }
        ):
            raise ValueError(
                "pipe/selector failure_stage requires a Harness failure kind"
            )
        if self.cli_profile is CodexCliProfile.NOT_SELECTED:
            if (
                self.execution_stage
                is not CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
                or self.process_started
                or self.approval_policy is not None
                or self.approval_basis is not None
            ):
                raise ValueError(
                    "an unselected Codex profile cannot have execution policy Evidence"
                )
        elif self.cli_profile is CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2:
            if self.cli_version not in CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS:
                raise ValueError(
                    "explicit-never Codex profile requires an allowlisted CLI version"
                )
            if self.verified_flags != sorted(CODEX_REQUIRED_EXEC_FLAGS):
                raise ValueError(
                    "selected Codex profile requires exactly its preflight flags"
                )
            if self.execution_stage is CodexExecutionStage.PREFLIGHT_NOT_COMPLETED:
                raise ValueError(
                    "selected Codex profile requires completed preflight Evidence"
                )
            invocation_attempted = (
                self.execution_stage
                is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
            )
            if invocation_attempted and (
                self.approval_policy != "never"
                or self.approval_basis is not CodexApprovalBasis.EXPLICIT_CONFIG_NEVER
            ):
                raise ValueError(
                    "approval Evidence must match the Provider invocation stage"
                )
            if not invocation_attempted and (
                self.approval_policy is not None
                or self.approval_basis is not None
            ):
                raise ValueError(
                    "approval Evidence must be absent before Provider invocation"
                )
            if self.process_started and not invocation_attempted:
                raise ValueError(
                    "a started Codex process requires a Provider invocation attempt"
                )
        if (
            not self.web_search_disabled
            or not self.command_network_disabled
            or self.raw_stream_persisted
        ):
            raise ValueError("Codex safety and redaction flags must use their required values")
        if self.preflight_checked_at > self.started_at:
            raise ValueError("Codex preflight must not occur after process start")
        if self.completed_at < self.started_at:
            raise ValueError("Codex completed_at must not precede started_at")
        if self.unknown_event_count > self.event_count:
            raise ValueError("unknown_event_count must not exceed event_count")
        core_event_count = (
            self.thread_started_count
            + self.turn_started_count
            + self.turn_completed_count
            + self.turn_failed_count
            + self.error_event_count
        )
        if (
            core_event_count
            + sum(self.item_type_counts.values())
            + self.unknown_event_count
            != self.event_count
        ):
            raise ValueError("normalized Codex event counts must equal event_count")
        if self.verified_flags != sorted(set(self.verified_flags)):
            raise ValueError("verified_flags must be unique and sorted")
        if self.schema_version == "1.4":
            if self.terminal_event is CodexTerminalEvent.NONE:
                terminal_counts_match = (
                    self.turn_completed_count,
                    self.turn_failed_count,
                ) == (0, 0)
            elif self.terminal_event is CodexTerminalEvent.TURN_COMPLETED:
                terminal_counts_match = (
                    self.turn_completed_count,
                    self.turn_failed_count,
                    self.error_event_count,
                ) == (1, 0, 0)
            elif self.terminal_event is CodexTerminalEvent.TURN_FAILED:
                terminal_counts_match = (
                    self.turn_completed_count,
                    self.turn_failed_count,
                ) == (0, 1)
            else:
                terminal_counts_match = False
            if not terminal_counts_match:
                raise ValueError(
                    "Codex Evidence 1.4 terminal_event must match turn terminal "
                    "counts; top-level errors are independent observations"
                )
        else:
            terminal_counts = {
                CodexTerminalEvent.NONE: (0, 0, 0),
                CodexTerminalEvent.TURN_COMPLETED: (1, 0, 0),
                CodexTerminalEvent.TURN_FAILED: (0, 1, 0),
                CodexTerminalEvent.ERROR: (0, 0, 1),
            }
            if (
                self.turn_completed_count,
                self.turn_failed_count,
                self.error_event_count,
            ) != terminal_counts[self.terminal_event]:
                raise ValueError(
                    "terminal_event must match normalized terminal counts"
                )
        if not self.process_started and (
            self.exit_code is not None
            or self.event_count != 0
            or self.duration_ms != 0
            or self.terminal_event is not CodexTerminalEvent.NONE
        ):
            raise ValueError("a Provider process that was not started cannot have process Evidence")
        if self.usage_metrics.source not in {
            UsageMetricSource.PROVIDER_REPORTED,
            UsageMetricSource.NOT_AVAILABLE,
        }:
            raise ValueError("Codex Usage source must be provider_reported or not_available")
        if (
            self.usage_metrics.estimated_api_cost is not None
            or self.usage_metrics.quota_consumption is not None
        ):
            raise ValueError("Codex Evidence must not contain estimated cost or quota values")
        token_values = (
            self.usage_metrics.input_tokens,
            self.usage_metrics.cached_input_tokens,
            self.usage_metrics.output_tokens,
            self.usage_metrics.reasoning_output_tokens,
        )
        if (
            self.usage_metrics.source is UsageMetricSource.PROVIDER_REPORTED
            and all(value is None for value in token_values)
        ):
            raise ValueError("provider_reported Codex Usage requires at least one token value")

        allowed_failure_kinds = {
            LiveFailureKind.PROVIDER_TURN_FAILED,
            LiveFailureKind.PROVIDER_CLI_NONZERO,
            LiveFailureKind.PROVIDER_SIGNAL_TERMINATION,
            LiveFailureKind.PROVIDER_TIMEOUT,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            LiveFailureKind.PROVIDER_SPAWN_ERROR,
            LiveFailureKind.PROVIDER_INPUT_ERROR,
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
            LiveFailureKind.PROCESS_CLEANUP_ERROR,
            LiveFailureKind.EVIDENCE_ERROR,
            LiveFailureKind.UNSUPPORTED_PLATFORM,
        }
        if self.status is ProviderExecutionStatus.SUCCEEDED:
            if self.failure_kind is not LiveFailureKind.NONE or self.exit_code != 0:
                raise ValueError("successful Codex Evidence requires failure_kind none and exit 0")
            if self.event_count < 3:
                raise ValueError("successful Codex Evidence requires the core lifecycle events")
            if (
                self.thread_started_count,
                self.turn_started_count,
                self.terminal_event,
                self.turn_completed_count,
                self.turn_failed_count,
                self.error_event_count,
            ) != (1, 1, CodexTerminalEvent.TURN_COMPLETED, 1, 0, 0):
                raise ValueError(
                    "successful Codex Evidence requires one complete lifecycle"
                )
            if not self.process_started:
                raise ValueError("successful Codex Evidence requires a started process")
            if not self.termination.process_group_cleared:
                raise ValueError("successful Codex Evidence requires process cleanup")
            if self.stdout_limit_exceeded:
                raise ValueError("successful Codex Evidence cannot exceed stdout limit")
        elif self.failure_kind is LiveFailureKind.NONE:
            raise ValueError("failed Codex Evidence requires a failure kind")
        elif self.failure_kind not in allowed_failure_kinds:
            raise ValueError("Codex Evidence contains a non-Provider failure kind")
        if (
            self.failure_kind
            in {
                LiveFailureKind.PROVIDER_UNAVAILABLE,
                LiveFailureKind.PROVIDER_SPAWN_ERROR,
                LiveFailureKind.UNSUPPORTED_PLATFORM,
            }
            and self.process_started
        ):
            raise ValueError("pre-spawn Provider failures must not start a process")
        if (
            self.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
            and self.termination.reason is not TerminationReason.TIMEOUT
        ):
            raise ValueError("provider_timeout requires timeout termination Evidence")
        if (
            self.failure_kind is LiveFailureKind.PROVIDER_CLI_NONZERO
            and (self.exit_code is None or self.exit_code <= 0)
        ):
            raise ValueError("provider_cli_nonzero requires a positive non-zero exit code")
        if (
            self.failure_kind is LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
            and (self.exit_code is None or self.exit_code >= 0)
        ):
            raise ValueError("provider_signal_termination requires a negative return code")
        if (
            self.failure_kind is LiveFailureKind.PROVIDER_TURN_FAILED
            and self.terminal_event
            not in {CodexTerminalEvent.TURN_FAILED, CodexTerminalEvent.ERROR}
        ):
            raise ValueError("provider_turn_failed requires a failed terminal event")
        if (
            self.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
            and not self.stdout_limit_exceeded
        ):
            raise ValueError("provider_output_limit requires stdout_limit_exceeded")
        if (
            self.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
            and self.termination.process_group_cleared
        ):
            raise ValueError("process_cleanup_error requires an uncleared process group")
        return self


class LiveFailureDiagnostic(ContractModel):
    """Standalone redacted diagnosis when strict paired Live outputs cannot be built."""

    schema_version: Literal["1.0"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    failure_kind: Literal[
        LiveFailureKind.EVIDENCE_ERROR,
        LiveFailureKind.PROCESS_CLEANUP_ERROR,
    ]
    diagnostic_code: LiveDiagnosticCode
    failure_stage: DiagnosticFailureStage
    runner_state: DiagnosticRunnerState
    invocation_state: DiagnosticInvocationState
    cleanup_state: DiagnosticCleanupState
    workspace_lifecycle: WorkspaceLifecycle
    paired_artifacts_published: Literal[False]
    gate_executed: StrictBool
    provider_activity_determined: ProviderActivityDetermination
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def diagnostic_timestamp_must_use_datetime_or_iso_string(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError(
                "Failure Diagnostic timestamp must be an ISO string or datetime value"
            )
        return value

    @field_validator("created_at")
    @classmethod
    def diagnostic_timestamp_must_be_timezone_aware(
        cls,
        value: datetime,
    ) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Failure Diagnostic timestamp must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def diagnostic_lifecycle_must_be_truthful(self) -> LiveFailureDiagnostic:
        lifecycle_values = (
            self.runner_state,
            self.invocation_state,
            self.cleanup_state,
            self.failure_stage,
        )
        contains_unknown = any(value.value == "unknown" for value in lifecycle_values)
        expected_determination = (
            ProviderActivityDetermination.UNKNOWN
            if contains_unknown
            else ProviderActivityDetermination.DETERMINED
        )
        if self.provider_activity_determined is not expected_determination:
            raise ValueError(
                "provider_activity_determined must match lifecycle observations"
            )

        invocation_attempted = self.invocation_state in {
            DiagnosticInvocationState.SPAWN_ATTEMPTED,
            DiagnosticInvocationState.PROCESS_STARTED,
        }
        if (
            self.runner_state is DiagnosticRunnerState.NOT_STARTED
            and invocation_attempted
        ):
            raise ValueError("a not-started runner cannot have an invocation attempt")
        if (
            self.invocation_state is DiagnosticInvocationState.PROCESS_STARTED
            and self.runner_state is not DiagnosticRunnerState.STARTED
        ):
            raise ValueError("a started process requires a started runner")
        if self.cleanup_state in {
            DiagnosticCleanupState.CLEARED,
            DiagnosticCleanupState.FAILED,
        } and self.invocation_state is not DiagnosticInvocationState.PROCESS_STARTED:
            raise ValueError("a Provider cleanup result requires a started process")
        if (
            self.invocation_state
            in {
                DiagnosticInvocationState.NOT_ATTEMPTED,
                DiagnosticInvocationState.SPAWN_ATTEMPTED,
            }
            and self.cleanup_state is not DiagnosticCleanupState.NOT_APPLICABLE
        ):
            raise ValueError(
                "a lifecycle without a started process requires not_applicable cleanup"
            )
        if (
            self.cleanup_state is DiagnosticCleanupState.FAILED
        ) is not (
            self.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
        ):
            raise ValueError(
                "process_cleanup_error must exactly match an observed failed cleanup"
            )
        return self


class LiveRunArtifact(ContractModel):
    """Versioned Phase 3 Evidence with a one-way hash reference to its Recording."""

    schema_version: Literal["1.0"]
    run_id: StrictStr = Field(min_length=1)
    experiment_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    repetition_index: StrictInt = Field(ge=0)
    workflow: Literal[Workflow.ONE_SHOT]
    provider: Literal[Provider.CODEX]
    execution_mode: Literal[ExecutionMode.LIVE]
    overall_status: LiveOverallStatus
    failure_kind: LiveFailureKind
    started_at: datetime
    completed_at: datetime
    spec_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: StrictBool
    runner: RunnerSettings
    codex: CodexExecutionEvidence
    gate_commands: list[CommandEvidence]
    diff: DiffEvidence
    metrics: RunMetrics | None
    workspace_lifecycle: WorkspaceLifecycle
    recording_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    raw_provider_output_persisted: StrictBool

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def live_timestamps_must_use_datetime_or_iso_string(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("Live timestamps must be ISO strings or datetime values")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def live_timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Live timestamps must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def live_status_metrics_and_evidence_must_match(self) -> LiveRunArtifact:
        if not self.prompt_redacted or self.raw_provider_output_persisted:
            raise ValueError("Live Prompt/provider redaction flags must use required values")
        if self.completed_at < self.started_at:
            raise ValueError("Live completed_at must not precede started_at")
        expected_failure = {
            LiveOverallStatus.PASSED: LiveFailureKind.NONE,
            LiveOverallStatus.FAILED: LiveFailureKind.QUALITY_GATE_FAILURE,
        }
        if (
            self.overall_status in expected_failure
            and self.failure_kind is not expected_failure[self.overall_status]
        ):
            raise ValueError("Live overall_status and failure_kind are inconsistent")
        provider_failure_kinds = {
            LiveFailureKind.PROVIDER_TURN_FAILED,
            LiveFailureKind.PROVIDER_CLI_NONZERO,
            LiveFailureKind.PROVIDER_SIGNAL_TERMINATION,
            LiveFailureKind.PROVIDER_TIMEOUT,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            LiveFailureKind.PROVIDER_SPAWN_ERROR,
            LiveFailureKind.PROVIDER_INPUT_ERROR,
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        if (
            self.overall_status is LiveOverallStatus.PROVIDER_ERROR
        ) is not (self.failure_kind in provider_failure_kinds):
            raise ValueError("provider_error status must match a Provider failure kind")
        harness_failure_kinds = {
            LiveFailureKind.PROCESS_CLEANUP_ERROR,
            LiveFailureKind.GATE_HARNESS_ERROR,
            LiveFailureKind.EVIDENCE_ERROR,
            LiveFailureKind.UNSUPPORTED_PLATFORM,
        }
        if (
            self.overall_status is LiveOverallStatus.HARNESS_ERROR
        ) is not (self.failure_kind in harness_failure_kinds):
            raise ValueError("harness_error status must match a Harness failure kind")
        if self.overall_status is LiveOverallStatus.PROVIDER_ERROR:
            if self.codex.failure_kind is not self.failure_kind:
                raise ValueError("Artifact and Codex Provider failure kinds must match")
            if self.gate_commands:
                raise ValueError("quality Gates must not run after a Provider failure")
        if (
            self.overall_status in {LiveOverallStatus.PASSED, LiveOverallStatus.FAILED}
            and self.codex.status is not ProviderExecutionStatus.SUCCEEDED
        ):
            raise ValueError("quality result requires successful Codex execution")
        if (
            self.codex.status is ProviderExecutionStatus.FAILED
            and self.gate_commands
        ):
            raise ValueError("quality Gates must not run after failed Codex execution")
        if (
            self.codex.execution_stage
            is CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
            and self.workspace_lifecycle is not WorkspaceLifecycle.NOT_CREATED
        ):
            raise ValueError(
                "preflight_not_completed requires a not_created Workspace"
            )
        if (
            self.codex.execution_stage
            is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
            and self.workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
        ):
            raise ValueError(
                "provider_invocation_attempted requires a created Workspace"
            )
        if (
            self.workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
            and self.gate_commands
        ):
            raise ValueError(
                "not_created Workspace cannot contain Provider or Gate execution"
            )
        if (
            self.workspace_lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
            and self.overall_status is not LiveOverallStatus.HARNESS_ERROR
        ):
            raise ValueError("cleanup_failed Workspace requires a Harness error")
        if self.failure_kind in {
            LiveFailureKind.PROCESS_CLEANUP_ERROR,
            LiveFailureKind.UNSUPPORTED_PLATFORM,
        } and (
            self.codex.status is not ProviderExecutionStatus.FAILED
            or self.codex.failure_kind is not self.failure_kind
        ):
            raise ValueError("Provider Harness failure must match Codex Evidence")
        if self.failure_kind is LiveFailureKind.GATE_HARNESS_ERROR:
            abnormal_gate = any(
                command.status
                not in {CommandStatus.PASSED, CommandStatus.FAILED}
                or not command.termination.process_group_cleared
                for command in self.gate_commands
            )
            if (
                self.codex.status is not ProviderExecutionStatus.SUCCEEDED
                or not self.gate_commands
                or not abnormal_gate
            ):
                raise ValueError(
                    "gate_harness_error requires successful Codex and an abnormal Gate"
                )

        commands_completed_normally = bool(self.gate_commands) and all(
            command.status in {CommandStatus.PASSED, CommandStatus.FAILED}
            and command.termination.process_group_cleared
            for command in self.gate_commands
        )
        if self.overall_status in {
            LiveOverallStatus.PASSED,
            LiveOverallStatus.FAILED,
        } and (
            not commands_completed_normally
            or not self.diff.line_counts_complete
            or self.workspace_lifecycle is not WorkspaceLifecycle.REMOVED
        ):
            raise ValueError(
                "quality result requires complete Gate, diff, and Workspace Evidence"
            )
        metrics_permitted = (
            self.codex.status is ProviderExecutionStatus.SUCCEEDED
            and commands_completed_normally
            and self.failure_kind
            in {LiveFailureKind.NONE, LiveFailureKind.QUALITY_GATE_FAILURE}
            and self.diff.line_counts_complete
            and self.workspace_lifecycle is WorkspaceLifecycle.REMOVED
        )
        if metrics_permitted != (self.metrics is not None):
            raise ValueError("Live metrics presence is inconsistent with Evidence completeness")
        if self.metrics is not None:
            acceptance = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.ACCEPTANCE
            ]
            regression = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.REGRESSION
            ]
            lint = [
                command for command in self.gate_commands if command.gate is GateKind.LINT
            ]
            typecheck = [
                command
                for command in self.gate_commands
                if command.gate is GateKind.TYPECHECK
            ]
            expected_quality_pass = all(
                command.status is CommandStatus.PASSED for command in self.gate_commands
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
            if (
                self.metrics.quality_gate_pass is not expected_quality_pass
                or actual_counts != expected_counts
                or self.metrics.agent_duration_ms != self.codex.duration_ms
                or self.metrics.total_duration_ms
                != self.metrics.agent_duration_ms + self.metrics.evaluation_duration_ms
                or self.metrics.agent_call_count != 1
                or self.metrics.retry_count != 0
                or self.metrics.changed_files != self.diff.changed_files
                or self.metrics.added_lines != self.diff.added_lines
                or self.metrics.deleted_lines != self.diff.deleted_lines
                or self.metrics.usage_metrics != self.codex.usage_metrics
            ):
                raise ValueError("Phase 3 RunMetrics do not match Live Evidence")
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
