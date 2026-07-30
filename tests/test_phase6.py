from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from agentlab.campaign import CampaignRunStatus, CampaignStopReason
from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    CODEX_REQUIRED_EXEC_FLAGS,
    CodexApprovalBasis,
    CodexCleanupState,
    CodexCliProfile,
    CodexExecutionEvidence,
    CodexExecutionStage,
    CodexInvocationState,
    CodexProviderFailureHint,
    CodexRunnerState,
    CodexStdinWriteState,
    CodexTerminalEvent,
    CommandEvidence,
    CommandStatus,
    GateKind,
    LiveFailureKind,
    Provider,
    ProviderExecutionStatus,
    RunMetrics,
    TerminationEvidence,
    TerminationReason,
    UsageMetrics,
    UsageMetricSource,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.phase6 import (
    ArtifactReference,
    ChecksumEntry,
    DiffPolicy,
    EditablePathPolicy,
    ExternalChecksumAnchor,
    FixtureAcceptanceRecord,
    FixtureManifest,
    GateAcceptanceSummary,
    GateNotExecutedReason,
    HistoricalVerificationRecord,
    Language,
    LanguageStatus,
    LiveRunArtifactV1_2,
    LoadedPhase6Campaign,
    Phase6CampaignFinishedEvent,
    Phase6CampaignOutcome,
    Phase6CampaignRunEvent,
    Phase6CampaignStartedEvent,
    Phase6ContractError,
    Phase6FailureKind,
    Phase6PathError,
    Phase6Recording,
    Phase6RecordingStartedEvent,
    Phase6RecordingTerminalEvent,
    PrimarySuiteSource,
    ProtectedPathPolicy,
    ProviderCoverage,
    ProviderEvaluationStatus,
    PublicChecksums,
    PublicLanguageReport,
    PublicRunRecord,
    PublicSuiteManifest,
    PublicSuiteReport,
    ReleaseMetadata,
    SourceClass,
    ToolchainComponent,
    ToolchainComponentRole,
    ToolchainIdentity,
    WorkflowExperimentSpecV2_1,
    WorkflowPlanV1_2,
    _validate_primary_live_bindings,
    canonical_json_bytes,
    derive_language_status,
    load_campaign_contract,
    load_external_checksum_anchor,
    load_fixture_manifest,
    load_public_checksums,
    load_public_language_report,
    load_public_run_record,
    load_public_suite_inputs,
    load_public_suite_manifest,
    load_public_suite_report,
    load_release_metadata,
    load_workflow_plan_contract,
    load_workflow_spec_contract,
    validate_checksum_contract,
    validate_data_cutoff,
    validate_expected_language_status,
    validate_plan_bindings,
    validate_public_suite_inputs,
)
from agentlab.workflow import (
    WorkflowExperimentSpec,
    WorkflowPlan,
    build_workflow_plan,
    workflow_plan_bytes,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
COMMIT = "1" * 40
T0 = "2026-07-30T01:02:03.000000Z"
T1 = "2026-07-30T02:03:04.000000Z"


def _toolchain() -> ToolchainIdentity:
    component = ToolchainComponent(
        role=ToolchainComponentRole.PYTHON_RUNTIME,
        resolved_executable_path="/opt/agentlab/toolchain/python",
        executable_sha256=HASH_A,
        version_argv=[
            "/opt/agentlab/toolchain/python",
            "--version",
        ],
        exact_version="measured-runtime 1.2.3",
        version_output_sha256=HASH_B,
        package_version=None,
        package_fingerprint=None,
    )
    raw = {
        "architecture": "arm64",
        "components": [component.model_dump(mode="json")],
        "gate_path_entries": ["/opt/agentlab/toolchain"],
        "os": "darwin",
        "workspace_executable_lookup_allowed": False,
    }
    fingerprint = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    return ToolchainIdentity(
        os="darwin",
        architecture="arm64",
        gate_path_entries=["/opt/agentlab/toolchain"],
        workspace_executable_lookup_allowed=False,
        components=[component],
        fingerprint=fingerprint,
    )


def _fixture_contracts() -> tuple[
    FixtureManifest,
    DiffPolicy,
    FixtureAcceptanceRecord,
]:
    toolchain = _toolchain()
    manifest = FixtureManifest(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="python-task-v1",
        fixture_sha256=HASH_A,
        gate_contract_sha256=HASH_B,
        toolchain=toolchain,
    )
    policy = DiffPolicy(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="python-task-v1",
        editable_paths=[
            EditablePathPolicy(
                path="src/task.py",
                allow_create=False,
                allow_delete=False,
            )
        ],
        protected_paths=[
            ProtectedPathPolicy(
                path="fixture.manifest.json",
                role="fixture_manifest",
            ),
            ProtectedPathPolicy(
                path="tools/gate.py",
                role="gate_helper",
            ),
        ],
        reject_unclassified_paths=True,
        reject_symlinks=True,
        reject_hardlinks=True,
        reject_special_files=True,
    )
    acceptance = FixtureAcceptanceRecord(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="python-task-v1",
        acceptance_agentlab_commit=COMMIT,
        fixture_source_commit=COMMIT,
        fixture_sha256=HASH_A,
        fixture_manifest_sha256=hashlib.sha256(
            canonical_json_bytes(manifest)
        ).hexdigest(),
        diff_policy_sha256=hashlib.sha256(
            canonical_json_bytes(policy)
        ).hexdigest(),
        gate_contract_sha256=HASH_B,
        reference_solution_sha256=HASH_C,
        reference_solution_in_provider_workspace=False,
        toolchain=toolchain,
        result=GateAcceptanceSummary(
            acceptance_failed_as_expected=True,
            regression_passed=True,
            lint_passed=True,
            typecheck_passed=True,
            reference_all_gates_passed=True,
            source_unchanged=True,
            workspace_cleanup_succeeded=True,
        ),
        verified_at=T0,
    )
    return manifest, policy, acceptance


def _phase6_spec(tmp_path: Path) -> tuple[Path, WorkflowExperimentSpecV2_1]:
    raw = yaml.safe_load(
        Path("experiments/examples/workflow-ab.yaml").read_text(encoding="utf-8")
    )
    raw.update(
        {
            "schema_version": "2.1",
            "language": "python",
            "reviewed_commit": COMMIT,
            "fixture_manifest_path": "fixture.manifest.json",
            "fixture_acceptance_path": "fixture.acceptance.json",
            "diff_policy_path": "diff-policy.json",
        }
    )
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    loaded = load_workflow_spec_contract(path)
    assert isinstance(loaded.spec, WorkflowExperimentSpecV2_1)
    return path, loaded.spec


def _phase6_plan(
    tmp_path: Path,
    spec_path: Path,
    spec: WorkflowExperimentSpecV2_1,
    manifest: FixtureManifest,
    policy: DiffPolicy,
    acceptance: FixtureAcceptanceRecord,
) -> WorkflowPlanV1_2:
    old_plan = build_workflow_plan(Path("experiments/examples/workflow-ab.yaml"))
    raw = old_plan.model_dump(mode="json")
    raw.update(
        {
            "schema_version": "1.2",
            "experiment_spec_schema_version": "2.1",
            "experiment_spec_sha256": hashlib.sha256(
                spec_path.read_bytes()
            ).hexdigest(),
            "fixture_sha256": manifest.fixture_sha256,
            "fixture_revision": manifest.fixture_revision,
            "language": spec.language.value,
            "reviewed_commit": spec.reviewed_commit,
            "fixture_manifest_sha256": hashlib.sha256(
                canonical_json_bytes(manifest)
            ).hexdigest(),
            "fixture_acceptance_sha256": hashlib.sha256(
                canonical_json_bytes(acceptance)
            ).hexdigest(),
            "diff_policy_sha256": hashlib.sha256(
                canonical_json_bytes(policy)
            ).hexdigest(),
            "gate_contract_sha256": manifest.gate_contract_sha256,
            "reference_solution_sha256": acceptance.reference_solution_sha256,
            "toolchain_fingerprint": manifest.toolchain.fingerprint,
        }
    )
    for run in raw["runs"]:
        run["fixture_revision"] = manifest.fixture_revision
    return WorkflowPlanV1_2.model_validate(raw)


def _coverage() -> list[ProviderCoverage]:
    return [
        ProviderCoverage(
            provider=Provider.CODEX,
            evaluation_status=ProviderEvaluationStatus.NOT_EVALUATED,
            evaluated_languages=[],
            blocker=None,
        ),
        ProviderCoverage(
            provider=Provider.ANTIGRAVITY,
            evaluation_status=ProviderEvaluationStatus.NOT_EVALUATED,
            evaluated_languages=[],
            blocker="upstream_artifact_signature_invalid",
        ),
    ]


def _suite_manifest(
    source: PrimarySuiteSource,
    *,
    cutoff: str = T0,
) -> PublicSuiteManifest:
    return PublicSuiteManifest(
        schema_version="1.0",
        suite_id="phase6-suite",
        renderer_version="phase6-renderer-1.0",
        data_cutoff_at=cutoff,
        primary_sources=[source],
        historical_sources=[],
        provider_coverage=_coverage(),
        antigravity_blocker="upstream_artifact_signature_invalid",
        planned_outputs=[
            "checksums.json",
            "release-metadata.json",
            "report.json",
        ],
        automatic_winner_selected=False,
        leaderboard_generated=False,
        statistical_significance_claimed=False,
    )


def _completed_campaign() -> LoadedPhase6Campaign:
    started = Phase6CampaignStartedEvent(
        schema_version="1.2",
        sequence=0,
        event_type="campaign_started",
        experiment_id="phase6-python",
        plan_sha256=HASH_A,
        fixture_manifest_sha256=HASH_A,
        fixture_acceptance_sha256=HASH_A,
        diff_policy_sha256=HASH_A,
        toolchain_fingerprint=HASH_A,
        planned_run_count=2,
        planned_provider_call_count=2,
        occurred_at=T0,
    )
    run_started: list[Phase6CampaignRunEvent] = []
    terminal: list[Phase6CampaignRunEvent] = []
    for index, workflow in enumerate((Workflow.ONE_SHOT, Workflow.STAGED)):
        run_id = f"run-{workflow.value}"
        run_started.append(
            Phase6CampaignRunEvent(
                schema_version="1.2",
                sequence=1 + index * 2,
                event_type="run_state",
                run_id=run_id,
                task_id="task",
                workflow=workflow,
                repetition_index=0,
                status=CampaignRunStatus.STARTED,
                outcome=None,
                stop_reason=None,
                provider_call_count=None,
                gate_executed=False,
                counted_failure=False,
                fail_fast_applies=False,
                max_failures_applies=False,
                failure_kind=None,
                occurred_at=T0,
            )
        )
        terminal.append(
            Phase6CampaignRunEvent(
                schema_version="1.2",
                sequence=2 + index * 2,
                event_type="run_state",
                run_id=run_id,
                task_id="task",
                workflow=workflow,
                repetition_index=0,
                status=CampaignRunStatus.COMPLETED,
                outcome=Phase6CampaignOutcome.SUCCESS,
                stop_reason=None,
                provider_call_count=1,
                gate_executed=True,
                counted_failure=False,
                fail_fast_applies=False,
                max_failures_applies=False,
                failure_kind=Phase6FailureKind.NONE,
                occurred_at=T0,
            )
        )
    finished = Phase6CampaignFinishedEvent(
        schema_version="1.2",
        sequence=5,
        event_type="campaign_finished",
        experiment_id="phase6-python",
        stop_reason=CampaignStopReason.NONE,
        attempted_run_count=2,
        provider_call_count=2,
        provider_call_count_unknown_runs=0,
        counted_failure_count=0,
        retry_count=0,
        occurred_at=T1,
    )
    return LoadedPhase6Campaign(
        (
            started,
            run_started[0],
            terminal[0],
            run_started[1],
            terminal[1],
            finished,
        )
    )


def _campaign_jsonl(events: tuple[Any, ...]) -> bytes:
    lines: list[bytes] = []
    for event in events:
        payload = event.model_dump(mode="json")
        payload["occurred_at"] = event.occurred_at.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        lines.append(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    return b"".join(lines)


def _successful_codex(
    *,
    model: str,
    reasoning_effort: Any,
    prompt_bytes: int,
) -> CodexExecutionEvidence:
    timestamp = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    return CodexExecutionEvidence(
        schema_version="1.5",
        provider=Provider.CODEX,
        cli_version=next(iter(CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS)),
        cli_profile=CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2,
        execution_stage=CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        failure_stage=None,
        runner_state=CodexRunnerState.STARTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        preflight_checked_at=timestamp,
        verified_flags=sorted(CODEX_REQUIRED_EXEC_FLAGS),
        requested_model=model,
        requested_reasoning_effort=reasoning_effort,
        sandbox_mode="workspace-write",
        approval_policy="never",
        approval_basis=CodexApprovalBasis.EXPLICIT_CONFIG_NEVER,
        web_search_disabled=True,
        command_network_disabled=True,
        raw_stream_persisted=False,
        process_started=True,
        stdin_write_state=CodexStdinWriteState.COMPLETE,
        stdin_bytes_written=prompt_bytes,
        stdin_bytes_total=prompt_bytes,
        provider_failure_hint=CodexProviderFailureHint.NOT_APPLICABLE,
        status=ProviderExecutionStatus.SUCCEEDED,
        failure_kind=LiveFailureKind.NONE,
        exit_code=0,
        started_at=timestamp,
        completed_at=timestamp,
        duration_ms=10,
        event_count=3,
        unknown_event_count=0,
        thread_started_count=1,
        turn_started_count=1,
        terminal_event=CodexTerminalEvent.TURN_COMPLETED,
        turn_completed_count=1,
        turn_failed_count=0,
        error_event_count=0,
        item_type_counts={},
        usage_metrics=UsageMetrics(source=UsageMetricSource.NOT_AVAILABLE),
        stdout_bytes=1,
        stderr_bytes=0,
        stdout_limit_exceeded=False,
        stderr_truncated=False,
        termination=TerminationEvidence(
            reason=TerminationReason.NONE,
            sigterm_sent=False,
            sigkill_sent=False,
            process_group_cleared=True,
            error=None,
        ),
    )


def _passing_metrics() -> RunMetrics:
    return RunMetrics(
        quality_gate_pass=True,
        acceptance_tests_passed=1,
        acceptance_tests_total=1,
        regression_failures=0,
        lint_errors=0,
        typecheck_errors=0,
        agent_duration_ms=10,
        evaluation_duration_ms=0,
        total_duration_ms=10,
        agent_call_count=1,
        retry_count=0,
        changed_files=[],
        added_lines=0,
        deleted_lines=0,
        usage_metrics=UsageMetrics(source=UsageMetricSource.NOT_AVAILABLE),
    )


def _passing_gate_commands() -> list[CommandEvidence]:
    timestamp = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    termination = TerminationEvidence(
        reason=TerminationReason.NONE,
        sigterm_sent=False,
        sigkill_sent=False,
        process_group_cleared=True,
        error=None,
    )
    return [
        CommandEvidence(
            gate=gate,
            command_index=0,
            argv=["gate"],
            status=CommandStatus.PASSED,
            return_code=0,
            started_at=timestamp,
            completed_at=timestamp,
            duration_ms=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_decode_replaced=False,
            stderr_decode_replaced=False,
            termination=termination,
            error=None,
        )
        for gate in (
            GateKind.ACCEPTANCE,
            GateKind.REGRESSION,
            GateKind.LINT,
            GateKind.TYPECHECK,
        )
    ]


def _public_output_record_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "reviewed_commit": COMMIT,
        "experiment_id": "phase6-python",
        "run_id": "phase6-python-one-shot-000",
        "task_id": "task",
        "language": "python",
        "provider": "codex",
        "workflow": "one_shot",
        "repetition_index": 0,
        "exact_model_id": "gpt-fixed-model",
        "reasoning_effort": "high",
        "cli_profile": "headless_exec_explicit_never_v2",
        "cli_version": "codex-cli fixed",
        "os": "darwin",
        "architecture": "arm64",
        "toolchain_fingerprint": HASH_A,
        "fixture_sha256": HASH_A,
        "prompt_sha256": HASH_A,
        "plan_sha256": HASH_A,
        "campaign_sha256": HASH_A,
        "evidence_sha256": HASH_A,
        "recording_sha256": HASH_A,
        "overall_status": "rejected",
        "failure_kind": "output_contract_violation",
        "provider_call_count": 1,
        "gate_executed": False,
        "gate_not_executed_reason": "output_contract_violation",
        "run_metrics_available": False,
        "acceptance_passed": 0,
        "acceptance_total": 0,
        "regression_failures": 0,
        "lint_errors": 0,
        "typecheck_errors": 0,
        "usage_status": "missing",
        "usage_source": None,
        "started_at": T0,
        "completed_at": T1,
    }


def _minimal_plan(plan: WorkflowPlanV1_2) -> WorkflowPlanV1_2:
    raw = plan.model_dump(mode="json")
    raw["runs"] = raw["runs"][:2]
    raw["planned_run_count"] = 2
    raw["planned_provider_call_count"] = 2
    return WorkflowPlanV1_2.model_validate(raw)


def _successful_live_pair(
    *,
    spec: WorkflowExperimentSpecV2_1,
    spec_sha256: str,
    plan: WorkflowPlanV1_2,
    plan_sha256: str,
    run: Any,
) -> tuple[LiveRunArtifactV1_2, Phase6Recording, str]:
    prompt_sha256 = (
        plan.one_shot_prompt_sha256
        if run.workflow is Workflow.ONE_SHOT
        else plan.staged_prompt_sha256
    )
    prompt_bytes = (
        plan.one_shot_prompt_bytes
        if run.workflow is Workflow.ONE_SHOT
        else plan.staged_prompt_bytes
    )
    codex = _successful_codex(
        model=plan.model,
        reasoning_effort=plan.reasoning_effort,
        prompt_bytes=prompt_bytes,
    )
    metrics = _passing_metrics()
    started = Phase6RecordingStartedEvent(
        schema_version="1.2",
        sequence=0,
        event_type="run_started",
        run_id=run.run_id,
        experiment_id=plan.experiment_id,
        task_id=run.task_id,
        language=plan.language,
        workflow=run.workflow,
        provider=Provider.CODEX,
        repetition_index=run.repetition_index,
        execution_mode="live",
        occurred_at=T0,
        plan_sha256=plan_sha256,
        fixture_sha256=plan.fixture_sha256,
        fixture_manifest_sha256=plan.fixture_manifest_sha256,
        fixture_acceptance_sha256=plan.fixture_acceptance_sha256,
        diff_policy_sha256=plan.diff_policy_sha256,
        prompt_sha256=prompt_sha256,
        prompt_bytes=prompt_bytes,
        prompt_redacted=True,
        requested_model=plan.model,
        requested_reasoning_effort=plan.reasoning_effort,
        cli_version=codex.cli_version,
    )
    terminal = Phase6RecordingTerminalEvent(
        schema_version="1.2",
        sequence=1,
        event_type="run_completed",
        run_id=run.run_id,
        experiment_id=plan.experiment_id,
        occurred_at=T0,
        overall_status="passed",
        failure_kind="none",
        codex=codex,
        gate_executed=True,
        gate_not_executed_reason=None,
        metrics=metrics,
    )
    recording = Phase6Recording(started, terminal)
    recording_sha256 = hashlib.sha256(
        f"recording:{run.run_id}".encode()
    ).hexdigest()
    artifact = LiveRunArtifactV1_2(
        schema_version="1.2",
        run_id=run.run_id,
        experiment_id=plan.experiment_id,
        task_id=run.task_id,
        language=plan.language,
        repetition_index=run.repetition_index,
        workflow=run.workflow,
        provider=Provider.CODEX,
        execution_mode="live",
        overall_status="passed",
        failure_kind="none",
        started_at=T0,
        completed_at=T0,
        reviewed_commit=plan.reviewed_commit,
        spec_sha256=spec_sha256,
        plan_sha256=plan_sha256,
        fixture_sha256=plan.fixture_sha256,
        fixture_manifest_sha256=plan.fixture_manifest_sha256,
        fixture_acceptance_sha256=plan.fixture_acceptance_sha256,
        diff_policy_sha256=plan.diff_policy_sha256,
        toolchain_fingerprint=plan.toolchain_fingerprint,
        prompt_sha256=prompt_sha256,
        prompt_bytes=prompt_bytes,
        prompt_redacted=True,
        runner=spec.runner,
        codex=codex,
        gate_executed=True,
        gate_not_executed_reason=None,
        gate_commands=_passing_gate_commands(),
        diff={
            "changed_files": [],
            "binary_files": [],
            "added_lines": 0,
            "deleted_lines": 0,
            "unified_diff": "",
            "diff_truncated": False,
            "line_counts_complete": True,
            "collection_error": None,
        },
        metrics=metrics,
        workspace_lifecycle=WorkspaceLifecycle.REMOVED,
        recording_sha256=recording_sha256,
        raw_provider_output_persisted=False,
    )
    return artifact, recording, recording_sha256


def _cross_artifact_case(
    tmp_path: Path,
) -> tuple[
    PrimarySuiteSource,
    WorkflowExperimentSpecV2_1,
    WorkflowPlanV1_2,
    LoadedPhase6Campaign,
    list[LiveRunArtifactV1_2],
    list[Phase6Recording],
]:
    fixture_manifest, policy, acceptance = _fixture_contracts()
    spec_path, spec = _phase6_spec(tmp_path)
    plan = _minimal_plan(
        _phase6_plan(
            tmp_path,
            spec_path,
            spec,
            fixture_manifest,
            policy,
            acceptance,
        )
    )
    plan_sha256 = hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
    events: list[Any] = [
        Phase6CampaignStartedEvent(
            schema_version="1.2",
            sequence=0,
            event_type="campaign_started",
            experiment_id=plan.experiment_id,
            plan_sha256=plan_sha256,
            fixture_manifest_sha256=plan.fixture_manifest_sha256,
            fixture_acceptance_sha256=plan.fixture_acceptance_sha256,
            diff_policy_sha256=plan.diff_policy_sha256,
            toolchain_fingerprint=plan.toolchain_fingerprint,
            planned_run_count=2,
            planned_provider_call_count=2,
            occurred_at=T0,
        )
    ]
    artifacts: list[LiveRunArtifactV1_2] = []
    recordings: list[Phase6Recording] = []
    recording_hashes: list[str] = []
    for index, run in enumerate(plan.runs):
        events.extend(
            (
                Phase6CampaignRunEvent(
                    schema_version="1.2",
                    sequence=1 + index * 2,
                    event_type="run_state",
                    run_id=run.run_id,
                    task_id=run.task_id,
                    workflow=run.workflow,
                    repetition_index=run.repetition_index,
                    status=CampaignRunStatus.STARTED,
                    outcome=None,
                    stop_reason=None,
                    provider_call_count=None,
                    gate_executed=False,
                    counted_failure=False,
                    fail_fast_applies=False,
                    max_failures_applies=False,
                    failure_kind=None,
                    occurred_at=T0,
                ),
                Phase6CampaignRunEvent(
                    schema_version="1.2",
                    sequence=2 + index * 2,
                    event_type="run_state",
                    run_id=run.run_id,
                    task_id=run.task_id,
                    workflow=run.workflow,
                    repetition_index=run.repetition_index,
                    status=CampaignRunStatus.COMPLETED,
                    outcome=Phase6CampaignOutcome.SUCCESS,
                    stop_reason=None,
                    provider_call_count=1,
                    gate_executed=True,
                    counted_failure=False,
                    fail_fast_applies=False,
                    max_failures_applies=False,
                    failure_kind=Phase6FailureKind.NONE,
                    occurred_at=T0,
                ),
            )
        )
        artifact, recording, recording_hash = _successful_live_pair(
            spec=spec,
            spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            plan=plan,
            plan_sha256=plan_sha256,
            run=run,
        )
        artifacts.append(artifact)
        recordings.append(recording)
        recording_hashes.append(recording_hash)
    events.append(
        Phase6CampaignFinishedEvent(
            schema_version="1.2",
            sequence=5,
            event_type="campaign_finished",
            experiment_id=plan.experiment_id,
            stop_reason=CampaignStopReason.NONE,
            attempted_run_count=2,
            provider_call_count=2,
            provider_call_count_unknown_runs=0,
            counted_failure_count=0,
            retry_count=0,
            occurred_at=T1,
        )
    )
    campaign = LoadedPhase6Campaign(tuple(events))
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.EVALUATED,
        spec=ArtifactReference(
            role="spec",
            path="workflow.yaml",
            sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        ),
        fixture_manifest=ArtifactReference(
            role="fixture_manifest",
            path="fixture.manifest.json",
            sha256=plan.fixture_manifest_sha256,
        ),
        fixture_acceptance=ArtifactReference(
            role="fixture_acceptance",
            path="fixture.acceptance.json",
            sha256=plan.fixture_acceptance_sha256,
        ),
        diff_policy=ArtifactReference(
            role="diff_policy",
            path="diff-policy.json",
            sha256=plan.diff_policy_sha256,
        ),
        plan=ArtifactReference(
            role="plan",
            path="plan.json",
            sha256=plan_sha256,
        ),
        campaign=ArtifactReference(
            role="campaign",
            path="campaign.jsonl",
            sha256=HASH_A,
        ),
        evidence=[
            ArtifactReference(
                role="evidence",
                path=f"evidence/{artifact.run_id}.json",
                sha256=hashlib.sha256(
                    canonical_json_bytes(artifact)
                ).hexdigest(),
            )
            for artifact in artifacts
        ],
        recordings=[
            ArtifactReference(
                role="recording",
                path=f"recordings/{recording.started.run_id}.jsonl",
                sha256=recording_hash,
            )
            for recording, recording_hash in zip(
                recordings,
                recording_hashes,
                strict=True,
            )
        ],
    )
    return source, spec, plan, campaign, artifacts, recordings


def test_compatible_loaders_keep_spec_2_0_and_plan_1_1(
    tmp_path: Path,
) -> None:
    old_spec_path = Path("experiments/examples/workflow-ab.yaml")
    old_loaded = load_workflow_spec_contract(old_spec_path)
    assert isinstance(old_loaded.spec, WorkflowExperimentSpec)
    assert not isinstance(old_loaded.spec, WorkflowExperimentSpecV2_1)

    plan = build_workflow_plan(old_spec_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(workflow_plan_bytes(plan))
    loaded_plan = load_workflow_plan_contract(plan_path)
    assert isinstance(loaded_plan, WorkflowPlan)
    assert not isinstance(loaded_plan, WorkflowPlanV1_2)

    noncanonical_path = tmp_path / "plan-noncanonical.json"
    noncanonical_path.write_text(
        json.dumps(plan.model_dump(mode="json")),
        encoding="utf-8",
    )
    assert isinstance(
        load_workflow_plan_contract(noncanonical_path),
        WorkflowPlan,
    )


def test_plan_1_2_binds_acceptance_policy_toolchain_and_commit(
    tmp_path: Path,
) -> None:
    manifest, policy, acceptance = _fixture_contracts()
    spec_path, spec = _phase6_spec(tmp_path)
    plan = _phase6_plan(
        tmp_path,
        spec_path,
        spec,
        manifest,
        policy,
        acceptance,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_bytes(plan))

    loaded_spec = load_workflow_spec_contract(spec_path)
    loaded_plan = load_workflow_plan_contract(plan_path)
    assert isinstance(loaded_plan, WorkflowPlanV1_2)
    validate_plan_bindings(
        loaded_spec=loaded_spec,
        plan=loaded_plan,
        fixture_manifest_bytes=canonical_json_bytes(manifest),
        fixture_manifest=manifest,
        fixture_acceptance_bytes=canonical_json_bytes(acceptance),
        fixture_acceptance=acceptance,
        diff_policy_bytes=canonical_json_bytes(policy),
        diff_policy=policy,
    )

    changed = loaded_plan.model_copy(update={"reviewed_commit": "2" * 40})
    with pytest.raises(Phase6ContractError, match="bindings"):
        validate_plan_bindings(
            loaded_spec=loaded_spec,
            plan=changed,
            fixture_manifest_bytes=canonical_json_bytes(manifest),
            fixture_manifest=manifest,
            fixture_acceptance_bytes=canonical_json_bytes(acceptance),
            fixture_acceptance=acceptance,
            diff_policy_bytes=canonical_json_bytes(policy),
            diff_policy=policy,
        )


def test_phase6_canonical_loader_rejects_noncanonical_json(
    tmp_path: Path,
) -> None:
    manifest, _policy, _acceptance = _fixture_contracts()
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_json_bytes(manifest))
    assert load_fixture_manifest(path) == manifest

    path.write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    with pytest.raises(Phase6ContractError, match="canonical"):
        load_fixture_manifest(path)


def test_input_changed_is_zero_call_zero_gate_safety_stop() -> None:
    event = Phase6CampaignRunEvent(
        schema_version="1.2",
        sequence=1,
        event_type="run_state",
        run_id="run-one",
        task_id="task",
        workflow=Workflow.ONE_SHOT,
        repetition_index=0,
        status=CampaignRunStatus.NOT_RUN,
        outcome=Phase6CampaignOutcome.STOP_CONDITION,
        stop_reason=CampaignStopReason.INPUT_CHANGED,
        provider_call_count=0,
        gate_executed=False,
        counted_failure=False,
        fail_fast_applies=False,
        max_failures_applies=False,
        failure_kind=None,
        occurred_at=T0,
    )
    assert event.stop_reason is CampaignStopReason.INPUT_CHANGED

    payload = event.model_dump(mode="json")
    payload["occurred_at"] = T0
    with pytest.raises(ValidationError, match="Harness safety stop"):
        Phase6CampaignRunEvent.model_validate(
            {
                **payload,
                "provider_call_count": 1,
                "counted_failure": True,
            }
        )


def test_output_contract_violation_has_separate_campaign_transition() -> None:
    event = Phase6CampaignRunEvent(
        schema_version="1.2",
        sequence=1,
        event_type="run_state",
        run_id="run-one",
        task_id="task",
        workflow=Workflow.ONE_SHOT,
        repetition_index=0,
        status=CampaignRunStatus.FAILED,
        outcome=Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION,
        stop_reason=None,
        provider_call_count=1,
        gate_executed=False,
        counted_failure=True,
        fail_fast_applies=True,
        max_failures_applies=True,
        failure_kind=Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION,
        occurred_at=T0,
    )
    assert event.gate_executed is False

    payload = event.model_dump(mode="json")
    payload["occurred_at"] = T0
    with pytest.raises(ValidationError, match="one Provider call"):
        Phase6CampaignRunEvent.model_validate(
            {**payload, "gate_executed": True}
        )


def test_cleanup_failure_has_priority_over_other_failure_kinds() -> None:
    with pytest.raises(ValidationError, match="priority"):
        Phase6CampaignRunEvent(
            schema_version="1.2",
            sequence=1,
            event_type="run_state",
            run_id="run-one",
            task_id="task",
            workflow=Workflow.ONE_SHOT,
            repetition_index=0,
            status=CampaignRunStatus.FAILED,
            outcome=Phase6CampaignOutcome.CLEANUP_FAILURE,
            stop_reason=None,
            provider_call_count=1,
            gate_executed=False,
            counted_failure=False,
            fail_fast_applies=False,
            max_failures_applies=False,
            failure_kind=Phase6FailureKind.EVIDENCE_ERROR,
            occurred_at=T0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_commit", "2" * 40),
        ("spec_sha256", HASH_B),
    ],
)
def test_cross_validator_rejects_artifact_provenance_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    source, spec, plan, campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    changed = list(artifacts)
    changed[0] = changed[0].model_copy(update={field: value})

    with pytest.raises(Phase6ContractError, match="identities differ"):
        _validate_primary_live_bindings(
            source=source,
            spec=spec,
            plan=plan,
            campaign=campaign,
            evidence=changed,
            recordings=recordings,
        )


def test_cross_validator_rejects_campaign_artifact_outcome_mismatch(
    tmp_path: Path,
) -> None:
    source, spec, plan, campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    events = list(campaign.events)
    terminal = events[2]
    assert isinstance(terminal, Phase6CampaignRunEvent)
    events[2] = terminal.model_copy(
        update={
            "outcome": Phase6CampaignOutcome.QUALITY_GATE_FAILURE,
            "counted_failure": True,
            "fail_fast_applies": True,
            "max_failures_applies": True,
            "failure_kind": Phase6FailureKind.QUALITY_GATE_FAILURE,
        }
    )

    with pytest.raises(Phase6ContractError, match="identities differ"):
        _validate_primary_live_bindings(
            source=source,
            spec=spec,
            plan=plan,
            campaign=LoadedPhase6Campaign(tuple(events)),
            evidence=artifacts,
            recordings=recordings,
        )


def test_cross_validator_requires_exact_plan_campaign_run_ids(
    tmp_path: Path,
) -> None:
    source, spec, plan, campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    events = list(campaign.events)
    terminal = events[2]
    assert isinstance(terminal, Phase6CampaignRunEvent)
    events[2] = terminal.model_copy(update={"run_id": "unexpected-run"})

    with pytest.raises(Phase6ContractError, match="run IDs"):
        _validate_primary_live_bindings(
            source=source,
            spec=spec,
            plan=plan,
            campaign=LoadedPhase6Campaign(tuple(events)),
            evidence=artifacts,
            recordings=recordings,
        )


@pytest.mark.parametrize("case", ["missing", "duplicate"])
def test_cross_validator_derives_required_artifact_set_from_campaign(
    tmp_path: Path,
    case: str,
) -> None:
    source, spec, plan, campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    if case == "missing":
        artifacts = artifacts[:-1]
        recordings = recordings[:-1]
    else:
        artifacts = [*artifacts, artifacts[0]]
        recordings = [*recordings, recordings[0]]

    with pytest.raises(Phase6ContractError, match="incomplete or duplicated"):
        _validate_primary_live_bindings(
            source=source,
            spec=spec,
            plan=plan,
            campaign=campaign,
            evidence=artifacts,
            recordings=recordings,
        )


@pytest.mark.parametrize("mismatch", ["metrics", "codex"])
def test_cross_validator_rejects_recording_artifact_evidence_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    source, spec, plan, campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    changed = list(recordings)
    terminal = changed[0].terminal
    if mismatch == "metrics":
        assert terminal.metrics is not None
        metrics = terminal.metrics.model_copy(
            update={
                "evaluation_duration_ms": 1,
                "total_duration_ms": 11,
            }
        )
        terminal = terminal.model_copy(update={"metrics": metrics})
    else:
        codex = terminal.codex.model_copy(update={"stdout_bytes": 2})
        terminal = terminal.model_copy(update={"codex": codex})
    changed[0] = Phase6Recording(changed[0].started, terminal)

    with pytest.raises(Phase6ContractError, match="identities differ"):
        _validate_primary_live_bindings(
            source=source,
            spec=spec,
            plan=plan,
            campaign=campaign,
            evidence=artifacts,
            recordings=changed,
        )


def test_campaign_rejects_terminal_before_started(tmp_path: Path) -> None:
    campaign = _completed_campaign()
    original = campaign.events
    reordered = (
        original[0],
        original[2].model_copy(update={"sequence": 1}),
        original[1].model_copy(update={"sequence": 2}),
        original[3],
        original[4],
        original[5],
    )
    path = tmp_path / "campaign.jsonl"
    path.write_bytes(_campaign_jsonl(reordered))

    with pytest.raises(Phase6ContractError, match="must precede"):
        load_campaign_contract(path)


def test_campaign_rejects_started_terminal_identity_mismatch(
    tmp_path: Path,
) -> None:
    campaign = _completed_campaign()
    events = list(campaign.events)
    events[1] = events[1].model_copy(update={"task_id": "other-task"})
    path = tmp_path / "campaign.jsonl"
    path.write_bytes(_campaign_jsonl(tuple(events)))

    with pytest.raises(Phase6ContractError, match="identities differ"):
        load_campaign_contract(path)


def test_campaign_rejects_decreasing_timestamps(tmp_path: Path) -> None:
    campaign = _completed_campaign()
    events = list(campaign.events)
    events[2] = events[2].model_copy(
        update={"occurred_at": datetime.fromisoformat(T1.replace("Z", "+00:00"))}
    )
    path = tmp_path / "campaign.jsonl"
    path.write_bytes(_campaign_jsonl(tuple(events)))

    with pytest.raises(Phase6ContractError, match="non-decreasing"):
        load_campaign_contract(path)


def test_campaign_finished_reason_must_match_not_run_reason(
    tmp_path: Path,
) -> None:
    campaign = _completed_campaign()
    first_started = campaign.events[1]
    first_terminal = campaign.events[2]
    second_terminal = campaign.events[4]
    assert isinstance(first_started, Phase6CampaignRunEvent)
    assert isinstance(first_terminal, Phase6CampaignRunEvent)
    assert isinstance(second_terminal, Phase6CampaignRunEvent)
    not_run = Phase6CampaignRunEvent(
        schema_version="1.2",
        sequence=3,
        event_type="run_state",
        run_id=second_terminal.run_id,
        task_id=second_terminal.task_id,
        workflow=second_terminal.workflow,
        repetition_index=second_terminal.repetition_index,
        status=CampaignRunStatus.NOT_RUN,
        outcome=Phase6CampaignOutcome.STOP_CONDITION,
        stop_reason=CampaignStopReason.MAX_FAILURES,
        provider_call_count=0,
        gate_executed=False,
        counted_failure=False,
        fail_fast_applies=False,
        max_failures_applies=False,
        failure_kind=None,
        occurred_at=T0,
    )
    finished = Phase6CampaignFinishedEvent(
        schema_version="1.2",
        sequence=4,
        event_type="campaign_finished",
        experiment_id=campaign.started.experiment_id,
        stop_reason=CampaignStopReason.FAIL_FAST,
        attempted_run_count=1,
        provider_call_count=1,
        provider_call_count_unknown_runs=0,
        counted_failure_count=0,
        retry_count=0,
        occurred_at=T1,
    )
    events = (
        campaign.events[0],
        first_started,
        first_terminal,
        not_run,
        finished,
    )
    path = tmp_path / "campaign.jsonl"
    path.write_bytes(_campaign_jsonl(events))

    with pytest.raises(Phase6ContractError, match="stop reason differs"):
        load_campaign_contract(path)


def test_typescript_fixture_requires_node_and_compiler_components() -> None:
    python_toolchain = _toolchain()
    with pytest.raises(ValidationError, match="exact toolchain roles"):
        FixtureManifest(
            schema_version="1.0",
            language=Language.TYPESCRIPT,
            fixture_revision="typescript-task-v1",
            fixture_sha256=HASH_A,
            gate_contract_sha256=HASH_B,
            toolchain=python_toolchain,
        )


def test_live_artifact_and_recording_reject_input_changed(
    tmp_path: Path,
) -> None:
    _source, _spec, _plan, _campaign, artifacts, recordings = (
        _cross_artifact_case(tmp_path)
    )
    artifact_payload = artifacts[0].model_dump(mode="json")
    artifact_payload.update(
        {
            "overall_status": "rejected",
            "failure_kind": "output_contract_violation",
            "started_at": T0,
            "completed_at": T0,
            "gate_executed": False,
            "gate_not_executed_reason": "input_changed",
            "gate_commands": [],
            "metrics": None,
        }
    )
    with pytest.raises(ValidationError, match="must not create"):
        LiveRunArtifactV1_2.model_validate(artifact_payload)

    terminal_payload = recordings[0].terminal.model_dump(mode="json")
    terminal_payload.update(
        {
            "event_type": "run_failed",
            "occurred_at": T0,
            "overall_status": "rejected",
            "failure_kind": "output_contract_violation",
            "gate_executed": False,
            "gate_not_executed_reason": "input_changed",
            "metrics": None,
        }
    )
    with pytest.raises(ValidationError, match="must not create"):
        Phase6RecordingTerminalEvent.model_validate(terminal_payload)


def test_language_status_is_derived_without_public_run_record() -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.EVALUATED,
        blocker=None,
        spec=ArtifactReference(role="spec", path="spec.yaml", sha256=HASH_A),
        fixture_manifest=ArtifactReference(
            role="fixture_manifest",
            path="manifest.json",
            sha256=HASH_A,
        ),
        fixture_acceptance=ArtifactReference(
            role="fixture_acceptance",
            path="acceptance.json",
            sha256=HASH_A,
        ),
        diff_policy=ArtifactReference(
            role="diff_policy",
            path="policy.json",
            sha256=HASH_A,
        ),
        plan=ArtifactReference(role="plan", path="plan.json", sha256=HASH_A),
        campaign=ArtifactReference(
            role="campaign",
            path="campaign.jsonl",
            sha256=HASH_A,
        ),
        evidence=[
                ArtifactReference(
                    role="evidence",
                    path="evidence/one.json",
                    sha256=HASH_A,
                ),
                ArtifactReference(
                    role="evidence",
                    path="evidence/two.json",
                    sha256=HASH_A,
                ),
        ],
        recordings=[
                ArtifactReference(
                    role="recording",
                    path="recordings/one.jsonl",
                    sha256=HASH_A,
                )
        ],
    )
    campaign = _completed_campaign()
    evidence_ids = {"run-one_shot", "run-staged"}
    assert (
        derive_language_status(
            source,
            campaign=campaign,
            evidence_run_ids=evidence_ids,
        )
        is LanguageStatus.EVALUATED
    )

    changed = source.model_copy(
        update={"expected_language_status": LanguageStatus.READY_NOT_RUN}
    )
    with pytest.raises(Phase6ContractError, match="differs from derived"):
        validate_expected_language_status(
            changed,
            campaign=campaign,
            evidence_run_ids=evidence_ids,
        )


def test_data_cutoff_is_recomputed_from_terminal_and_verification() -> None:
    _manifest, _policy, acceptance = _fixture_contracts()
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
    )
    suite = _suite_manifest(source, cutoff=T1)
    assert validate_data_cutoff(
        suite,
        fixture_acceptances=[acceptance],
        campaigns=[_completed_campaign()],
        historical_verifications=[],
    ) == datetime(2026, 7, 30, 2, 3, 4, tzinfo=UTC)

    wrong = suite.model_copy(
        update={"data_cutoff_at": datetime(2026, 7, 30, 3, tzinfo=UTC)}
    )
    with pytest.raises(Phase6ContractError, match="differs"):
        validate_data_cutoff(
            wrong,
            fixture_acceptances=[acceptance],
            campaigns=[_completed_campaign()],
            historical_verifications=[],
        )


def test_data_cutoff_requires_canonical_rfc3339() -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
    )
    raw = _suite_manifest(source).model_dump(mode="json")
    raw["data_cutoff_at"] = "2026-07-30T01:02:03Z"
    with pytest.raises(ValidationError, match="canonical UTC RFC 3339"):
        PublicSuiteManifest.model_validate(raw)


def test_manifest_loader_reads_only_listed_safe_regular_files(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    unlisted = tmp_path / "unlisted-secret.txt"
    unlisted.write_text("must not be read\n", encoding="utf-8")
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
        spec=ArtifactReference(
            role="spec",
            path="input.json",
            sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        ),
    )
    suite = _suite_manifest(source)
    manifest_path = tmp_path / "suite.json"
    manifest_path.write_bytes(canonical_json_bytes(suite))

    loaded = load_public_suite_inputs(manifest_path, root=tmp_path)
    assert set(loaded.paths) == {"input.json"}
    assert "unlisted-secret.txt" not in loaded.paths


@pytest.mark.skipif(os.name != "posix", reason="link safety is POSIX-specific")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_manifest_loader_rejects_links(
    tmp_path: Path,
    link_kind: str,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    listed = tmp_path / "listed.json"
    if link_kind == "symlink":
        listed.symlink_to(target)
    else:
        os.link(target, listed)
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
        spec=ArtifactReference(
            role="spec",
            path="listed.json",
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        ),
    )
    manifest_path = tmp_path / "suite.json"
    manifest_path.write_bytes(canonical_json_bytes(_suite_manifest(source)))

    with pytest.raises(Phase6PathError, match=link_kind):
        load_public_suite_inputs(manifest_path, root=tmp_path)


@pytest.mark.parametrize("unsafe", ["/tmp/input.json", "../input.json", "a/../b"])
def test_manifest_artifact_path_rejects_unbounded_values(unsafe: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactReference(role="spec", path=unsafe, sha256=HASH_A)


def test_manifest_rejects_normalized_duplicate_inputs() -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
        spec=ArtifactReference(role="spec", path="same.json", sha256=HASH_A),
        plan=ArtifactReference(role="plan", path="same.json", sha256=HASH_A),
    )
    with pytest.raises(ValidationError, match="unique"):
        _suite_manifest(source)


def test_checksum_contract_covers_release_metadata_and_uses_external_anchor() -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
    )
    manifest = _suite_manifest(source)
    checksums = PublicChecksums(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        entries=[
            ChecksumEntry(
                path="release-metadata.json",
                size_bytes=10,
                sha256=HASH_A,
            ),
            ChecksumEntry(path="report.json", size_bytes=20, sha256=HASH_B),
        ],
        excluded_paths=["checksums.json"],
        authenticity_claimed=False,
    )
    release = ReleaseMetadata(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        renderer_version=manifest.renderer_version,
        data_cutoff_at=T0,
        checksum_manifest_path="checksums.json",
        checksum_digest_anchored_externally=True,
        authenticity_claimed=False,
    )
    checksum_bytes = canonical_json_bytes(checksums)
    anchor = ExternalChecksumAnchor(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        checksum_manifest_path="checksums.json",
        checksum_manifest_sha256=hashlib.sha256(checksum_bytes).hexdigest(),
        authenticity_claimed=False,
    )
    validate_checksum_contract(
        manifest=manifest,
        checksums=checksums,
        release_metadata=release,
        external_anchor=anchor,
        checksum_bytes=checksum_bytes,
    )

    with pytest.raises(Phase6ContractError, match="anchor"):
        validate_checksum_contract(
            manifest=manifest,
            checksums=checksums,
            release_metadata=release,
            external_anchor=None,
            checksum_bytes=checksum_bytes,
        )


def test_public_language_report_retains_input_changed_reason() -> None:
    report = PublicLanguageReport(
        schema_version="1.0",
        language=Language.PYTHON,
        status=LanguageStatus.BLOCKED,
        scheduled_runs=2,
        attempted_runs=0,
        completed_runs=0,
        failed_runs=0,
        interrupted_runs=0,
        not_run_runs=2,
        output_rejected_runs=0,
        gate_not_executed_runs=2,
        gate_not_executed_reason={
            GateNotExecutedReason.INPUT_CHANGED: 2,
        },
        scheduled_pair_count=1,
        complete_pair_count=0,
        estimability="not_estimable",
    )
    assert (
        report.gate_not_executed_reason[GateNotExecutedReason.INPUT_CHANGED]
        == 2
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"acceptance_passed": 1, "acceptance_total": 0},
        {
            "overall_status": "passed",
            "failure_kind": "none",
            "gate_not_executed_reason": "provider_failure",
        },
        {"run_metrics_available": True},
    ],
)
def test_public_run_record_rejects_contradictory_state(
    changes: dict[str, Any],
) -> None:
    payload = _public_output_record_payload()
    payload.update(changes)

    with pytest.raises(ValidationError):
        PublicRunRecord.model_validate(payload)


def test_nested_codex_evidence_1_5_is_required_without_mutating_it() -> None:
    codex = CodexExecutionEvidence.model_construct(
        schema_version="1.4",
        status=ProviderExecutionStatus.SUCCEEDED,
    )
    with pytest.raises(ValidationError, match=r"1\.5"):
        LiveRunArtifactV1_2.model_validate(
            {
                "schema_version": "1.2",
                "run_id": "run",
                "experiment_id": "experiment",
                "task_id": "task",
                "language": "python",
                "repetition_index": 0,
                "workflow": "one_shot",
                "provider": "codex",
                "execution_mode": "live",
                "overall_status": "rejected",
                "failure_kind": "output_contract_violation",
                "started_at": T0,
                "completed_at": T1,
                "reviewed_commit": COMMIT,
                "spec_sha256": HASH_A,
                "plan_sha256": HASH_A,
                "fixture_sha256": HASH_A,
                "fixture_manifest_sha256": HASH_A,
                "fixture_acceptance_sha256": HASH_A,
                "diff_policy_sha256": HASH_A,
                "toolchain_fingerprint": HASH_A,
                "prompt_sha256": HASH_A,
                "prompt_bytes": 1,
                "prompt_redacted": True,
                "runner": {
                    "fixture_path": "fixture",
                    "command_timeout_ms": 1,
                    "termination_grace_ms": 1,
                    "max_output_bytes": 1,
                    "max_diff_bytes": 1,
                },
                "codex": codex,
                "gate_executed": False,
                "gate_not_executed_reason": "output_contract_violation",
                "gate_commands": [],
                "diff": {
                    "changed_files": [],
                    "binary_files": [],
                    "added_lines": 0,
                    "deleted_lines": 0,
                    "unified_diff": "",
                    "diff_truncated": False,
                    "line_counts_complete": True,
                    "collection_error": None,
                },
                "metrics": None,
                "workspace_lifecycle": "removed",
                "recording_sha256": HASH_A,
                "raw_provider_output_persisted": False,
            }
        )


def test_historical_record_forbids_toolchain_backfill() -> None:
    raw: dict[str, Any] = {
        "schema_version": "1.0",
        "source_class": "historical",
        "language": "python",
        "experiment_id": "workflow-ab-codex-live-002",
        "source_reviewed_commit": COMMIT,
        "verification_agentlab_commit": COMMIT,
        "toolchain_version_status": "unknown",
        "plan_sha256": HASH_A,
        "campaign_sha256": HASH_A,
        "report_json_sha256": HASH_A,
        "report_markdown_sha256": HASH_A,
        "strict_schema_validation_passed": True,
        "cross_artifact_validation_passed": True,
        "artifact_regenerated": False,
        "campaign_reexecuted": False,
        "validation_commands": [["shasum", "-a", "256", "report.json"]],
        "verified_at": T0,
    }
    assert (
        HistoricalVerificationRecord.model_validate(raw).toolchain_version_status
        == "unknown"
    )
    raw["toolchain_version_status"] = "measured-runtime 1.2.3"
    with pytest.raises(ValidationError):
        HistoricalVerificationRecord.model_validate(raw)


def test_all_public_json_contracts_have_canonical_strict_loaders(
    tmp_path: Path,
) -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
        spec=ArtifactReference(role="spec", path="spec.yaml", sha256=HASH_A),
    )
    manifest = _suite_manifest(source)
    language_report = PublicLanguageReport(
        schema_version="1.0",
        language=Language.PYTHON,
        status=LanguageStatus.BLOCKED,
        scheduled_runs=1,
        attempted_runs=1,
        completed_runs=0,
        failed_runs=1,
        interrupted_runs=0,
        not_run_runs=0,
        output_rejected_runs=1,
        gate_not_executed_runs=1,
        gate_not_executed_reason={
            GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION: 1,
        },
        scheduled_pair_count=1,
        complete_pair_count=0,
        estimability="not_estimable",
    )
    run_record = PublicRunRecord(
        schema_version="1.0",
        reviewed_commit=COMMIT,
        experiment_id="phase6-python",
        run_id="phase6-python-one-shot-000",
        task_id="task",
        language=Language.PYTHON,
        provider=Provider.CODEX,
        workflow=Workflow.ONE_SHOT,
        repetition_index=0,
        exact_model_id="gpt-fixed-model",
        reasoning_effort="high",
        cli_profile="headless_exec_explicit_never_v2",
        cli_version="codex-cli fixed",
        os="darwin",
        architecture="arm64",
        toolchain_fingerprint=HASH_A,
        fixture_sha256=HASH_A,
        prompt_sha256=HASH_A,
        plan_sha256=HASH_A,
        campaign_sha256=HASH_A,
        evidence_sha256=HASH_A,
        recording_sha256=HASH_A,
        overall_status="rejected",
        failure_kind="output_contract_violation",
        provider_call_count=1,
        gate_executed=False,
        gate_not_executed_reason="output_contract_violation",
        run_metrics_available=False,
        acceptance_passed=0,
        acceptance_total=0,
        regression_failures=0,
        lint_errors=0,
        typecheck_errors=0,
        usage_status="missing",
        usage_source=None,
        started_at=T0,
        completed_at=T1,
    )
    suite_report = PublicSuiteReport(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        renderer_version=manifest.renderer_version,
        generated_at=T0,
        data_cutoff_at=T0,
        languages=[language_report],
        provider_coverage=_coverage(),
        automatic_winner_selected=False,
        leaderboard_generated=False,
        statistical_significance_claimed=False,
    )
    checksums = PublicChecksums(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        entries=[
            ChecksumEntry(
                path="release-metadata.json",
                size_bytes=1,
                sha256=HASH_A,
            ),
            ChecksumEntry(path="report.json", size_bytes=1, sha256=HASH_A),
        ],
        excluded_paths=["checksums.json"],
        authenticity_claimed=False,
    )
    release = ReleaseMetadata(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        renderer_version=manifest.renderer_version,
        data_cutoff_at=T0,
        checksum_manifest_path="checksums.json",
        checksum_digest_anchored_externally=True,
        authenticity_claimed=False,
    )
    anchor = ExternalChecksumAnchor(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        checksum_manifest_path="checksums.json",
        checksum_manifest_sha256=HASH_A,
        authenticity_claimed=False,
    )
    cases = [
        ("manifest.json", manifest, load_public_suite_manifest),
        ("run.json", run_record, load_public_run_record),
        ("language.json", language_report, load_public_language_report),
        ("suite-report.json", suite_report, load_public_suite_report),
        ("checksums.json", checksums, load_public_checksums),
        ("release-metadata.json", release, load_release_metadata),
        ("external-anchor.json", anchor, load_external_checksum_anchor),
    ]
    for filename, model, loader in cases:
        path = tmp_path / filename
        path.write_bytes(canonical_json_bytes(model))
        assert loader(path) == model


def _write_ready_suite(
    tmp_path: Path,
    *,
    mismatched_spec_paths: bool = False,
) -> tuple[Path, Any]:
    fixture_manifest, policy, acceptance = _fixture_contracts()
    spec_path, spec = _phase6_spec(tmp_path)
    plan = _phase6_plan(
        tmp_path,
        spec_path,
        spec,
        fixture_manifest,
        policy,
        acceptance,
    )
    manifest_name = (
        "alternate-manifest.json"
        if mismatched_spec_paths
        else "fixture.manifest.json"
    )
    acceptance_name = (
        "alternate-acceptance.json"
        if mismatched_spec_paths
        else "fixture.acceptance.json"
    )
    paths_and_models = {
        manifest_name: fixture_manifest,
        acceptance_name: acceptance,
        "diff-policy.json": policy,
        "plan.json": plan,
    }
    for path_name, model in paths_and_models.items():
        (tmp_path / path_name).write_bytes(canonical_json_bytes(model))
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.READY_NOT_RUN,
        spec=ArtifactReference(
            role="spec",
            path=spec_path.name,
            sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        ),
        fixture_manifest=ArtifactReference(
            role="fixture_manifest",
            path=manifest_name,
            sha256=hashlib.sha256(
                canonical_json_bytes(fixture_manifest)
            ).hexdigest(),
        ),
        fixture_acceptance=ArtifactReference(
            role="fixture_acceptance",
            path=acceptance_name,
            sha256=hashlib.sha256(canonical_json_bytes(acceptance)).hexdigest(),
        ),
        diff_policy=ArtifactReference(
            role="diff_policy",
            path="diff-policy.json",
            sha256=hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
        ),
        plan=ArtifactReference(
            role="plan",
            path="plan.json",
            sha256=hashlib.sha256(canonical_json_bytes(plan)).hexdigest(),
        ),
    )
    suite_manifest = _suite_manifest(source)
    manifest_path = tmp_path / "suite-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(suite_manifest))
    return manifest_path, load_public_suite_inputs(manifest_path, root=tmp_path)


def test_suite_cross_validator_accepts_ready_not_run_bound_inputs(
    tmp_path: Path,
) -> None:
    _manifest_path, loaded = _write_ready_suite(tmp_path)

    validated = validate_public_suite_inputs(loaded)

    assert validated.derived_language_status == {
        Language.PYTHON: LanguageStatus.READY_NOT_RUN,
    }
    assert validated.data_cutoff_at == datetime.fromisoformat(
        T0.replace("Z", "+00:00")
    )


def test_suite_manifest_itself_must_not_be_a_symlink(
    tmp_path: Path,
) -> None:
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.NOT_READY,
    )
    real = tmp_path / "real-suite.json"
    real.write_bytes(canonical_json_bytes(_suite_manifest(source)))
    link = tmp_path / "suite.json"
    link.symlink_to(real)

    with pytest.raises(Phase6PathError, match="symlink"):
        load_public_suite_inputs(link, root=tmp_path)


def test_suite_rejects_file_replacement_after_hash_verification(
    tmp_path: Path,
) -> None:
    _manifest_path, loaded = _write_ready_suite(tmp_path)
    target = loaded.paths["diff-policy.json"]
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(loaded.bytes_by_path["diff-policy.json"])
    os.replace(replacement, target)

    with pytest.raises(Phase6PathError, match="changed after Manifest load"):
        validate_public_suite_inputs(loaded)


def test_suite_rejects_spec_and_manifest_reference_path_mismatch(
    tmp_path: Path,
) -> None:
    _manifest_path, loaded = _write_ready_suite(
        tmp_path,
        mismatched_spec_paths=True,
    )

    with pytest.raises(Phase6ContractError, match="Spec input path differs"):
        validate_public_suite_inputs(loaded)
