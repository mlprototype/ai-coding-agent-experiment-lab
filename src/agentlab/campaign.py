"""Phase 4 sequential Workflow Campaign scheduler and append-only state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.live import (
    LiveCodexError,
    LiveCodexOutcome,
    LiveDiagnosticCreatedError,
    LiveDiagnosticPublicationError,
    run_live_codex,
)
from agentlab.models import (
    ContractModel,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.workflow import (
    FixedWorkflowInputs,
    LoadedWorkflowSpec,
    WorkflowExperimentSpec,
    WorkflowPlan,
    WorkflowPlanError,
    WorkflowPlanRun,
    WorkflowSpecError,
    _publish_create_only_pair,
    _reject_non_finite,
    _unique_object,
    build_workflow_plan_from_inputs,
    capture_workflow_inputs,
    load_workflow_plan,
    load_workflow_spec,
    workflow_inputs_unchanged,
    workflow_plan_bytes,
)
from agentlab.workspace import (
    DirectorySnapshot,
    paths_refer_to_same_file,
    remove_temporary_root,
    validate_fixture_source,
)


class CampaignError(ValueError):
    """A safe Campaign validation or execution failure."""


class CampaignRunStatus(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    NOT_RUN = "not_run"


class CampaignOutcome(StrEnum):
    SUCCESS = "success"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    HARNESS_FAILURE = "harness_failure"
    CLEANUP_FAILURE = "cleanup_failure"
    HUMAN_INTERRUPTION = "human_interruption"
    STOP_CONDITION = "stop_condition"


class CampaignStopReason(StrEnum):
    NONE = "none"
    FAIL_FAST = "fail_fast"
    MAX_FAILURES = "max_failures"
    MAX_TOTAL_DURATION = "max_total_duration_ms"
    HARNESS_FAILURE = "harness_failure"
    CLEANUP_FAILURE = "cleanup_failure"
    HUMAN_INTERRUPTION = "human_interruption"
    INPUT_CHANGED = "input_changed"


class AdapterCleanupState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    CLEARED = "cleared"
    FAILED = "failed"


class CampaignStartedEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: Literal[0]
    event_type: Literal["campaign_started"]
    experiment_id: StrictStr
    plan_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    planned_run_count: StrictInt = Field(gt=0)
    planned_provider_call_count: StrictInt = Field(gt=0)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)


class CampaignRunEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["run_state"]
    run_id: StrictStr
    task_id: StrictStr
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    status: CampaignRunStatus
    outcome: CampaignOutcome | None
    stop_reason: CampaignStopReason | None
    provider_call_count: StrictInt | None = Field(default=None, ge=0, le=1)
    retry_count: Literal[0]
    live_failure_kind: LiveFailureKind | None
    adapter_cleanup_state: AdapterCleanupState
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def status_details_are_consistent(self) -> CampaignRunEvent:
        if self.status is CampaignRunStatus.STARTED:
            if any(
                value is not None
                for value in (
                    self.outcome,
                    self.stop_reason,
                    self.provider_call_count,
                    self.live_failure_kind,
                )
            ) or self.adapter_cleanup_state is not AdapterCleanupState.NOT_APPLICABLE:
                raise ValueError("started state must not predict a terminal result")
        elif self.status is CampaignRunStatus.NOT_RUN:
            if (
                self.outcome is not CampaignOutcome.STOP_CONDITION
                or self.stop_reason in {None, CampaignStopReason.NONE}
                or self.provider_call_count != 0
                or self.live_failure_kind is not None
                or self.adapter_cleanup_state
                is not AdapterCleanupState.NOT_APPLICABLE
            ):
                raise ValueError("not_run requires a fixed stop reason and zero calls")
        elif self.status is CampaignRunStatus.INTERRUPTED:
            if (
                self.outcome is not CampaignOutcome.HUMAN_INTERRUPTION
                or self.stop_reason is not CampaignStopReason.HUMAN_INTERRUPTION
            ):
                raise ValueError("interrupted state requires human_interruption")
        elif self.status in {CampaignRunStatus.COMPLETED, CampaignRunStatus.FAILED}:
            if self.outcome is None or self.stop_reason is not None:
                raise ValueError("terminal attempted state requires an outcome only")
            completed_outcomes = {
                CampaignOutcome.SUCCESS,
                CampaignOutcome.QUALITY_GATE_FAILURE,
            }
            if (self.status is CampaignRunStatus.COMPLETED) is not (
                self.outcome in completed_outcomes
            ):
                raise ValueError("completed/failed state must match outcome taxonomy")
        else:
            raise ValueError("Campaign recording must not append planned states")
        if (
            self.adapter_cleanup_state is AdapterCleanupState.FAILED
            and self.outcome is not CampaignOutcome.CLEANUP_FAILURE
        ):
            raise ValueError("failed adapter cleanup requires cleanup_failure")
        return self


class CampaignFinishedEvent(ContractModel):
    schema_version: Literal["1.1"]
    sequence: StrictInt = Field(gt=0)
    event_type: Literal["campaign_finished"]
    experiment_id: StrictStr
    stop_reason: CampaignStopReason
    attempted_run_count: StrictInt = Field(ge=0)
    provider_call_count: StrictInt = Field(ge=0)
    provider_call_count_unknown_runs: StrictInt = Field(ge=0)
    retry_count: Literal[0]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)


CampaignEvent = Annotated[
    CampaignStartedEvent | CampaignRunEvent | CampaignFinishedEvent,
    Field(discriminator="event_type"),
]
_CAMPAIGN_EVENT_ADAPTER: TypeAdapter[CampaignEvent] = TypeAdapter(CampaignEvent)


@dataclass(frozen=True)
class CampaignRunExecution:
    outcome: CampaignOutcome
    provider_call_count: int | None
    live_failure_kind: LiveFailureKind | None
    adapter_cleanup_state: AdapterCleanupState = AdapterCleanupState.NOT_APPLICABLE


@dataclass(frozen=True)
class CampaignOutcomeSummary:
    campaign_path: Path
    stop_reason: CampaignStopReason
    attempted_run_count: int
    provider_call_count: int
    provider_call_count_unknown_runs: int


RunExecutor = Callable[
    [
        Path,
        LoadedWorkflowSpec,
        WorkflowPlanRun,
        FixedWorkflowInputs,
        Mapping[str, str] | None,
    ],
    CampaignRunExecution,
]

AdapterCleanup = Callable[[Path], tuple[bool, str | None]]


class _AdapterCleanupInterrupted(Exception):
    def __init__(self, original: KeyboardInterrupt | SystemExit) -> None:
        super().__init__("adapter cleanup failed during interruption")
        self.original = original


def _utc_timestamp(value: datetime) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("Campaign timestamps must be timezone-aware UTC")
    return value


def _event_bytes(event: CampaignEvent) -> bytes:
    return (
        json.dumps(
            event.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _ensure_no_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise CampaignError(f"could not inspect {label}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CampaignError(f"{label} path must not contain symlinks")


def _create_campaign(path: Path, event: CampaignStartedEvent) -> None:
    _ensure_no_symlink_components(path, "Campaign")
    if os.path.lexists(path):
        raise CampaignError("Campaign output already exists; resume is not supported")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_event_bytes(event))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except (FileExistsError, OSError) as error:
        raise CampaignError("could not create Campaign atomically") from error
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _append_campaign(path: Path, event: CampaignRunEvent | CampaignFinishedEvent) -> None:
    _ensure_no_symlink_components(path, "Campaign")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CampaignError("Campaign must remain a regular file")
        payload = _event_bytes(event)
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise CampaignError("Campaign append was incomplete")
        os.fsync(descriptor)
    except OSError as error:
        raise CampaignError("could not append Campaign atomically") from error
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def load_campaign(path: Path) -> list[CampaignEvent]:
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CampaignError("could not read Campaign JSONL") from error
    if not lines:
        raise CampaignError("Campaign JSONL must not be empty")
    events: list[CampaignEvent] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_finite,
            )
            event = _CAMPAIGN_EVENT_ADAPTER.validate_python(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            raise CampaignError(f"invalid Campaign event at line {line_number}") from error
        events.append(event)
    if not isinstance(events[0], CampaignStartedEvent):
        raise CampaignError("Campaign must start with campaign_started")
    if [event.sequence for event in events] != list(range(len(events))):
        raise CampaignError("Campaign event sequence must be contiguous")
    if not isinstance(events[-1], CampaignFinishedEvent):
        raise CampaignError("Campaign must end with campaign_finished")
    if sum(isinstance(event, CampaignStartedEvent) for event in events) != 1:
        raise CampaignError("Campaign must contain exactly one campaign_started")
    if sum(isinstance(event, CampaignFinishedEvent) for event in events) != 1:
        raise CampaignError("Campaign must contain exactly one campaign_finished")
    header = events[0]
    finished = events[-1]
    assert isinstance(header, CampaignStartedEvent)
    assert isinstance(finished, CampaignFinishedEvent)
    if header.experiment_id != finished.experiment_id:
        raise CampaignError("Campaign experiment ID must remain stable")
    seen_started: dict[str, CampaignRunEvent] = {}
    seen_terminal: set[str] = set()
    for event in events:
        if not isinstance(event, CampaignRunEvent):
            continue
        if event.status is CampaignRunStatus.STARTED:
            if event.run_id in seen_started:
                raise CampaignError("a run may start only once")
            seen_started[event.run_id] = event
        else:
            if event.run_id in seen_terminal:
                raise CampaignError("a run may have only one terminal state")
            if event.status is not CampaignRunStatus.NOT_RUN and event.run_id not in seen_started:
                raise CampaignError("an attempted terminal state requires started")
            if event.status is not CampaignRunStatus.NOT_RUN:
                started = seen_started[event.run_id]
                if (
                    event.task_id != started.task_id
                    or event.workflow is not started.workflow
                    or event.repetition_index != started.repetition_index
                ):
                    raise CampaignError("run identity must remain stable")
            seen_terminal.add(event.run_id)
    run_events = [event for event in events if isinstance(event, CampaignRunEvent)]
    terminal_events = [
        event for event in run_events if event.status is not CampaignRunStatus.STARTED
    ]
    known_calls = sum(event.provider_call_count or 0 for event in terminal_events)
    unknown_call_runs = sum(
        event.provider_call_count is None
        for event in terminal_events
        if event.status is not CampaignRunStatus.NOT_RUN
    )
    if (
        len(terminal_events) != header.planned_run_count
        or len(seen_started) != finished.attempted_run_count
        or known_calls != finished.provider_call_count
        or unknown_call_runs != finished.provider_call_count_unknown_runs
    ):
        raise CampaignError("Campaign header, run states, and terminal counts must match")
    not_run_reasons = {
        event.stop_reason
        for event in terminal_events
        if event.status is CampaignRunStatus.NOT_RUN
    }
    if (
        (
            finished.stop_reason is CampaignStopReason.NONE
            and not_run_reasons
        )
        or (
            finished.stop_reason is not CampaignStopReason.NONE
            and not_run_reasons != {finished.stop_reason}
        )
    ):
        raise CampaignError("Campaign stop reason must match every not_run state")
    return events


def _plan_digest(plan: WorkflowPlan) -> str:
    return hashlib.sha256(workflow_plan_bytes(plan)).hexdigest()


def _resolve_artifact(spec_path: Path, configured: str) -> Path:
    return spec_path.parent / Path(configured)


def _protect_campaign_outputs(
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    plan: WorkflowPlan,
) -> None:
    loaded = load_workflow_spec(spec_path)
    source, _snapshot = validate_fixture_source(
        spec_path,
        loaded.spec.runner.fixture_path,
    )
    prompt_path = spec_path.parent / loaded.spec.task_prompt_path
    try:
        resolved_source = source.resolve(strict=True)
        resolved_prompt = prompt_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CampaignError("could not resolve protected Campaign inputs") from error
    reserved = [
        _resolve_artifact(spec_path, configured)
        for run in plan.runs
        for configured in (run.recording_path, run.evidence_path, run.diagnostic_path)
    ]
    all_outputs = [campaign_path, *reserved]
    for output in all_outputs:
        _ensure_no_symlink_components(output, "Campaign Artifact")
        if os.path.lexists(output):
            raise CampaignError("Campaign Artifact reservation already exists")
        if paths_refer_to_same_file(output, spec_path) or paths_refer_to_same_file(
            output, plan_path
        ):
            raise CampaignError("Campaign Artifact must not alias Spec or Plan")
        try:
            resolved_output = output.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise CampaignError("could not resolve Campaign Artifact") from error
        if (
            resolved_output in {resolved_source, resolved_prompt}
            or resolved_output.is_relative_to(resolved_source)
        ):
            raise CampaignError("Campaign Artifact must not overwrite Fixture or Prompt")
    absolute_strings = [str(path.absolute()) for path in all_outputs]
    if len(absolute_strings) != len(set(absolute_strings)):
        raise CampaignError("Campaign Artifact reservations must be unique")


def _adapter_spec(
    spec: WorkflowExperimentSpec,
    run: WorkflowPlanRun,
) -> dict[str, Any]:
    treatment = (
        Workflow.STAGED.value if run.workflow is Workflow.ONE_SHOT else Workflow.ONE_SHOT.value
    )
    return {
        "schema_version": "1.0",
        "experiment_id": spec.experiment_id,
        "title": spec.title,
        "research_question": spec.research_question,
        "hypothesis": spec.hypothesis,
        "comparison_axis": "workflow",
        "workflow": run.workflow.value,
        "provider": "codex",
        "control": run.workflow.value,
        "treatments": [treatment],
        "fixed_factors": {
            "phase": "4",
            "fixture_revision": spec.fixture_revision,
            "task_prompt_revision": spec.task_prompt_revision,
            "workflow_revision": run.workflow_revision,
            "sandbox": spec.sandbox,
            "network_access": spec.network_access,
        },
        "task_ids": [run.task_id],
        "repetitions": run.repetition_index + 1,
        "random_seed": spec.random_seed,
        "quality_gate": spec.quality_gate.model_dump(mode="json"),
        "stop_conditions": spec.stop_conditions.model_dump(mode="json"),
        "execution_mode": "live",
        "live": {
            "record_to": "recording.jsonl",
            "diagnostic_to": "diagnostic.json",
            "prompt_path": "prompt.md",
            "model": spec.model,
            "reasoning_effort": spec.reasoning_effort.value,
            "provider_timeout_ms": spec.provider_timeout_ms,
            "max_prompt_bytes": spec.max_prompt_bytes,
            "max_event_line_bytes": spec.max_event_line_bytes,
            "max_provider_output_bytes": spec.max_provider_output_bytes,
            "require_explicit_confirmation": True,
        },
        "runner": {
            **spec.runner.model_dump(mode="json"),
            "fixture_path": "fixture",
        },
    }


def _publish_adapter_artifact(source: Path, destination: Path) -> None:
    _ensure_no_symlink_components(destination, "reserved Artifact")
    if os.path.lexists(destination):
        raise CampaignError("reserved Artifact appeared during execution")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as error:
        raise CampaignError("could not publish reserved Artifact") from error


def _provider_call_count(artifact: LiveRunArtifact) -> int:
    if artifact.codex.execution_stage.value == "provider_invocation_attempted":
        return 1
    return 0


def _classify_artifact(artifact: LiveRunArtifact) -> CampaignRunExecution:
    call_count = _provider_call_count(artifact)
    if artifact.overall_status is LiveOverallStatus.PASSED:
        return CampaignRunExecution(
            CampaignOutcome.SUCCESS,
            call_count,
            artifact.failure_kind,
        )
    if artifact.overall_status is LiveOverallStatus.FAILED:
        return CampaignRunExecution(
            CampaignOutcome.QUALITY_GATE_FAILURE,
            call_count,
            artifact.failure_kind,
        )
    if artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR:
        outcome = (
            CampaignOutcome.PROVIDER_TIMEOUT
            if artifact.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
            else CampaignOutcome.PROVIDER_FAILURE
        )
        return CampaignRunExecution(outcome, call_count, artifact.failure_kind)
    outcome = (
        CampaignOutcome.CLEANUP_FAILURE
        if artifact.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
        or artifact.workspace_lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
        else CampaignOutcome.HARNESS_FAILURE
    )
    return CampaignRunExecution(outcome, call_count, artifact.failure_kind)


def _materialize_fixture_snapshot(
    snapshot: DirectorySnapshot,
    destination: Path,
) -> None:
    destination.mkdir()
    for relative in snapshot.directories:
        (destination / Path(relative)).mkdir(parents=True)
    for relative, content in sorted(snapshot.files.items()):
        path = destination / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _redact_adapter_prompt(adapter_root: Path) -> None:
    prompt_path = adapter_root / "prompt.md"
    with suppress(OSError):
        prompt_path.write_bytes(b"")
    with suppress(OSError):
        prompt_path.unlink(missing_ok=True)


def _default_run_executor(
    spec_path: Path,
    loaded: LoadedWorkflowSpec,
    run: WorkflowPlanRun,
    fixed: FixedWorkflowInputs,
    parent_environment: Mapping[str, str] | None,
    *,
    adapter_cleanup: AdapterCleanup,
) -> CampaignRunExecution:
    spec = loaded.spec
    prompt = fixed.prompts[run.workflow]
    evidence_path = _resolve_artifact(spec_path, run.evidence_path)
    recording_path = _resolve_artifact(spec_path, run.recording_path)
    diagnostic_path = _resolve_artifact(spec_path, run.diagnostic_path)
    adapter_root: Path | None = None
    execution: CampaignRunExecution | None = None
    caught: BaseException | None = None
    try:
        unresolved_root = Path(tempfile.mkdtemp(prefix="agentlab-phase4-adapter-"))
        adapter_root = unresolved_root
        adapter_root = unresolved_root.resolve()
        _materialize_fixture_snapshot(fixed.fixture, adapter_root / "fixture")
        (adapter_root / "prompt.md").write_bytes(prompt.content)
        adapter_path = adapter_root / "experiment.yaml"
        adapter_path.write_text(
            yaml.safe_dump(_adapter_spec(spec, run), sort_keys=False),
            encoding="utf-8",
        )
        adapter_evidence = adapter_root / "evidence.json"
        try:
            outcome: LiveCodexOutcome = run_live_codex(
                adapter_path,
                task_id=run.task_id,
                repetition_index=run.repetition_index,
                run_id=run.run_id,
                output_path=adapter_evidence,
                confirm_live_codex=True,
                force=False,
                parent_environment=parent_environment,
                _allow_workflow_campaign=True,
            )
        except (LiveDiagnosticCreatedError, LiveDiagnosticPublicationError, LiveCodexError):
            adapter_diagnostic = adapter_root / "diagnostic.json"
            if adapter_diagnostic.exists():
                _publish_adapter_artifact(adapter_diagnostic, diagnostic_path)
            raise
        try:
            _publish_create_only_pair(
                recording_path,
                outcome.recording_path.read_bytes(),
                evidence_path,
                adapter_evidence.read_bytes(),
            )
        except (OSError, WorkflowPlanError) as error:
            raise CampaignError("could not publish paired run Artifacts") from error
        execution = _classify_artifact(outcome.artifact)
    except BaseException as error:
        caught = error

    cleanup_cleared = True
    if adapter_root is not None:
        try:
            cleanup_cleared, _cleanup_error = adapter_cleanup(adapter_root)
        except Exception:
            cleanup_cleared = False
        if not cleanup_cleared:
            _redact_adapter_prompt(adapter_root)
            if isinstance(caught, (KeyboardInterrupt, SystemExit)):
                raise _AdapterCleanupInterrupted(caught)
            return CampaignRunExecution(
                outcome=CampaignOutcome.CLEANUP_FAILURE,
                provider_call_count=(
                    None if execution is None else execution.provider_call_count
                ),
                live_failure_kind=(
                    None if execution is None else execution.live_failure_kind
                ),
                adapter_cleanup_state=AdapterCleanupState.FAILED,
            )
    if caught is not None:
        raise caught
    assert execution is not None
    return replace(
        execution,
        adapter_cleanup_state=AdapterCleanupState.CLEARED,
    )


def _run_event(
    *,
    sequence: int,
    run: WorkflowPlanRun,
    status: CampaignRunStatus,
    outcome: CampaignOutcome | None = None,
    stop_reason: CampaignStopReason | None = None,
    provider_call_count: int | None = None,
    live_failure_kind: LiveFailureKind | None = None,
    adapter_cleanup_state: AdapterCleanupState = AdapterCleanupState.NOT_APPLICABLE,
) -> CampaignRunEvent:
    return CampaignRunEvent(
        schema_version="1.1",
        sequence=sequence,
        event_type="run_state",
        run_id=run.run_id,
        task_id=run.task_id,
        workflow=run.workflow,
        repetition_index=run.repetition_index,
        status=status,
        outcome=outcome,
        stop_reason=stop_reason,
        provider_call_count=provider_call_count,
        retry_count=0,
        live_failure_kind=live_failure_kind,
        adapter_cleanup_state=adapter_cleanup_state,
        occurred_at=datetime.now(UTC),
    )


def run_workflow_campaign(
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    *,
    confirm_live_codex: bool,
    confirm_provider_calls: int | None,
    parent_environment: Mapping[str, str] | None = None,
    run_executor: RunExecutor | None = None,
    adapter_cleanup: AdapterCleanup = remove_temporary_root,
    monotonic: Callable[[], float] = time.monotonic,
) -> CampaignOutcomeSummary:
    """Run a preregistered Plan sequentially with no retry, fallback, or resume."""
    if not confirm_live_codex or confirm_provider_calls is None:
        raise CampaignError(
            "Campaign requires --confirm-live-codex and --confirm-provider-calls; "
            "no subprocess was started"
        )
    try:
        loaded = load_workflow_spec(spec_path)
        plan = load_workflow_plan(plan_path)
        fixed = capture_workflow_inputs(spec_path, loaded.spec)
    except (WorkflowSpecError, WorkflowPlanError) as error:
        raise CampaignError(str(error)) from error
    if confirm_provider_calls != plan.planned_provider_call_count:
        raise CampaignError(
            "confirmed Provider call count does not match the preregistered Plan; "
            "no subprocess was started"
        )
    rebuilt = build_workflow_plan_from_inputs(spec_path, loaded, fixed)
    if workflow_plan_bytes(rebuilt) != workflow_plan_bytes(plan):
        raise CampaignError("Spec, Prompt, Fixture, or Plan changed after preregistration")
    _protect_campaign_outputs(spec_path, plan_path, campaign_path, plan)

    started = CampaignStartedEvent(
        schema_version="1.1",
        sequence=0,
        event_type="campaign_started",
        experiment_id=plan.experiment_id,
        plan_sha256=_plan_digest(plan),
        planned_run_count=plan.planned_run_count,
        planned_provider_call_count=plan.planned_provider_call_count,
        occurred_at=datetime.now(UTC),
    )
    _create_campaign(campaign_path, started)
    if run_executor is None:

        def default_executor(
            executor_spec_path: Path,
            executor_loaded: LoadedWorkflowSpec,
            executor_run: WorkflowPlanRun,
            executor_fixed: FixedWorkflowInputs,
            executor_environment: Mapping[str, str] | None,
        ) -> CampaignRunExecution:
            return _default_run_executor(
                executor_spec_path,
                executor_loaded,
                executor_run,
                executor_fixed,
                executor_environment,
                adapter_cleanup=adapter_cleanup,
            )

        executor: RunExecutor = default_executor
    else:
        executor = run_executor
    sequence = 1
    attempted = 0
    provider_calls = 0
    unknown_call_runs = 0
    counted_failures = 0
    stop_reason = CampaignStopReason.NONE
    campaign_started = monotonic()
    next_index = 0
    pending_interrupt: KeyboardInterrupt | SystemExit | None = None

    try:
        for index, run in enumerate(plan.runs):
            next_index = index
            if not workflow_inputs_unchanged(spec_path, loaded.spec, fixed):
                stop_reason = CampaignStopReason.INPUT_CHANGED
                break
            maximum = loaded.spec.stop_conditions.max_total_duration_ms
            if maximum is not None and (monotonic() - campaign_started) * 1000 >= maximum:
                stop_reason = CampaignStopReason.MAX_TOTAL_DURATION
                break
            _append_campaign(
                campaign_path,
                _run_event(
                    sequence=sequence,
                    run=run,
                    status=CampaignRunStatus.STARTED,
                ),
            )
            sequence += 1
            attempted += 1
            try:
                execution = executor(
                    spec_path,
                    loaded,
                    run,
                    fixed,
                    parent_environment,
                )
            except _AdapterCleanupInterrupted as error:
                execution = CampaignRunExecution(
                    outcome=CampaignOutcome.CLEANUP_FAILURE,
                    provider_call_count=None,
                    live_failure_kind=None,
                    adapter_cleanup_state=AdapterCleanupState.FAILED,
                )
                pending_interrupt = error.original
            except (KeyboardInterrupt, SystemExit):
                _append_campaign(
                    campaign_path,
                    _run_event(
                        sequence=sequence,
                        run=run,
                        status=CampaignRunStatus.INTERRUPTED,
                        outcome=CampaignOutcome.HUMAN_INTERRUPTION,
                        stop_reason=CampaignStopReason.HUMAN_INTERRUPTION,
                        provider_call_count=None,
                    ),
                )
                sequence += 1
                stop_reason = CampaignStopReason.HUMAN_INTERRUPTION
                unknown_call_runs += 1
                next_index = index + 1
                raise
            except (LiveCodexError, CampaignError, OSError, ValueError):
                execution = CampaignRunExecution(
                    outcome=CampaignOutcome.HARNESS_FAILURE,
                    provider_call_count=None,
                    live_failure_kind=LiveFailureKind.EVIDENCE_ERROR,
                )
            if execution.provider_call_count is not None:
                provider_calls += execution.provider_call_count
            else:
                unknown_call_runs += 1
            terminal_status = (
                CampaignRunStatus.COMPLETED
                if execution.outcome
                in {CampaignOutcome.SUCCESS, CampaignOutcome.QUALITY_GATE_FAILURE}
                else CampaignRunStatus.FAILED
            )
            _append_campaign(
                campaign_path,
                _run_event(
                    sequence=sequence,
                    run=run,
                    status=terminal_status,
                    outcome=execution.outcome,
                    provider_call_count=execution.provider_call_count,
                    live_failure_kind=execution.live_failure_kind,
                    adapter_cleanup_state=execution.adapter_cleanup_state,
                ),
            )
            sequence += 1
            next_index = index + 1
            if execution.outcome in {
                CampaignOutcome.QUALITY_GATE_FAILURE,
                CampaignOutcome.PROVIDER_FAILURE,
                CampaignOutcome.PROVIDER_TIMEOUT,
            }:
                counted_failures += 1
                if loaded.spec.stop_conditions.fail_fast:
                    stop_reason = CampaignStopReason.FAIL_FAST
                    break
                maximum_failures = loaded.spec.stop_conditions.max_failures
                if maximum_failures is not None and counted_failures >= maximum_failures:
                    stop_reason = CampaignStopReason.MAX_FAILURES
                    break
            if execution.outcome is CampaignOutcome.HARNESS_FAILURE:
                stop_reason = CampaignStopReason.HARNESS_FAILURE
                break
            if execution.outcome is CampaignOutcome.CLEANUP_FAILURE:
                stop_reason = CampaignStopReason.CLEANUP_FAILURE
                break
    except (KeyboardInterrupt, SystemExit):
        for run in plan.runs[next_index:]:
            _append_campaign(
                campaign_path,
                _run_event(
                    sequence=sequence,
                    run=run,
                    status=CampaignRunStatus.NOT_RUN,
                    outcome=CampaignOutcome.STOP_CONDITION,
                    stop_reason=CampaignStopReason.HUMAN_INTERRUPTION,
                    provider_call_count=0,
                ),
            )
            sequence += 1
        _append_campaign(
            campaign_path,
            CampaignFinishedEvent(
                schema_version="1.1",
                sequence=sequence,
                event_type="campaign_finished",
                experiment_id=plan.experiment_id,
                stop_reason=CampaignStopReason.HUMAN_INTERRUPTION,
                attempted_run_count=attempted,
                provider_call_count=provider_calls,
                provider_call_count_unknown_runs=unknown_call_runs,
                retry_count=0,
                occurred_at=datetime.now(UTC),
            ),
        )
        raise

    if stop_reason is not CampaignStopReason.NONE:
        for run in plan.runs[next_index:]:
            _append_campaign(
                campaign_path,
                _run_event(
                    sequence=sequence,
                    run=run,
                    status=CampaignRunStatus.NOT_RUN,
                    outcome=CampaignOutcome.STOP_CONDITION,
                    stop_reason=stop_reason,
                    provider_call_count=0,
                ),
            )
            sequence += 1
    _append_campaign(
        campaign_path,
        CampaignFinishedEvent(
            schema_version="1.1",
            sequence=sequence,
            event_type="campaign_finished",
            experiment_id=plan.experiment_id,
            stop_reason=stop_reason,
            attempted_run_count=attempted,
            provider_call_count=provider_calls,
            provider_call_count_unknown_runs=unknown_call_runs,
            retry_count=0,
            occurred_at=datetime.now(UTC),
        ),
    )
    if pending_interrupt is not None:
        raise pending_interrupt
    return CampaignOutcomeSummary(
        campaign_path=campaign_path,
        stop_reason=stop_reason,
        attempted_run_count=attempted,
        provider_call_count=provider_calls,
        provider_call_count_unknown_runs=unknown_call_runs,
    )
