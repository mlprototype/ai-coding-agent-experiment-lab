from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_phase6 import _successful_codex
from typer.testing import CliRunner

import agentlab.phase6_campaign as campaign
from agentlab.cli import app
from agentlab.live import PromptInput
from agentlab.models import (
    CodexExecutionEvidence,
    CodexFailureStage,
    CodexTerminalEvent,
    CommandEvidence,
    CommandStatus,
    FailureKind,
    GateKind,
    LiveFailureKind,
    Provider,
    ProviderExecutionStatus,
    TerminationEvidence,
    TerminationReason,
    Workflow,
)
from agentlab.phase6 import (
    DiffPolicy,
    EditablePathPolicy,
    FixtureAcceptanceRecord,
    FixtureManifest,
    GateAcceptanceSummary,
    Language,
    LoadedWorkflowSpecContract,
    ProtectedPathPolicy,
    ToolchainComponent,
    ToolchainComponentRole,
    ToolchainIdentity,
    WorkflowPlanV1_2,
    canonical_json_bytes,
    load_campaign_contract,
    load_live_run_artifact_contract,
    load_recording_contract,
)
from agentlab.phase6_campaign import Phase6CampaignError, PlanBoundInputs
from agentlab.phase6_fixtures import (
    FixtureAcceptanceError,
    SecureTreeSnapshot,
    secure_tree_snapshot,
)
from agentlab.workflow import (
    BuiltWorkflowPrompt,
    FixedWorkflowInputs,
    WorkflowPlanPublication,
    WorkflowPlanRun,
)
from agentlab.workspace import snapshot_directory

HASH = "a" * 64
COMMIT = "1" * 40


def _toolchain() -> ToolchainIdentity:
    component = ToolchainComponent(
        role=ToolchainComponentRole.PYTHON_RUNTIME,
        resolved_executable_path="/opt/fake/python",
        executable_sha256=HASH,
        version_argv=["/opt/fake/python", "--version"],
        exact_version="Python fake",
        version_output_sha256=HASH,
    )
    raw = {
        "architecture": "arm64",
        "components": [component.model_dump(mode="json")],
        "gate_path_entries": ["/opt/fake"],
        "os": "darwin",
        "workspace_executable_lookup_allowed": False,
    }
    return ToolchainIdentity(
        **raw,
        fingerprint=hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
    )


def _inputs(tmp_path: Path) -> PlanBoundInputs:
    root = tmp_path / "bundle"
    fixture = root / "inputs" / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "target.py").write_text("value = 0\n", encoding="utf-8")
    (fixture / "check.py").write_text("# protected\n", encoding="utf-8")
    source = secure_tree_snapshot(fixture)
    workspace_source = snapshot_directory(fixture)
    toolchain = _toolchain()
    manifest = FixtureManifest(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="tag-normalizer-python-v1",
        fixture_sha256=source.sha256,
        gate_contract_sha256=HASH,
        toolchain=toolchain,
    )
    policy = DiffPolicy(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="tag-normalizer-python-v1",
        editable_paths=[
            EditablePathPolicy(path="target.py", allow_create=False, allow_delete=False)
        ],
        protected_paths=[ProtectedPathPolicy(path="check.py", role="gate_helper")],
        reject_unclassified_paths=True,
        reject_symlinks=True,
        reject_hardlinks=True,
        reject_special_files=True,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    policy_bytes = canonical_json_bytes(policy)
    acceptance = FixtureAcceptanceRecord(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="tag-normalizer-python-v1",
        acceptance_agentlab_commit=COMMIT,
        fixture_source_commit=COMMIT,
        fixture_sha256=source.sha256,
        fixture_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        diff_policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        gate_contract_sha256=HASH,
        reference_solution_sha256=HASH,
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
        verified_at="2026-08-01T00:00:00.000000Z",
    )
    spec = campaign._build_spec(
        language=Language.PYTHON,
        reviewed_commit=COMMIT,
        commands=[(gate, ["/opt/fake/python", "check.py", gate.value]) for gate in GateKind],
    )
    spec_path = root / "workflow-spec.yaml"
    plan_path = root / "workflow-plan.json"
    prompt_bytes = b"task\n"
    task = PromptInput(
        path=root / "inputs" / "task-prompt.md",
        content=prompt_bytes,
        sha256=hashlib.sha256(prompt_bytes).hexdigest(),
        byte_count=len(prompt_bytes),
    )
    prompts = {
        workflow: BuiltWorkflowPrompt(
            workflow=workflow,
            content=prompt_bytes + workflow.value.encode(),
            sha256=hashlib.sha256(prompt_bytes + workflow.value.encode()).hexdigest(),
            byte_count=len(prompt_bytes + workflow.value.encode()),
            task_sha256=task.sha256,
            task_byte_count=task.byte_count,
            workflow_revision=f"{workflow.value}-v1",
        )
        for workflow in (Workflow.ONE_SHOT, Workflow.STAGED)
    }
    runs = [
        WorkflowPlanRun(
            run_id=f"phase6_python_{workflow.value}",
            task_id="tag-normalizer",
            workflow=workflow,
            repetition_index=0,
            initial_state="planned",
            planned_provider_calls=1,
            task_prompt_revision=spec.task_prompt_revision,
            workflow_revision=prompts[workflow].workflow_revision,
            fixture_revision=spec.fixture_revision,
            recording_path=f"campaign-artifacts/recordings/{workflow.value}.jsonl",
            evidence_path=f"campaign-artifacts/evidence/{workflow.value}.json",
            diagnostic_path=f"campaign-artifacts/diagnostics/{workflow.value}.json",
        )
        for workflow in (Workflow.ONE_SHOT, Workflow.STAGED)
    ]
    plan = WorkflowPlanV1_2(
        schema_version="1.2",
        experiment_spec_schema_version="2.1",
        experiment_id=spec.experiment_id,
        experiment_spec_sha256=HASH,
        task_prompt_sha256=task.sha256,
        fixture_sha256=source.sha256,
        one_shot_prompt_sha256=prompts[Workflow.ONE_SHOT].sha256,
        one_shot_prompt_bytes=prompts[Workflow.ONE_SHOT].byte_count,
        staged_prompt_sha256=prompts[Workflow.STAGED].sha256,
        staged_prompt_bytes=prompts[Workflow.STAGED].byte_count,
        random_seed=spec.random_seed,
        comparison_axis="workflow",
        provider=Provider.CODEX,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        provider_timeout_ms=spec.provider_timeout_ms,
        task_prompt_revision=spec.task_prompt_revision,
        one_shot_revision=spec.one_shot_revision,
        staged_revision=spec.staged_revision,
        fixture_revision=spec.fixture_revision,
        planned_run_count=2,
        planned_provider_call_count=2,
        runs=runs,
        language=Language.PYTHON,
        reviewed_commit=COMMIT,
        fixture_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        fixture_acceptance_sha256=HASH,
        diff_policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        gate_contract_sha256=HASH,
        reference_solution_sha256=HASH,
        toolchain_fingerprint=toolchain.fingerprint,
    )
    return PlanBoundInputs(
        repository_root=tmp_path,
        spec_path=spec_path,
        plan_path=plan_path,
        artifact_root=root / "campaign-artifacts",
        loaded_spec=LoadedWorkflowSpecContract(spec=spec, sha256=HASH),
        plan=plan,
        plan_sha256=HASH,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        acceptance=acceptance,
        acceptance_bytes=canonical_json_bytes(acceptance),
        policy=policy,
        policy_bytes=policy_bytes,
        fixed=FixedWorkflowInputs(fixture=workspace_source, task_prompt=task, prompts=prompts),
        fixture_source=fixture,
        fixture_secure=source,
        reference_source=tmp_path / "reference",
        reference_sha256=HASH,
        gate_commands=tuple((gate, ["gate"]) for gate in GateKind),
        toolchain_binding=object(),
    )


def _commands(codex_completed: datetime) -> list[CommandEvidence]:
    now = max(datetime.now(UTC), codex_completed)
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
            command_index=index,
            argv=["gate"],
            status=CommandStatus.PASSED,
            return_code=0,
            started_at=now,
            completed_at=now,
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
        for index, gate in enumerate(GateKind)
    ]


def _patch_inputs(monkeypatch: pytest.MonkeyPatch, inputs: PlanBoundInputs) -> None:
    monkeypatch.setattr(campaign, "load_plan_bound_inputs", lambda *_a, **_k: inputs)
    monkeypatch.setattr(campaign, "revalidate_plan_bound_inputs", lambda _inputs: None)


def _materialize_plan_bundle(
    inputs: PlanBoundInputs,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkflowPlanV1_2, Path, Path]:
    root = inputs.spec_path.parent
    source = root / "inputs"
    source.mkdir(parents=True, exist_ok=True)
    prompt_path = source / "task-prompt.md"
    manifest_path = source / "fixture-manifest.json"
    acceptance_path = source / "fixture-acceptance.json"
    policy_path = source / "diff-policy.json"
    prompt_path.write_bytes(inputs.fixed.task_prompt.content)
    manifest_path.write_bytes(inputs.manifest_bytes)
    acceptance_path.write_bytes(inputs.acceptance_bytes)
    policy_path.write_bytes(inputs.policy_bytes)
    spec_bytes = campaign._spec_bytes(inputs.loaded_spec.spec)
    fixed = campaign._fixed_inputs_from_snapshots(
        spec=inputs.loaded_spec.spec,
        prompt_path=prompt_path,
        prompt_bytes=inputs.fixed.task_prompt.content,
        fixture=inputs.fixture_secure,
    )
    plan = inputs.plan.model_copy(
        update={
            "experiment_spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "fixture_acceptance_sha256": hashlib.sha256(inputs.acceptance_bytes).hexdigest(),
            "one_shot_prompt_sha256": fixed.prompts[Workflow.ONE_SHOT].sha256,
            "one_shot_prompt_bytes": fixed.prompts[Workflow.ONE_SHOT].byte_count,
            "staged_prompt_sha256": fixed.prompts[Workflow.STAGED].sha256,
            "staged_prompt_bytes": fixed.prompts[Workflow.STAGED].byte_count,
        }
    )
    plan_bytes = canonical_json_bytes(plan)
    inputs.spec_path.write_bytes(spec_bytes)
    inputs.plan_path.write_bytes(plan_bytes)
    campaign.plan_publication_path(inputs.plan_path).write_bytes(
        canonical_json_bytes(
            WorkflowPlanPublication(
                schema_version="1.0",
                plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
                created_at="2026-08-01T00:00:00.000000Z",
            )
        )
    )
    original_secure_snapshot = campaign.secure_tree_snapshot

    def secure_snapshot(path: Path) -> SecureTreeSnapshot:
        if path == inputs.fixture_source:
            return original_secure_snapshot(path)
        return SecureTreeSnapshot(files={}, directories=(), sha256=HASH)

    monkeypatch.setattr(campaign, "verify_repository_provenance", lambda _root: COMMIT)
    monkeypatch.setattr(campaign, "secure_tree_snapshot", secure_snapshot)
    monkeypatch.setattr(campaign, "_capture_toolchain_binding", lambda *_args: object())
    monkeypatch.setattr(campaign, "_commands_for", lambda *_args: inputs.gate_commands)
    monkeypatch.setattr(campaign, "_gate_contract_hash", lambda *_args: HASH)
    return plan, policy_path, inputs.plan_path


def _provider(
    inputs: PlanBoundInputs,
    mutation: Any | None = None,
) -> Any:
    def execute(**kwargs: Any) -> CodexExecutionEvidence:
        if mutation is not None:
            mutation(Path(kwargs["workspace"]))
        now = datetime.now(UTC)
        return _successful_codex(
            model=inputs.plan.model,
            reasoning_effort=inputs.plan.reasoning_effort,
            prompt_bytes=len(kwargs["prompt"]),
        ).model_copy(
            update={
                "preflight_checked_at": now,
                "started_at": now,
                "completed_at": now,
            }
        )

    return execute


def _failed_provider_evidence(
    inputs: PlanBoundInputs,
    failure: LiveFailureKind,
    prompt_bytes: int,
) -> CodexExecutionEvidence:
    now = datetime.now(UTC)
    success = _successful_codex(
        model=inputs.plan.model,
        reasoning_effort=inputs.plan.reasoning_effort,
        prompt_bytes=prompt_bytes,
    ).model_dump(mode="python")
    success.update(
        {
            "status": ProviderExecutionStatus.FAILED,
            "failure_kind": failure,
            "failure_stage": CodexFailureStage.PROVIDER_PROCESS_COLLECTION,
            "started_at": now,
            "completed_at": now,
            "preflight_checked_at": now,
            "event_count": 2,
            "terminal_event": CodexTerminalEvent.NONE,
            "turn_completed_count": 0,
            "exit_code": 1,
        }
    )
    if failure is LiveFailureKind.PROVIDER_TIMEOUT:
        success["exit_code"] = -15
        success["termination"] = TerminationEvidence(
            reason=TerminationReason.TIMEOUT,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=True,
            error=None,
        )
    return CodexExecutionEvidence.model_validate(success)


def test_fake_campaign_accepts_allowed_diff_and_strict_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)

    def provider(**kwargs: Any) -> Any:
        Path(kwargs["workspace"], "target.py").write_text("value = 1\n", encoding="utf-8")
        now = datetime.now(UTC)
        return _successful_codex(
            model=inputs.plan.model,
            reasoning_effort=inputs.plan.reasoning_effort,
            prompt_bytes=len(kwargs["prompt"]),
        ).model_copy(update={"preflight_checked_at": now, "started_at": now, "completed_at": now})

    monkeypatch.setattr(
        campaign,
        "_run_gates",
        lambda _i, _w, _e, _t: campaign.GateExecutionOutcome(
            _commands(datetime.now(UTC)), 0, None, None
        ),
    )
    campaign_path = inputs.spec_path.parent / "campaign-artifacts" / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=provider,
    )
    assert outcome.provider_call_count == 2
    assert load_campaign_contract(campaign_path).finished.provider_call_count == 2  # type: ignore[union-attr]
    for run in inputs.plan.runs:
        artifact = load_live_run_artifact_contract(inputs.spec_path.parent / run.evidence_path)
        assert artifact.overall_status.value == "passed"  # type: ignore[union-attr]


def test_output_contract_violation_runs_zero_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    gate_calls = 0

    def provider(**kwargs: Any) -> Any:
        Path(kwargs["workspace"], "check.py").write_text("changed\n", encoding="utf-8")
        now = datetime.now(UTC)
        return _successful_codex(
            model=inputs.plan.model,
            reasoning_effort=inputs.plan.reasoning_effort,
            prompt_bytes=len(kwargs["prompt"]),
        ).model_copy(update={"preflight_checked_at": now, "started_at": now, "completed_at": now})

    def gates(*_args: Any) -> Any:
        nonlocal gate_calls
        gate_calls += 1
        raise AssertionError("Gate must not run")

    monkeypatch.setattr(campaign, "_run_gates", gates)
    path = inputs.spec_path.parent / "campaign-artifacts" / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=provider,
    )
    assert outcome.provider_call_count == 2
    assert gate_calls == 0


def test_input_drift_before_first_call_has_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(campaign, "load_plan_bound_inputs", lambda *_a, **_k: inputs)
    checks = 0

    def drift(_inputs: PlanBoundInputs) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Phase6CampaignError("changed")

    monkeypatch.setattr(campaign, "revalidate_plan_bound_inputs", drift)
    provider_calls = 0

    def provider(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError

    path = inputs.spec_path.parent / "campaign-artifacts" / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=provider,
    )
    assert outcome.provider_call_count == 0
    assert provider_calls == 0
    assert outcome.stop_reason.value == "input_changed"


@pytest.mark.parametrize("changed_input", ["plan", "policy"])
def test_plan_bound_load_uses_one_stable_snapshot_and_revalidation_detects_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_input: str,
) -> None:
    inputs = _inputs(tmp_path)
    plan, policy_path, plan_path = _materialize_plan_bundle(inputs, monkeypatch)
    if changed_input == "plan":
        target = plan_path
        replacement = canonical_json_bytes(
            plan.model_copy(update={"random_seed": plan.random_seed + 1})
        )
    else:
        target = policy_path
        replacement = canonical_json_bytes(
            inputs.policy.model_copy(
                update={
                    "editable_paths": [
                        EditablePathPolicy(
                            path="target.py",
                            allow_create=True,
                            allow_delete=False,
                        )
                    ]
                }
            )
        )
    original_read = campaign._read_stable_regular_file
    replaced = False

    def replace_after_snapshot(path: Path, label: str) -> Any:
        nonlocal replaced
        snapshot = original_read(path, label)
        if not replaced and path == target:
            target.write_bytes(replacement)
            replaced = True
        return snapshot

    monkeypatch.setattr(campaign, "_read_stable_regular_file", replace_after_snapshot)
    loaded = campaign.load_plan_bound_inputs(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
    )
    assert replaced
    if changed_input == "plan":
        assert loaded.plan == plan
    else:
        assert loaded.policy == inputs.policy
    with pytest.raises(ValueError):
        campaign.revalidate_plan_bound_inputs(loaded)


def test_phase6_cli_confirmation_stops_before_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    invoked = False

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal invoked
        invoked = True
        raise AssertionError

    monkeypatch.setattr(campaign, "prepare_phase6_campaign", forbidden)
    result = CliRunner().invoke(app, ["prepare-phase6-campaign", "--language", "python"])
    assert result.exit_code == 2
    assert invoked is False
    assert "subprocesses executed: 0" in result.stdout


def test_explicit_acceptance_root_is_resolved_without_default_fallback(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    selected = repository / "custom" / "acceptance" / "new-head"
    selected.mkdir(parents=True)

    assert campaign._repository_input_directory(
        repository,
        Path("custom/acceptance/new-head"),
        "Fixture Acceptance root",
    ) == selected


@pytest.mark.parametrize(
    "configured",
    [Path("custom/../acceptance"), Path("../outside")],
)
def test_explicit_acceptance_root_rejects_traversal(
    tmp_path: Path,
    configured: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(Phase6CampaignError, match="must not contain"):
        campaign._repository_input_directory(
            repository,
            configured,
            "Fixture Acceptance root",
        )


def test_explicit_acceptance_root_rejects_symlink_parent(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(Phase6CampaignError, match="link"):
        campaign._repository_input_directory(
            repository,
            Path("linked/acceptance"),
            "Fixture Acceptance root",
        )


def test_plan_fixture_hash_uses_phase6_manifest_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    fixed = inputs.fixed
    assert fixed.fixture.sha256 != inputs.manifest.fixture_sha256
    monkeypatch.setattr(
        campaign,
        "build_workflow_plan_from_inputs",
        lambda *_args: inputs.plan.model_copy(
            update={"schema_version": "1.1", "experiment_spec_schema_version": "2.0"}
        ),
    )
    plan = campaign._build_plan(
        spec_path=inputs.spec_path,
        loaded=inputs.loaded_spec,
        fixed=fixed,
        manifest=inputs.manifest,
        manifest_bytes=inputs.manifest_bytes,
        acceptance=inputs.acceptance,
        acceptance_bytes=inputs.acceptance_bytes,
        policy_bytes=inputs.policy_bytes,
    )
    assert plan.fixture_sha256 == inputs.manifest.fixture_sha256
    assert plan.fixture_acceptance_sha256 == hashlib.sha256(
        inputs.acceptance_bytes
    ).hexdigest()


def test_run_cli_requires_live_confirmation_before_runtime() -> None:
    result = CliRunner().invoke(
        app,
        ["run-phase6-campaign", "missing.yaml", "--plan", "missing.json", "--campaign", "out"],
    )
    assert result.exit_code != 0


def test_artifact_root_symlink_is_rejected_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    outside = tmp_path / "outside"
    outside.mkdir()
    inputs.artifact_root.symlink_to(outside, target_is_directory=True)
    provider_calls = 0

    def forbidden(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError

    with pytest.raises(Phase6CampaignError, match="symlink"):
        campaign.run_phase6_campaign(
            tmp_path,
            inputs.spec_path,
            inputs.plan_path,
            inputs.artifact_root / "campaign.jsonl",
            confirm_live_codex=True,
            confirm_provider_calls=2,
            provider_executor=forbidden,
        )
    assert provider_calls == 0
    assert list(outside.iterdir()) == []


def test_artifact_reservation_parent_symlink_is_rejected_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    outside = tmp_path / "outside-recordings"
    outside.mkdir()
    inputs.artifact_root.mkdir()
    (inputs.artifact_root / "recordings").symlink_to(outside, target_is_directory=True)
    provider_calls = 0

    def forbidden(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError

    with pytest.raises(Phase6CampaignError, match="symlink"):
        campaign.run_phase6_campaign(
            tmp_path,
            inputs.spec_path,
            inputs.plan_path,
            inputs.artifact_root / "campaign.jsonl",
            confirm_live_codex=True,
            confirm_provider_calls=2,
            provider_executor=forbidden,
        )
    assert provider_calls == 0
    assert list(outside.iterdir()) == []


def test_artifact_reservation_conflict_stops_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    reserved = inputs.spec_path.parent / inputs.plan.runs[0].recording_path
    reserved.parent.mkdir(parents=True)
    reserved.write_text("existing", encoding="utf-8")
    provider_calls = 0

    def forbidden(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError

    with pytest.raises(Phase6CampaignError, match="reservation already exists"):
        campaign.run_phase6_campaign(
            tmp_path,
            inputs.spec_path,
            inputs.plan_path,
            inputs.artifact_root / "campaign.jsonl",
            confirm_live_codex=True,
            confirm_provider_calls=2,
            provider_executor=forbidden,
        )
    assert provider_calls == 0


def test_gate_process_cleanup_failure_has_priority_and_stops_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    uncleared = TerminationEvidence(
        reason=TerminationReason.RESIDUAL_PROCESS,
        sigterm_sent=True,
        sigkill_sent=False,
        process_group_cleared=False,
        error="process group remains",
    )

    def gates(*_args: Any) -> Any:
        commands = _commands(datetime.now(UTC))
        commands[0] = commands[0].model_copy(update={"termination": uncleared})
        return campaign.GateExecutionOutcome(commands, 0, FailureKind.PROCESS_CLEANUP_ERROR, None)

    monkeypatch.setattr(
        campaign,
        "_run_gates",
        gates,
    )
    path = inputs.artifact_root / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=_provider(
            inputs,
            lambda workspace: (workspace / "target.py").write_text("value = 1\n", encoding="utf-8"),
        ),
    )
    assert outcome.provider_call_count == 1
    assert outcome.stop_reason.value == "cleanup_failure"
    artifact = load_live_run_artifact_contract(
        inputs.spec_path.parent / inputs.plan.runs[0].evidence_path
    )
    assert artifact.failure_kind.value == "process_cleanup_error"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("verification_failure_call", "expected_gate_count"),
    [(1, 0), (2, 1)],
)
def test_toolchain_change_before_or_after_gate_finishes_canonical_harness_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_failure_call: int,
    expected_gate_count: int,
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    verification_calls = 0
    runner_calls = 0

    def verify(*_args: Any) -> None:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == verification_failure_call:
            raise FixtureAcceptanceError("toolchain changed")

    class FakeGateRunner:
        def run(self, **kwargs: Any) -> Any:
            nonlocal runner_calls
            runner_calls += 1
            command = _commands(datetime.now(UTC))[kwargs["command_index"]].model_copy(
                update={"argv": kwargs["argv"]}
            )
            return SimpleNamespace(evidence=command, harness_failure=None)

    monkeypatch.setattr(campaign, "_verify_toolchain_binding", verify)
    monkeypatch.setattr(campaign, "LocalCommandRunner", lambda _settings: FakeGateRunner())
    campaign_path = inputs.artifact_root / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=_provider(
            inputs,
            lambda workspace: (workspace / "target.py").write_text("value = 1\n", encoding="utf-8"),
        ),
    )
    assert runner_calls == expected_gate_count
    assert outcome.provider_call_count == 1
    assert outcome.counted_failure_count == 0
    assert outcome.stop_reason.value == "harness_failure"

    run = inputs.plan.runs[0]
    artifact = load_live_run_artifact_contract(inputs.spec_path.parent / run.evidence_path)
    assert artifact.overall_status.value == "harness_error"  # type: ignore[union-attr]
    assert artifact.failure_kind.value == "evidence_error"  # type: ignore[union-attr]
    assert len(artifact.gate_commands) == expected_gate_count  # type: ignore[union-attr]
    assert artifact.gate_executed is bool(expected_gate_count)  # type: ignore[union-attr]
    assert artifact.diff.collection_error is not None  # type: ignore[union-attr]

    recording = load_recording_contract(inputs.spec_path.parent / run.recording_path)
    assert recording.terminal.failure_kind.value == "evidence_error"  # type: ignore[union-attr]
    assert len(recording.terminal.gate_commands) == expected_gate_count  # type: ignore[union-attr]
    loaded_campaign = load_campaign_contract(campaign_path)
    assert loaded_campaign.finished.stop_reason.value == "harness_failure"  # type: ignore[union-attr]
    terminal = next(
        event
        for event in loaded_campaign.events  # type: ignore[union-attr]
        if getattr(event, "run_id", None) == run.run_id
        and getattr(getattr(event, "status", None), "value", None) == "failed"
    )
    assert terminal.failure_kind.value == "evidence_error"  # type: ignore[union-attr]
    assert terminal.gate_executed is bool(expected_gate_count)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "failure",
    [LiveFailureKind.PROVIDER_CLI_NONZERO, LiveFailureKind.PROVIDER_TIMEOUT],
)
def test_provider_failure_and_timeout_run_zero_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: LiveFailureKind,
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    gate_calls = 0

    def gates(*_args: Any) -> Any:
        nonlocal gate_calls
        gate_calls += 1
        raise AssertionError

    monkeypatch.setattr(campaign, "_run_gates", gates)
    path = inputs.artifact_root / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=lambda **kwargs: _failed_provider_evidence(
            inputs, failure, len(kwargs["prompt"])
        ),
    )
    assert outcome.provider_call_count == 2
    assert gate_calls == 0


def test_provider_evidence_error_is_harness_failure_and_runs_zero_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    gate_calls = 0

    def gates(*_args: Any) -> Any:
        nonlocal gate_calls
        gate_calls += 1
        raise AssertionError

    monkeypatch.setattr(campaign, "_run_gates", gates)
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        inputs.artifact_root / "campaign.jsonl",
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=lambda **kwargs: _failed_provider_evidence(
            inputs, LiveFailureKind.EVIDENCE_ERROR, len(kwargs["prompt"])
        ),
    )
    assert outcome.provider_call_count == 1
    assert outcome.counted_failure_count == 0
    assert outcome.stop_reason.value == "harness_failure"
    assert gate_calls == 0


@pytest.mark.parametrize(
    ("status", "runner_failure", "return_code", "termination"),
    [
        (
            CommandStatus.TIMED_OUT,
            FailureKind.TIMEOUT,
            -15,
            TerminationEvidence(
                reason=TerminationReason.TIMEOUT,
                sigterm_sent=True,
                sigkill_sent=False,
                process_group_cleared=True,
                error=None,
            ),
        ),
        (
            CommandStatus.SIGNAL_TERMINATED,
            FailureKind.SIGNAL_TERMINATION,
            -15,
            TerminationEvidence(
                reason=TerminationReason.RESIDUAL_PROCESS,
                sigterm_sent=True,
                sigkill_sent=False,
                process_group_cleared=True,
                error=None,
            ),
        ),
        (
            CommandStatus.SPAWN_ERROR,
            FailureKind.SPAWN_ERROR,
            None,
            TerminationEvidence(
                reason=TerminationReason.NONE,
                sigterm_sent=False,
                sigkill_sent=False,
                process_group_cleared=True,
                error=None,
            ),
        ),
        (
            CommandStatus.COLLECTION_ERROR,
            FailureKind.EVIDENCE_ERROR,
            0,
            TerminationEvidence(
                reason=TerminationReason.NONE,
                sigterm_sent=False,
                sigkill_sent=False,
                process_group_cleared=True,
                error=None,
            ),
        ),
    ],
)
def test_abnormal_gate_results_are_harness_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: CommandStatus,
    runner_failure: FailureKind,
    return_code: int | None,
    termination: TerminationEvidence,
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)

    def gates(*_args: Any) -> Any:
        now = datetime.now(UTC)
        command = CommandEvidence(
            gate=GateKind.ACCEPTANCE,
            command_index=0,
            argv=["gate"],
            status=status,
            return_code=return_code,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_decode_replaced=False,
            stderr_decode_replaced=False,
            termination=termination,
            error=(
                "gate harness failure"
                if status in {CommandStatus.SPAWN_ERROR, CommandStatus.COLLECTION_ERROR}
                else None
            ),
        )
        return campaign.GateExecutionOutcome([command], 0, runner_failure, None)

    monkeypatch.setattr(
        campaign,
        "_run_gates",
        gates,
    )
    path = inputs.artifact_root / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=_provider(
            inputs,
            lambda workspace: (workspace / "target.py").write_text("value = 1\n", encoding="utf-8"),
        ),
    )
    assert outcome.stop_reason.value == "harness_failure"
    artifact = load_live_run_artifact_contract(
        inputs.spec_path.parent / inputs.plan.runs[0].evidence_path
    )
    assert artifact.failure_kind.value == "gate_harness_error"  # type: ignore[union-attr]


def test_gate_workspace_change_is_evidence_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)

    def gates(_inputs: Any, workspace: Path, *_args: Any) -> Any:
        (workspace / "target.py").write_text("value = 2\n", encoding="utf-8")
        return campaign.GateExecutionOutcome(_commands(datetime.now(UTC)), 0, None, None)

    monkeypatch.setattr(campaign, "_run_gates", gates)
    path = inputs.artifact_root / "campaign.jsonl"
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=_provider(
            inputs,
            lambda workspace: (workspace / "target.py").write_text("value = 1\n", encoding="utf-8"),
        ),
    )
    assert outcome.stop_reason.value == "harness_failure"
    artifact = load_live_run_artifact_contract(
        inputs.spec_path.parent / inputs.plan.runs[0].evidence_path
    )
    assert artifact.failure_kind.value == "evidence_error"  # type: ignore[union-attr]


def test_input_drift_before_second_call_preserves_first_call_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(campaign, "load_plan_bound_inputs", lambda *_a, **_k: inputs)
    checks = 0

    def drift(_inputs: PlanBoundInputs) -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise Phase6CampaignError("changed")

    monkeypatch.setattr(campaign, "revalidate_plan_bound_inputs", drift)
    monkeypatch.setattr(
        campaign,
        "_run_gates",
        lambda *_args: campaign.GateExecutionOutcome(_commands(datetime.now(UTC)), 0, None, None),
    )
    provider_calls = 0

    def provider(**kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        return _provider(
            inputs,
            lambda workspace: (workspace / "target.py").write_text("value = 1\n", encoding="utf-8"),
        )(**kwargs)

    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        inputs.artifact_root / "campaign.jsonl",
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=provider,
    )
    assert provider_calls == 1
    assert outcome.provider_call_count == 1
    assert outcome.stop_reason.value == "input_changed"


@pytest.mark.parametrize("violation", ["create", "delete", "hardlink", "fifo"])
def test_diff_policy_rejects_create_delete_hardlink_and_special_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
) -> None:
    inputs = _inputs(tmp_path)
    _patch_inputs(monkeypatch, inputs)
    gate_calls = 0

    def mutate(workspace: Path) -> None:
        if violation == "create":
            (workspace / "extra.py").write_text("x\n", encoding="utf-8")
        elif violation == "delete":
            (workspace / "target.py").unlink()
        elif violation == "hardlink":
            os.link(workspace / "target.py", workspace / "extra.py")
        else:
            os.mkfifo(workspace / "pipe")

    def gates(*_args: Any) -> Any:
        nonlocal gate_calls
        gate_calls += 1
        raise AssertionError

    monkeypatch.setattr(campaign, "_run_gates", gates)
    outcome = campaign.run_phase6_campaign(
        tmp_path,
        inputs.spec_path,
        inputs.plan_path,
        inputs.artifact_root / "campaign.jsonl",
        confirm_live_codex=True,
        confirm_provider_calls=2,
        provider_executor=_provider(inputs, mutate),
    )
    assert outcome.provider_call_count == 2
    assert gate_calls == 0
