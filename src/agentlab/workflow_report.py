"""Offline-only aggregation for versioned Workflow Campaign Artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import (
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.campaign import (
    AdapterCleanupState,
    CampaignError,
    CampaignOutcome,
    CampaignRunEvent,
    CampaignRunStatus,
    CampaignStartedEvent,
    load_campaign,
)
from agentlab.live import LiveArtifactLoadError, load_live_artifact
from agentlab.models import (
    CodexExecutionStage,
    CommandStatus,
    ContractModel,
    GateKind,
    GateKindSummary,
    LiveEvaluationSummary,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    UsageMetrics,
    UsageMetricSource,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.phase6 import (
    ArtifactReference,
    LanguageStatus,
    LiveRunArtifactV1_2,
    LiveRunArtifactV1_3,
    LoadedPhase6Campaign,
    Phase6CampaignOutcome,
    Phase6CampaignRunEvent,
    Phase6ContractError,
    Phase6LiveRunArtifact,
    Phase6Recording,
    PrimarySuiteSource,
    SourceClass,
    WorkflowExperimentSpecV2_1,
    WorkflowPlanV1_2,
    _canonical_jsonl_line,
    _validate_primary_live_bindings,
    canonical_json_bytes,
    load_campaign_contract,
    load_diff_policy,
    load_fixture_acceptance,
    load_fixture_manifest,
    load_live_run_artifact_contract,
    load_recording_contract,
    load_workflow_plan_contract,
    load_workflow_spec_contract,
    validate_plan_bindings,
)
from agentlab.recording import (
    LiveRunCompletedEvent,
    LiveRunFailedEvent,
    LiveRunStartedEvent,
    RecordingLoadError,
    ReplayRecording,
    load_replay_recording,
)
from agentlab.workflow import (
    WorkflowPlan,
    WorkflowPlanError,
    WorkflowPlanRun,
    WorkflowSpecError,
    _publish_create_only_pair,
    _strict_json,
    workflow_plan_bytes,
    workflow_prompt_fingerprint,
)

ReportArtifact = LiveRunArtifact | Phase6LiveRunArtifact


@dataclass(frozen=True)
class _ReportRunEvent:
    status: CampaignRunStatus
    outcome: CampaignOutcome
    provider_call_count: int | None
    retry_count: int


class WorkflowReportError(ValueError):
    """A strict offline aggregation or publication error."""


class Estimability(StrEnum):
    ESTIMABLE = "estimable"
    NOT_ESTIMABLE = "not_estimable"


class ObservedIntegerAggregate(ContractModel):
    denominator_runs: StrictInt = Field(ge=0)
    observed_runs: StrictInt = Field(ge=0)
    missing_runs: StrictInt = Field(ge=0)
    total: StrictInt | None
    minimum: StrictInt | None
    maximum: StrictInt | None

    @model_validator(mode="after")
    def observation_counts_and_values_match(self) -> ObservedIntegerAggregate:
        if self.observed_runs + self.missing_runs != self.denominator_runs:
            raise ValueError("observed and missing runs must match denominator")
        values = (self.total, self.minimum, self.maximum)
        if (self.observed_runs == 0) is not all(value is None for value in values):
            raise ValueError("integer aggregate values must match observation availability")
        return self


class GateResultAggregate(ContractModel):
    evidence_runs: StrictInt = Field(ge=0)
    command_count: StrictInt = Field(ge=0)
    passed_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    abnormal_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def command_status_counts_match(self) -> GateResultAggregate:
        if (
            self.passed_count + self.failed_count + self.abnormal_count
            != self.command_count
        ):
            raise ValueError("Gate result counts must match command_count")
        return self


class UsageMetricAggregate(ContractModel):
    observed_runs: StrictInt = Field(ge=0)
    total: StrictInt | None


class UsageSourceAggregate(ContractModel):
    run_count: StrictInt = Field(ge=0)
    input_tokens: UsageMetricAggregate
    cached_input_tokens: UsageMetricAggregate
    output_tokens: UsageMetricAggregate
    reasoning_output_tokens: UsageMetricAggregate


class UsageAggregate(ContractModel):
    evidence_runs: StrictInt = Field(ge=0)
    usage_available_runs: StrictInt = Field(ge=0)
    usage_missing_runs: StrictInt = Field(ge=0)
    provider_reported: UsageSourceAggregate
    estimated: UsageSourceAggregate

    @model_validator(mode="after")
    def usage_availability_matches_evidence(self) -> UsageAggregate:
        if self.usage_available_runs + self.usage_missing_runs != self.evidence_runs:
            raise ValueError("Usage availability must match evidence_runs")
        if (
            self.provider_reported.run_count + self.estimated.run_count
            != self.usage_available_runs
        ):
            raise ValueError("Usage source groups must remain disjoint and complete")
        return self


class WorkflowAggregate(ContractModel):
    workflow: Workflow
    scheduled_runs: StrictInt = Field(ge=0)
    attempted_runs: StrictInt = Field(ge=0)
    completed_runs: StrictInt = Field(ge=0)
    quality_gate_passed_runs: StrictInt = Field(ge=0)
    quality_gate_failed_runs: StrictInt = Field(ge=0)
    provider_failed_runs: StrictInt = Field(ge=0)
    provider_timeout_runs: StrictInt = Field(ge=0)
    harness_failed_runs: StrictInt = Field(ge=0)
    cleanup_failed_runs: StrictInt = Field(ge=0)
    interrupted_runs: StrictInt = Field(ge=0)
    not_run_runs: StrictInt = Field(ge=0)
    evidence_runs: StrictInt = Field(ge=0)
    acceptance: GateResultAggregate
    regression: GateResultAggregate
    lint: GateResultAggregate
    typecheck: GateResultAggregate
    agent_duration_ms: ObservedIntegerAggregate
    evaluation_duration_ms: ObservedIntegerAggregate
    total_duration_ms: ObservedIntegerAggregate
    agent_call_count: ObservedIntegerAggregate
    retry_count: ObservedIntegerAggregate
    changed_file_count: ObservedIntegerAggregate
    added_lines: ObservedIntegerAggregate
    deleted_lines: ObservedIntegerAggregate
    usage: UsageAggregate

    @model_validator(mode="after")
    def run_denominators_match_taxonomy(self) -> WorkflowAggregate:
        if self.quality_gate_passed_runs + self.quality_gate_failed_runs != self.completed_runs:
            raise ValueError("completed runs must be quality pass or quality failure")
        terminal_attempted = (
            self.completed_runs
            + self.provider_failed_runs
            + self.harness_failed_runs
            + self.interrupted_runs
        )
        if (
            terminal_attempted != self.attempted_runs
            or self.attempted_runs + self.not_run_runs != self.scheduled_runs
            or self.provider_timeout_runs > self.provider_failed_runs
            or self.cleanup_failed_runs > self.harness_failed_runs
            or self.evidence_runs > self.attempted_runs
        ):
            raise ValueError("Workflow run counts must match the fixed taxonomy")
        return self


class PairingAggregate(ContractModel):
    status: Estimability
    scheduled_pair_count: StrictInt = Field(ge=0)
    complete_pair_count: StrictInt = Field(ge=0)
    denominator: Literal["task_id_x_repetition_with_both_workflows_completed"]

    @model_validator(mode="after")
    def pairing_state_matches_count(self) -> PairingAggregate:
        if self.complete_pair_count > self.scheduled_pair_count:
            raise ValueError("complete pair count must not exceed scheduled pairs")
        if (self.status is Estimability.ESTIMABLE) is not (
            self.complete_pair_count > 0
        ):
            raise ValueError("pairing status must match complete pair count")
        return self


class WorkflowReport(ContractModel):
    schema_version: Literal["1.0"]
    experiment_id: StrictStr
    plan_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    campaign_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    comparison_axis: Literal["workflow"]
    automatic_winner_selected: Literal[False]
    denominator_note: Literal[
        "run counts use scheduled_runs; metric aggregates report observed and missing runs"
    ]
    workflows: list[WorkflowAggregate]
    pairing: PairingAggregate

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("report created_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def report_has_both_workflows_once(self) -> WorkflowReport:
        if [item.workflow for item in self.workflows] != [
            Workflow.ONE_SHOT,
            Workflow.STAGED,
        ]:
            raise ValueError("report must contain one ordered aggregate per Workflow")
        return self


def _observed(values: Sequence[int | None]) -> ObservedIntegerAggregate:
    present = [value for value in values if value is not None]
    return ObservedIntegerAggregate(
        denominator_runs=len(values),
        observed_runs=len(present),
        missing_runs=len(values) - len(present),
        total=sum(present) if present else None,
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
    )


def _gate(artifacts: list[ReportArtifact], gate: GateKind) -> GateResultAggregate:
    commands = [
        command
        for artifact in artifacts
        for command in artifact.gate_commands
        if command.gate is gate
    ]
    return GateResultAggregate(
        evidence_runs=len(artifacts),
        command_count=len(commands),
        passed_count=sum(command.status is CommandStatus.PASSED for command in commands),
        failed_count=sum(command.status is CommandStatus.FAILED for command in commands),
        abnormal_count=sum(
            command.status not in {CommandStatus.PASSED, CommandStatus.FAILED}
            for command in commands
        ),
    )


def _usage_metric(
    usage: Sequence[UsageMetrics],
    field_name: str,
) -> UsageMetricAggregate:
    values = [value for item in usage if (value := getattr(item, field_name)) is not None]
    return UsageMetricAggregate(
        observed_runs=len(values),
        total=sum(values) if values else None,
    )


def _usage_source(
    artifacts: list[ReportArtifact],
    source: UsageMetricSource,
) -> UsageSourceAggregate:
    selected = [
        artifact.codex.usage_metrics
        for artifact in artifacts
        if artifact.codex.usage_metrics is not None
        and artifact.codex.usage_metrics.source is source
    ]
    return UsageSourceAggregate(
        run_count=len(selected),
        input_tokens=_usage_metric(selected, "input_tokens"),
        cached_input_tokens=_usage_metric(selected, "cached_input_tokens"),
        output_tokens=_usage_metric(selected, "output_tokens"),
        reasoning_output_tokens=_usage_metric(selected, "reasoning_output_tokens"),
    )


def _usage(artifacts: list[ReportArtifact]) -> UsageAggregate:
    available = [
        artifact
        for artifact in artifacts
        if artifact.codex.usage_metrics is not None
        and artifact.codex.usage_metrics.source
        in {UsageMetricSource.PROVIDER_REPORTED, UsageMetricSource.ESTIMATED}
    ]
    return UsageAggregate(
        evidence_runs=len(artifacts),
        usage_available_runs=len(available),
        usage_missing_runs=len(artifacts) - len(available),
        provider_reported=_usage_source(
            artifacts,
            UsageMetricSource.PROVIDER_REPORTED,
        ),
        estimated=_usage_source(artifacts, UsageMetricSource.ESTIMATED),
    )


def _artifact_provider_call_count(artifact: LiveRunArtifact) -> int:
    return int(
        artifact.codex.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )


def _artifact_outcome(artifact: LiveRunArtifact) -> CampaignOutcome:
    if artifact.overall_status is LiveOverallStatus.PASSED:
        return CampaignOutcome.SUCCESS
    if artifact.overall_status is LiveOverallStatus.FAILED:
        return CampaignOutcome.QUALITY_GATE_FAILURE
    if artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR:
        if artifact.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT:
            return CampaignOutcome.PROVIDER_TIMEOUT
        return CampaignOutcome.PROVIDER_FAILURE
    if (
        artifact.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
        or artifact.workspace_lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
    ):
        return CampaignOutcome.CLEANUP_FAILURE
    return CampaignOutcome.HARNESS_FAILURE


def _evidence_evaluation_summary(
    run: WorkflowPlanRun,
    artifact: LiveRunArtifact,
) -> LiveEvaluationSummary:
    duration = artifact.evaluation_duration_ms
    if duration is None and artifact.metrics is not None:
        duration = artifact.metrics.evaluation_duration_ms
    if duration is None and not artifact.gate_commands:
        duration = 0
    if duration is None:
        raise WorkflowReportError(
            f"failed Evidence lacks evaluation duration for {run.run_id}"
        )

    def gate_summary(gate: GateKind) -> GateKindSummary:
        selected = [
            command for command in artifact.gate_commands if command.gate is gate
        ]
        return GateKindSummary(
            command_count=len(selected),
            passed_count=sum(
                command.status is CommandStatus.PASSED for command in selected
            ),
            failed_count=sum(
                command.status is CommandStatus.FAILED for command in selected
            ),
        )

    return LiveEvaluationSummary(
        acceptance=gate_summary(GateKind.ACCEPTANCE),
        regression=gate_summary(GateKind.REGRESSION),
        lint=gate_summary(GateKind.LINT),
        typecheck=gate_summary(GateKind.TYPECHECK),
        all_commands_completed_normally=all(
            command.status in {CommandStatus.PASSED, CommandStatus.FAILED}
            and command.termination.process_group_cleared
            for command in artifact.gate_commands
        ),
        evaluation_duration_ms=duration,
        changed_files=artifact.diff.changed_files,
        added_lines=artifact.diff.added_lines,
        deleted_lines=artifact.diff.deleted_lines,
        diff_line_counts_complete=artifact.diff.line_counts_complete,
        workspace_lifecycle=artifact.workspace_lifecycle,
    )


def _validate_recording_relationships(
    plan: WorkflowPlan,
    run: WorkflowPlanRun,
    artifact: LiveRunArtifact,
    recording: ReplayRecording,
) -> None:
    started = recording.started
    if not isinstance(started, LiveRunStartedEvent):
        raise WorkflowReportError(f"run {run.run_id} requires a Live Recording")
    expected_prompt_sha, expected_prompt_bytes = workflow_prompt_fingerprint(
        plan,
        run.workflow,
    )
    if (
        artifact.run_id != run.run_id
        or artifact.experiment_id != plan.experiment_id
        or artifact.task_id != run.task_id
        or artifact.repetition_index != run.repetition_index
        or artifact.workflow is not run.workflow
        or artifact.fixture_sha256 != plan.fixture_sha256
        or artifact.prompt_sha256 != expected_prompt_sha
        or artifact.prompt_bytes != expected_prompt_bytes
        or artifact.codex.requested_model != plan.model
        or artifact.codex.requested_reasoning_effort is not plan.reasoning_effort
        or started.run_id != run.run_id
        or started.experiment_id != plan.experiment_id
        or started.task_id != run.task_id
        or started.repetition_index != run.repetition_index
        or started.workflow is not run.workflow
        or started.prompt_sha256 != expected_prompt_sha
        or started.prompt_bytes != expected_prompt_bytes
        or started.requested_model != plan.model
        or started.requested_reasoning_effort is not plan.reasoning_effort
    ):
        raise WorkflowReportError(
            f"Plan, Evidence, and Recording conditions differ for {run.run_id}"
        )
    terminal = recording.completed or recording.failed
    assert terminal is not None
    if not isinstance(terminal, (LiveRunCompletedEvent, LiveRunFailedEvent)):
        raise WorkflowReportError(f"run {run.run_id} requires a Live terminal event")
    if terminal.codex != artifact.codex:
        raise WorkflowReportError(
            f"Evidence and Recording Codex summaries differ for {run.run_id}"
        )
    if terminal.evaluation != _evidence_evaluation_summary(run, artifact):
        raise WorkflowReportError(
            f"Evidence and Recording evaluation summaries differ for {run.run_id}"
        )
    if isinstance(terminal, LiveRunCompletedEvent):
        if terminal.metrics != artifact.metrics:
            raise WorkflowReportError(
                f"Evidence and Recording Metrics differ for {run.run_id}"
            )
    elif isinstance(terminal, LiveRunFailedEvent) and (
        terminal.failure_kind is not artifact.failure_kind
        or artifact.metrics is not None
    ):
        raise WorkflowReportError(
            f"Evidence and failed Recording differ for {run.run_id}"
        )


def _validate_campaign_artifact_relationship(
    run: WorkflowPlanRun,
    event: CampaignRunEvent,
    artifact: LiveRunArtifact,
) -> None:
    if event.provider_call_count != _artifact_provider_call_count(artifact):
        raise WorkflowReportError(
            f"Campaign and Evidence Provider call counts differ for {run.run_id}"
        )
    if event.live_failure_kind is not artifact.failure_kind:
        raise WorkflowReportError(
            f"Campaign and Evidence failure kinds differ for {run.run_id}"
        )
    if event.adapter_cleanup_state is AdapterCleanupState.FAILED:
        if event.outcome is not CampaignOutcome.CLEANUP_FAILURE:
            raise WorkflowReportError(
                f"adapter cleanup state is inconsistent for {run.run_id}"
            )
    elif event.outcome is not _artifact_outcome(artifact):
        raise WorkflowReportError(
            f"Campaign outcome and Evidence status differ for {run.run_id}"
        )


def _load_artifacts(
    spec_path: Path,
    plan: WorkflowPlan,
    terminal_events: dict[str, CampaignRunEvent],
) -> dict[str, LiveRunArtifact]:
    artifacts: dict[str, LiveRunArtifact] = {}
    evidence_required = {
        CampaignOutcome.SUCCESS,
        CampaignOutcome.QUALITY_GATE_FAILURE,
        CampaignOutcome.PROVIDER_FAILURE,
        CampaignOutcome.PROVIDER_TIMEOUT,
    }
    for run in plan.runs:
        event = terminal_events[run.run_id]
        evidence_path = spec_path.parent / Path(run.evidence_path)
        recording_path = spec_path.parent / Path(run.recording_path)
        if not os.path.lexists(evidence_path):
            if os.path.lexists(recording_path):
                raise WorkflowReportError(
                    f"Recording exists without Evidence for {run.run_id}"
                )
            if event.outcome in evidence_required:
                raise WorkflowReportError(
                    f"Campaign outcome requires Evidence for {run.run_id}"
                )
            continue
        if event.status in {
            CampaignRunStatus.INTERRUPTED,
            CampaignRunStatus.NOT_RUN,
        }:
            raise WorkflowReportError(
                f"unattempted or interrupted run has Evidence: {run.run_id}"
            )
        if not os.path.lexists(recording_path):
            raise WorkflowReportError(f"Evidence for {run.run_id} has no Recording")
        try:
            artifact = load_live_artifact(evidence_path)
            recording = load_replay_recording(recording_path)
        except (LiveArtifactLoadError, RecordingLoadError) as error:
            raise WorkflowReportError(
                f"invalid saved run Artifact for {run.run_id}"
            ) from error
        recording_bytes = recording_path.read_bytes()
        if (
            artifact.recording_sha256
            != hashlib.sha256(recording_bytes).hexdigest()
        ):
            raise WorkflowReportError(
                f"Evidence Recording hash differs for {run.run_id}"
            )
        _validate_recording_relationships(plan, run, artifact, recording)
        _validate_campaign_artifact_relationship(run, event, artifact)
        artifacts[run.run_id] = artifact
    return artifacts


def _terminal_events(events: Sequence[object]) -> dict[str, CampaignRunEvent]:
    result: dict[str, CampaignRunEvent] = {}
    for event in events:
        if isinstance(event, CampaignRunEvent) and event.status is not CampaignRunStatus.STARTED:
            result[event.run_id] = event
    return result


def _legacy_report_events(
    terminal: dict[str, CampaignRunEvent],
) -> dict[str, _ReportRunEvent]:
    normalized: dict[str, _ReportRunEvent] = {}
    for run_id, event in terminal.items():
        if event.outcome is None:
            raise WorkflowReportError(f"Campaign terminal outcome is absent for {run_id}")
        normalized[run_id] = _ReportRunEvent(
            status=event.status,
            outcome=event.outcome,
            provider_call_count=event.provider_call_count,
            retry_count=event.retry_count,
        )
    return normalized


def _phase6_report_events(
    campaign: LoadedPhase6Campaign,
) -> dict[str, _ReportRunEvent]:
    normalized: dict[str, _ReportRunEvent] = {}
    for event in campaign.events:
        if (
            not isinstance(event, Phase6CampaignRunEvent)
            or event.status is CampaignRunStatus.STARTED
        ):
            continue
        if event.outcome is None:
            raise WorkflowReportError(
                f"Campaign 1.2 terminal outcome is absent for {event.run_id}"
            )
        if event.outcome is Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION:
            raise WorkflowReportError(
                "Workflow Report 1.0 cannot represent output_contract_violation"
            )
        try:
            outcome = CampaignOutcome(event.outcome.value)
        except ValueError as error:
            raise WorkflowReportError(
                f"unsupported Campaign 1.2 outcome for {event.run_id}"
            ) from error
        normalized[event.run_id] = _ReportRunEvent(
            status=event.status,
            outcome=outcome,
            provider_call_count=event.provider_call_count,
            retry_count=campaign.finished.retry_count,
        )
    return normalized


def _phase6_recording_bytes(recording: Phase6Recording) -> bytes:
    return b"".join(
        _canonical_jsonl_line(event)
        for event in (recording.started, recording.terminal)
    )


def _load_phase6_report_inputs(
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    plan: WorkflowPlanV1_2,
) -> tuple[
    bytes,
    bytes,
    dict[str, _ReportRunEvent],
    dict[str, ReportArtifact],
]:
    loaded_spec = load_workflow_spec_contract(spec_path)
    if not isinstance(loaded_spec.spec, WorkflowExperimentSpecV2_1):
        raise WorkflowReportError("Workflow Plan 1.2 requires Workflow Spec 2.1")
    spec = loaded_spec.spec
    fixture_manifest = load_fixture_manifest(
        spec_path.parent / Path(spec.fixture_manifest_path)
    )
    fixture_acceptance = load_fixture_acceptance(
        spec_path.parent / Path(spec.fixture_acceptance_path)
    )
    diff_policy = load_diff_policy(spec_path.parent / Path(spec.diff_policy_path))
    fixture_manifest_bytes = canonical_json_bytes(fixture_manifest)
    fixture_acceptance_bytes = canonical_json_bytes(fixture_acceptance)
    diff_policy_bytes = canonical_json_bytes(diff_policy)
    validate_plan_bindings(
        loaded_spec=loaded_spec,
        plan=plan,
        fixture_manifest_bytes=fixture_manifest_bytes,
        fixture_manifest=fixture_manifest,
        fixture_acceptance_bytes=fixture_acceptance_bytes,
        fixture_acceptance=fixture_acceptance,
        diff_policy_bytes=diff_policy_bytes,
        diff_policy=diff_policy,
    )

    campaign = load_campaign_contract(campaign_path)
    if not isinstance(campaign, LoadedPhase6Campaign):
        raise WorkflowReportError("Workflow Plan 1.2 requires Campaign 1.2")
    terminal = _phase6_report_events(campaign)
    expected_ids = {run.run_id for run in plan.runs}
    if set(terminal) != expected_ids:
        raise WorkflowReportError(
            "Campaign must retain one terminal state for every planned run"
        )

    artifacts: list[Phase6LiveRunArtifact] = []
    recordings: list[Phase6Recording] = []
    evidence_references: list[ArtifactReference] = []
    recording_references: list[ArtifactReference] = []
    artifacts_by_run: dict[str, ReportArtifact] = {}
    for run in plan.runs:
        event = terminal[run.run_id]
        evidence_path = spec_path.parent / Path(run.evidence_path)
        recording_path = spec_path.parent / Path(run.recording_path)
        artifact_required = event.status in {
            CampaignRunStatus.COMPLETED,
            CampaignRunStatus.FAILED,
        }
        if not artifact_required:
            if os.path.lexists(evidence_path) or os.path.lexists(recording_path):
                raise WorkflowReportError(
                    f"unattempted or interrupted run has saved Artifacts: {run.run_id}"
                )
            continue
        artifact = load_live_run_artifact_contract(evidence_path)
        recording = load_recording_contract(recording_path)
        if not isinstance(
            artifact,
            (LiveRunArtifactV1_2, LiveRunArtifactV1_3),
        ) or not isinstance(
            recording,
            Phase6Recording,
        ):
            raise WorkflowReportError(
                "Workflow Plan 1.2 requires Phase 6 Artifact schema 1.2 or "
                f"1.3 for {run.run_id}"
            )
        evidence_sha256 = hashlib.sha256(canonical_json_bytes(artifact)).hexdigest()
        recording_sha256 = hashlib.sha256(
            _phase6_recording_bytes(recording)
        ).hexdigest()
        artifacts.append(artifact)
        recordings.append(recording)
        evidence_references.append(
            ArtifactReference(
                role="evidence",
                path=run.evidence_path,
                sha256=evidence_sha256,
            )
        )
        recording_references.append(
            ArtifactReference(
                role="recording",
                path=run.recording_path,
                sha256=recording_sha256,
            )
        )
        artifacts_by_run[run.run_id] = artifact

    canonical_plan = workflow_plan_bytes(plan)
    campaign_bytes = b"".join(
        _canonical_jsonl_line(event) for event in campaign.events
    )
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=plan.language,
        expected_language_status=(
            LanguageStatus.EVALUATED if artifacts else LanguageStatus.BLOCKED
        ),
        blocker=None if artifacts else "no_attempted_run_artifacts",
        spec=ArtifactReference(
            role="spec",
            path=spec_path.name,
            sha256=loaded_spec.sha256,
        ),
        fixture_manifest=ArtifactReference(
            role="fixture_manifest",
            path=spec.fixture_manifest_path,
            sha256=hashlib.sha256(fixture_manifest_bytes).hexdigest(),
        ),
        fixture_acceptance=ArtifactReference(
            role="fixture_acceptance",
            path=spec.fixture_acceptance_path,
            sha256=hashlib.sha256(fixture_acceptance_bytes).hexdigest(),
        ),
        diff_policy=ArtifactReference(
            role="diff_policy",
            path=spec.diff_policy_path,
            sha256=hashlib.sha256(diff_policy_bytes).hexdigest(),
        ),
        plan=ArtifactReference(
            role="plan",
            path=plan_path.name,
            sha256=hashlib.sha256(canonical_plan).hexdigest(),
        ),
        campaign=ArtifactReference(
            role="campaign",
            path=campaign_path.name,
            sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        ),
        evidence=evidence_references,
        recordings=recording_references,
    )
    _validate_primary_live_bindings(
        source=source,
        spec=spec,
        plan=plan,
        campaign=campaign,
        evidence=artifacts,
        recordings=recordings,
    )
    return canonical_plan, campaign_bytes, terminal, artifacts_by_run


def _aggregate_workflow(
    workflow: Workflow,
    planned_runs: list[WorkflowPlanRun],
    terminal: dict[str, _ReportRunEvent],
    artifacts_by_run: dict[str, ReportArtifact],
) -> WorkflowAggregate:
    runs = [run for run in planned_runs if run.workflow is workflow]
    events = [terminal[run.run_id] for run in runs]
    artifacts = [artifacts_by_run[run.run_id] for run in runs if run.run_id in artifacts_by_run]
    provider_calls = [
        terminal[run.run_id].provider_call_count
        for run in runs
        if terminal[run.run_id].status is not CampaignRunStatus.NOT_RUN
    ]
    retries = [
        terminal[run.run_id].retry_count
        for run in runs
        if terminal[run.run_id].status is not CampaignRunStatus.NOT_RUN
    ]
    return WorkflowAggregate(
        workflow=workflow,
        scheduled_runs=len(runs),
        attempted_runs=sum(event.status is not CampaignRunStatus.NOT_RUN for event in events),
        completed_runs=sum(event.status is CampaignRunStatus.COMPLETED for event in events),
        quality_gate_passed_runs=sum(event.outcome is CampaignOutcome.SUCCESS for event in events),
        quality_gate_failed_runs=sum(
            event.outcome is CampaignOutcome.QUALITY_GATE_FAILURE for event in events
        ),
        provider_failed_runs=sum(
            event.outcome
            in {CampaignOutcome.PROVIDER_FAILURE, CampaignOutcome.PROVIDER_TIMEOUT}
            for event in events
        ),
        provider_timeout_runs=sum(
            event.outcome is CampaignOutcome.PROVIDER_TIMEOUT for event in events
        ),
        harness_failed_runs=sum(
            event.outcome
            in {CampaignOutcome.HARNESS_FAILURE, CampaignOutcome.CLEANUP_FAILURE}
            for event in events
        ),
        cleanup_failed_runs=sum(
            event.outcome is CampaignOutcome.CLEANUP_FAILURE for event in events
        ),
        interrupted_runs=sum(event.status is CampaignRunStatus.INTERRUPTED for event in events),
        not_run_runs=sum(event.status is CampaignRunStatus.NOT_RUN for event in events),
        evidence_runs=len(artifacts),
        acceptance=_gate(artifacts, GateKind.ACCEPTANCE),
        regression=_gate(artifacts, GateKind.REGRESSION),
        lint=_gate(artifacts, GateKind.LINT),
        typecheck=_gate(artifacts, GateKind.TYPECHECK),
        agent_duration_ms=_observed(
            [
                artifact.metrics.agent_duration_ms if artifact.metrics is not None else None
                for artifact in artifacts
            ]
        ),
        evaluation_duration_ms=_observed(
            [
                artifact.metrics.evaluation_duration_ms if artifact.metrics is not None else None
                for artifact in artifacts
            ]
        ),
        total_duration_ms=_observed(
            [
                artifact.metrics.total_duration_ms if artifact.metrics is not None else None
                for artifact in artifacts
            ]
        ),
        agent_call_count=_observed(provider_calls),
        retry_count=_observed(retries),
        changed_file_count=_observed([len(artifact.diff.changed_files) for artifact in artifacts]),
        added_lines=_observed([artifact.diff.added_lines for artifact in artifacts]),
        deleted_lines=_observed([artifact.diff.deleted_lines for artifact in artifacts]),
        usage=_usage(artifacts),
    )


def aggregate_workflow_campaign(
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
) -> WorkflowReport:
    """Read only persisted Artifacts; this function has no execution dependency."""
    try:
        plan = load_workflow_plan_contract(plan_path)
        if isinstance(plan, WorkflowPlanV1_2):
            canonical, campaign_bytes, terminal, artifacts = (
                _load_phase6_report_inputs(
                    spec_path,
                    plan_path,
                    campaign_path,
                    plan,
                )
            )
        else:
            events = load_campaign(campaign_path)
            first = events[0]
            assert isinstance(first, CampaignStartedEvent)
            canonical = workflow_plan_bytes(plan)
            if first.plan_sha256 != hashlib.sha256(canonical).hexdigest():
                raise WorkflowReportError("Campaign does not match Plan")
            legacy_terminal = _terminal_events(events)
            expected_ids = {run.run_id for run in plan.runs}
            if set(legacy_terminal) != expected_ids:
                raise WorkflowReportError(
                    "Campaign must retain one terminal state for every planned run"
                )
            for run in plan.runs:
                event = legacy_terminal[run.run_id]
                if (
                    event.task_id != run.task_id
                    or event.workflow is not run.workflow
                    or event.repetition_index != run.repetition_index
                ):
                    raise WorkflowReportError(
                        f"Campaign run identity differs from Plan for {run.run_id}"
                    )
            legacy_artifacts = _load_artifacts(
                spec_path,
                plan,
                legacy_terminal,
            )
            terminal = _legacy_report_events(legacy_terminal)
            artifacts = dict(legacy_artifacts)
            campaign_bytes = campaign_path.read_bytes()
    except (
        CampaignError,
        Phase6ContractError,
        ValidationError,
        WorkflowPlanError,
        WorkflowSpecError,
    ) as error:
        raise WorkflowReportError(str(error)) from error
    blocks: dict[tuple[str, int], list[WorkflowPlanRun]] = {}
    for run in plan.runs:
        blocks.setdefault((run.task_id, run.repetition_index), []).append(run)
    complete_pairs = sum(
        all(
            terminal[run.run_id].status is CampaignRunStatus.COMPLETED and run.run_id in artifacts
            for run in runs
        )
        for runs in blocks.values()
    )
    return WorkflowReport(
        schema_version="1.0",
        experiment_id=plan.experiment_id,
        plan_sha256=hashlib.sha256(canonical).hexdigest(),
        campaign_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        created_at=datetime.now(UTC),
        comparison_axis="workflow",
        automatic_winner_selected=False,
        denominator_note=(
            "run counts use scheduled_runs; metric aggregates report observed and missing runs"
        ),
        workflows=[
            _aggregate_workflow(workflow, plan.runs, terminal, artifacts)
            for workflow in (Workflow.ONE_SHOT, Workflow.STAGED)
        ],
        pairing=PairingAggregate(
            status=(Estimability.ESTIMABLE if complete_pairs > 0 else Estimability.NOT_ESTIMABLE),
            scheduled_pair_count=len(blocks),
            complete_pair_count=complete_pairs,
            denominator="task_id_x_repetition_with_both_workflows_completed",
        ),
    )


def workflow_report_json_bytes(report: WorkflowReport) -> bytes:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def load_workflow_report(path: Path) -> WorkflowReport:
    try:
        return WorkflowReport.model_validate(_strict_json(path, "Workflow Report"))
    except (ValidationError, WorkflowPlanError) as error:
        raise WorkflowReportError(f"invalid Workflow Report: {error}") from error


def workflow_report_markdown(report: WorkflowReport) -> str:
    lines = [
        f"# Workflow A/B Report: {report.experiment_id}",
        "",
        f"- Pairing: `{report.pairing.status.value}` "
        f"({report.pairing.complete_pair_count}/"
        f"{report.pairing.scheduled_pair_count} complete pairs)",
        "- Comparison axis: Workflow Prompt only",
        "- Automatic winner: no",
        "- Denominators: scheduled run counts; every metric states observed and missing runs.",
        "",
        "| Workflow | Scheduled | Attempted | Completed | Gate pass | Gate fail | "
        "Provider fail | Timeout | Harness | Cleanup | Interrupted | Not run |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.workflows:
        lines.append(
            f"| {item.workflow.value} | {item.scheduled_runs} | {item.attempted_runs} | "
            f"{item.completed_runs} | {item.quality_gate_passed_runs} | "
            f"{item.quality_gate_failed_runs} | {item.provider_failed_runs} | "
            f"{item.provider_timeout_runs} | {item.harness_failed_runs} | "
            f"{item.cleanup_failed_runs} | {item.interrupted_runs} | {item.not_run_runs} |"
        )
    for item in report.workflows:
        lines.extend(
            [
                "",
                f"## {item.workflow.value}",
                "",
                f"- Evidence: {item.evidence_runs}/{item.scheduled_runs} scheduled runs",
                f"- Agent duration (ms): total={item.agent_duration_ms.total}, "
                f"observed={item.agent_duration_ms.observed_runs}, "
                f"missing={item.agent_duration_ms.missing_runs}",
                f"- Evaluation duration (ms): total={item.evaluation_duration_ms.total}, "
                f"observed={item.evaluation_duration_ms.observed_runs}, "
                f"missing={item.evaluation_duration_ms.missing_runs}",
                f"- Total duration (ms): total={item.total_duration_ms.total}, "
                f"observed={item.total_duration_ms.observed_runs}, "
                f"missing={item.total_duration_ms.missing_runs}",
                f"- Agent calls: total={item.agent_call_count.total}, "
                f"observed={item.agent_call_count.observed_runs}, "
                f"missing={item.agent_call_count.missing_runs}",
                f"- Retries: total={item.retry_count.total}",
                f"- Changed files: total={item.changed_file_count.total}; "
                f"added lines={item.added_lines.total}; deleted lines={item.deleted_lines.total}",
                f"- Usage: available={item.usage.usage_available_runs}, "
                f"missing={item.usage.usage_missing_runs}",
                f"- Provider-reported input/output tokens: "
                f"{item.usage.provider_reported.input_tokens.total}/"
                f"{item.usage.provider_reported.output_tokens.total}",
                f"- Estimated input/output tokens: "
                f"{item.usage.estimated.input_tokens.total}/"
                f"{item.usage.estimated.output_tokens.total}",
                f"- Gate commands (pass/fail/abnormal): acceptance "
                f"{item.acceptance.passed_count}/{item.acceptance.failed_count}/"
                f"{item.acceptance.abnormal_count}; regression "
                f"{item.regression.passed_count}/{item.regression.failed_count}/"
                f"{item.regression.abnormal_count}; lint "
                f"{item.lint.passed_count}/{item.lint.failed_count}/"
                f"{item.lint.abnormal_count}; typecheck "
                f"{item.typecheck.passed_count}/{item.typecheck.failed_count}/"
                f"{item.typecheck.abnormal_count}",
            ]
        )
    lines.extend(
        [
            "",
            "This report makes no general model-performance or Provider-performance claim. "
            "Interpretation is limited to the preregistered Fixture, Prompt revisions, "
            "quality Gates, environment, and execution period.",
            "",
        ]
    )
    return "\n".join(lines)


def create_workflow_report(
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    output_path: Path,
    markdown_path: Path,
) -> WorkflowReport:
    report = aggregate_workflow_campaign(spec_path, plan_path, campaign_path)
    try:
        plan = load_workflow_plan_contract(plan_path)
    except (Phase6ContractError, WorkflowPlanError) as error:
        raise WorkflowReportError(str(error)) from error
    artifact_roots = {
        PurePosixPath(run.evidence_path).parent.parent
        for run in plan.runs
    }
    if len(artifact_roots) != 1:
        raise WorkflowReportError("Plan must use one fixed Artifact root")
    configured_root = next(iter(artifact_roots))
    try:
        resolved_root = (spec_path.parent / Path(configured_root)).resolve(strict=True)
        resolved_outputs = [
            output_path.resolve(strict=False),
            markdown_path.resolve(strict=False),
        ]
    except (OSError, RuntimeError) as error:
        raise WorkflowReportError("could not resolve report output boundary") from error
    if any(
        output == resolved_root or not output.is_relative_to(resolved_root)
        for output in resolved_outputs
    ):
        raise WorkflowReportError("report outputs must remain below the fixed Artifact root")
    try:
        _publish_create_only_pair(
            output_path,
            workflow_report_json_bytes(report),
            markdown_path,
            workflow_report_markdown(report).encode("utf-8"),
        )
    except WorkflowPlanError as error:
        raise WorkflowReportError(str(error)) from error
    return report
