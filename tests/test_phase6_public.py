from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_phase6 import (
    COMMIT,
    T0,
    T1,
    _cross_artifact_case,
    _fixture_contracts,
    _passing_gate_commands,
    _passing_metrics,
    _phase6_plan,
    _phase6_spec,
    _successful_codex,
)
from typer.testing import CliRunner

from agentlab.campaign import (
    AdapterCleanupState,
    CampaignFinishedEvent,
    CampaignOutcome,
    CampaignRunEvent,
    CampaignRunStatus,
    CampaignStartedEvent,
    CampaignStopReason,
)
from agentlab.cli import app
from agentlab.models import (
    DiffEvidence,
    ExecutionMode,
    GateKindSummary,
    LiveEvaluationSummary,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    Provider,
    WorkspaceLifecycle,
)
from agentlab.phase6 import (
    ArtifactReference,
    HistoricalVerificationRecord,
    Language,
    LanguageStatus,
    LiveRunArtifactV1_2,
    Phase6CampaignFinishedEvent,
    Phase6CampaignOutcome,
    Phase6CampaignRunEvent,
    Phase6CampaignStartedEvent,
    Phase6ContractError,
    PrimarySuiteSource,
    ProviderCoverage,
    ProviderEvaluationStatus,
    PublicSuiteManifest,
    SourceClass,
    _canonical_jsonl_line,
    canonical_json_bytes,
    derive_primary_snapshot_binding,
    derive_public_suite_source_provenance,
    load_historical_verification,
    load_public_suite_inputs,
    validate_public_suite_inputs,
    validate_public_suite_snapshot,
)
from agentlab.phase6_public import (
    RENDERER_VERSION,
    Phase6PublicError,
    _clean_git_head,
    _eligible_run_records,
    _gate_counts_from_commands,
    _primary_context,
    _rename_no_replace,
    _scan_public_bytes,
    _source_reviewed_commit,
    _write_staging_bundle,
    publish_public_suite,
    render_public_suite,
    verify_phase6_historical,
)
from agentlab.recording import LiveRunCompletedEvent, LiveRunStartedEvent
from agentlab.workflow import (
    build_workflow_plan,
    load_workflow_spec,
    workflow_plan_bytes,
    workflow_prompt_fingerprint,
)
from agentlab.workflow_report import (
    aggregate_workflow_campaign,
    workflow_report_json_bytes,
    workflow_report_markdown,
)

HISTORICAL_REVIEWED_SPEC = "experiments/examples/workflow-ab.yaml"
COMPLETED_HISTORICAL_SPEC = (
    "experiments/phase4-live/workflow-ab-codex-live-002.yaml"
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _coverage(*, evaluated: bool) -> list[ProviderCoverage]:
    return [
        ProviderCoverage(
            provider=Provider.CODEX,
            evaluation_status=(
                ProviderEvaluationStatus.EVALUATED
                if evaluated
                else ProviderEvaluationStatus.NOT_EVALUATED
            ),
            evaluated_languages=[Language.PYTHON] if evaluated else [],
            blocker=None,
        ),
        ProviderCoverage(
            provider=Provider.ANTIGRAVITY,
            evaluation_status=ProviderEvaluationStatus.NOT_EVALUATED,
            evaluated_languages=[],
            blocker="upstream_artifact_signature_invalid",
        ),
    ]


def _manifest(
    source: PrimarySuiteSource,
    *,
    cutoff: str,
    outputs: list[str],
    evaluated: bool,
) -> PublicSuiteManifest:
    return PublicSuiteManifest(
        schema_version="1.0",
        suite_id="phase6-fake-suite",
        renderer_version=RENDERER_VERSION,
        data_cutoff_at=cutoff,
        primary_sources=[source],
        historical_sources=[],
        provider_coverage=_coverage(evaluated=evaluated),
        antigravity_blocker="upstream_artifact_signature_invalid",
        zero_call_run_publication="aggregate_only_no_run_record",
        planned_outputs=sorted(outputs),
        automatic_winner_selected=False,
        leaderboard_generated=False,
        statistical_significance_claimed=False,
    )


def _ready_suite(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "inputs"
    root.mkdir(parents=True)
    fixture, policy, acceptance = _fixture_contracts()
    spec_path, spec = _phase6_spec(root)
    plan = _phase6_plan(root, spec_path, spec, fixture, policy, acceptance)
    files = {
        "fixture.manifest.json": canonical_json_bytes(fixture),
        "fixture.acceptance.json": canonical_json_bytes(acceptance),
        "diff-policy.json": canonical_json_bytes(policy),
        "plan.json": canonical_json_bytes(plan),
    }
    for relative, content in files.items():
        _write(root / relative, content)
    source = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.PYTHON,
        expected_language_status=LanguageStatus.READY_NOT_RUN,
        spec=ArtifactReference(
            role="spec",
            path="workflow.yaml",
            sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        ),
        fixture_manifest=ArtifactReference(
            role="fixture_manifest",
            path="fixture.manifest.json",
            sha256=hashlib.sha256(files["fixture.manifest.json"]).hexdigest(),
        ),
        fixture_acceptance=ArtifactReference(
            role="fixture_acceptance",
            path="fixture.acceptance.json",
            sha256=hashlib.sha256(files["fixture.acceptance.json"]).hexdigest(),
        ),
        diff_policy=ArtifactReference(
            role="diff_policy",
            path="diff-policy.json",
            sha256=hashlib.sha256(files["diff-policy.json"]).hexdigest(),
        ),
        plan=ArtifactReference(
            role="plan",
            path="plan.json",
            sha256=hashlib.sha256(files["plan.json"]).hexdigest(),
        ),
        campaign=None,
        evidence=[],
        recordings=[],
    )
    outputs = [
        "checksums.json",
        "languages/python/report.json",
        "languages/python/report.md",
        "provider-coverage.json",
        "release-metadata.json",
        "suite.json",
        "suite.md",
    ]
    manifest = _manifest(
        source,
        cutoff=T0,
        outputs=outputs,
        evaluated=False,
    )
    manifest_path = root / "suite-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return root, manifest_path


def _evaluated_suite(
    tmp_path: Path,
    *,
    diagnostics_1_6: bool = False,
) -> tuple[Path, Path]:
    root = tmp_path / "evaluated-inputs"
    root.mkdir(parents=True)
    source, _spec, plan, campaign, artifacts, recordings = _cross_artifact_case(
        root,
        diagnostics_1_6=diagnostics_1_6,
    )
    fixture, policy, acceptance = _fixture_contracts()
    core = {
        "fixture.manifest.json": canonical_json_bytes(fixture),
        "fixture.acceptance.json": canonical_json_bytes(acceptance),
        "diff-policy.json": canonical_json_bytes(policy),
        "plan.json": canonical_json_bytes(plan),
    }
    for relative, content in core.items():
        _write(root / relative, content)
    campaign_bytes = b"".join(_canonical_jsonl_line(event) for event in campaign.events)
    _write(root / "campaign.jsonl", campaign_bytes)
    assert source.campaign is not None
    source = source.model_copy(
        update={
            "campaign": source.campaign.model_copy(
                update={"sha256": hashlib.sha256(campaign_bytes).hexdigest()}
            )
        }
    )
    recording_bytes = [
        _canonical_jsonl_line(recording.started)
        + _canonical_jsonl_line(recording.terminal)
        for recording in recordings
    ]
    artifacts = [
        artifact.model_copy(
            update={"recording_sha256": hashlib.sha256(content).hexdigest()}
        )
        for artifact, content in zip(artifacts, recording_bytes, strict=True)
    ]
    evidence_references = []
    for reference, artifact in zip(source.evidence, artifacts, strict=True):
        content = canonical_json_bytes(artifact)
        _write(root / reference.path, content)
        evidence_references.append(
            reference.model_copy(
                update={"sha256": hashlib.sha256(content).hexdigest()}
            )
        )
    recording_references = []
    for reference, content in zip(source.recordings, recording_bytes, strict=True):
        _write(root / reference.path, content)
        recording_references.append(
            reference.model_copy(
                update={"sha256": hashlib.sha256(content).hexdigest()}
            )
        )
    source = source.model_copy(
        update={
            "evidence": evidence_references,
            "recordings": recording_references,
        }
    )
    outputs = [
        "checksums.json",
        "languages/python/report.json",
        "languages/python/report.md",
        "provider-coverage.json",
        "release-metadata.json",
        "runs/python/000.json",
        "runs/python/001.json",
        "suite.json",
        "suite.md",
    ]
    manifest = _manifest(
        source,
        cutoff=T1,
        outputs=outputs,
        evaluated=True,
    )
    manifest_path = root / "suite-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return root, manifest_path


def _input_changed_suite(tmp_path: Path) -> tuple[Path, Path, int]:
    root, manifest_path = _ready_suite(tmp_path)
    manifest = PublicSuiteManifest.model_validate_json(manifest_path.read_bytes())
    source = manifest.primary_sources[0]
    assert source.plan is not None
    plan = json.loads((root / source.plan.path).read_bytes())
    runs = plan["runs"]
    plan_sha256 = hashlib.sha256((root / source.plan.path).read_bytes()).hexdigest()
    fixture = source.fixture_manifest
    acceptance = source.fixture_acceptance
    policy = source.diff_policy
    assert fixture is not None and acceptance is not None and policy is not None
    events: list[Any] = [
        Phase6CampaignStartedEvent(
            schema_version="1.2",
            sequence=0,
            event_type="campaign_started",
            experiment_id=plan["experiment_id"],
            plan_sha256=plan_sha256,
            fixture_manifest_sha256=fixture.sha256,
            fixture_acceptance_sha256=acceptance.sha256,
            diff_policy_sha256=policy.sha256,
            toolchain_fingerprint=plan["toolchain_fingerprint"],
            planned_run_count=len(runs),
            planned_provider_call_count=len(runs),
            occurred_at=T0,
        )
    ]
    for sequence, run in enumerate(runs, start=1):
        events.append(
            Phase6CampaignRunEvent(
                schema_version="1.2",
                sequence=sequence,
                event_type="run_state",
                run_id=run["run_id"],
                task_id=run["task_id"],
                workflow=run["workflow"],
                repetition_index=run["repetition_index"],
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
        )
    events.append(
        Phase6CampaignFinishedEvent(
            schema_version="1.2",
            sequence=len(events),
            event_type="campaign_finished",
            experiment_id=plan["experiment_id"],
            stop_reason=CampaignStopReason.INPUT_CHANGED,
            attempted_run_count=0,
            provider_call_count=0,
            provider_call_count_unknown_runs=0,
            counted_failure_count=0,
            retry_count=0,
            occurred_at=T1,
        )
    )
    campaign_bytes = b"".join(_canonical_jsonl_line(event) for event in events)
    _write(root / "campaign.jsonl", campaign_bytes)
    campaign_reference = ArtifactReference(
        role="campaign",
        path="campaign.jsonl",
        sha256=hashlib.sha256(campaign_bytes).hexdigest(),
    )
    blocked_source = source.model_copy(
        update={
            "expected_language_status": LanguageStatus.BLOCKED,
            "blocker": "input_changed",
            "campaign": campaign_reference,
        }
    )
    updated = manifest.model_copy(
        update={
            "data_cutoff_at": datetime.fromisoformat(T1.replace("Z", "+00:00")),
            "primary_sources": [blocked_source],
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(updated))
    return root, manifest_path, len(runs)


def _validated(root: Path, manifest_path: Path) -> Any:
    return validate_public_suite_inputs(
        load_public_suite_inputs(manifest_path, root=root)
    )


def test_public_suite_facades_validate_caller_owned_bytes_only(tmp_path: Path) -> None:
    root, manifest_path = _evaluated_suite(tmp_path)
    manifest = PublicSuiteManifest.model_validate_json(manifest_path.read_bytes())
    loaded = load_public_suite_inputs(manifest_path, root=root)
    (root / "campaign.jsonl").unlink()

    validated = validate_public_suite_snapshot(manifest, loaded.bytes_by_path)
    source = manifest.primary_sources[0]
    binding = derive_primary_snapshot_binding(source, validated.loaded.bytes_by_path)

    assert binding.experiment_id == "workflow-ab-smoke"
    assert binding.reviewed_commit == COMMIT
    assert len(binding.planned_run_ids) == 2
    assert binding.complete_pairs
    provenance = derive_public_suite_source_provenance(validated)
    assert len(provenance) == 1
    assert provenance[0].role == "primary"
    assert provenance[0].experiment_id == "workflow-ab-smoke"
    assert provenance[0].reviewed_commit == COMMIT


def test_ready_not_run_render_is_deterministic_and_ignores_unlisted_secret(
    tmp_path: Path,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    (root / "unlisted-broken-secret.json").write_text(
        '{"prompt":"SECRET-SENTINEL", broken',
        encoding="utf-8",
    )
    validated = _validated(root, manifest_path)

    first = render_public_suite(validated)
    second = render_public_suite(validated)

    assert first.files == second.files
    assert first.external_anchor_bytes == second.external_anchor_bytes
    report = json.loads(first.files["languages/python/report.json"])
    assert report["status"] == "ready_not_run"
    assert report["scheduled_runs"] == 0
    assert b"SECRET-SENTINEL" not in b"".join(first.files.values())
    assert b"upstream_artifact_signature_invalid" in first.files["suite.json"]
    assert b"winner" in first.files["suite.md"].lower()


def test_evaluated_suite_emits_plan_indexed_allowlisted_run_records(
    tmp_path: Path,
) -> None:
    root, manifest_path = _evaluated_suite(tmp_path)
    rendered = render_public_suite(_validated(root, manifest_path))

    assert "runs/python/000.json" in rendered.files
    assert "runs/python/001.json" in rendered.files
    record = json.loads(rendered.files["runs/python/000.json"])
    assert record["provider_call_count"] == 1
    assert record["run_metrics_available"] is True
    assert "prompt" not in record
    assert "raw_provider_output" not in record
    assert "unified_diff" not in record
    report = json.loads(rendered.files["languages/python/report.json"])
    assert report["status"] == "evaluated"
    assert report["complete_pair_count"] == 1


def test_public_suite_accepts_phase6_diagnostic_schema(tmp_path: Path) -> None:
    root, manifest_path = _evaluated_suite(tmp_path, diagnostics_1_6=True)

    rendered = render_public_suite(_validated(root, manifest_path))

    assert "runs/python/000.json" in rendered.files
    report = json.loads(rendered.files["languages/python/report.json"])
    assert report["status"] == "evaluated"
    assert report["complete_pair_count"] == 1


@pytest.mark.parametrize(
    (
        "failure_kind",
        "command_change",
        "diff_change",
        "workspace_lifecycle",
        "expected_gate_counts",
    ),
    [
        (
            "evidence_error",
            None,
            {
                "added_lines": None,
                "deleted_lines": None,
                "line_counts_complete": False,
                "collection_error": "synthetic post-Gate collection failure",
            },
            "removed",
            (1, 1, 0, 0, 0),
        ),
        (
            "gate_harness_error",
            {
                "status": "timed_out",
                "return_code": None,
                "termination": {
                    "reason": "timeout",
                    "sigterm_sent": True,
                    "sigkill_sent": False,
                    "process_group_cleared": True,
                    "error": None,
                },
            },
            None,
            "removed",
            (0, 1, 0, 0, 0),
        ),
        (
            "process_cleanup_error",
            {
                "termination": {
                    "reason": "residual_process",
                    "sigterm_sent": True,
                    "sigkill_sent": True,
                    "process_group_cleared": False,
                    "error": "synthetic cleanup failure",
                },
            },
            None,
            "cleanup_failed",
            (1, 1, 0, 0, 0),
        ),
    ],
)
def test_public_gate_counts_survive_post_gate_harness_failures(
    tmp_path: Path,
    failure_kind: str,
    command_change: dict[str, Any] | None,
    diff_change: dict[str, Any] | None,
    workspace_lifecycle: str,
    expected_gate_counts: tuple[int, int, int, int, int],
) -> None:
    root, manifest_path = _evaluated_suite(tmp_path)
    validated = _validated(root, manifest_path)
    source = validated.loaded.manifest.primary_sources[0]
    context = _primary_context(validated, source)
    assert context.plan is not None
    run = context.plan.runs[0]
    evidence_reference, artifact = context.evidence[run.run_id]
    raw = json.loads(canonical_json_bytes(artifact))
    raw.update(
        {
            "overall_status": "harness_error",
            "failure_kind": failure_kind,
            "metrics": None,
            "workspace_lifecycle": workspace_lifecycle,
        }
    )
    if command_change is not None:
        raw["gate_commands"][0].update(command_change)
    if diff_change is not None:
        raw["diff"].update(diff_change)
    failed = LiveRunArtifactV1_2.model_validate(raw)
    changed_evidence = dict(context.evidence)
    changed_evidence[run.run_id] = (evidence_reference, failed)
    changed_context = replace(context, evidence=changed_evidence)
    records = dict(_eligible_run_records(validated, changed_context))

    assert failed.metrics is None
    assert _gate_counts_from_commands(failed) == expected_gate_counts
    assert records[0].run_metrics_available is False
    assert (
        records[0].acceptance_passed,
        records[0].acceptance_total,
        records[0].regression_failures,
        records[0].lint_errors,
        records[0].typecheck_errors,
    ) == expected_gate_counts


def test_input_changed_is_zero_call_aggregate_only(tmp_path: Path) -> None:
    root, manifest_path, run_count = _input_changed_suite(tmp_path)
    rendered = render_public_suite(_validated(root, manifest_path))

    assert not any(path.startswith("runs/") for path in rendered.files)
    report = json.loads(rendered.files["languages/python/report.json"])
    assert report["status"] == "blocked"
    assert report["zero_call_runs"] == run_count
    assert report["gate_not_executed_reason"] == {"input_changed": run_count}
    assert report["estimability"] == "not_estimable"


def test_not_ready_language_is_zero_count_and_not_estimable(tmp_path: Path) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    manifest = PublicSuiteManifest.model_validate_json(manifest_path.read_bytes())
    not_ready = PrimarySuiteSource(
        source_class=SourceClass.PRIMARY,
        language=Language.TYPESCRIPT,
        expected_language_status=LanguageStatus.NOT_READY,
    )
    outputs = sorted(
        [
            *manifest.planned_outputs,
            "languages/typescript/report.json",
            "languages/typescript/report.md",
        ]
    )
    updated = manifest.model_copy(
        update={
            "primary_sources": [manifest.primary_sources[0], not_ready],
            "planned_outputs": outputs,
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(updated))

    rendered = render_public_suite(_validated(root, manifest_path))
    report = json.loads(rendered.files["languages/typescript/report.json"])
    assert report["status"] == "not_ready"
    assert report["scheduled_runs"] == 0
    assert report["complete_pair_count"] == 0
    assert report["estimability"] == "not_estimable"


def test_renderer_rejects_planned_output_drift(tmp_path: Path) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    raw = json.loads(manifest_path.read_bytes())
    raw["planned_outputs"].append("unexpected.json")
    raw["planned_outputs"].sort()
    manifest_path.write_bytes(canonical_json_bytes(raw))

    with pytest.raises(Phase6PublicError, match="planned_outputs"):
        render_public_suite(_validated(root, manifest_path))


def test_renderer_rejects_version_and_render_time_input_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    raw = json.loads(manifest_path.read_bytes())
    raw["renderer_version"] = "future-renderer"
    manifest_path.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(Phase6PublicError, match="renderer version"):
        render_public_suite(_validated(root, manifest_path))

    root, manifest_path = _ready_suite(tmp_path / "drift")
    validated = _validated(root, manifest_path)
    plan_path = root / "plan.json"
    original_reload = __import__(
        "agentlab.phase6_public",
        fromlist=["_strict_reload_generated_json"],
    )._strict_reload_generated_json
    changed = False

    def replace_input(path: str, content: bytes) -> None:
        nonlocal changed
        original_reload(path, content)
        if not changed:
            replacement = root / "replacement-plan.json"
            replacement.write_bytes(plan_path.read_bytes())
            replacement.replace(plan_path)
            changed = True

    monkeypatch.setattr(
        "agentlab.phase6_public._strict_reload_generated_json",
        replace_input,
    )
    with pytest.raises(Phase6ContractError, match="changed"):
        render_public_suite(validated)


def test_staging_rejects_hash_size_markdown_and_leak_tampering(
    tmp_path: Path,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    rendered = render_public_suite(_validated(root, manifest_path))
    changed_files = dict(rendered.files)
    changed_files["suite.md"] += b"tamper\n"
    tampered = replace(rendered, files=changed_files)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(Phase6PublicError, match="checksum or size"):
        _write_staging_bundle(staging, tampered)
    assert list(staging.iterdir()) == []
    with pytest.raises(Phase6PublicError, match="leak scan"):
        _scan_public_bytes({"suite.md": b"Authorization: Bearer SECRET"}, b"")


def test_publication_revalidates_actual_staging_bytes_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "staging-race"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    module = __import__(
        "agentlab.phase6_public",
        fromlist=["_validate_staging_bundle"],
    )
    original_validate = module._validate_staging_bundle
    calls = 0

    def change_staging_before_final_check(
        staging: Path,
        rendered: Any,
    ) -> tuple[int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            (staging / "suite.md").write_bytes(b"tampered staged Markdown\n")
        return original_validate(staging, rendered)

    monkeypatch.setattr(
        "agentlab.phase6_public._validate_staging_bundle",
        change_staging_before_final_check,
    )
    with pytest.raises(Phase6PublicError, match=r"checksum or size|bytes differ"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )

    assert not destination.exists()
    assert not anchor.exists()
    assert not list(publish_parent.glob(".bundle-staging-*"))


def test_identical_inputs_are_independent_of_time_environment_and_evidence_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _evaluated_suite(tmp_path)
    first = render_public_suite(_validated(root, manifest_path))
    manifest = PublicSuiteManifest.model_validate_json(manifest_path.read_bytes())
    source = manifest.primary_sources[0]
    reversed_source = source.model_copy(
        update={
            "evidence": list(reversed(source.evidence)),
            "recordings": list(reversed(source.recordings)),
        }
    )
    manifest_path.write_bytes(
        canonical_json_bytes(
            manifest.model_copy(update={"primary_sources": [reversed_source]})
        )
    )
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setenv("CODEX_HOME", "/private/SECRET-CODEX-HOME")
    second = render_public_suite(_validated(root, manifest_path))

    assert first.files == second.files
    assert first.external_anchor_bytes == second.external_anchor_bytes
    combined = b"".join([*second.files.values(), second.external_anchor_bytes])
    assert b"SECRET-CODEX-HOME" not in combined


def test_publication_is_create_only_and_checksum_anchor_is_external(
    tmp_path: Path,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "publication"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "bundle.checksums.sha256.json"

    outcome = publish_public_suite(
        manifest_path=manifest_path,
        root=root,
        destination=destination,
        external_anchor_path=anchor,
        confirm_publication=True,
    )

    assert outcome.published_file_count == 7
    assert destination.is_dir()
    assert anchor.is_file()
    anchor_json = json.loads(anchor.read_bytes())
    assert anchor_json["checksum_manifest_sha256"] == hashlib.sha256(
        (destination / "checksums.json").read_bytes()
    ).hexdigest()
    checksums = json.loads((destination / "checksums.json").read_bytes())
    paths = {entry["path"] for entry in checksums["entries"]}
    assert "release-metadata.json" in paths
    assert "checksums.json" not in paths
    with pytest.raises(Phase6PublicError, match="already exists"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )


@pytest.mark.parametrize("existing_kind", ["file", "directory", "symlink"])
def test_existing_destination_kinds_are_never_replaced(
    tmp_path: Path,
    existing_kind: str,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "existing"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    target = publish_parent / "symlink-target"
    if existing_kind == "file":
        destination.write_bytes(b"keep-file")
    elif existing_kind == "directory":
        destination.mkdir()
        (destination / "keep").write_bytes(b"keep-directory")
    else:
        target.write_bytes(b"keep-target")
        destination.symlink_to(target)

    with pytest.raises(Phase6PublicError, match="already exists"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )

    if existing_kind == "file":
        assert destination.read_bytes() == b"keep-file"
    elif existing_kind == "directory":
        assert (destination / "keep").read_bytes() == b"keep-directory"
    else:
        assert destination.is_symlink()
        assert target.read_bytes() == b"keep-target"
    assert not anchor.exists()


def test_atomic_no_replace_preserves_empty_directory_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "race"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    raced_identity: tuple[int, int] | None = None

    def race(source: Path, target: Path) -> None:
        nonlocal raced_identity
        if target == destination and not target.exists():
            target.mkdir()
            metadata = target.lstat()
            raced_identity = (metadata.st_dev, metadata.st_ino)
        _rename_no_replace(source, target)

    monkeypatch.setattr("agentlab.phase6_public._rename_no_replace", race)
    with pytest.raises(Phase6PublicError, match="already exists"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )
    metadata = destination.lstat()
    assert (metadata.st_dev, metadata.st_ino) == raced_identity
    assert list(destination.iterdir()) == []
    assert not anchor.exists()


def test_anchor_race_rolls_back_only_owned_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "anchor-race"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    calls = 0

    def race(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            anchor.write_bytes(b"raced-anchor")
        _rename_no_replace(source, target)

    monkeypatch.setattr("agentlab.phase6_public._rename_no_replace", race)
    with pytest.raises(Phase6PublicError, match="already exists"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )
    assert not destination.exists()
    assert anchor.read_bytes() == b"raced-anchor"


def test_existing_publish_lock_is_preserved_and_staging_is_cleaned(
    tmp_path: Path,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "locked"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    lock = publish_parent / ".bundle.publish.lock"
    lock.write_bytes(b"other-publisher")

    with pytest.raises(Phase6PublicError, match="lock is held"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )

    assert lock.read_bytes() == b"other-publisher"
    assert not destination.exists()
    assert not anchor.exists()
    assert not list(publish_parent.glob(".bundle-staging-*"))
    assert not list(publish_parent.glob(".phase6-anchor-*"))


def test_parent_fsync_failure_rolls_back_bundle_and_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / "fsync-failure"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    module = __import__(
        "agentlab.phase6_public",
        fromlist=["_fsync_directory"],
    )
    original_fsync = module._fsync_directory
    failed = False

    def fail_after_both(path: Path) -> None:
        nonlocal failed
        if not failed and destination.exists() and anchor.exists():
            failed = True
            raise OSError("synthetic parent fsync failure")
        original_fsync(path)

    monkeypatch.setattr("agentlab.phase6_public._fsync_directory", fail_after_both)
    with pytest.raises(Phase6PublicError, match="failed safely"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )
    assert not destination.exists()
    assert not anchor.exists()
    assert not list(publish_parent.glob(".bundle-staging-*"))


@pytest.mark.parametrize("failed_target", ["bundle", "anchor"])
def test_post_rename_identity_failure_rolls_back_all_owned_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_target: str,
) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    publish_parent = tmp_path / f"identity-{failed_target}"
    publish_parent.mkdir()
    destination = publish_parent / "bundle"
    anchor = publish_parent / "anchor.json"
    module = __import__(
        "agentlab.phase6_public",
        fromlist=["_published_identity"],
    )
    original_identity = module._published_identity
    failed = False

    def fail_after_rename(path: Path, directory: bool) -> tuple[int, int, int]:
        nonlocal failed
        target = destination if failed_target == "bundle" else anchor
        if path == target and path.exists() and not failed:
            failed = True
            raise OSError("synthetic post-rename identity failure")
        return original_identity(path, directory)

    monkeypatch.setattr(
        "agentlab.phase6_public._published_identity",
        fail_after_rename,
    )
    with pytest.raises(Phase6PublicError, match="failed safely"):
        publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=anchor,
            confirm_publication=True,
        )

    assert failed
    assert not destination.exists()
    assert not anchor.exists()
    assert not list(publish_parent.glob(".bundle-staging-*"))
    assert not list(publish_parent.glob(".phase6-anchor-*"))


def test_no_replace_fails_closed_on_unsupported_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    monkeypatch.setattr(sys, "platform", "unsupported")

    with pytest.raises(Phase6PublicError, match="unsupported"):
        _rename_no_replace(source, destination)

    assert source.is_dir()
    assert not destination.exists()


def test_confirmation_flags_have_zero_side_effects(tmp_path: Path) -> None:
    root, manifest_path = _ready_suite(tmp_path)
    destination = tmp_path / "never-created" / "bundle"
    anchor = tmp_path / "never-created" / "anchor.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "publish-phase6-public-suite",
            str(manifest_path),
            "--root",
            str(root),
            "--destination",
            str(destination),
            "--external-anchor",
            str(anchor),
        ],
    )

    assert result.exit_code == 2
    assert "subprocesses" in result.output
    assert not destination.parent.exists()


def _historical_fixture(root: Path) -> tuple[str, str, str, str]:
    source_spec = Path(HISTORICAL_REVIEWED_SPEC)
    plan = build_workflow_plan(source_spec)
    _write(root.parent / HISTORICAL_REVIEWED_SPEC, source_spec.read_bytes())
    plan_bytes = workflow_plan_bytes(plan)
    plan_path = "plan.json"
    campaign_path = "campaign.jsonl"
    report_json_path = "report.json"
    report_markdown_path = "report.md"
    _write(root / plan_path, plan_bytes)
    timestamp = datetime(2026, 7, 30, 1, 2, 3, tzinfo=UTC)
    events: list[Any] = [
        CampaignStartedEvent(
            schema_version="1.1",
            sequence=0,
            event_type="campaign_started",
            experiment_id=plan.experiment_id,
            plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            planned_run_count=plan.planned_run_count,
            planned_provider_call_count=plan.planned_provider_call_count,
            occurred_at=timestamp,
        )
    ]
    for sequence, run in enumerate(plan.runs, start=1):
        events.append(
            CampaignRunEvent(
                schema_version="1.1",
                sequence=sequence,
                event_type="run_state",
                run_id=run.run_id,
                task_id=run.task_id,
                workflow=run.workflow,
                repetition_index=run.repetition_index,
                status=CampaignRunStatus.NOT_RUN,
                outcome=CampaignOutcome.STOP_CONDITION,
                stop_reason=CampaignStopReason.FAIL_FAST,
                provider_call_count=0,
                retry_count=0,
                live_failure_kind=None,
                adapter_cleanup_state=AdapterCleanupState.NOT_APPLICABLE,
                occurred_at=timestamp,
            )
        )
    events.append(
        CampaignFinishedEvent(
            schema_version="1.1",
            sequence=len(events),
            event_type="campaign_finished",
            experiment_id=plan.experiment_id,
            stop_reason=CampaignStopReason.FAIL_FAST,
            attempted_run_count=0,
            provider_call_count=0,
            provider_call_count_unknown_runs=0,
            retry_count=0,
            occurred_at=timestamp,
        )
    )
    campaign_bytes = b"".join(
        (
            json.dumps(
                event.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for event in events
    )
    _write(root / campaign_path, campaign_bytes)
    report = aggregate_workflow_campaign(
        root / f"{plan.experiment_id}.yaml",
        root / plan_path,
        root / campaign_path,
    )
    _write(root / report_json_path, workflow_report_json_bytes(report))
    _write(
        root / report_markdown_path,
        workflow_report_markdown(report).encode("utf-8"),
    )
    return plan_path, campaign_path, report_json_path, report_markdown_path


def _completed_historical_fixture(
    repository: Path,
) -> tuple[Path, str, str, str, str, Any]:
    reviewed_spec = repository / COMPLETED_HISTORICAL_SPEC
    reviewed_spec.parent.mkdir(parents=True)
    source = yaml.safe_load(
        Path(HISTORICAL_REVIEWED_SPEC).read_text(encoding="utf-8")
    )
    source["experiment_id"] = "workflow-ab-codex-live-002"
    source["repetitions"] = 1
    source["artifacts"]["root"] = ".artifacts/workflow-ab-codex-live-002"
    reviewed_spec.write_text(
        yaml.safe_dump(source, sort_keys=False),
        encoding="utf-8",
    )
    _write(
        reviewed_spec.parent / "prompts/codex-live-smoke.md",
        Path("experiments/examples/prompts/codex-live-smoke.md").read_bytes(),
    )
    shutil.copytree(
        Path("experiments/examples/fixtures/codex-live-smoke"),
        reviewed_spec.parent / "fixtures/codex-live-smoke",
    )

    historical_root = reviewed_spec.parent
    artifact_root = historical_root / ".artifacts/workflow-ab-codex-live-002"
    plan = build_workflow_plan(reviewed_spec)
    plan_bytes = workflow_plan_bytes(plan)
    plan_path = ".artifacts/workflow-ab-codex-live-002/plan.json"
    campaign_path = ".artifacts/workflow-ab-codex-live-002/campaign.jsonl"
    report_json_path = ".artifacts/workflow-ab-codex-live-002/report.json"
    report_markdown_path = ".artifacts/workflow-ab-codex-live-002/report.md"
    _write(historical_root / plan_path, plan_bytes)

    spec = load_workflow_spec(reviewed_spec).spec
    timestamp = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    commands = _passing_gate_commands()
    metrics = _passing_metrics()
    diff = DiffEvidence(
        changed_files=[],
        binary_files=[],
        added_lines=0,
        deleted_lines=0,
        unified_diff="",
        diff_truncated=False,
        line_counts_complete=True,
        collection_error=None,
    )
    gate_summary = GateKindSummary(
        command_count=1,
        passed_count=1,
        failed_count=0,
    )
    evaluation = LiveEvaluationSummary(
        acceptance=gate_summary,
        regression=gate_summary,
        lint=gate_summary,
        typecheck=gate_summary,
        all_commands_completed_normally=True,
        evaluation_duration_ms=0,
        changed_files=[],
        added_lines=0,
        deleted_lines=0,
        diff_line_counts_complete=True,
        workspace_lifecycle=WorkspaceLifecycle.REMOVED,
    )
    campaign_events: list[Any] = [
        CampaignStartedEvent(
            schema_version="1.1",
            sequence=0,
            event_type="campaign_started",
            experiment_id=plan.experiment_id,
            plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            planned_run_count=plan.planned_run_count,
            planned_provider_call_count=plan.planned_provider_call_count,
            occurred_at=timestamp,
        )
    ]
    sequence = 1
    for run in plan.runs:
        prompt_sha256, prompt_bytes = workflow_prompt_fingerprint(
            plan,
            run.workflow,
        )
        codex = _successful_codex(
            model=plan.model,
            reasoning_effort=plan.reasoning_effort,
            prompt_bytes=prompt_bytes,
        )
        started = LiveRunStartedEvent(
            schema_version="1.1",
            sequence=0,
            event_type="run_started",
            run_id=run.run_id,
            experiment_id=plan.experiment_id,
            task_id=run.task_id,
            workflow=run.workflow,
            provider=Provider.CODEX,
            repetition_index=run.repetition_index,
            execution_mode=ExecutionMode.LIVE,
            occurred_at=timestamp,
            prompt_sha256=prompt_sha256,
            prompt_bytes=prompt_bytes,
            prompt_redacted=True,
            requested_model=plan.model,
            requested_reasoning_effort=plan.reasoning_effort,
            cli_version=codex.cli_version,
        )
        completed = LiveRunCompletedEvent(
            schema_version="1.1",
            sequence=1,
            event_type="run_completed",
            run_id=run.run_id,
            experiment_id=plan.experiment_id,
            occurred_at=timestamp,
            metrics=metrics,
            codex=codex,
            evaluation=evaluation,
        )
        recording_bytes = (
            _canonical_jsonl_line(started) + _canonical_jsonl_line(completed)
        )
        artifact = LiveRunArtifact(
            schema_version="1.1",
            run_id=run.run_id,
            experiment_id=plan.experiment_id,
            task_id=run.task_id,
            repetition_index=run.repetition_index,
            workflow=run.workflow,
            provider=Provider.CODEX,
            execution_mode=ExecutionMode.LIVE,
            overall_status=LiveOverallStatus.PASSED,
            failure_kind=LiveFailureKind.NONE,
            started_at=timestamp,
            completed_at=timestamp,
            spec_sha256=hashlib.sha256(reviewed_spec.read_bytes()).hexdigest(),
            fixture_sha256=plan.fixture_sha256,
            prompt_sha256=prompt_sha256,
            prompt_bytes=prompt_bytes,
            prompt_redacted=True,
            runner=spec.runner,
            codex=codex,
            gate_commands=commands,
            diff=diff,
            metrics=metrics,
            evaluation_duration_ms=0,
            workspace_lifecycle=WorkspaceLifecycle.REMOVED,
            recording_sha256=hashlib.sha256(recording_bytes).hexdigest(),
            raw_provider_output_persisted=False,
        )
        _write(historical_root / run.recording_path, recording_bytes)
        _write(
            historical_root / run.evidence_path,
            canonical_json_bytes(artifact),
        )
        campaign_events.extend(
            [
                CampaignRunEvent(
                    schema_version="1.1",
                    sequence=sequence,
                    event_type="run_state",
                    run_id=run.run_id,
                    task_id=run.task_id,
                    workflow=run.workflow,
                    repetition_index=run.repetition_index,
                    status=CampaignRunStatus.STARTED,
                    outcome=None,
                    stop_reason=None,
                    provider_call_count=None,
                    retry_count=0,
                    live_failure_kind=None,
                    adapter_cleanup_state=AdapterCleanupState.NOT_APPLICABLE,
                    occurred_at=timestamp,
                ),
                CampaignRunEvent(
                    schema_version="1.1",
                    sequence=sequence + 1,
                    event_type="run_state",
                    run_id=run.run_id,
                    task_id=run.task_id,
                    workflow=run.workflow,
                    repetition_index=run.repetition_index,
                    status=CampaignRunStatus.COMPLETED,
                    outcome=CampaignOutcome.SUCCESS,
                    stop_reason=None,
                    provider_call_count=1,
                    retry_count=0,
                    live_failure_kind=LiveFailureKind.NONE,
                    adapter_cleanup_state=AdapterCleanupState.CLEARED,
                    occurred_at=timestamp,
                ),
            ]
        )
        sequence += 2
    campaign_events.append(
        CampaignFinishedEvent(
            schema_version="1.1",
            sequence=sequence,
            event_type="campaign_finished",
            experiment_id=plan.experiment_id,
            stop_reason=CampaignStopReason.NONE,
            attempted_run_count=len(plan.runs),
            provider_call_count=len(plan.runs),
            provider_call_count_unknown_runs=0,
            retry_count=0,
            occurred_at=timestamp,
        )
    )
    _write(
        historical_root / campaign_path,
        b"".join(_canonical_jsonl_line(event) for event in campaign_events),
    )
    report = aggregate_workflow_campaign(
        reviewed_spec,
        historical_root / plan_path,
        historical_root / campaign_path,
    )
    _write(
        historical_root / report_json_path,
        workflow_report_json_bytes(report),
    )
    _write(
        historical_root / report_markdown_path,
        workflow_report_markdown(report).encode("utf-8"),
    )
    assert artifact_root.is_dir()
    return (
        historical_root,
        plan_path,
        campaign_path,
        report_json_path,
        report_markdown_path,
        plan,
    )


def test_historical_verification_uses_saved_bytes_and_create_only_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "historical"
    root.mkdir()
    plan, campaign, report_json, report_markdown = _historical_fixture(root)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    monkeypatch.setattr("agentlab.phase6_public._clean_git_head", lambda _path: COMMIT)
    monkeypatch.setattr(
        "agentlab.phase6_public._source_reviewed_commit",
        lambda _repository, _path, _bytes, _plan: COMMIT,
    )

    result = verify_phase6_historical(
        repository=tmp_path,
        historical_root=root,
        reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
        plan_path=plan,
        campaign_path=campaign,
        report_json_path=report_json,
        report_markdown_path=report_markdown,
        output_path=output,
        language=Language.PYTHON,
        confirm_local_execution=True,
        now=lambda: datetime(2026, 7, 31, tzinfo=UTC),
    )

    loaded = HistoricalVerificationRecord.model_validate_json(output.read_bytes())
    assert loaded == result.record
    assert loaded.toolchain_version_status == "unknown"
    assert loaded.artifact_regenerated is False
    assert loaded.campaign_reexecuted is False
    with pytest.raises(Phase6PublicError, match="already exists"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )


def test_historical_verification_reaggregates_completed_nested_spec_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (
        historical_root,
        plan_path,
        campaign_path,
        report_json_path,
        report_markdown_path,
        plan,
    ) = _completed_historical_fixture(repository)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    real_aggregate = aggregate_workflow_campaign
    mirrored_artifacts: list[Path] = []
    mirrored_spec_parents: list[Path] = []

    def aggregate_from_nested_mirror(
        spec_path: Path,
        saved_plan_path: Path,
        saved_campaign_path: Path,
    ) -> Any:
        assert spec_path != repository / COMPLETED_HISTORICAL_SPEC
        mirrored_spec_parents.append(spec_path.parent)
        for run in plan.runs:
            evidence = spec_path.parent / run.evidence_path
            recording = spec_path.parent / run.recording_path
            assert evidence.is_file()
            assert recording.is_file()
            mirrored_artifacts.extend([evidence, recording])
        return real_aggregate(spec_path, saved_plan_path, saved_campaign_path)

    execution_calls = {
        "campaign": 0,
        "provider": 0,
        "external_process": 0,
    }

    def forbid_campaign(*_args: object, **_kwargs: object) -> None:
        execution_calls["campaign"] += 1
        raise AssertionError("Historical verification must not execute a Campaign")

    def forbid_provider(*_args: object, **_kwargs: object) -> None:
        execution_calls["provider"] += 1
        raise AssertionError("Historical verification must not execute a Provider")

    def forbid_process(*_args: object, **_kwargs: object) -> None:
        execution_calls["external_process"] += 1
        raise AssertionError("Historical verification must not execute a process")

    monkeypatch.setattr(
        "agentlab.phase6_public.aggregate_workflow_campaign",
        aggregate_from_nested_mirror,
    )
    monkeypatch.setattr("agentlab.campaign.run_workflow_campaign", forbid_campaign)
    monkeypatch.setattr("agentlab.live.run_live_codex", forbid_provider)
    monkeypatch.setattr("subprocess.Popen", forbid_process)
    monkeypatch.setattr("agentlab.phase6_public._clean_git_head", lambda _path: COMMIT)
    monkeypatch.setattr(
        "agentlab.phase6_public._source_reviewed_commit",
        lambda _repository, _path, _bytes, _plan: COMMIT,
    )

    result = verify_phase6_historical(
        repository=repository,
        historical_root=historical_root,
        reviewed_spec_path=COMPLETED_HISTORICAL_SPEC,
        plan_path=plan_path,
        campaign_path=campaign_path,
        report_json_path=report_json_path,
        report_markdown_path=report_markdown_path,
        output_path=output,
        language=Language.PYTHON,
        confirm_local_execution=True,
        now=lambda: datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert len(mirrored_artifacts) == len(plan.runs) * 2
    assert len(mirrored_spec_parents) == 1
    assert mirrored_spec_parents[0].parts[-2:] == ("experiments", "phase4-live")
    assert all(path.is_relative_to(mirrored_spec_parents[0]) for path in mirrored_artifacts)
    assert execution_calls == {
        "campaign": 0,
        "provider": 0,
        "external_process": 0,
    }
    assert load_historical_verification(output) == result.record
    assert output.read_bytes() == canonical_json_bytes(result.record)
    assert result.record.strict_schema_validation_passed is True
    assert result.record.cross_artifact_validation_passed is True
    assert result.record.artifact_regenerated is False
    assert result.record.campaign_reexecuted is False
    assert list(output_parent.iterdir()) == [output]


def test_historical_source_commit_uses_explicit_repository_spec_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "saved-campaign-002"
    root.mkdir()
    plan_path, _campaign, _report_json, _report_markdown = _historical_fixture(root)
    plan = __import__(
        "agentlab.workflow",
        fromlist=["WorkflowPlan"],
    ).WorkflowPlan.model_validate_json((root / plan_path).read_bytes())
    spec_bytes = (tmp_path / HISTORICAL_REVIEWED_SPEC).read_bytes()
    calls: list[tuple[str, ...]] = []

    def saved_git(_repository: Path, *arguments: str) -> bytes:
        calls.append(arguments)
        if arguments[:4] == ("log", "-1", "--format=%H", "--"):
            return f"{COMMIT}\n".encode("ascii")
        if arguments[:1] == ("show",):
            return spec_bytes
        raise AssertionError(f"unexpected Git argv: {arguments!r}")

    monkeypatch.setattr("agentlab.phase6_public._git", saved_git)
    source_commit = _source_reviewed_commit(
        tmp_path,
        HISTORICAL_REVIEWED_SPEC,
        spec_bytes,
        plan,
    )

    assert source_commit == COMMIT
    assert calls == [
        ("log", "-1", "--format=%H", "--", HISTORICAL_REVIEWED_SPEC),
        ("show", f"{COMMIT}:{HISTORICAL_REVIEWED_SPEC}"),
    ]
    assert not (root / f"{plan.experiment_id}.yaml").exists()


def test_historical_reviewed_spec_replacement_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "historical"
    root.mkdir()
    plan, campaign, report_json, report_markdown = _historical_fixture(root)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    spec_path = tmp_path / HISTORICAL_REVIEWED_SPEC

    def replace_reviewed_spec(_repository: Path) -> str:
        replacement = spec_path.with_name("replacement-workflow-ab.yaml")
        replacement.write_bytes(spec_path.read_bytes())
        replacement.replace(spec_path)
        return COMMIT

    monkeypatch.setattr(
        "agentlab.phase6_public._clean_git_head",
        replace_reviewed_spec,
    )
    monkeypatch.setattr(
        "agentlab.phase6_public._source_reviewed_commit",
        lambda _repository, _path, _bytes, _plan: COMMIT,
    )
    with pytest.raises(Phase6PublicError, match=r"reviewed Spec.*changed"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )

    assert not output.exists()


def test_historical_cli_requires_explicit_reviewed_spec_option() -> None:
    result = CliRunner().invoke(app, ["verify-phase6-historical", "--help"])

    assert result.exit_code == 0
    assert "--reviewed-spec" in result.output


def test_historical_rejects_absolute_path_symlink_and_missing_confirmation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "historical"
    root.mkdir()
    plan, campaign, report_json, report_markdown = _historical_fixture(root)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    with pytest.raises(Phase6PublicError, match="explicit confirmation"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=False,
        )
    assert not output.exists()
    with pytest.raises(Phase6PublicError, match="relative POSIX"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=str((root / plan).resolve()),
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )


def test_historical_rejects_json_markdown_and_hardlink_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "historical"
    root.mkdir()
    plan, campaign, report_json, report_markdown = _historical_fixture(root)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    monkeypatch.setattr("agentlab.phase6_public._clean_git_head", lambda _path: COMMIT)
    monkeypatch.setattr(
        "agentlab.phase6_public._source_reviewed_commit",
        lambda _repository, _path, _bytes, _plan: COMMIT,
    )

    original_markdown = (root / report_markdown).read_bytes()
    (root / report_markdown).write_bytes(original_markdown + b"tamper\n")
    with pytest.raises(Phase6PublicError, match="Markdown differs"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )
    (root / report_markdown).write_bytes(original_markdown)
    report = json.loads((root / report_json).read_bytes())
    report["pairing"]["complete_pair_count"] = 1
    report["pairing"]["status"] = "estimable"
    changed_report = __import__(
        "agentlab.workflow_report",
        fromlist=["WorkflowReport"],
    ).WorkflowReport.model_validate(report)
    (root / report_json).write_bytes(workflow_report_json_bytes(changed_report))
    (root / report_markdown).write_bytes(
        workflow_report_markdown(changed_report).encode("utf-8")
    )
    with pytest.raises(Phase6PublicError, match="offline aggregation"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )

    external = tmp_path / "external-plan.json"
    external.write_bytes((root / plan).read_bytes())
    (root / plan).unlink()
    os.link(external, root / plan)
    with pytest.raises(Phase6PublicError, match="single-link"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )


def test_historical_root_replacement_is_detected_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "historical"
    root.mkdir()
    plan, campaign, report_json, report_markdown = _historical_fixture(root)
    output_parent = tmp_path / "records"
    output_parent.mkdir()
    output = output_parent / "verification.json"
    moved = tmp_path / "historical-moved"

    def replace_root(_repository: Path) -> str:
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
        return COMMIT

    monkeypatch.setattr("agentlab.phase6_public._clean_git_head", replace_root)
    monkeypatch.setattr(
        "agentlab.phase6_public._source_reviewed_commit",
        lambda _repository, _path, _bytes, _plan: COMMIT,
    )
    with pytest.raises(Phase6PublicError, match="directory changed type"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=root,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )
    assert not output.exists()
    linked = tmp_path / "linked-historical"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(Phase6PublicError, match="symlinks"):
        verify_phase6_historical(
            repository=tmp_path,
            historical_root=linked,
            reviewed_spec_path=HISTORICAL_REVIEWED_SPEC,
            plan_path=plan,
            campaign_path=campaign,
            report_json_path=report_json,
            report_markdown_path=report_markdown,
            output_path=output,
            language=Language.PYTHON,
            confirm_local_execution=True,
        )


def test_historical_dirty_head_is_rejected_without_real_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentlab.phase6_public._git",
        lambda _repository, *_arguments: b" M tracked-file\n",
    )
    with pytest.raises(Phase6PublicError, match="clean tracked tree"):
        _clean_git_head(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="atomic no-replace needs POSIX")
def test_no_replace_primitive_rejects_existing_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    inode = destination.stat().st_ino

    with pytest.raises(Phase6PublicError, match="already exists"):
        _rename_no_replace(source, destination)

    assert destination.stat().st_ino == inode
    assert source.is_dir()
