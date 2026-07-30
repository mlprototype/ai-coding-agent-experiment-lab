"""Phase 6 versioned contracts and read-only cross-artifact validation.

This module intentionally has no Provider, Gate, publication, or fixture
materialization entry point.  Slice 6A defines and validates persisted
contracts only.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, NoReturn

import yaml
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.campaign import (
    CampaignEvent,
    CampaignFinishedEvent,
    CampaignRunEvent,
    CampaignRunStatus,
    CampaignStopReason,
    load_campaign,
)
from agentlab.models import (
    CodexExecutionEvidence,
    CommandEvidence,
    ContractModel,
    DiffEvidence,
    ExecutionMode,
    LiveRunArtifact,
    Provider,
    ProviderExecutionStatus,
    ReasoningEffort,
    RunMetrics,
    RunnerSettings,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.recording import ReplayRecording, load_replay_recording
from agentlab.workflow import (
    LoadedWorkflowSpec,
    WorkflowExperimentSpec,
    WorkflowPlan,
    WorkflowPlanError,
    WorkflowSpecError,
    _strict_json,
    load_workflow_plan,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$|^[0-9a-f]{64}$"
CANONICAL_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


class Phase6ContractError(ValueError):
    """Raised when a Phase 6 contract or relationship is invalid."""


class Phase6PathError(Phase6ContractError):
    """Raised when a listed Phase 6 path crosses its fixed root."""


class Language(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVA = "java"


class LanguageStatus(StrEnum):
    NOT_READY = "not_ready"
    READY_NOT_RUN = "ready_not_run"
    EVALUATED = "evaluated"
    BLOCKED = "blocked"


class SourceClass(StrEnum):
    PRIMARY = "primary"
    HISTORICAL = "historical"


class ProviderEvaluationStatus(StrEnum):
    EVALUATED = "evaluated"
    NOT_EVALUATED = "not_evaluated"


class Phase6OverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PROVIDER_ERROR = "provider_error"
    HARNESS_ERROR = "harness_error"
    REJECTED = "rejected"


class Phase6FailureKind(StrEnum):
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
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    GATE_HARNESS_ERROR = "gate_harness_error"
    EVIDENCE_ERROR = "evidence_error"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


class Phase6CampaignOutcome(StrEnum):
    SUCCESS = "success"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    HARNESS_FAILURE = "harness_failure"
    CLEANUP_FAILURE = "cleanup_failure"
    HUMAN_INTERRUPTION = "human_interruption"
    STOP_CONDITION = "stop_condition"


class GateNotExecutedReason(StrEnum):
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    OUTPUT_CONTRACT_VIOLATION = "output_contract_violation"
    PRE_GATE_HARNESS_FAILURE = "pre_gate_harness_failure"
    INPUT_CHANGED = "input_changed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"


class ToolchainComponentRole(StrEnum):
    PYTHON_RUNTIME = "python_runtime"
    NODE_RUNTIME = "node_runtime"
    TYPESCRIPT_COMPILER = "typescript_compiler"
    JAVA_RUNTIME = "java_runtime"
    JAVA_COMPILER = "java_compiler"


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_canonical_timestamp(value: object, field_name: str) -> object:
    if not isinstance(value, str) or not CANONICAL_RFC3339_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must use canonical UTC RFC 3339 "
            "YYYY-MM-DDTHH:MM:SS.ffffffZ"
        )
    return value


def _relative_file(value: str, field_name: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or "\\" in value
    ):
        raise ValueError(f"{field_name} must be a canonical relative POSIX file path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value in {".", "./"}
        or ".." in posix.parts
        or posix.as_posix() != value
    ):
        raise ValueError(f"{field_name} must remain below the fixed root")
    return value


class ToolchainComponent(ContractModel):
    role: ToolchainComponentRole
    resolved_executable_path: StrictStr = Field(min_length=1)
    executable_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    version_argv: list[StrictStr] = Field(min_length=1)
    exact_version: StrictStr = Field(min_length=1)
    version_output_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    package_version: StrictStr | None = None
    package_fingerprint: StrictStr | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def component_contract_is_complete(self) -> ToolchainComponent:
        path = Path(self.resolved_executable_path)
        if not path.is_absolute() or "\x00" in self.resolved_executable_path:
            raise ValueError("resolved_executable_path must be an absolute path")
        if self.version_argv[0] != self.resolved_executable_path:
            raise ValueError("version argv must use the resolved executable path")
        compiler = self.role is ToolchainComponentRole.TYPESCRIPT_COMPILER
        if compiler is not (
            self.package_version is not None
            and self.package_fingerprint is not None
        ):
            raise ValueError(
                "typescript_compiler requires package version and package fingerprint"
            )
        return self


class ToolchainIdentity(ContractModel):
    os: StrictStr = Field(pattern=r"^[a-z0-9_]+$")
    architecture: StrictStr = Field(pattern=r"^[a-z0-9_]+$")
    gate_path_entries: list[StrictStr] = Field(min_length=1)
    workspace_executable_lookup_allowed: Literal[False]
    components: list[ToolchainComponent] = Field(min_length=1)
    fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def roles_and_fingerprint_are_canonical(self) -> ToolchainIdentity:
        if len(self.gate_path_entries) != len(set(self.gate_path_entries)):
            raise ValueError("Gate PATH entries must be unique")
        if any(
            not Path(entry).is_absolute() or "\x00" in entry
            for entry in self.gate_path_entries
        ):
            raise ValueError("Gate PATH entries must be absolute")
        roles = [component.role for component in self.components]
        if roles != sorted(set(roles), key=lambda role: role.value):
            raise ValueError("toolchain components must have unique sorted roles")
        expected = hashlib.sha256(
            canonical_json_bytes(
                {
                    "architecture": self.architecture,
                    "components": [
                        component.model_dump(mode="json")
                        for component in self.components
                    ],
                    "gate_path_entries": self.gate_path_entries,
                    "os": self.os,
                    "workspace_executable_lookup_allowed":
                    self.workspace_executable_lookup_allowed,
                }
            )
        ).hexdigest()
        if self.fingerprint != expected:
            raise ValueError("toolchain fingerprint does not match components")
        return self


class FixtureManifest(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain: ToolchainIdentity

    @model_validator(mode="after")
    def language_requires_exact_toolchain_roles(self) -> FixtureManifest:
        expected = {
            Language.PYTHON: {ToolchainComponentRole.PYTHON_RUNTIME},
            Language.TYPESCRIPT: {
                ToolchainComponentRole.NODE_RUNTIME,
                ToolchainComponentRole.TYPESCRIPT_COMPILER,
            },
            Language.JAVA: {
                ToolchainComponentRole.JAVA_RUNTIME,
                ToolchainComponentRole.JAVA_COMPILER,
            },
        }[self.language]
        actual = {component.role for component in self.toolchain.components}
        if actual != expected:
            raise ValueError(
                f"{self.language.value} Fixture requires its exact toolchain roles"
            )
        return self


class EditablePathPolicy(ContractModel):
    path: StrictStr
    allow_create: StrictBool
    allow_delete: StrictBool

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "editable path")


class ProtectedPathPolicy(ContractModel):
    path: StrictStr
    role: Literal[
        "acceptance",
        "regression",
        "lint",
        "typecheck",
        "gate_helper",
        "lockfile",
        "configuration",
        "fixture_manifest",
    ]

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "protected path")


class DiffPolicy(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    editable_paths: list[EditablePathPolicy] = Field(min_length=1)
    protected_paths: list[ProtectedPathPolicy] = Field(min_length=1)
    reject_unclassified_paths: Literal[True]
    reject_symlinks: Literal[True]
    reject_hardlinks: Literal[True]
    reject_special_files: Literal[True]

    @model_validator(mode="after")
    def paths_are_disjoint_and_sorted(self) -> DiffPolicy:
        editable = [item.path for item in self.editable_paths]
        protected = [item.path for item in self.protected_paths]
        if editable != sorted(set(editable)):
            raise ValueError("editable paths must be unique and sorted")
        if protected != sorted(set(protected)):
            raise ValueError("protected paths must be unique and sorted")
        if set(editable).intersection(protected):
            raise ValueError("editable and protected paths must be disjoint")
        return self


class GateAcceptanceSummary(ContractModel):
    acceptance_failed_as_expected: Literal[True]
    regression_passed: Literal[True]
    lint_passed: Literal[True]
    typecheck_passed: Literal[True]
    reference_all_gates_passed: Literal[True]
    source_unchanged: Literal[True]
    workspace_cleanup_succeeded: Literal[True]


class FixtureAcceptanceRecord(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    acceptance_agentlab_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_source_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_in_provider_workspace: Literal[False]
    toolchain: ToolchainIdentity
    result: GateAcceptanceSummary
    verified_at: datetime

    @field_validator("verified_at", mode="before")
    @classmethod
    def verified_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "verified_at")

    @field_validator("verified_at")
    @classmethod
    def verified_at_is_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("verified_at must use UTC")
        return value


class WorkflowExperimentSpecV2_1(WorkflowExperimentSpec):
    schema_version: Literal["2.1"]  # type: ignore[assignment]
    language: Language
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_manifest_path: StrictStr
    fixture_acceptance_path: StrictStr
    diff_policy_path: StrictStr

    @field_validator(
        "fixture_manifest_path",
        "fixture_acceptance_path",
        "diff_policy_path",
    )
    @classmethod
    def phase6_input_path_is_relative(
        cls,
        value: str,
        info: Any,
    ) -> str:
        return _relative_file(value, str(info.field_name))


class WorkflowPlanV1_2(WorkflowPlan):
    schema_version: Literal["1.2"]  # type: ignore[assignment]
    experiment_spec_schema_version: Literal["2.1"]  # type: ignore[assignment]
    language: Language
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    gate_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    reference_solution_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)


WorkflowSpecContract = WorkflowExperimentSpec | WorkflowExperimentSpecV2_1
WorkflowPlanContract = WorkflowPlan | WorkflowPlanV1_2


@dataclass(frozen=True)
class LoadedWorkflowSpecContract:
    spec: WorkflowSpecContract
    sha256: str


class Phase6CampaignStartedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[0]
    event_type: Literal["campaign_started"]
    experiment_id: StrictStr
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    planned_run_count: StrictInt = Field(gt=0)
    planned_provider_call_count: StrictInt = Field(gt=0)
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


class Phase6CampaignRunEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["run_state"]
    run_id: StrictStr
    task_id: StrictStr
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    status: CampaignRunStatus
    outcome: Phase6CampaignOutcome | None
    stop_reason: CampaignStopReason | None
    provider_call_count: StrictInt | None = Field(default=None, ge=0, le=1)
    gate_executed: StrictBool
    counted_failure: StrictBool
    fail_fast_applies: StrictBool
    max_failures_applies: StrictBool
    failure_kind: Phase6FailureKind | None
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")

    @model_validator(mode="after")
    def state_transition_is_explicit(self) -> Phase6CampaignRunEvent:
        if self.status is CampaignRunStatus.PLANNED:
            raise ValueError("Campaign recording must not append planned states")
        if self.status is CampaignRunStatus.STARTED:
            if (
                self.outcome is not None
                or self.stop_reason is not None
                or self.provider_call_count is not None
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError("started state must not predict terminal observations")
            return self

        if self.stop_reason is CampaignStopReason.INPUT_CHANGED:
            if (
                self.status is not CampaignRunStatus.NOT_RUN
                or self.outcome is not Phase6CampaignOutcome.STOP_CONDITION
                or self.provider_call_count != 0
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError(
                    "input_changed is a zero-call, zero-Gate Harness safety stop "
                    "outside failure counting"
                )
            return self

        if self.outcome is Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.stop_reason is not None
                or self.provider_call_count != 1
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind
                is not Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            ):
                raise ValueError(
                    "output_contract_violation requires one Provider call, no Gate, "
                    "and normal failure counting"
                )
            return self

        if self.status is CampaignRunStatus.NOT_RUN:
            if (
                self.outcome is not Phase6CampaignOutcome.STOP_CONDITION
                or self.stop_reason in {None, CampaignStopReason.NONE}
                or self.provider_call_count != 0
                or self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not None
            ):
                raise ValueError("not_run requires a zero-call stop condition")
            return self

        if self.status is CampaignRunStatus.INTERRUPTED:
            if (
                self.outcome is not Phase6CampaignOutcome.HUMAN_INTERRUPTION
                or self.provider_call_count not in {0, 1, None}
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
            ):
                raise ValueError("interrupted state must remain outside failure counting")
            return self

        if (
            self.status not in {
                CampaignRunStatus.COMPLETED,
                CampaignRunStatus.FAILED,
            }
            or self.stop_reason is not None
        ):
            raise ValueError("attempted terminal state is incomplete")

        if self.outcome is Phase6CampaignOutcome.SUCCESS:
            if (
                self.status is not CampaignRunStatus.COMPLETED
                or self.provider_call_count != 1
                or not self.gate_executed
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.NONE
            ):
                raise ValueError("success requires one call, Gate completion, and no failure")
            return self

        if self.outcome is Phase6CampaignOutcome.QUALITY_GATE_FAILURE:
            if (
                self.status is not CampaignRunStatus.COMPLETED
                or self.provider_call_count != 1
                or not self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.QUALITY_GATE_FAILURE
            ):
                raise ValueError(
                    "quality_gate_failure requires one call, Gate completion, "
                    "and normal failure counting"
                )
            return self

        provider_failures = {
            Phase6FailureKind.PROVIDER_TURN_FAILED,
            Phase6FailureKind.PROVIDER_CLI_NONZERO,
            Phase6FailureKind.PROVIDER_SIGNAL_TERMINATION,
            Phase6FailureKind.PROVIDER_UNAVAILABLE,
            Phase6FailureKind.PROVIDER_SPAWN_ERROR,
            Phase6FailureKind.PROVIDER_INPUT_ERROR,
            Phase6FailureKind.PROVIDER_PROTOCOL_ERROR,
            Phase6FailureKind.PROVIDER_OUTPUT_LIMIT,
        }
        if self.outcome is Phase6CampaignOutcome.PROVIDER_FAILURE:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind not in provider_failures
            ):
                raise ValueError(
                    "provider_failure requires no Gate and normal failure counting"
                )
            return self

        if self.outcome is Phase6CampaignOutcome.PROVIDER_TIMEOUT:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.provider_call_count != 1
                or self.gate_executed
                or not self.counted_failure
                or not self.fail_fast_applies
                or not self.max_failures_applies
                or self.failure_kind is not Phase6FailureKind.PROVIDER_TIMEOUT
            ):
                raise ValueError(
                    "provider_timeout requires one call, no Gate, "
                    "and normal failure counting"
                )
            return self

        if self.outcome in {
            Phase6CampaignOutcome.HARNESS_FAILURE,
            Phase6CampaignOutcome.CLEANUP_FAILURE,
        }:
            if (
                self.status is not CampaignRunStatus.FAILED
                or self.counted_failure
                or self.fail_fast_applies
                or self.max_failures_applies
            ):
                raise ValueError("Harness failures stop independently of failure counters")
            if (
                self.outcome is Phase6CampaignOutcome.CLEANUP_FAILURE
                and self.failure_kind
                is not Phase6FailureKind.PROCESS_CLEANUP_ERROR
            ):
                raise ValueError(
                    "cleanup failure has priority and requires process_cleanup_error"
                )
            if (
                self.outcome is Phase6CampaignOutcome.HARNESS_FAILURE
                and self.failure_kind
                not in {
                    Phase6FailureKind.GATE_HARNESS_ERROR,
                    Phase6FailureKind.EVIDENCE_ERROR,
                    Phase6FailureKind.UNSUPPORTED_PLATFORM,
                }
            ):
                raise ValueError("Harness failure requires a Harness failure kind")
            return self

        raise ValueError("terminal Campaign outcome is unsupported")


class Phase6CampaignFinishedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["campaign_finished"]
    experiment_id: StrictStr
    stop_reason: CampaignStopReason
    attempted_run_count: StrictInt = Field(ge=0)
    provider_call_count: StrictInt = Field(ge=0)
    provider_call_count_unknown_runs: StrictInt = Field(ge=0)
    counted_failure_count: StrictInt = Field(ge=0)
    retry_count: Literal[0]
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


Phase6CampaignEvent = Annotated[
    Phase6CampaignStartedEvent
    | Phase6CampaignRunEvent
    | Phase6CampaignFinishedEvent,
    Field(discriminator="event_type"),
]
_PHASE6_CAMPAIGN_ADAPTER: TypeAdapter[Phase6CampaignEvent] = TypeAdapter(
    Phase6CampaignEvent
)


@dataclass(frozen=True)
class LoadedPhase6Campaign:
    events: tuple[Phase6CampaignEvent, ...]

    @property
    def started(self) -> Phase6CampaignStartedEvent:
        event = self.events[0]
        assert isinstance(event, Phase6CampaignStartedEvent)
        return event

    @property
    def finished(self) -> Phase6CampaignFinishedEvent:
        event = self.events[-1]
        assert isinstance(event, Phase6CampaignFinishedEvent)
        return event


CampaignContract = list[CampaignEvent] | LoadedPhase6Campaign


class Phase6RecordingStartedEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[0]
    event_type: Literal["run_started"]
    run_id: StrictStr
    experiment_id: StrictStr
    task_id: StrictStr
    language: Language
    workflow: Workflow
    provider: Literal[Provider.CODEX]
    repetition_index: StrictInt = Field(ge=0)
    execution_mode: Literal[ExecutionMode.LIVE]
    occurred_at: datetime
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: Literal[True]
    requested_model: StrictStr
    requested_reasoning_effort: ReasoningEffort
    cli_version: StrictStr

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")


class Phase6RecordingTerminalEvent(ContractModel):
    schema_version: Literal["1.2"]
    sequence: Literal[1]
    event_type: Literal["run_completed", "run_failed"]
    run_id: StrictStr
    experiment_id: StrictStr
    occurred_at: datetime
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    codex: CodexExecutionEvidence
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    metrics: RunMetrics | None

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.5":
            raise ValueError(
                "Recording 1.2 requires unchanged CodexExecutionEvidence 1.5"
            )
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "occurred_at")

    @model_validator(mode="after")
    def terminal_state_is_coherent(self) -> Phase6RecordingTerminalEvent:
        if self.event_type == "run_completed":
            if (
                self.overall_status
                not in {Phase6OverallStatus.PASSED, Phase6OverallStatus.FAILED}
                or self.failure_kind
                not in {
                    Phase6FailureKind.NONE,
                    Phase6FailureKind.QUALITY_GATE_FAILURE,
                }
                or not self.gate_executed
                or self.gate_not_executed_reason is not None
                or self.metrics is None
            ):
                raise ValueError("run_completed requires a complete quality result")
            return self
        if (
            self.overall_status
            not in {
                Phase6OverallStatus.PROVIDER_ERROR,
                Phase6OverallStatus.HARNESS_ERROR,
                Phase6OverallStatus.REJECTED,
            }
            or self.failure_kind
            in {
                Phase6FailureKind.NONE,
                Phase6FailureKind.QUALITY_GATE_FAILURE,
            }
            or self.metrics is not None
        ):
            raise ValueError("run_failed requires a non-quality terminal failure")
        if (
            self.overall_status is Phase6OverallStatus.REJECTED
        ) is not (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
        ):
            raise ValueError(
                "rejected status is reserved for output_contract_violation"
            )
        if (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            and (
                self.overall_status is not Phase6OverallStatus.REJECTED
                or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
                or self.gate_executed
                or self.gate_not_executed_reason
                is not GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
            )
        ):
            raise ValueError(
                "output_contract_violation requires successful Codex and no Gate"
            )
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        return self


@dataclass(frozen=True)
class Phase6Recording:
    started: Phase6RecordingStartedEvent
    terminal: Phase6RecordingTerminalEvent


RecordingContract = ReplayRecording | Phase6Recording


class LiveRunArtifactV1_2(ContractModel):
    """Phase 6 Live artifact; nested Codex Evidence remains version 1.5."""

    schema_version: Literal["1.2"]
    run_id: StrictStr
    experiment_id: StrictStr
    task_id: StrictStr
    language: Language
    repetition_index: StrictInt = Field(ge=0)
    workflow: Workflow
    provider: Literal[Provider.CODEX]
    execution_mode: Literal[ExecutionMode.LIVE]
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    started_at: datetime
    completed_at: datetime
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    spec_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_acceptance_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    diff_policy_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_bytes: StrictInt = Field(gt=0)
    prompt_redacted: Literal[True]
    runner: RunnerSettings
    codex: CodexExecutionEvidence
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    gate_commands: list[CommandEvidence]
    diff: DiffEvidence
    metrics: RunMetrics | None
    workspace_lifecycle: WorkspaceLifecycle
    recording_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    raw_provider_output_persisted: Literal[False]

    @field_validator("codex", mode="before")
    @classmethod
    def nested_codex_schema_is_1_5(cls, value: object) -> object:
        schema_version: object
        if isinstance(value, CodexExecutionEvidence):
            schema_version = value.schema_version
        elif isinstance(value, dict):
            schema_version = value.get("schema_version")
        else:
            schema_version = None
        if schema_version != "1.5":
            raise ValueError(
                "LiveRunArtifact 1.2 requires unchanged "
                "CodexExecutionEvidence 1.5"
            )
        return value

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def artifact_state_is_coherent(self) -> LiveRunArtifactV1_2:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.gate_executed != bool(self.gate_commands):
            raise ValueError("gate_executed must match Gate Evidence presence")
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        if (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
            and (
                self.overall_status is not Phase6OverallStatus.REJECTED
                or self.codex.status is not ProviderExecutionStatus.SUCCEEDED
                or self.gate_executed
                or self.gate_not_executed_reason
                is not GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
                or self.metrics is not None
            )
        ):
            raise ValueError(
                "output_contract_violation requires rejected status, "
                "successful Codex, no Gate, and no Metrics"
            )
        if (
            self.overall_status is Phase6OverallStatus.REJECTED
        ) is not (
            self.failure_kind is Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
        ):
            raise ValueError(
                "rejected status is reserved for output_contract_violation"
            )
        if self.overall_status in {
            Phase6OverallStatus.PASSED,
            Phase6OverallStatus.FAILED,
        } and (not self.gate_executed or self.metrics is None):
            raise ValueError("quality result requires Gate Evidence and Metrics")
        if (
            self.overall_status is Phase6OverallStatus.PASSED
            and self.failure_kind is not Phase6FailureKind.NONE
        ) or (
            self.overall_status is Phase6OverallStatus.FAILED
            and self.failure_kind is not Phase6FailureKind.QUALITY_GATE_FAILURE
        ):
            raise ValueError("quality status and failure kind differ")
        if self.overall_status in {
            Phase6OverallStatus.PROVIDER_ERROR,
            Phase6OverallStatus.HARNESS_ERROR,
        } and (
            self.failure_kind
            in {
                Phase6FailureKind.NONE,
                Phase6FailureKind.QUALITY_GATE_FAILURE,
                Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION,
            }
            or self.metrics is not None
        ):
            raise ValueError("non-quality failures must not contain Metrics")
        return self


LiveRunArtifactContract = LiveRunArtifact | LiveRunArtifactV1_2


class HistoricalVerificationRecord(ContractModel):
    schema_version: Literal["1.0"]
    source_class: Literal[SourceClass.HISTORICAL]
    language: Language
    experiment_id: StrictStr
    source_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    verification_agentlab_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    toolchain_version_status: Literal["unknown"]
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    campaign_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    report_json_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    report_markdown_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    strict_schema_validation_passed: Literal[True]
    cross_artifact_validation_passed: Literal[True]
    artifact_regenerated: Literal[False]
    campaign_reexecuted: Literal[False]
    validation_commands: list[list[StrictStr]] = Field(min_length=1)
    verified_at: datetime

    @field_validator("verified_at", mode="before")
    @classmethod
    def verified_at_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "verified_at")


class ArtifactReference(ContractModel):
    role: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]*$")
    path: StrictStr
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "Artifact path")


class PrimarySuiteSource(ContractModel):
    source_class: Literal[SourceClass.PRIMARY]
    language: Language
    expected_language_status: LanguageStatus
    blocker: StrictStr | None = None
    spec: ArtifactReference | None = None
    fixture_manifest: ArtifactReference | None = None
    fixture_acceptance: ArtifactReference | None = None
    diff_policy: ArtifactReference | None = None
    plan: ArtifactReference | None = None
    campaign: ArtifactReference | None = None
    evidence: list[ArtifactReference] = Field(default_factory=list)
    recordings: list[ArtifactReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def expected_status_has_required_inputs(self) -> PrimarySuiteSource:
        fixed_roles = (
            (self.spec, "spec"),
            (self.fixture_manifest, "fixture_manifest"),
            (self.fixture_acceptance, "fixture_acceptance"),
            (self.diff_policy, "diff_policy"),
            (self.plan, "plan"),
            (self.campaign, "campaign"),
        )
        if any(
            reference is not None and reference.role != role
            for reference, role in fixed_roles
        ):
            raise ValueError("Primary Artifact reference role does not match its field")
        if any(reference.role != "evidence" for reference in self.evidence):
            raise ValueError("Primary Evidence references must use role=evidence")
        if any(reference.role != "recording" for reference in self.recordings):
            raise ValueError("Primary Recording references must use role=recording")
        core = (
            self.spec,
            self.fixture_manifest,
            self.fixture_acceptance,
            self.diff_policy,
            self.plan,
        )
        if self.expected_language_status in {
            LanguageStatus.READY_NOT_RUN,
            LanguageStatus.EVALUATED,
        } and any(item is None for item in core):
            raise ValueError("ready/evaluated language requires all Plan-bound inputs")
        if self.expected_language_status is LanguageStatus.READY_NOT_RUN and (
            self.campaign is not None or self.evidence or self.recordings
        ):
            raise ValueError("ready_not_run must not claim Live Artifacts")
        if self.expected_language_status is LanguageStatus.EVALUATED and (
            self.campaign is None or not self.evidence or not self.recordings
        ):
            raise ValueError("evaluated language requires Campaign, Evidence, and Recording")
        if self.expected_language_status is LanguageStatus.BLOCKED:
            if self.blocker is None:
                raise ValueError("blocked language requires a blocker")
        elif self.blocker is not None:
            raise ValueError("only blocked language may contain a blocker")
        return self


class HistoricalSuiteSource(ContractModel):
    source_class: Literal[SourceClass.HISTORICAL]
    language: Language
    verification_record: ArtifactReference
    plan: ArtifactReference
    campaign: ArtifactReference
    report_json: ArtifactReference
    report_markdown: ArtifactReference
    included_in_primary_denominator: Literal[False]

    @model_validator(mode="after")
    def reference_roles_match_fields(self) -> HistoricalSuiteSource:
        fixed_roles = (
            (self.verification_record, "historical_verification"),
            (self.plan, "plan"),
            (self.campaign, "campaign"),
            (self.report_json, "report_json"),
            (self.report_markdown, "report_markdown"),
        )
        if any(reference.role != role for reference, role in fixed_roles):
            raise ValueError("Historical Artifact reference role does not match its field")
        return self


class ProviderCoverage(ContractModel):
    provider: Provider
    evaluation_status: ProviderEvaluationStatus
    evaluated_languages: list[Language]
    blocker: StrictStr | None = None

    @model_validator(mode="after")
    def coverage_state_is_explicit(self) -> ProviderCoverage:
        if self.provider is Provider.ANTIGRAVITY:
            if (
                self.evaluation_status
                is not ProviderEvaluationStatus.NOT_EVALUATED
                or self.evaluated_languages
                or self.blocker != "upstream_artifact_signature_invalid"
            ):
                raise ValueError(
                    "Antigravity must remain not_evaluated with its fixed blocker"
                )
        elif self.provider is Provider.CODEX:
            if self.evaluation_status is ProviderEvaluationStatus.EVALUATED:
                if not self.evaluated_languages or self.blocker is not None:
                    raise ValueError(
                        "evaluated Codex coverage requires languages and no blocker"
                    )
            elif self.evaluated_languages:
                raise ValueError("not_evaluated coverage must not list languages")
        else:
            raise ValueError("Public Suite coverage supports Codex and Antigravity only")
        return self


class PublicSuiteManifest(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    renderer_version: StrictStr = Field(min_length=1)
    data_cutoff_at: datetime
    primary_sources: list[PrimarySuiteSource] = Field(min_length=1)
    historical_sources: list[HistoricalSuiteSource]
    provider_coverage: list[ProviderCoverage]
    antigravity_blocker: Literal["upstream_artifact_signature_invalid"]
    planned_outputs: list[StrictStr] = Field(min_length=1)
    automatic_winner_selected: Literal[False]
    leaderboard_generated: Literal[False]
    statistical_significance_claimed: Literal[False]

    @field_validator("data_cutoff_at", mode="before")
    @classmethod
    def data_cutoff_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "data_cutoff_at")

    @field_validator("planned_outputs")
    @classmethod
    def output_paths_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = [_relative_file(value, "planned output") for value in values]
        if normalized != sorted(set(normalized)):
            raise ValueError("planned outputs must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def sources_and_paths_are_unique(self) -> PublicSuiteManifest:
        primary_languages = [source.language for source in self.primary_sources]
        if primary_languages != sorted(set(primary_languages), key=lambda item: item.value):
            raise ValueError("primary languages must be unique and sorted")
        references = list(_manifest_references(self))
        paths = [reference.path for reference in references]
        if len(paths) != len(set(paths)):
            raise ValueError("input Artifact paths must be unique after normalization")
        if set(paths).intersection(self.planned_outputs):
            raise ValueError("planned outputs must not alias input Artifacts")
        if not {"checksums.json", "release-metadata.json"}.issubset(
            self.planned_outputs
        ):
            raise ValueError(
                "planned outputs require checksums.json and release-metadata.json"
            )
        providers = [coverage.provider for coverage in self.provider_coverage]
        if providers != [Provider.CODEX, Provider.ANTIGRAVITY]:
            raise ValueError("Provider coverage must contain Codex then Antigravity")
        return self


class PublicRunRecord(ContractModel):
    schema_version: Literal["1.0"]
    reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    experiment_id: StrictStr
    run_id: StrictStr
    task_id: StrictStr
    language: Language
    provider: Literal[Provider.CODEX]
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    exact_model_id: StrictStr
    reasoning_effort: ReasoningEffort
    cli_profile: StrictStr
    cli_version: StrictStr
    os: StrictStr
    architecture: StrictStr
    toolchain_fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)
    fixture_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    prompt_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    plan_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    campaign_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    evidence_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    recording_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    overall_status: Phase6OverallStatus
    failure_kind: Phase6FailureKind
    provider_call_count: Literal[1]
    gate_executed: StrictBool
    gate_not_executed_reason: GateNotExecutedReason | None
    acceptance_passed: StrictInt = Field(ge=0)
    acceptance_total: StrictInt = Field(ge=0)
    regression_failures: StrictInt = Field(ge=0)
    lint_errors: StrictInt = Field(ge=0)
    typecheck_errors: StrictInt = Field(ge=0)
    agent_duration_ms: StrictInt | None = Field(default=None, ge=0)
    evaluation_duration_ms: StrictInt | None = Field(default=None, ge=0)
    total_duration_ms: StrictInt | None = Field(default=None, ge=0)
    changed_file_count: StrictInt | None = Field(default=None, ge=0)
    added_lines: StrictInt | None = Field(default=None, ge=0)
    deleted_lines: StrictInt | None = Field(default=None, ge=0)
    usage_status: Literal["observed", "missing"]
    usage_source: Literal["provider_reported", "estimated"] | None
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    cached_input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    reasoning_output_tokens: StrictInt | None = Field(default=None, ge=0)
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def public_allowlist_values_are_coherent(self) -> PublicRunRecord:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.gate_executed is (
            self.gate_not_executed_reason is not None
        ):
            raise ValueError(
                "Gate execution and gate_not_executed_reason must be complementary"
            )
        if self.gate_not_executed_reason is GateNotExecutedReason.INPUT_CHANGED:
            raise ValueError(
                "input_changed has no Provider call and belongs only in aggregate counts"
            )
        usage_values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
        )
        if self.usage_status == "missing":
            if self.usage_source is not None or any(
                value is not None for value in usage_values
            ):
                raise ValueError("missing Usage must not be converted to zero")
        elif self.usage_source is None:
            raise ValueError("observed Usage requires a source")
        return self


class PublicLanguageReport(ContractModel):
    schema_version: Literal["1.0"]
    language: Language
    status: LanguageStatus
    scheduled_runs: StrictInt = Field(ge=0)
    attempted_runs: StrictInt = Field(ge=0)
    completed_runs: StrictInt = Field(ge=0)
    failed_runs: StrictInt = Field(ge=0)
    interrupted_runs: StrictInt = Field(ge=0)
    not_run_runs: StrictInt = Field(ge=0)
    output_rejected_runs: StrictInt = Field(ge=0)
    gate_not_executed_runs: StrictInt = Field(ge=0)
    gate_not_executed_reason: dict[GateNotExecutedReason, StrictInt]
    scheduled_pair_count: StrictInt = Field(ge=0)
    complete_pair_count: StrictInt = Field(ge=0)
    estimability: Literal["estimable", "not_estimable"]

    @model_validator(mode="after")
    def report_counts_are_coherent(self) -> PublicLanguageReport:
        if (
            self.completed_runs
            + self.failed_runs
            + self.interrupted_runs
            + self.not_run_runs
            != self.scheduled_runs
            or self.attempted_runs
            != self.completed_runs + self.failed_runs + self.interrupted_runs
        ):
            raise ValueError("language run counts do not match the terminal taxonomy")
        if sum(self.gate_not_executed_reason.values()) != self.gate_not_executed_runs:
            raise ValueError("Gate non-execution reasons must match their total")
        if (
            self.gate_not_executed_reason.get(
                GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION,
                0,
            )
            != self.output_rejected_runs
        ):
            raise ValueError(
                "output_rejected_runs must match output_contract_violation reasons"
            )
        if self.complete_pair_count > self.scheduled_pair_count:
            raise ValueError("complete pairs must not exceed scheduled pairs")
        if (self.estimability == "estimable") is not (
            self.complete_pair_count > 0
        ):
            raise ValueError("estimability must match complete pair count")
        return self


class PublicSuiteReport(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    renderer_version: StrictStr
    generated_at: datetime
    data_cutoff_at: datetime
    languages: list[PublicLanguageReport]
    provider_coverage: list[ProviderCoverage]
    automatic_winner_selected: Literal[False]
    leaderboard_generated: Literal[False]
    statistical_significance_claimed: Literal[False]

    @field_validator("generated_at", "data_cutoff_at", mode="before")
    @classmethod
    def timestamps_are_canonical(cls, value: object, info: Any) -> object:
        return _validate_canonical_timestamp(value, str(info.field_name))

    @model_validator(mode="after")
    def report_uses_cutoff_as_generation_time(self) -> PublicSuiteReport:
        if self.generated_at != self.data_cutoff_at:
            raise ValueError("generated_at must equal deterministic data_cutoff_at")
        languages = [item.language for item in self.languages]
        if languages != sorted(set(languages), key=lambda item: item.value):
            raise ValueError("language reports must be unique and sorted")
        return self


class ChecksumEntry(ContractModel):
    path: StrictStr
    size_bytes: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "checksum path")


class PublicChecksums(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    entries: list[ChecksumEntry] = Field(min_length=1)
    excluded_paths: list[Literal["checksums.json"]]
    authenticity_claimed: Literal[False]

    @model_validator(mode="after")
    def entries_are_sorted_and_exclude_only_self(self) -> PublicChecksums:
        if self.excluded_paths != ["checksums.json"]:
            raise ValueError("checksums.json must be the only excluded path")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(set(paths)):
            raise ValueError("checksum entries must be unique and sorted")
        if "checksums.json" in paths:
            raise ValueError("checksums.json must exclude only itself")
        if "release-metadata.json" not in paths:
            raise ValueError("release-metadata.json must be checksum-protected")
        return self


class ReleaseMetadata(ContractModel):
    schema_version: Literal["1.0"]
    suite_id: StrictStr
    renderer_version: StrictStr
    data_cutoff_at: datetime
    checksum_manifest_path: Literal["checksums.json"]
    checksum_digest_anchored_externally: Literal[True]
    authenticity_claimed: Literal[False]

    @field_validator("data_cutoff_at", mode="before")
    @classmethod
    def data_cutoff_is_canonical(cls, value: object) -> object:
        return _validate_canonical_timestamp(value, "data_cutoff_at")


class ExternalChecksumAnchor(ContractModel):
    """Bundle-external integrity anchor; it is not an authenticity claim."""

    schema_version: Literal["1.0"]
    suite_id: StrictStr
    checksum_manifest_path: Literal["checksums.json"]
    checksum_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    authenticity_claimed: Literal[False]


def canonical_json_bytes(value: ContractModel | dict[str, Any]) -> bytes:
    """Serialize one Phase 6 contract with byte-stable JSON settings."""
    raw = value.model_dump(mode="python") if isinstance(value, ContractModel) else value
    return (
        json.dumps(
            _canonical_json_value(raw),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, dict):
        return {
            key: _canonical_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def _canonical_jsonl_line(event: ContractModel) -> bytes:
    return (
        json.dumps(
            _canonical_json_value(event.model_dump(mode="python")),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _load_canonical_model[TContract: ContractModel](
    path: Path,
    model: type[TContract],
    label: str,
) -> TContract:
    try:
        raw = _strict_json(path, label)
    except WorkflowPlanError as error:
        raise Phase6ContractError(str(error)) from error
    try:
        validated = model.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid {label}: {error}") from error
    if path.read_bytes() != canonical_json_bytes(validated):
        raise Phase6ContractError(f"{label} must use canonical JSON serialization")
    return validated


def load_fixture_manifest(path: Path) -> FixtureManifest:
    return _load_canonical_model(path, FixtureManifest, "Fixture Manifest")


def load_diff_policy(path: Path) -> DiffPolicy:
    return _load_canonical_model(path, DiffPolicy, "Diff Policy")


def load_fixture_acceptance(path: Path) -> FixtureAcceptanceRecord:
    return _load_canonical_model(
        path,
        FixtureAcceptanceRecord,
        "Fixture Acceptance Record",
    )


def load_historical_verification(path: Path) -> HistoricalVerificationRecord:
    return _load_canonical_model(
        path,
        HistoricalVerificationRecord,
        "Historical Verification Record",
    )


def load_public_suite_manifest(path: Path) -> PublicSuiteManifest:
    return _load_canonical_model(
        path,
        PublicSuiteManifest,
        "Public Suite Manifest",
    )


def load_public_run_record(path: Path) -> PublicRunRecord:
    return _load_canonical_model(path, PublicRunRecord, "Public Run Record")


def load_public_language_report(path: Path) -> PublicLanguageReport:
    return _load_canonical_model(
        path,
        PublicLanguageReport,
        "Public Language Report",
    )


def load_public_suite_report(path: Path) -> PublicSuiteReport:
    return _load_canonical_model(path, PublicSuiteReport, "Public Suite Report")


def load_public_checksums(path: Path) -> PublicChecksums:
    return _load_canonical_model(path, PublicChecksums, "Public checksums")


def load_release_metadata(path: Path) -> ReleaseMetadata:
    return _load_canonical_model(path, ReleaseMetadata, "release metadata")


def load_external_checksum_anchor(path: Path) -> ExternalChecksumAnchor:
    return _load_canonical_model(
        path,
        ExternalChecksumAnchor,
        "external checksum anchor",
    )


def load_workflow_spec_contract(path: Path) -> LoadedWorkflowSpecContract:
    try:
        source = path.read_bytes()
        raw: Any = yaml.safe_load(source.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WorkflowSpecError(f"could not read Workflow Spec YAML: {error}") from error
    if not isinstance(raw, dict):
        raise WorkflowSpecError("Workflow Spec must be a YAML mapping")
    selected: type[WorkflowExperimentSpec] | type[WorkflowExperimentSpecV2_1]
    if raw.get("schema_version") == "2.0":
        selected = WorkflowExperimentSpec
    elif raw.get("schema_version") == "2.1":
        selected = WorkflowExperimentSpecV2_1
    else:
        raise WorkflowSpecError("unsupported Workflow Spec schema_version")
    try:
        spec = selected.model_validate(raw)
    except ValidationError as error:
        raise WorkflowSpecError(str(error)) from error
    return LoadedWorkflowSpecContract(
        spec=spec,
        sha256=hashlib.sha256(source).hexdigest(),
    )


def load_workflow_plan_contract(path: Path) -> WorkflowPlanContract:
    try:
        raw = _strict_json(path, "Workflow Plan")
    except WorkflowPlanError as error:
        raise Phase6ContractError(str(error)) from error
    if raw.get("schema_version") == "1.1":
        return load_workflow_plan(path)
    if raw.get("schema_version") != "1.2":
        raise Phase6ContractError("unsupported Workflow Plan schema_version")
    try:
        plan = WorkflowPlanV1_2.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid Workflow Plan: {error}") from error
    if path.read_bytes() != canonical_json_bytes(plan):
        raise Phase6ContractError("Workflow Plan must use canonical JSON serialization")
    return plan


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def _load_jsonl_objects(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise Phase6ContractError(f"could not read {label}") from error
    if not lines or any(not line.strip() for line in lines):
        raise Phase6ContractError(f"{label} must be non-empty JSONL without blank lines")
    objects: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_finite,
            )
        except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as error:
            raise Phase6ContractError(
                f"invalid {label} JSON at line {index}"
            ) from error
        if not isinstance(raw, dict):
            raise Phase6ContractError(f"{label} line {index} must be an object")
        objects.append(raw)
    return objects


def load_campaign_contract(path: Path) -> CampaignContract:
    raw_events = _load_jsonl_objects(path, "Campaign")
    version = raw_events[0].get("schema_version")
    if version == "1.1":
        return load_campaign(path)
    if version != "1.2" or any(
        event.get("schema_version") != "1.2" for event in raw_events
    ):
        raise Phase6ContractError("Campaign must use one supported schema version")
    events: list[Phase6CampaignEvent] = []
    for raw in raw_events:
        try:
            events.append(_PHASE6_CAMPAIGN_ADAPTER.validate_python(raw))
        except ValidationError as error:
            raise Phase6ContractError(f"invalid Campaign 1.2 event: {error}") from error
    _validate_phase6_campaign(events)
    canonical = b"".join(_canonical_jsonl_line(event) for event in events)
    if path.read_bytes() != canonical:
        raise Phase6ContractError("Campaign 1.2 must use canonical JSONL serialization")
    return LoadedPhase6Campaign(tuple(events))


def _validate_phase6_campaign(events: list[Phase6CampaignEvent]) -> None:
    if (
        not events
        or not isinstance(events[0], Phase6CampaignStartedEvent)
        or not isinstance(events[-1], Phase6CampaignFinishedEvent)
    ):
        raise Phase6ContractError("Campaign 1.2 requires started and finished boundaries")
    if [event.sequence for event in events] != list(range(len(events))):
        raise Phase6ContractError("Campaign 1.2 sequence must be contiguous")
    started = events[0]
    finished = events[-1]
    assert isinstance(started, Phase6CampaignStartedEvent)
    assert isinstance(finished, Phase6CampaignFinishedEvent)
    if started.experiment_id != finished.experiment_id:
        raise Phase6ContractError("Campaign experiment ID must remain stable")
    runs = [event for event in events if isinstance(event, Phase6CampaignRunEvent)]
    started_ids = {
        event.run_id for event in runs if event.status is CampaignRunStatus.STARTED
    }
    started_events = [
        event for event in runs if event.status is CampaignRunStatus.STARTED
    ]
    terminal = [
        event for event in runs if event.status is not CampaignRunStatus.STARTED
    ]
    if (
        len(terminal) != started.planned_run_count
        or len(started_events) != len(started_ids)
        or len(started_ids) != finished.attempted_run_count
        or sum(event.provider_call_count or 0 for event in terminal)
        != finished.provider_call_count
        or sum(event.provider_call_count is None for event in terminal)
        != finished.provider_call_count_unknown_runs
        or sum(event.counted_failure for event in terminal)
        != finished.counted_failure_count
    ):
        raise Phase6ContractError("Campaign 1.2 header and terminal counts differ")
    terminal_ids = [event.run_id for event in terminal]
    if len(terminal_ids) != len(set(terminal_ids)):
        raise Phase6ContractError("Campaign 1.2 run may have only one terminal state")
    if any(
        event.status is not CampaignRunStatus.NOT_RUN
        and event.run_id not in started_ids
        for event in terminal
    ):
        raise Phase6ContractError("attempted terminal state requires a started event")
    input_changed = [
        event
        for event in terminal
        if event.stop_reason is CampaignStopReason.INPUT_CHANGED
    ]
    if input_changed and finished.stop_reason is not CampaignStopReason.INPUT_CHANGED:
        raise Phase6ContractError(
            "input_changed run requires input_changed Campaign stop reason"
        )


def load_recording_contract(path: Path) -> RecordingContract:
    raw_events = _load_jsonl_objects(path, "Recording")
    version = raw_events[0].get("schema_version")
    if version in {"1.0", "1.1"}:
        return load_replay_recording(path)
    if (
        version != "1.2"
        or len(raw_events) != 2
        or any(event.get("schema_version") != "1.2" for event in raw_events)
    ):
        raise Phase6ContractError("Recording must use one supported schema version")
    try:
        started = Phase6RecordingStartedEvent.model_validate(raw_events[0])
        terminal = Phase6RecordingTerminalEvent.model_validate(raw_events[1])
    except ValidationError as error:
        raise Phase6ContractError(f"invalid Recording 1.2: {error}") from error
    if (
        started.run_id != terminal.run_id
        or started.experiment_id != terminal.experiment_id
        or terminal.occurred_at < started.occurred_at
        or started.requested_model != terminal.codex.requested_model
        or started.requested_reasoning_effort
        is not terminal.codex.requested_reasoning_effort
        or started.cli_version != terminal.codex.cli_version
    ):
        raise Phase6ContractError("Recording 1.2 event identities differ")
    canonical = b"".join(
        _canonical_jsonl_line(event) for event in (started, terminal)
    )
    if path.read_bytes() != canonical:
        raise Phase6ContractError("Recording 1.2 must use canonical JSONL serialization")
    return Phase6Recording(started, terminal)


def load_live_run_artifact_contract(path: Path) -> LiveRunArtifactContract:
    raw = _strict_json(path, "LiveRunArtifact")
    version = raw.get("schema_version")
    selected: type[LiveRunArtifact] | type[LiveRunArtifactV1_2]
    if version in {"1.0", "1.1"}:
        selected = LiveRunArtifact
    elif version == "1.2":
        selected = LiveRunArtifactV1_2
    else:
        raise Phase6ContractError("unsupported LiveRunArtifact schema_version")
    try:
        artifact = selected.model_validate(raw)
    except ValidationError as error:
        raise Phase6ContractError(f"invalid LiveRunArtifact: {error}") from error
    if version == "1.2" and path.read_bytes() != canonical_json_bytes(artifact):
        raise Phase6ContractError(
            "LiveRunArtifact 1.2 must use canonical JSON serialization"
        )
    return artifact


def validate_plan_bindings(
    *,
    loaded_spec: LoadedWorkflowSpecContract,
    plan: WorkflowPlanV1_2,
    fixture_manifest_bytes: bytes,
    fixture_manifest: FixtureManifest,
    fixture_acceptance_bytes: bytes,
    fixture_acceptance: FixtureAcceptanceRecord,
    diff_policy_bytes: bytes,
    diff_policy: DiffPolicy,
) -> None:
    """Cross-check every Slice 6A input bound by Workflow Plan 1.2."""
    expected = [
        plan.experiment_spec_sha256 == loaded_spec.sha256,
        plan.fixture_manifest_sha256
        == hashlib.sha256(fixture_manifest_bytes).hexdigest(),
        plan.fixture_acceptance_sha256
        == hashlib.sha256(fixture_acceptance_bytes).hexdigest(),
        plan.diff_policy_sha256 == hashlib.sha256(diff_policy_bytes).hexdigest(),
        plan.fixture_sha256 == fixture_manifest.fixture_sha256,
        plan.fixture_sha256 == fixture_acceptance.fixture_sha256,
        plan.gate_contract_sha256 == fixture_manifest.gate_contract_sha256,
        plan.gate_contract_sha256 == fixture_acceptance.gate_contract_sha256,
        plan.reference_solution_sha256
        == fixture_acceptance.reference_solution_sha256,
        plan.toolchain_fingerprint == fixture_manifest.toolchain.fingerprint,
        plan.toolchain_fingerprint == fixture_acceptance.toolchain.fingerprint,
        plan.reviewed_commit == fixture_acceptance.acceptance_agentlab_commit,
        plan.language is fixture_manifest.language,
        plan.language is fixture_acceptance.language,
        plan.language is diff_policy.language,
        plan.fixture_revision == fixture_manifest.fixture_revision,
        plan.fixture_revision == fixture_acceptance.fixture_revision,
        plan.fixture_revision == diff_policy.fixture_revision,
    ]
    if not isinstance(loaded_spec.spec, WorkflowExperimentSpecV2_1):
        raise Phase6ContractError("Workflow Plan 1.2 requires Workflow Spec 2.1")
    expected.extend(
        [
        plan.reviewed_commit == loaded_spec.spec.reviewed_commit,
        plan.language is loaded_spec.spec.language,
        fixture_acceptance.fixture_manifest_sha256
        == hashlib.sha256(fixture_manifest_bytes).hexdigest(),
        fixture_acceptance.diff_policy_sha256
        == hashlib.sha256(diff_policy_bytes).hexdigest(),
        ]
    )
    if not all(expected):
        raise Phase6ContractError("Workflow Plan 1.2 bindings are inconsistent")


def _manifest_references(
    manifest: PublicSuiteManifest,
) -> tuple[ArtifactReference, ...]:
    references: list[ArtifactReference] = []
    for primary in manifest.primary_sources:
        references.extend(
            item
            for item in (
                primary.spec,
                primary.fixture_manifest,
                primary.fixture_acceptance,
                primary.diff_policy,
                primary.plan,
                primary.campaign,
            )
            if item is not None
        )
        references.extend(primary.evidence)
        references.extend(primary.recordings)
    for historical in manifest.historical_sources:
        references.extend(
            (
                historical.verification_record,
                historical.plan,
                historical.campaign,
                historical.report_json,
                historical.report_markdown,
            )
        )
    return tuple(references)


def _safe_root(root: Path) -> Path:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise Phase6PathError("could not resolve fixed Manifest root") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase6PathError("Manifest root must be a real directory")
    return resolved


def _safe_listed_file(root: Path, relative: str) -> Path:
    resolved_root = _safe_root(root)
    relative = _relative_file(relative, "listed Artifact")
    current = resolved_root
    for component in PurePosixPath(relative).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise Phase6PathError(f"listed Artifact is unavailable: {relative}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise Phase6PathError(f"listed Artifact path contains symlink: {relative}")
    try:
        resolved = current.resolve(strict=True)
        metadata = current.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as error:
        raise Phase6PathError(f"could not resolve listed Artifact: {relative}") from error
    if not resolved.is_relative_to(resolved_root):
        raise Phase6PathError("listed Artifact escapes the fixed Manifest root")
    if not stat.S_ISREG(metadata.st_mode):
        raise Phase6PathError("listed Artifact must be a regular file")
    if metadata.st_nlink != 1:
        raise Phase6PathError("listed Artifact hardlink is not allowed")
    return resolved


@dataclass(frozen=True)
class LoadedPublicSuiteInputs:
    manifest: PublicSuiteManifest
    root: Path
    paths: dict[str, Path]
    bytes_by_path: dict[str, bytes]


@dataclass(frozen=True)
class ValidatedPublicSuiteInputs:
    loaded: LoadedPublicSuiteInputs
    derived_language_status: dict[Language, LanguageStatus]
    data_cutoff_at: datetime


def load_public_suite_inputs(
    manifest_path: Path,
    *,
    root: Path,
) -> LoadedPublicSuiteInputs:
    """Load exactly the Manifest and its explicitly listed input files."""
    resolved_root = _safe_root(root)
    try:
        relative_manifest = manifest_path.resolve(strict=True).relative_to(
            resolved_root
        ).as_posix()
    except (OSError, RuntimeError, ValueError) as error:
        raise Phase6PathError("Public Suite Manifest must remain below root") from error
    safe_manifest = _safe_listed_file(resolved_root, relative_manifest)
    manifest = _load_canonical_model(
        safe_manifest,
        PublicSuiteManifest,
        "Public Suite Manifest",
    )
    paths: dict[str, Path] = {}
    bytes_by_path: dict[str, bytes] = {}
    for reference in _manifest_references(manifest):
        path = _safe_listed_file(resolved_root, reference.path)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise Phase6ContractError(
                f"listed Artifact hash differs: {reference.path}"
            )
        paths[reference.path] = path
        bytes_by_path[reference.path] = content
    return LoadedPublicSuiteInputs(
        manifest=manifest,
        root=resolved_root,
        paths=paths,
        bytes_by_path=bytes_by_path,
    )


def derive_language_status(
    source: PrimarySuiteSource,
    *,
    campaign: CampaignContract | None,
    evidence_run_ids: set[str],
) -> LanguageStatus:
    """Derive status without depending on Public Run Records."""
    if source.blocker is not None:
        return LanguageStatus.BLOCKED
    core = (
        source.spec,
        source.fixture_manifest,
        source.fixture_acceptance,
        source.diff_policy,
        source.plan,
    )
    if any(item is None for item in core):
        return LanguageStatus.NOT_READY
    if campaign is None:
        return LanguageStatus.READY_NOT_RUN
    if isinstance(campaign, list):
        terminal_observations = [
            (
                event.status,
                event.run_id,
                event.task_id,
                event.repetition_index,
                event.workflow,
            )
            for event in campaign
            if isinstance(event, CampaignRunEvent)
            and event.status is not CampaignRunStatus.STARTED
        ]
    else:
        terminal_observations = [
            (
                event.status,
                event.run_id,
                event.task_id,
                event.repetition_index,
                event.workflow,
            )
            for event in campaign.events
            if isinstance(event, Phase6CampaignRunEvent)
            and event.status is not CampaignRunStatus.STARTED
        ]
    blocks: dict[tuple[str, int], set[Workflow]] = {}
    for status, run_id, task_id, repetition_index, workflow in terminal_observations:
        if (
            status is CampaignRunStatus.COMPLETED
            and run_id in evidence_run_ids
        ):
            blocks.setdefault(
                (task_id, repetition_index),
                set(),
            ).add(workflow)
    if any(
        workflows == {Workflow.ONE_SHOT, Workflow.STAGED}
        for workflows in blocks.values()
    ):
        return LanguageStatus.EVALUATED
    return LanguageStatus.BLOCKED


def validate_expected_language_status(
    source: PrimarySuiteSource,
    *,
    campaign: CampaignContract | None,
    evidence_run_ids: set[str],
) -> LanguageStatus:
    derived = derive_language_status(
        source,
        campaign=campaign,
        evidence_run_ids=evidence_run_ids,
    )
    if source.expected_language_status is not derived:
        raise Phase6ContractError(
            f"expected language status {source.expected_language_status.value} "
            f"differs from derived status {derived.value}"
        )
    return derived


def validate_data_cutoff(
    manifest: PublicSuiteManifest,
    *,
    fixture_acceptances: list[FixtureAcceptanceRecord],
    campaigns: list[CampaignContract],
    historical_verifications: list[HistoricalVerificationRecord],
    live_artifacts: Sequence[LiveRunArtifactV1_2] = (),
    recordings: Sequence[Phase6Recording] = (),
) -> datetime:
    """Require data_cutoff_at to equal the maximum persisted terminal time."""
    timestamps = [record.verified_at for record in fixture_acceptances]
    timestamps.extend(record.verified_at for record in historical_verifications)
    timestamps.extend(artifact.completed_at for artifact in live_artifacts)
    timestamps.extend(recording.terminal.occurred_at for recording in recordings)
    for campaign in campaigns:
        if isinstance(campaign, LoadedPhase6Campaign):
            timestamps.append(campaign.finished.occurred_at)
        else:
            finished = campaign[-1]
            if not isinstance(finished, CampaignFinishedEvent):
                raise Phase6ContractError("Campaign 1.1 lacks a finished event")
            timestamps.append(finished.occurred_at)
    if not timestamps:
        raise Phase6ContractError(
            "data_cutoff_at requires a terminal or verification timestamp"
        )
    recomputed = max(timestamps)
    if (
        manifest.data_cutoff_at != recomputed
        or _canonical_timestamp(recomputed)
        != _canonical_timestamp(manifest.data_cutoff_at)
    ):
        raise Phase6ContractError(
            "Manifest data_cutoff_at differs from canonical input maximum"
        )
    return recomputed


def validate_checksum_contract(
    *,
    manifest: PublicSuiteManifest,
    checksums: PublicChecksums,
    release_metadata: ReleaseMetadata,
    external_anchor: ExternalChecksumAnchor | None,
    checksum_bytes: bytes | None = None,
) -> None:
    """Validate the one-way checksum trust model without claiming authenticity."""
    if (
        checksums.suite_id != manifest.suite_id
        or release_metadata.suite_id != manifest.suite_id
        or release_metadata.data_cutoff_at != manifest.data_cutoff_at
        or release_metadata.renderer_version != manifest.renderer_version
    ):
        raise Phase6ContractError("release contracts do not match the Suite Manifest")
    expected = set(manifest.planned_outputs) - {"checksums.json"}
    actual = {entry.path for entry in checksums.entries}
    if expected != actual or "release-metadata.json" not in actual:
        raise Phase6ContractError(
            "checksums must cover every planned output except checksums.json itself"
        )
    if external_anchor is None:
        if checksum_bytes is not None:
            raise Phase6ContractError(
                "checksum bytes require a bundle-external digest anchor"
            )
        return
    if (
        checksum_bytes is None
        or external_anchor.suite_id != manifest.suite_id
        or external_anchor.checksum_manifest_sha256
        != hashlib.sha256(checksum_bytes).hexdigest()
    ):
        raise Phase6ContractError("external checksum anchor does not match checksums.json")


def validate_public_suite_inputs(
    loaded: LoadedPublicSuiteInputs,
) -> ValidatedPublicSuiteInputs:
    """Strict-load and cross-check every explicitly listed Suite input."""
    statuses: dict[Language, LanguageStatus] = {}
    acceptances: list[FixtureAcceptanceRecord] = []
    campaigns: list[CampaignContract] = []
    historical_records: list[HistoricalVerificationRecord] = []
    live_artifacts: list[LiveRunArtifactV1_2] = []
    live_recordings: list[Phase6Recording] = []

    for source in loaded.manifest.primary_sources:
        spec = (
            load_workflow_spec_contract(loaded.paths[source.spec.path])
            if source.spec is not None
            else None
        )
        fixture_manifest = (
            load_fixture_manifest(loaded.paths[source.fixture_manifest.path])
            if source.fixture_manifest is not None
            else None
        )
        acceptance = (
            load_fixture_acceptance(loaded.paths[source.fixture_acceptance.path])
            if source.fixture_acceptance is not None
            else None
        )
        policy = (
            load_diff_policy(loaded.paths[source.diff_policy.path])
            if source.diff_policy is not None
            else None
        )
        plan = (
            load_workflow_plan_contract(loaded.paths[source.plan.path])
            if source.plan is not None
            else None
        )
        campaign = (
            load_campaign_contract(loaded.paths[source.campaign.path])
            if source.campaign is not None
            else None
        )
        if campaign is not None:
            campaigns.append(campaign)
        if acceptance is not None:
            acceptances.append(acceptance)

        core = (spec, fixture_manifest, acceptance, policy, plan)
        if all(item is not None for item in core):
            if (
                not isinstance(spec, LoadedWorkflowSpecContract)
                or not isinstance(spec.spec, WorkflowExperimentSpecV2_1)
                or not isinstance(fixture_manifest, FixtureManifest)
                or not isinstance(acceptance, FixtureAcceptanceRecord)
                or not isinstance(policy, DiffPolicy)
                or not isinstance(plan, WorkflowPlanV1_2)
            ):
                raise Phase6ContractError(
                    "Primary Phase 6 source requires Spec 2.1 and Plan 1.2 contracts"
                )
            assert source.fixture_manifest is not None
            assert source.fixture_acceptance is not None
            assert source.diff_policy is not None
            validate_plan_bindings(
                loaded_spec=spec,
                plan=plan,
                fixture_manifest_bytes=loaded.bytes_by_path[
                    source.fixture_manifest.path
                ],
                fixture_manifest=fixture_manifest,
                fixture_acceptance_bytes=loaded.bytes_by_path[
                    source.fixture_acceptance.path
                ],
                fixture_acceptance=acceptance,
                diff_policy_bytes=loaded.bytes_by_path[source.diff_policy.path],
                diff_policy=policy,
            )
        elif any(item is not None for item in core) and (
            campaign is not None or source.evidence or source.recordings
        ):
            raise Phase6ContractError(
                "Live inputs must not exist before every Plan-bound input is present"
            )

        evidence: list[LiveRunArtifactV1_2] = []
        for reference in source.evidence:
            artifact = load_live_run_artifact_contract(loaded.paths[reference.path])
            if not isinstance(artifact, LiveRunArtifactV1_2):
                raise Phase6ContractError(
                    "Primary Phase 6 Evidence must use LiveRunArtifact 1.2"
                )
            evidence.append(artifact)
            live_artifacts.append(artifact)
        recordings: list[Phase6Recording] = []
        for reference in source.recordings:
            recording = load_recording_contract(loaded.paths[reference.path])
            if not isinstance(recording, Phase6Recording):
                raise Phase6ContractError(
                    "Primary Phase 6 Recording must use schema 1.2"
                )
            recordings.append(recording)
            live_recordings.append(recording)
        _validate_primary_live_bindings(
            source=source,
            plan=plan,
            campaign=campaign,
            evidence=evidence,
            recordings=recordings,
        )
        status = validate_expected_language_status(
            source,
            campaign=campaign,
            evidence_run_ids={artifact.run_id for artifact in evidence},
        )
        statuses[source.language] = status

    for historical_source in loaded.manifest.historical_sources:
        record = load_historical_verification(
            loaded.paths[historical_source.verification_record.path]
        )
        if (
            record.language is not historical_source.language
            or record.plan_sha256 != historical_source.plan.sha256
            or record.campaign_sha256 != historical_source.campaign.sha256
            or record.report_json_sha256
            != historical_source.report_json.sha256
            or record.report_markdown_sha256
            != historical_source.report_markdown.sha256
        ):
            raise Phase6ContractError(
                "Historical Verification Record differs from Manifest references"
            )
        historical_records.append(record)

    _validate_provider_coverage(loaded.manifest, statuses)
    cutoff = validate_data_cutoff(
        loaded.manifest,
        fixture_acceptances=acceptances,
        campaigns=campaigns,
        historical_verifications=historical_records,
        live_artifacts=live_artifacts,
        recordings=live_recordings,
    )
    return ValidatedPublicSuiteInputs(
        loaded=loaded,
        derived_language_status=statuses,
        data_cutoff_at=cutoff,
    )


def _validate_primary_live_bindings(
    *,
    source: PrimarySuiteSource,
    plan: WorkflowPlanContract | None,
    campaign: CampaignContract | None,
    evidence: list[LiveRunArtifactV1_2],
    recordings: list[Phase6Recording],
) -> None:
    if campaign is None:
        if evidence or recordings:
            raise Phase6ContractError(
                "Evidence and Recording require a listed Campaign"
            )
        return
    if not isinstance(plan, WorkflowPlanV1_2) or not isinstance(
        campaign,
        LoadedPhase6Campaign,
    ):
        raise Phase6ContractError(
            "Primary Campaign requires Workflow Plan 1.2 and Campaign 1.2"
        )
    assert source.plan is not None
    started = campaign.started
    if (
        started.experiment_id != plan.experiment_id
        or started.plan_sha256 != source.plan.sha256
        or started.fixture_manifest_sha256 != plan.fixture_manifest_sha256
        or started.fixture_acceptance_sha256 != plan.fixture_acceptance_sha256
        or started.diff_policy_sha256 != plan.diff_policy_sha256
        or started.toolchain_fingerprint != plan.toolchain_fingerprint
        or started.planned_run_count != plan.planned_run_count
        or started.planned_provider_call_count
        != plan.planned_provider_call_count
    ):
        raise Phase6ContractError("Campaign header differs from Workflow Plan 1.2")

    planned = {run.run_id: run for run in plan.runs}
    terminal = {
        event.run_id: event
        for event in campaign.events
        if isinstance(event, Phase6CampaignRunEvent)
        and event.status is not CampaignRunStatus.STARTED
    }
    evidence_by_run = {artifact.run_id: artifact for artifact in evidence}
    recording_by_run = {
        recording.started.run_id: recording for recording in recordings
    }
    if (
        len(evidence_by_run) != len(evidence)
        or len(recording_by_run) != len(recordings)
        or set(evidence_by_run) != set(recording_by_run)
        or not set(evidence_by_run).issubset(terminal)
    ):
        raise Phase6ContractError(
            "Evidence and Recording run identities are incomplete or duplicated"
        )
    evidence_hash_by_run = {
        artifact.run_id: reference.sha256
        for artifact, reference in zip(
            evidence,
            source.evidence,
            strict=True,
        )
    }
    recording_hash_by_run = {
        recording.started.run_id: reference.sha256
        for recording, reference in zip(
            recordings,
            source.recordings,
            strict=True,
        )
    }
    for run_id, artifact in evidence_by_run.items():
        run = planned.get(run_id)
        event = terminal[run_id]
        recording = recording_by_run[run_id]
        if run is None:
            raise Phase6ContractError("Evidence run is absent from Workflow Plan")
        if (
            artifact.experiment_id != plan.experiment_id
            or artifact.language is not source.language
            or artifact.task_id != run.task_id
            or artifact.workflow is not run.workflow
            or artifact.repetition_index != run.repetition_index
            or artifact.plan_sha256 != source.plan.sha256
            or artifact.fixture_sha256 != plan.fixture_sha256
            or artifact.fixture_manifest_sha256 != plan.fixture_manifest_sha256
            or artifact.fixture_acceptance_sha256
            != plan.fixture_acceptance_sha256
            or artifact.diff_policy_sha256 != plan.diff_policy_sha256
            or artifact.toolchain_fingerprint != plan.toolchain_fingerprint
            or artifact.recording_sha256 != recording_hash_by_run.get(run_id)
            or recording.started.task_id != run.task_id
            or recording.started.workflow is not run.workflow
            or recording.started.repetition_index != run.repetition_index
            or recording.started.plan_sha256 != source.plan.sha256
            or recording.started.fixture_sha256 != plan.fixture_sha256
            or recording.started.fixture_manifest_sha256
            != plan.fixture_manifest_sha256
            or recording.started.fixture_acceptance_sha256
            != plan.fixture_acceptance_sha256
            or recording.started.diff_policy_sha256 != plan.diff_policy_sha256
            or recording.terminal.overall_status is not artifact.overall_status
            or recording.terminal.failure_kind is not artifact.failure_kind
            or recording.terminal.occurred_at != artifact.completed_at
            or evidence_hash_by_run.get(run_id) is None
            or event.task_id != run.task_id
            or event.workflow is not run.workflow
            or event.repetition_index != run.repetition_index
        ):
            raise Phase6ContractError(
                "Plan, Campaign, Evidence, and Recording identities differ"
            )
        if campaign.finished.occurred_at < artifact.completed_at:
            raise Phase6ContractError(
                "Campaign terminal timestamp precedes a run terminal timestamp"
            )


def _validate_provider_coverage(
    manifest: PublicSuiteManifest,
    statuses: dict[Language, LanguageStatus],
) -> None:
    evaluated = sorted(
        (
            language
            for language, status in statuses.items()
            if status is LanguageStatus.EVALUATED
        ),
        key=lambda language: language.value,
    )
    codex = manifest.provider_coverage[0]
    if evaluated:
        expected_status = ProviderEvaluationStatus.EVALUATED
    else:
        expected_status = ProviderEvaluationStatus.NOT_EVALUATED
    if (
        codex.evaluation_status is not expected_status
        or codex.evaluated_languages != evaluated
    ):
        raise Phase6ContractError(
            "Codex Provider coverage differs from derived language status"
        )


def legacy_loaded_spec(
    loaded: LoadedWorkflowSpec,
) -> LoadedWorkflowSpecContract:
    """Adapt an existing 2.0 loader result without changing its runtime path."""
    return LoadedWorkflowSpecContract(spec=loaded.spec, sha256=loaded.sha256)


def legacy_plan(path: Path) -> WorkflowPlan:
    """Exercise the unchanged Plan 1.1 loader through the compatibility surface."""
    return load_workflow_plan(path)
