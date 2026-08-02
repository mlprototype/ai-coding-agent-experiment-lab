from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_phase6_campaign import COMMIT, _inputs, _materialize_plan_bundle
from typer.testing import CliRunner

import agentlab.cli as cli
import agentlab.phase6_approval as approval
import agentlab.phase6_campaign as campaign
from agentlab.cli import app
from agentlab.models import Provider
from agentlab.phase6 import (
    ArtifactReference,
    Language,
    LanguageStatus,
    LoadedWorkflowSpecContract,
    PrimarySuiteSource,
    ProviderCoverage,
    ProviderEvaluationStatus,
    PublicSuiteManifest,
    SourceClass,
    canonical_json_bytes,
)
from agentlab.phase6_approval import SupplementalApprovalError
from agentlab.phase6_fixtures import _gate_contract_bytes, secure_tree_snapshot


def _reference(role: str, path: str) -> ArtifactReference:
    return ArtifactReference(role=role, path=path, sha256="b" * 64)


def _source(language: Language, status: LanguageStatus) -> PrimarySuiteSource:
    prefix = language.value
    common: dict[str, Any] = {
        "source_class": SourceClass.PRIMARY,
        "language": language,
        "expected_language_status": status,
        "spec": _reference("spec", f"{prefix}/spec.yaml"),
        "fixture_manifest": _reference(
            "fixture_manifest", f"{prefix}/fixture-manifest.json"
        ),
        "fixture_acceptance": _reference(
            "fixture_acceptance", f"{prefix}/fixture-acceptance.json"
        ),
        "diff_policy": _reference("diff_policy", f"{prefix}/diff-policy.json"),
        "plan": _reference("plan", f"{prefix}/plan.json"),
    }
    if status is LanguageStatus.EVALUATED:
        common.update(
            campaign=_reference("campaign", f"{prefix}/campaign.jsonl"),
            evidence=[_reference("evidence", f"{prefix}/evidence.json")],
            recordings=[_reference("recording", f"{prefix}/recording.jsonl")],
        )
    return PrimarySuiteSource(**common)


def _accepted_manifest(path: Path) -> Path:
    manifest = PublicSuiteManifest(
        schema_version="1.0",
        suite_id="phase6-python-evaluated-test-001",
        renderer_version="test-renderer",
        data_cutoff_at="2026-08-01T00:00:00.000000Z",
        primary_sources=[
            _source(Language.JAVA, LanguageStatus.READY_NOT_RUN),
            _source(Language.PYTHON, LanguageStatus.EVALUATED),
        ],
        historical_sources=[],
        provider_coverage=[
            ProviderCoverage(
                provider=Provider.CODEX,
                evaluation_status=ProviderEvaluationStatus.EVALUATED,
                evaluated_languages=[Language.PYTHON],
                blocker=None,
            ),
            ProviderCoverage(
                provider=Provider.ANTIGRAVITY,
                evaluation_status=ProviderEvaluationStatus.NOT_EVALUATED,
                evaluated_languages=[],
                blocker="upstream_artifact_signature_invalid",
            ),
        ],
        antigravity_blocker="upstream_artifact_signature_invalid",
        zero_call_run_publication="aggregate_only_no_run_record",
        planned_outputs=["checksums.json", "release-metadata.json"],
        automatic_winner_selected=False,
        leaderboard_generated=False,
        statistical_significance_claimed=False,
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(manifest))
    return path


def _approval_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, campaign.PlanBoundInputs, Path]:
    repository = tmp_path / "repository"
    inputs = _inputs(repository)
    reference_root = (
        repository / "experiments" / "phase6" / "fixtures" / "java" / "reference"
    )
    reference_root.mkdir(parents=True)
    (reference_root / "target.java").write_text("final class Target {}\n", encoding="utf-8")
    reference_snapshot = secure_tree_snapshot(reference_root)
    gate_bytes = _gate_contract_bytes(
        Language.JAVA,
        inputs.acceptance.toolchain,
        inputs.gate_commands,
    )
    gate_sha = hashlib.sha256(gate_bytes).hexdigest()
    spec = inputs.loaded_spec.spec.model_copy(
        update={
            "experiment_id": "phase6-java-workflow",
            "language": Language.JAVA,
            "fixture_revision": "tag-normalizer-java-v1",
        }
    )
    manifest = inputs.manifest.model_copy(
        update={
            "language": Language.JAVA,
            "fixture_revision": "tag-normalizer-java-v1",
            "gate_contract_sha256": gate_sha,
        }
    )
    policy = inputs.policy.model_copy(
        update={
            "language": Language.JAVA,
            "fixture_revision": "tag-normalizer-java-v1",
        }
    )
    acceptance_record = inputs.acceptance.model_copy(
        update={
            "language": Language.JAVA,
            "fixture_revision": "tag-normalizer-java-v1",
            "gate_contract_sha256": gate_sha,
            "reference_solution_sha256": reference_snapshot.sha256,
        }
    )
    runs = [
        run.model_copy(update={"fixture_revision": "tag-normalizer-java-v1"})
        for run in inputs.plan.runs
    ]
    plan = inputs.plan.model_copy(
        update={
            "experiment_id": "phase6-java-workflow",
            "language": Language.JAVA,
            "fixture_revision": "tag-normalizer-java-v1",
            "gate_contract_sha256": gate_sha,
            "reference_solution_sha256": reference_snapshot.sha256,
            "runs": runs,
        }
    )
    prepared = replace(
        inputs,
        loaded_spec=LoadedWorkflowSpecContract(spec=spec, sha256=inputs.loaded_spec.sha256),
        plan=plan,
        manifest=manifest,
        manifest_bytes=canonical_json_bytes(manifest),
        acceptance=acceptance_record,
        acceptance_bytes=canonical_json_bytes(acceptance_record),
        policy=policy,
        policy_bytes=canonical_json_bytes(policy),
        reference_source=reference_root,
        reference_sha256=reference_snapshot.sha256,
    )
    materialized_plan, _, _ = _materialize_plan_bundle(prepared, monkeypatch)
    prompt_path = prepared.spec_path.parent / "inputs" / "task-prompt.md"
    fixed = campaign._fixed_inputs_from_snapshots(
        spec=spec,
        prompt_path=prompt_path,
        prompt_bytes=prompt_path.read_bytes(),
        fixture=prepared.fixture_secure,
    )
    prepared = replace(
        prepared,
        plan=materialized_plan,
        plan_sha256=hashlib.sha256(prepared.plan_path.read_bytes()).hexdigest(),
        loaded_spec=LoadedWorkflowSpecContract(
            spec=spec,
            sha256=hashlib.sha256(prepared.spec_path.read_bytes()).hexdigest(),
        ),
        fixed=fixed,
    )
    manifest_path = _accepted_manifest(repository / "public" / "suite-manifest.json")
    monkeypatch.setattr(approval, "verify_repository_provenance", lambda _root: COMMIT)
    monkeypatch.setattr(
        approval,
        "load_plan_bound_inputs",
        lambda *_args, **_kwargs: prepared,
    )
    return repository, prepared, manifest_path


def _generate(
    repository: Path,
    inputs: campaign.PlanBoundInputs,
    manifest_path: Path,
    output: Path,
) -> approval.SupplementalApprovalPublication:
    return approval.prepare_supplemental_live_campaign_approval(
        repository_root=repository,
        approval_id="phase6-java-supplemental-test-001",
        spec_path=inputs.spec_path,
        plan_path=inputs.plan_path,
        campaign_path=inputs.artifact_root / "campaign.jsonl",
        accepted_manifest_path=manifest_path,
        prior_provider_call_minimum=2,
        prior_provider_call_maximum=3,
        output_path=output,
        confirm_local_execution=True,
    )


def _canonical_raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def test_valid_java_packet_is_pending_canonical_and_strictly_reloadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_or_campaign_called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal provider_or_campaign_called
        provider_or_campaign_called = True
        raise AssertionError("Live execution must not run")

    monkeypatch.setattr(campaign, "execute_real_codex_provider", forbidden)
    monkeypatch.setattr(campaign, "run_phase6_campaign", forbidden)
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    publication = _generate(repository, inputs, manifest_path, output)

    loaded = approval.load_supplemental_live_campaign_approval(
        output,
        repository_root=repository,
    )
    assert output.read_bytes() == canonical_json_bytes(loaded)
    assert publication.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert publication.byte_count == len(output.read_bytes())
    assert loaded.approval_status == "pending_human_live_approval"
    assert loaded.accepted_public_suite.python_status is LanguageStatus.EVALUATED
    assert loaded.accepted_public_suite.java_status is LanguageStatus.READY_NOT_RUN
    assert loaded.provider_accounting.projected_minimum_calls == 4
    assert loaded.provider_accounting.projected_maximum_calls == 5
    assert loaded.campaign.workflow_order == [run.workflow for run in inputs.plan.runs]
    assert loaded.campaign.per_run_provider_calls == [1, 1]
    assert loaded.exact_argv == [
        ".venv/bin/agentlab",
        "run-phase6-campaign",
        loaded.artifacts.spec.path,
        "--plan",
        loaded.artifacts.plan.path,
        "--campaign",
        loaded.campaign.campaign_jsonl,
        "--repository-root",
        ".",
        "--confirm-live-codex",
        "--confirm-provider-calls",
        "2",
    ]
    assert inputs.artifact_root.exists() is False
    assert provider_or_campaign_called is False


def test_generator_is_create_only_and_rejects_preexisting_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    first = _generate(repository, inputs, manifest_path, output)
    before = output.read_bytes()

    with pytest.raises(SupplementalApprovalError, match="already exists"):
        _generate(repository, inputs, manifest_path, output)

    assert output.read_bytes() == before
    assert first.sha256 == hashlib.sha256(before).hexdigest()


def test_generator_rolls_back_owned_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "new" / "nested" / "supplemental.json"
    monkeypatch.setattr(
        approval,
        "load_supplemental_live_campaign_approval",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SupplementalApprovalError("synthetic reload failure")
        ),
    )

    with pytest.raises(SupplementalApprovalError, match="synthetic"):
        _generate(repository, inputs, manifest_path, output)

    assert output.exists() is False
    assert (repository / "new").exists() is False


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda raw: raw.pop("approval_id"), "invalid"),
        (lambda raw: raw.update(unexpected=True), "invalid"),
        (lambda raw: raw.update(schema_version="1.1"), "invalid"),
        (lambda raw: raw.update(document_type="evaluation_decision"), "invalid"),
        (lambda raw: raw.update(approval_status="approved_for_live"), "invalid"),
        (
            lambda raw: raw["artifacts"]["spec"].update(path="/absolute/spec.yaml"),
            "invalid",
        ),
        (
            lambda raw: raw["artifacts"]["spec"].update(path="bundle/../spec.yaml"),
            "invalid",
        ),
        (
            lambda raw: raw["provider_accounting"].update(projected_maximum_calls=99),
            "invalid",
        ),
        (
            lambda raw: raw["provider_accounting"].update(prior_minimum_calls=True),
            "invalid",
        ),
        (
            lambda raw: raw["campaign"].update(provider="antigravity"),
            "invalid",
        ),
        (
            lambda raw: raw["campaign"].update(exact_provider_calls=3),
            "invalid",
        ),
        (lambda raw: raw["exact_argv"].append("--force"), "invalid"),
    ],
)
def test_strict_schema_and_cross_field_mutations_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
    match: str,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    raw = _canonical_raw(output)
    mutation(raw)
    output.write_bytes(canonical_json_bytes(raw))

    with pytest.raises(SupplementalApprovalError, match=match):
        approval.load_supplemental_live_campaign_approval(
            output,
            repository_root=repository,
        )


@pytest.mark.parametrize(
    "field_update",
    [
        {"reviewed_repository_head": "f" * 40},
        {"campaign": {"model": "different-model"}},
        {"campaign": {"reasoning_effort": "medium"}},
        {"campaign": {"workflow_order": ["staged", "one_shot"]}},
    ],
)
def test_rederived_head_provider_and_workflow_bindings_reject_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_update: dict[str, Any],
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    raw = _canonical_raw(output)
    for field, update in field_update.items():
        if isinstance(update, dict):
            raw[field].update(update)
        else:
            raw[field] = update
    output.write_bytes(canonical_json_bytes(raw))

    with pytest.raises(SupplementalApprovalError):
        approval.load_supplemental_live_campaign_approval(
            output,
            repository_root=repository,
        )


@pytest.mark.parametrize(
    "target",
    [
        "spec",
        "plan",
        "manifest_input",
        "acceptance",
        "policy",
        "metadata",
        "fixture",
        "reference",
        "prompt",
        "public_manifest",
    ],
)
def test_source_artifact_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    paths = {
        "spec": inputs.spec_path,
        "plan": inputs.plan_path,
        "manifest_input": inputs.spec_path.parent / "inputs" / "fixture-manifest.json",
        "acceptance": inputs.spec_path.parent / "inputs" / "fixture-acceptance.json",
        "policy": inputs.spec_path.parent / "inputs" / "diff-policy.json",
        "metadata": campaign.plan_publication_path(inputs.plan_path),
        "fixture": inputs.fixture_source / "target.py",
        "reference": inputs.reference_source / "target.java",
        "prompt": inputs.spec_path.parent / "inputs" / "task-prompt.md",
        "public_manifest": manifest_path,
    }
    paths[target].write_bytes(paths[target].read_bytes() + b"drift")

    with pytest.raises((SupplementalApprovalError, ValueError)):
        approval.load_supplemental_live_campaign_approval(
            output,
            repository_root=repository,
        )


def test_duplicate_nonfinite_and_noncanonical_json_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    canonical = output.read_bytes()
    variants = [
        b'{"approval_id":"duplicate",' + canonical[1:],
        canonical.replace(b'"prior_minimum_calls": 2', b'"prior_minimum_calls": NaN'),
        json.dumps(json.loads(canonical), indent=2).encode("utf-8"),
        b"\xff" + canonical[1:],
    ]
    for index, content in enumerate(variants):
        candidate = repository / "packets" / f"invalid-{index}.json"
        candidate.write_bytes(content)
        with pytest.raises(SupplementalApprovalError):
            approval.load_supplemental_live_campaign_approval(
                candidate,
                repository_root=repository,
            )


def test_packet_symlink_hardlink_special_file_and_root_escape_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    symlink = repository / "packets" / "linked.json"
    symlink.symlink_to(output)
    with pytest.raises(SupplementalApprovalError, match="symlink"):
        approval.load_supplemental_live_campaign_approval(
            symlink,
            repository_root=repository,
        )
    hardlink = repository / "packets" / "hardlinked.json"
    os.link(output, hardlink)
    with pytest.raises(SupplementalApprovalError, match="single-link"):
        approval.load_supplemental_live_campaign_approval(
            output,
            repository_root=repository,
        )
    hardlink.unlink()
    fifo = repository / "packets" / "special.json"
    os.mkfifo(fifo)
    with pytest.raises(SupplementalApprovalError, match="single-link"):
        approval.load_supplemental_live_campaign_approval(
            fifo,
            repository_root=repository,
        )
    outside = tmp_path / "outside.json"
    outside.write_bytes(output.read_bytes())
    with pytest.raises(SupplementalApprovalError, match="below repository"):
        approval.load_supplemental_live_campaign_approval(
            outside,
            repository_root=repository,
        )


def test_campaign_output_collision_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    output = repository / "packets" / "supplemental.json"
    _generate(repository, inputs, manifest_path, output)
    inputs.artifact_root.mkdir()

    with pytest.raises(SupplementalApprovalError, match="collision"):
        approval.load_supplemental_live_campaign_approval(
            output,
            repository_root=repository,
        )


def test_java_language_and_accounting_range_are_enforced_during_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    python_inputs = replace(
        inputs,
        plan=inputs.plan.model_copy(update={"language": Language.PYTHON}),
    )
    monkeypatch.setattr(
        approval,
        "load_plan_bound_inputs",
        lambda *_args, **_kwargs: python_inputs,
    )
    with pytest.raises(SupplementalApprovalError, match="Java Plan"):
        _generate(repository, python_inputs, manifest_path, repository / "packet.json")

    monkeypatch.setattr(
        approval,
        "load_plan_bound_inputs",
        lambda *_args, **_kwargs: inputs,
    )
    with pytest.raises(SupplementalApprovalError, match="minimum"):
        approval.prepare_supplemental_live_campaign_approval(
            repository_root=repository,
            approval_id="phase6-java-supplemental-test-002",
            spec_path=inputs.spec_path,
            plan_path=inputs.plan_path,
            campaign_path=inputs.artifact_root / "campaign.jsonl",
            accepted_manifest_path=manifest_path,
            prior_provider_call_minimum=4,
            prior_provider_call_maximum=3,
            output_path=repository / "packet-2.json",
            confirm_local_execution=True,
        )


def test_generator_rejects_symlink_output_parent_without_writing_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inputs, manifest_path = _approval_case(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SupplementalApprovalError, match="symlink"):
        _generate(
            repository,
            inputs,
            manifest_path,
            repository / "linked" / "packet.json",
        )

    assert list(outside.iterdir()) == []


def test_cli_help_and_confirmation_describe_pending_offline_create_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(**_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("generator must not run")

    monkeypatch.setattr(cli, "prepare_supplemental_live_campaign_approval", forbidden)
    runner = CliRunner()
    help_result = runner.invoke(app, ["prepare-phase6-supplemental-approval", "--help"])
    stopped = runner.invoke(
        app,
        [
            "prepare-phase6-supplemental-approval",
            "spec.yaml",
            "--approval-id",
            "test",
            "--plan",
            "plan.json",
            "--campaign",
            "campaign.jsonl",
            "--accepted-manifest",
            "manifest.json",
            "--prior-provider-call-min",
            "2",
            "--prior-provider-call-max",
            "3",
            "--output",
            "packet.json",
        ],
    )

    assert help_result.exit_code == 0
    assert "pending" in help_result.output
    assert "create-only" in help_result.output
    assert "not Live" in help_result.output
    assert "approval" in help_result.output
    assert stopped.exit_code == 2
    assert "files created: 0" in stopped.output
    assert called is False
