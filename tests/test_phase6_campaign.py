from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_phase6 import _successful_codex
from typer.testing import CliRunner

import agentlab.phase6_campaign as campaign
from agentlab.cli import app
from agentlab.live import PromptInput
from agentlab.models import (
    CommandEvidence,
    CommandStatus,
    GateKind,
    Provider,
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
)
from agentlab.phase6_campaign import Phase6CampaignError, PlanBoundInputs
from agentlab.phase6_fixtures import secure_tree_snapshot
from agentlab.workflow import BuiltWorkflowPrompt, FixedWorkflowInputs, WorkflowPlanRun
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
    monkeypatch.setattr(campaign, "load_workflow_plan_contract", lambda _path: inputs.plan)
    monkeypatch.setattr(campaign, "load_plan_bound_inputs", lambda *_a, **_k: inputs)
    monkeypatch.setattr(campaign, "revalidate_plan_bound_inputs", lambda _inputs: None)


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
        lambda _i, _w, _e, _t: (_commands(datetime.now(UTC)), 0, False),
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
    monkeypatch.setattr(campaign, "load_workflow_plan_contract", lambda _path: inputs.plan)
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


def test_run_cli_requires_live_confirmation_before_runtime() -> None:
    result = CliRunner().invoke(
        app,
        ["run-phase6-campaign", "missing.yaml", "--plan", "missing.json", "--campaign", "out"],
    )
    assert result.exit_code != 0
