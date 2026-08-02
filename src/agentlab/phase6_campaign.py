"""Slice 6C Plan-bound preparation and Campaign execution.

The preparation path is local-only.  The execution path is deliberately split at
the Provider boundary so tests can use an offline fake without invoking Codex.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import yaml

from agentlab.campaign import CampaignRunStatus, CampaignStopReason
from agentlab.codex_provider import (
    CodexLifecycleTracker,
    CodexProcessRunner,
    CodexRunnerError,
    lifecycle_failure_evidence,
    preflight_codex,
    preflight_failure_evidence,
    unsupported_platform_evidence,
)
from agentlab.live import PromptInput
from agentlab.models import (
    CodexCleanupState,
    CodexExecutionEvidence,
    CodexExecutionStage,
    CommandEvidence,
    CommandStatus,
    DiffEvidence,
    ExecutionMode,
    FailureKind,
    GateKind,
    LiveFailureKind,
    LiveSettings,
    Provider,
    ProviderExecutionStatus,
    QualityGate,
    ReasoningEffort,
    RunMetrics,
    RunnerSettings,
    Workflow,
    WorkspaceLifecycle,
)
from agentlab.phase6 import (
    DiffPolicy,
    FixtureAcceptanceRecord,
    FixtureManifest,
    GateNotExecutedReason,
    Language,
    LiveRunArtifactV1_2,
    LoadedWorkflowSpecContract,
    Phase6CampaignFinishedEvent,
    Phase6CampaignOutcome,
    Phase6CampaignRunEvent,
    Phase6CampaignStartedEvent,
    Phase6FailureKind,
    Phase6OverallStatus,
    Phase6RecordingStartedEvent,
    Phase6RecordingTerminalEvent,
    WorkflowExperimentSpecV2_1,
    WorkflowPlanV1_2,
    _canonical_jsonl_line,
    _load_canonical_model_bytes,
    _load_workflow_plan_1_2_bytes,
    _load_workflow_spec_contract_bytes,
    _read_stable_regular_file,
    canonical_json_bytes,
    load_campaign_contract,
    load_live_run_artifact_contract,
    load_recording_contract,
    validate_plan_bindings,
)
from agentlab.phase6_fixtures import (
    FixtureAcceptanceError,
    SecureTreeSnapshot,
    ToolchainCandidates,
    _capture_toolchain_binding,
    _commands_for,
    _gate_contract_hash,
    _minimal_environment,
    _rename_directory_no_replace,
    _validate_workspace_policy,
    _verify_toolchain_binding,
    audit_toolchain,
    discover_toolchain_candidates,
    fixture_definitions,
    secure_tree_snapshot,
    verify_fixture_sources_committed,
    verify_repository_provenance,
)
from agentlab.runner import LocalCommandRunner, UnsupportedRunnerPlatformError
from agentlab.workflow import (
    BuiltWorkflowPrompt,
    FixedWorkflowInputs,
    LoadedWorkflowSpec,
    WorkflowArtifactSettings,
    WorkflowPlanPublication,
    WorkflowPlanRun,
    WorkflowStopConditions,
    _build_workflow_prompt_from_task,
    _publish_create_only_pair,
    build_workflow_plan_from_inputs,
    plan_publication_path,
)
from agentlab.workspace import (
    DirectorySnapshot,
    SnapshotError,
    _snapshot_hash,
    build_diff_evidence,
    incomplete_diff_evidence,
    prepare_disposable_workspace,
    remove_disposable_workspace,
    snapshot_directory,
)

_MODEL = "gpt-5.6-sol"
_REASONING = ReasoningEffort.HIGH
_PROVIDER_TIMEOUT_MS = 600_000
_MAX_PROMPT_BYTES = 65_536
_MAX_EVENT_LINE_BYTES = 1_048_576
_MAX_PROVIDER_OUTPUT_BYTES = 16_777_216
_COMMAND_TIMEOUT_MS = 15_000
_TERMINATION_GRACE_MS = 500
_MAX_OUTPUT_BYTES = 65_536
_MAX_DIFF_BYTES = 262_144


class Phase6CampaignError(ValueError):
    """A fail-closed Slice 6C preparation or execution error."""


@dataclass(frozen=True)
class Phase6PreparationOutcome:
    language: Language
    reviewed_commit: str
    root: Path
    spec_path: Path
    plan_path: Path
    metadata_path: Path
    spec_sha256: str
    plan_sha256: str
    plan: WorkflowPlanV1_2


@dataclass(frozen=True)
class PlanBoundInputs:
    repository_root: Path
    spec_path: Path
    plan_path: Path
    artifact_root: Path
    loaded_spec: LoadedWorkflowSpecContract
    plan: WorkflowPlanV1_2
    plan_sha256: str
    manifest: FixtureManifest
    manifest_bytes: bytes
    acceptance: FixtureAcceptanceRecord
    acceptance_bytes: bytes
    policy: DiffPolicy
    policy_bytes: bytes
    fixed: FixedWorkflowInputs
    fixture_source: Path
    fixture_secure: SecureTreeSnapshot
    reference_source: Path
    reference_sha256: str
    gate_commands: tuple[tuple[GateKind, list[str]], ...]
    toolchain_binding: object


@dataclass(frozen=True)
class GateExecutionOutcome:
    commands: list[CommandEvidence]
    evaluation_duration_ms: int
    harness_failure: FailureKind | None
    evidence_collection_error: str | None


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _path_below(root: Path, configured: str, label: str) -> Path:
    candidate = root.joinpath(*Path(configured).parts)
    current = root
    for component in Path(configured).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise Phase6CampaignError(f"{label} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise Phase6CampaignError(f"{label} path contains a symlink")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise Phase6CampaignError(f"{label} must remain below its fixed root") from error
    return resolved


def _copy_tree_snapshot(snapshot: SecureTreeSnapshot, destination: Path) -> None:
    destination.mkdir(parents=True)
    for relative in snapshot.directories:
        (destination / relative).mkdir(parents=True)
    for relative, content in snapshot.files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _fixed_inputs_from_snapshots(
    *,
    spec: WorkflowExperimentSpecV2_1,
    prompt_path: Path,
    prompt_bytes: bytes,
    fixture: SecureTreeSnapshot,
) -> FixedWorkflowInputs:
    task = PromptInput(
        path=prompt_path,
        content=prompt_bytes,
        sha256=_sha256(prompt_bytes),
        byte_count=len(prompt_bytes),
    )
    prompts: dict[Workflow, BuiltWorkflowPrompt] = {
        workflow: _build_workflow_prompt_from_task(spec, workflow, task)
        for workflow in (Workflow.ONE_SHOT, Workflow.STAGED)
    }
    fixture_snapshot = DirectorySnapshot(
        files=dict(fixture.files),
        directories=fixture.directories,
        sha256=_snapshot_hash(dict(fixture.files), fixture.directories),
    )
    return FixedWorkflowInputs(
        fixture=fixture_snapshot,
        task_prompt=task,
        prompts=prompts,
    )


def _bounded_real_path(
    root: Path,
    candidate: Path,
    label: str,
    *,
    final_may_be_file: bool = False,
) -> Path:
    """Reject symlink/non-directory components using lexical, not resolved, paths."""
    lexical_root = Path(os.path.abspath(root))
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as error:
        raise Phase6CampaignError(f"{label} escapes the fixed bundle root") from error
    current = lexical_root
    components = ("", *relative.parts)
    for index, component in enumerate(components):
        if component:
            current /= component
        if not os.path.lexists(current):
            continue
        try:
            metadata = current.lstat()
        except OSError as error:
            raise Phase6CampaignError(f"could not inspect {label}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise Phase6CampaignError(f"{label} path must not contain symlinks")
        is_final = index == len(components) - 1
        if not stat.S_ISDIR(metadata.st_mode) and not (final_may_be_file and is_final):
            raise Phase6CampaignError(f"{label} parent must be a real directory")
    return lexical_candidate


def _validate_artifact_path(
    inputs: PlanBoundInputs,
    path: Path,
    label: str,
    *,
    final_may_be_file: bool = True,
) -> Path:
    bounded = _bounded_real_path(
        inputs.spec_path.parent,
        path,
        label,
        final_may_be_file=final_may_be_file,
    )
    lexical_artifact_root = Path(os.path.abspath(inputs.artifact_root))
    try:
        bounded.relative_to(lexical_artifact_root)
    except ValueError as error:
        raise Phase6CampaignError(f"{label} escapes the Artifact root") from error
    _bounded_real_path(
        inputs.spec_path.parent,
        inputs.artifact_root,
        "Artifact root",
    )
    return bounded


def _validate_artifact_reservations(
    inputs: PlanBoundInputs,
    *,
    check_exists: bool,
) -> None:
    for run in inputs.plan.runs:
        for configured in (run.recording_path, run.evidence_path, run.diagnostic_path):
            candidate = inputs.spec_path.parent / configured
            _validate_artifact_path(
                inputs,
                candidate,
                "Artifact reservation",
            )
            if check_exists and os.path.lexists(candidate):
                raise Phase6CampaignError("Artifact reservation already exists")


def _spec_bytes(spec: WorkflowExperimentSpecV2_1) -> bytes:
    return yaml.safe_dump(
        spec.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _gate_quality_contract(
    commands: Sequence[tuple[GateKind, list[str]]],
) -> QualityGate:
    grouped: dict[str, list[list[str]]] = {gate.value: [] for gate in GateKind}
    for gate, argv in commands:
        grouped[gate.value].append(list(argv))
    return QualityGate.model_validate(grouped)


def _build_spec(
    *,
    language: Language,
    reviewed_commit: str,
    commands: Sequence[tuple[GateKind, list[str]]],
) -> WorkflowExperimentSpecV2_1:
    return WorkflowExperimentSpecV2_1(
        schema_version="2.1",
        experiment_id=f"phase6-{language.value}-workflow",
        title=f"Phase 6 {language.value} Workflow fixture",
        research_question="How do the fixed Workflow prompts behave on this fixture?",
        hypothesis="Workflow structure may change outcomes under fixed conditions.",
        comparison_axis="workflow",
        control=Workflow.ONE_SHOT,
        treatment=Workflow.STAGED,
        provider=Provider.CODEX,
        model=_MODEL,
        reasoning_effort=_REASONING,
        task_ids=["tag-normalizer"],
        repetitions=1,
        random_seed={Language.PYTHON: 6101, Language.TYPESCRIPT: 6102, Language.JAVA: 6103}[
            language
        ],
        task_prompt_path="inputs/task-prompt.md",
        task_prompt_revision=f"tag-normalizer-{language.value}-task-v1",
        one_shot_revision="one-shot-v1",
        staged_revision="staged-v1",
        fixture_revision=f"tag-normalizer-{language.value}-v1",
        provider_timeout_ms=_PROVIDER_TIMEOUT_MS,
        max_prompt_bytes=_MAX_PROMPT_BYTES,
        max_event_line_bytes=_MAX_EVENT_LINE_BYTES,
        max_provider_output_bytes=_MAX_PROVIDER_OUTPUT_BYTES,
        sandbox="workspace-write",
        network_access=False,
        quality_gate=_gate_quality_contract(commands),
        runner=RunnerSettings(
            fixture_path="inputs/fixture",
            command_timeout_ms=_COMMAND_TIMEOUT_MS,
            termination_grace_ms=_TERMINATION_GRACE_MS,
            max_output_bytes=_MAX_OUTPUT_BYTES,
            max_diff_bytes=_MAX_DIFF_BYTES,
        ),
        stop_conditions=WorkflowStopConditions(
            max_failures=2,
            max_total_duration_ms=3_600_000,
            fail_fast=False,
        ),
        artifacts=WorkflowArtifactSettings(root="campaign-artifacts"),
        language=language,
        reviewed_commit=reviewed_commit,
        fixture_manifest_path="inputs/fixture-manifest.json",
        fixture_acceptance_path="inputs/fixture-acceptance.json",
        diff_policy_path="inputs/diff-policy.json",
    )


def _build_plan(
    *,
    spec_path: Path,
    loaded: LoadedWorkflowSpecContract,
    fixed: FixedWorkflowInputs,
    manifest: FixtureManifest,
    manifest_bytes: bytes,
    acceptance: FixtureAcceptanceRecord,
    acceptance_bytes: bytes,
    policy_bytes: bytes,
) -> WorkflowPlanV1_2:
    assert isinstance(loaded.spec, WorkflowExperimentSpecV2_1)
    base = build_workflow_plan_from_inputs(
        spec_path,
        LoadedWorkflowSpec(spec=loaded.spec, sha256=loaded.sha256),
        fixed,
    )
    raw = base.model_dump(mode="json")
    raw.update(
        {
            "schema_version": "1.2",
            "experiment_spec_schema_version": "2.1",
            "language": loaded.spec.language.value,
            "reviewed_commit": loaded.spec.reviewed_commit,
            "fixture_sha256": manifest.fixture_sha256,
            "fixture_manifest_sha256": _sha256(manifest_bytes),
            "fixture_acceptance_sha256": _sha256(acceptance_bytes),
            "diff_policy_sha256": _sha256(policy_bytes),
            "gate_contract_sha256": acceptance.gate_contract_sha256,
            "reference_solution_sha256": acceptance.reference_solution_sha256,
            "toolchain_fingerprint": acceptance.toolchain.fingerprint,
        }
    )
    return WorkflowPlanV1_2.model_validate(raw)


def prepare_phase6_campaign(
    repository_root: Path,
    acceptance_root: Path,
    output_root: Path,
    *,
    language: Language,
    confirm_local_execution: bool,
    candidates: ToolchainCandidates | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Phase6PreparationOutcome:
    """Create one immutable Spec/Plan bundle after explicit local confirmation."""
    if not confirm_local_execution:
        raise Phase6CampaignError("--confirm-local-execution is required")
    if language not in {Language.PYTHON, Language.JAVA, Language.TYPESCRIPT}:
        raise Phase6CampaignError("unsupported Phase 6 language")
    repository = repository_root.resolve(strict=True)
    acceptance_root = _repository_input_directory(
        repository,
        acceptance_root,
        "Fixture Acceptance root",
    )
    commit = verify_repository_provenance(repository)
    definition = next(
        item
        for item in fixture_definitions(repository, acceptance_root)
        if item.language is language
    )
    verify_fixture_sources_committed(repository, definition)
    manifest_path = acceptance_root / language.value / "fixture-manifest.json"
    acceptance_path = acceptance_root / language.value / "fixture-acceptance.json"
    if not manifest_path.exists() or not acceptance_path.exists():
        raise Phase6CampaignError(f"{language.value} Fixture Acceptance is unavailable")
    manifest_snapshot = _read_stable_regular_file(manifest_path, "Fixture Manifest")
    acceptance_snapshot = _read_stable_regular_file(acceptance_path, "Fixture Acceptance")
    policy_snapshot = _read_stable_regular_file(definition.policy_path, "Diff Policy")
    manifest = _load_canonical_model_bytes(
        manifest_snapshot.content,
        FixtureManifest,
        "Fixture Manifest",
    )
    acceptance = _load_canonical_model_bytes(
        acceptance_snapshot.content,
        FixtureAcceptanceRecord,
        "Fixture Acceptance",
    )
    policy = _load_canonical_model_bytes(
        policy_snapshot.content,
        DiffPolicy,
        "Diff Policy",
    )
    if (
        manifest.language is not language
        or acceptance.language is not language
        or policy.language is not language
        or acceptance.acceptance_agentlab_commit != commit
        or acceptance.fixture_source_commit != commit
    ):
        raise Phase6CampaignError("Fixture Acceptance provenance differs from HEAD")
    fixture = secure_tree_snapshot(definition.fixture_root)
    reference = secure_tree_snapshot(definition.reference_root)
    prompt_path = (
        repository / "experiments" / "phase6" / "prompts" / language.value / "tag-normalizer-v1.md"
    )
    prompt = _read_stable_regular_file(prompt_path, "Task Prompt")
    if (
        fixture.sha256 != manifest.fixture_sha256
        or fixture.sha256 != acceptance.fixture_sha256
        or reference.sha256 != acceptance.reference_solution_sha256
        or acceptance.fixture_manifest_sha256 != manifest_snapshot.sha256
        or acceptance.diff_policy_sha256 != policy_snapshot.sha256
    ):
        raise Phase6CampaignError("Fixture, reference, Manifest, or Policy drift detected")

    measured = audit_toolchain(
        language,
        candidates or discover_toolchain_candidates(),
        forbidden_roots=(definition.fixture_root, definition.reference_root),
    )
    if measured != manifest.toolchain or measured != acceptance.toolchain:
        raise Phase6CampaignError("measured toolchain differs from Fixture Acceptance")
    binding = _capture_toolchain_binding(language, measured)
    commands = _commands_for(language, measured, binding)
    if _gate_contract_hash(language, measured, commands) != manifest.gate_contract_sha256:
        raise Phase6CampaignError("Gate contract drift detected")

    destination = output_root / language.value
    if os.path.lexists(destination):
        raise Phase6CampaignError("Phase 6 Campaign preparation output already exists")
    for protected in (
        definition.fixture_root,
        definition.reference_root,
        prompt_path,
        acceptance_root,
    ):
        resolved_protected = protected.resolve(strict=False)
        resolved_destination = destination.resolve(strict=False)
        if resolved_destination == resolved_protected or resolved_destination.is_relative_to(
            resolved_protected
        ):
            raise Phase6CampaignError("Campaign output overlaps a protected input root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{language.value}-", dir=destination.parent))
    try:
        inputs = staging / "inputs"
        _copy_tree_snapshot(fixture, inputs / "fixture")
        (inputs / "task-prompt.md").write_bytes(prompt.content)
        (inputs / "fixture-manifest.json").write_bytes(manifest_snapshot.content)
        (inputs / "fixture-acceptance.json").write_bytes(acceptance_snapshot.content)
        (inputs / "diff-policy.json").write_bytes(policy_snapshot.content)
        spec = _build_spec(language=language, reviewed_commit=commit, commands=commands)
        spec_path = staging / "workflow-spec.yaml"
        generated_spec_bytes = _spec_bytes(spec)
        spec_path.write_bytes(generated_spec_bytes)
        loaded = _load_workflow_spec_contract_bytes(generated_spec_bytes)
        fixed = _fixed_inputs_from_snapshots(
            spec=spec,
            prompt_path=inputs / "task-prompt.md",
            prompt_bytes=prompt.content,
            fixture=fixture,
        )
        plan = _build_plan(
            spec_path=spec_path,
            loaded=loaded,
            fixed=fixed,
            manifest=manifest,
            manifest_bytes=manifest_snapshot.content,
            acceptance=acceptance,
            acceptance_bytes=acceptance_snapshot.content,
            policy_bytes=policy_snapshot.content,
        )
        validate_plan_bindings(
            loaded_spec=loaded,
            plan=plan,
            fixture_manifest_bytes=manifest_snapshot.content,
            fixture_manifest=manifest,
            fixture_acceptance_bytes=acceptance_snapshot.content,
            fixture_acceptance=acceptance,
            diff_policy_bytes=policy_snapshot.content,
            diff_policy=policy,
        )
        plan_bytes = canonical_json_bytes(plan)
        plan_path = staging / "workflow-plan.json"
        metadata_path = plan_publication_path(plan_path)
        plan_path.write_bytes(plan_bytes)
        metadata_path.write_bytes(
            canonical_json_bytes(
                WorkflowPlanPublication(
                    schema_version="1.0",
                    plan_sha256=_sha256(plan_bytes),
                    created_at=now(),
                )
            )
        )
        if _load_workflow_plan_1_2_bytes(plan_bytes) != plan:
            raise Phase6CampaignError("canonical Workflow Plan reload failed")
        _rename_directory_no_replace(staging, destination)
    except Exception:
        with suppress(OSError):
            shutil.rmtree(staging)
        raise
    return Phase6PreparationOutcome(
        language=language,
        reviewed_commit=commit,
        root=destination,
        spec_path=destination / "workflow-spec.yaml",
        plan_path=destination / "workflow-plan.json",
        metadata_path=destination / "workflow-plan.metadata.json",
        spec_sha256=loaded.sha256,
        plan_sha256=_sha256(plan_bytes),
        plan=plan,
    )


def _repository_input_directory(repository: Path, configured: Path, label: str) -> Path:
    """Resolve one existing repository directory without following symlink components."""
    if ".." in configured.parts:
        raise Phase6CampaignError(f"{label} must not contain '..'")
    candidate = configured if configured.is_absolute() else repository / configured
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(repository)
    except ValueError as error:
        raise Phase6CampaignError(f"{label} must remain below repository") from error
    current = repository
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise Phase6CampaignError(f"{label} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise Phase6CampaignError(f"{label} contains a link or non-directory")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(repository)
    except (OSError, RuntimeError, ValueError) as error:
        raise Phase6CampaignError(f"{label} must remain below repository") from error
    return resolved


def load_plan_bound_inputs(
    repository_root: Path,
    spec_path: Path,
    plan_path: Path,
    *,
    check_reservations: bool = True,
) -> PlanBoundInputs:
    """Snapshot and cross-check every input before a Campaign is created."""
    repository = repository_root.resolve(strict=True)
    commit = verify_repository_provenance(repository)
    spec_snapshot = _read_stable_regular_file(spec_path, "Workflow Spec")
    plan_snapshot = _read_stable_regular_file(plan_path, "Workflow Plan")
    loaded = _load_workflow_spec_contract_bytes(spec_snapshot.content)
    plan = _load_workflow_plan_1_2_bytes(plan_snapshot.content)
    if not isinstance(loaded.spec, WorkflowExperimentSpecV2_1) or not isinstance(
        plan, WorkflowPlanV1_2
    ):
        raise Phase6CampaignError("Slice 6C requires Workflow Spec 2.1 and Plan 1.2")
    if loaded.spec.reviewed_commit != commit or plan.reviewed_commit != commit:
        raise Phase6CampaignError("reviewed commit differs from clean HEAD")
    base = Path(os.path.abspath(spec_path.parent))
    _bounded_real_path(base, base, "Plan bundle root")
    manifest_path = _path_below(base, loaded.spec.fixture_manifest_path, "Fixture Manifest")
    acceptance_path = _path_below(base, loaded.spec.fixture_acceptance_path, "Fixture Acceptance")
    policy_path = _path_below(base, loaded.spec.diff_policy_path, "Diff Policy")
    prompt_path = _path_below(base, loaded.spec.task_prompt_path, "Task Prompt")
    fixture_source = _path_below(base, loaded.spec.runner.fixture_path, "Fixture")
    resolved = [manifest_path, acceptance_path, policy_path, prompt_path, fixture_source]
    if len(resolved) != len(set(resolved)):
        raise Phase6CampaignError("Plan-bound input paths alias")
    manifest_snapshot = _read_stable_regular_file(manifest_path, "Fixture Manifest")
    acceptance_snapshot = _read_stable_regular_file(acceptance_path, "Fixture Acceptance")
    policy_snapshot = _read_stable_regular_file(policy_path, "Diff Policy")
    prompt_snapshot = _read_stable_regular_file(prompt_path, "Task Prompt")
    manifest = _load_canonical_model_bytes(
        manifest_snapshot.content,
        FixtureManifest,
        "Fixture Manifest",
    )
    acceptance = _load_canonical_model_bytes(
        acceptance_snapshot.content,
        FixtureAcceptanceRecord,
        "Fixture Acceptance",
    )
    policy = _load_canonical_model_bytes(
        policy_snapshot.content,
        DiffPolicy,
        "Diff Policy",
    )
    validate_plan_bindings(
        loaded_spec=loaded,
        plan=plan,
        fixture_manifest_bytes=manifest_snapshot.content,
        fixture_manifest=manifest,
        fixture_acceptance_bytes=acceptance_snapshot.content,
        fixture_acceptance=acceptance,
        diff_policy_bytes=policy_snapshot.content,
        diff_policy=policy,
    )
    fixture_secure = secure_tree_snapshot(fixture_source)
    fixed = _fixed_inputs_from_snapshots(
        spec=loaded.spec,
        prompt_path=prompt_path,
        prompt_bytes=prompt_snapshot.content,
        fixture=fixture_secure,
    )
    if (
        fixture_secure.sha256 != plan.fixture_sha256
        or fixed.fixture.files != fixture_secure.files
        or fixed.fixture.directories != fixture_secure.directories
    ):
        raise Phase6CampaignError("Fixture snapshot differs from Plan")
    if fixed.task_prompt.sha256 != plan.task_prompt_sha256:
        raise Phase6CampaignError("Task Prompt differs from Plan")
    for workflow, expected_hash, expected_bytes in (
        (Workflow.ONE_SHOT, plan.one_shot_prompt_sha256, plan.one_shot_prompt_bytes),
        (Workflow.STAGED, plan.staged_prompt_sha256, plan.staged_prompt_bytes),
    ):
        prompt = fixed.prompts[workflow]
        if prompt.sha256 != expected_hash or prompt.byte_count != expected_bytes:
            raise Phase6CampaignError("Workflow Prompt differs from Plan")
    reference_source = (
        repository / "experiments" / "phase6" / "fixtures" / plan.language.value / "reference"
    )
    reference = secure_tree_snapshot(reference_source)
    if reference.sha256 != plan.reference_solution_sha256:
        raise Phase6CampaignError("reference solution differs from Plan")
    binding = _capture_toolchain_binding(plan.language, acceptance.toolchain)
    commands = _commands_for(plan.language, acceptance.toolchain, binding)
    if (
        _gate_contract_hash(plan.language, acceptance.toolchain, commands)
        != plan.gate_contract_sha256
    ):
        raise Phase6CampaignError("Gate contract differs from Plan")
    metadata_snapshot = _read_stable_regular_file(plan_publication_path(plan_path), "Plan metadata")
    try:
        metadata = WorkflowPlanPublication.model_validate_json(metadata_snapshot.content)
    except Exception as error:
        raise Phase6CampaignError("Plan publication metadata is invalid") from error
    if metadata.plan_sha256 != plan_snapshot.sha256:
        raise Phase6CampaignError("Plan publication metadata hash differs")
    artifact_root = base / loaded.spec.artifacts.root
    _bounded_real_path(base, artifact_root, "Artifact root")
    for run in plan.runs:
        for configured in (run.recording_path, run.evidence_path, run.diagnostic_path):
            candidate = Path(os.path.abspath(base / configured))
            try:
                candidate.relative_to(Path(os.path.abspath(artifact_root)))
            except ValueError as error:
                raise Phase6CampaignError("Artifact reservation escapes fixed root") from error
            _bounded_real_path(
                base,
                candidate,
                "Artifact reservation",
                final_may_be_file=True,
            )
            if check_reservations and os.path.lexists(candidate):
                raise Phase6CampaignError("Artifact reservation already exists")
    return PlanBoundInputs(
        repository_root=repository,
        spec_path=spec_path,
        plan_path=plan_path,
        artifact_root=artifact_root,
        loaded_spec=loaded,
        plan=plan,
        plan_sha256=plan_snapshot.sha256,
        manifest=manifest,
        manifest_bytes=manifest_snapshot.content,
        acceptance=acceptance,
        acceptance_bytes=acceptance_snapshot.content,
        policy=policy,
        policy_bytes=policy_snapshot.content,
        fixed=fixed,
        fixture_source=fixture_source,
        fixture_secure=fixture_secure,
        reference_source=reference_source,
        reference_sha256=reference.sha256,
        gate_commands=commands,
        toolchain_binding=binding,
    )


def revalidate_plan_bound_inputs(inputs: PlanBoundInputs) -> None:
    """Re-read only for drift detection; execution keeps using fixed bytes."""
    current = load_plan_bound_inputs(
        inputs.repository_root,
        inputs.spec_path,
        inputs.plan_path,
        check_reservations=False,
    )
    stable = (
        current.loaded_spec.sha256 == inputs.loaded_spec.sha256
        and current.loaded_spec.spec == inputs.loaded_spec.spec
        and current.plan_sha256 == inputs.plan_sha256
        and current.plan == inputs.plan
        and current.manifest_bytes == inputs.manifest_bytes
        and current.manifest == inputs.manifest
        and current.acceptance_bytes == inputs.acceptance_bytes
        and current.acceptance == inputs.acceptance
        and current.policy_bytes == inputs.policy_bytes
        and current.policy == inputs.policy
        and current.fixed.task_prompt.content == inputs.fixed.task_prompt.content
        and current.fixed.prompts == inputs.fixed.prompts
        and current.fixture_secure == inputs.fixture_secure
        and current.reference_sha256 == inputs.reference_sha256
        and current.gate_commands == inputs.gate_commands
        and current.acceptance.toolchain == inputs.acceptance.toolchain
    )
    if not stable:
        raise Phase6CampaignError("Plan-bound input changed")


class ProviderExecutor(Protocol):
    def __call__(
        self,
        *,
        prompt: bytes,
        workspace: Path,
        environment_root: Path,
        spec: WorkflowExperimentSpecV2_1,
    ) -> CodexExecutionEvidence: ...


def _live_settings(spec: WorkflowExperimentSpecV2_1) -> LiveSettings:
    return LiveSettings(
        record_to="unused/recording.jsonl",
        diagnostic_to="unused/diagnostic.json",
        prompt_path="unused/prompt.md",
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        provider_timeout_ms=spec.provider_timeout_ms,
        max_prompt_bytes=spec.max_prompt_bytes,
        max_event_line_bytes=spec.max_event_line_bytes,
        max_provider_output_bytes=spec.max_provider_output_bytes,
        require_explicit_confirmation=True,
    )


def execute_real_codex_provider(
    *,
    prompt: bytes,
    workspace: Path,
    environment_root: Path,
    spec: WorkflowExperimentSpecV2_1,
) -> CodexExecutionEvidence:
    """Run only the Provider process; Diff policy and Gates remain outside it."""
    live = _live_settings(spec)
    try:
        preflight = preflight_codex()
    except Exception as error:
        if hasattr(error, "failure_kind"):
            return preflight_failure_evidence(error, live=live)  # type: ignore[arg-type]
        raise
    lifecycle = CodexLifecycleTracker()
    runner = CodexProcessRunner(live=live, runner=spec.runner, lifecycle=lifecycle)
    environment_root.mkdir(parents=True, exist_ok=False)
    try:
        return runner.run(
            preflight=preflight,
            prompt=prompt,
            workspace=workspace,
            environment_root=environment_root,
        ).evidence
    except UnsupportedRunnerPlatformError as error:
        return unsupported_platform_evidence(error, preflight=preflight, live=live)
    except CodexRunnerError as error:
        return lifecycle_failure_evidence(preflight, live=live, lifecycle=error.lifecycle)


@dataclass(frozen=True)
class Phase6CampaignOutcomeRecord:
    campaign_path: Path
    attempted_run_count: int
    provider_call_count: int
    counted_failure_count: int
    stop_reason: CampaignStopReason


def _append_event(path: Path, event: object, *, create: bool = False) -> None:
    flags = os.O_WRONLY | os.O_APPEND
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, _canonical_jsonl_line(event))  # type: ignore[arg-type]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _phase6_failure(value: LiveFailureKind) -> Phase6FailureKind:
    return Phase6FailureKind(value.value)


def _provider_terminal(
    codex: CodexExecutionEvidence,
) -> tuple[Phase6OverallStatus, Phase6FailureKind, Phase6CampaignOutcome, GateNotExecutedReason]:
    failure = _phase6_failure(codex.failure_kind)
    if codex.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR:
        return (
            Phase6OverallStatus.HARNESS_ERROR,
            failure,
            Phase6CampaignOutcome.CLEANUP_FAILURE,
            GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE,
        )
    if codex.failure_kind is LiveFailureKind.UNSUPPORTED_PLATFORM:
        return (
            Phase6OverallStatus.HARNESS_ERROR,
            failure,
            Phase6CampaignOutcome.HARNESS_FAILURE,
            GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE,
        )
    if codex.failure_kind is LiveFailureKind.EVIDENCE_ERROR:
        return (
            Phase6OverallStatus.HARNESS_ERROR,
            failure,
            Phase6CampaignOutcome.HARNESS_FAILURE,
            GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE,
        )
    reason = (
        GateNotExecutedReason.PROVIDER_TIMEOUT
        if codex.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
        else GateNotExecutedReason.PROVIDER_FAILURE
    )
    outcome = (
        Phase6CampaignOutcome.PROVIDER_TIMEOUT
        if codex.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
        else Phase6CampaignOutcome.PROVIDER_FAILURE
    )
    return Phase6OverallStatus.PROVIDER_ERROR, failure, outcome, reason


def _run_gates(
    inputs: PlanBoundInputs,
    workspace: Path,
    environment_root: Path,
    temporary_root: Path,
) -> GateExecutionOutcome:
    runner = LocalCommandRunner(inputs.loaded_spec.spec.runner)
    commands: list[CommandEvidence] = []
    started = datetime.now(UTC)
    harness_failure: FailureKind | None = None
    evidence_collection_error: str | None = None
    for index, (gate, argv) in enumerate(inputs.gate_commands):
        try:
            _verify_toolchain_binding(
                inputs.toolchain_binding,  # type: ignore[arg-type]
                inputs.acceptance.toolchain,
            )
        except Exception:
            harness_failure = FailureKind.EVIDENCE_ERROR
            evidence_collection_error = "Gate pre-execution toolchain verification failed"
            break
        result = runner.run(
            gate=gate,
            command_index=index,
            argv=argv,
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
            parent_environment=_minimal_environment(inputs.acceptance.toolchain.gate_path_entries),
        )
        commands.append(result.evidence)
        try:
            _verify_toolchain_binding(
                inputs.toolchain_binding,  # type: ignore[arg-type]
                inputs.acceptance.toolchain,
            )
        except Exception:
            harness_failure = result.harness_failure or FailureKind.EVIDENCE_ERROR
            evidence_collection_error = "Gate post-execution toolchain verification failed"
            break
        if result.harness_failure is not None or result.evidence.status not in {
            CommandStatus.PASSED,
            CommandStatus.FAILED,
        }:
            harness_failure = result.harness_failure or FailureKind.EVIDENCE_ERROR
            break
    elapsed = max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))
    return GateExecutionOutcome(
        commands=commands,
        evaluation_duration_ms=elapsed,
        harness_failure=harness_failure,
        evidence_collection_error=evidence_collection_error,
    )


def _rejected_output_diff_evidence() -> DiffEvidence:
    """Represent an unsafe tree that was rejected before a safe diff was possible."""
    return DiffEvidence(
        changed_files=[],
        binary_files=[],
        added_lines=None,
        deleted_lines=None,
        unified_diff="",
        diff_truncated=False,
        line_counts_complete=False,
        collection_error=None,
    )


def _metrics(
    codex: CodexExecutionEvidence,
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    evaluation_ms: int,
) -> RunMetrics:
    changed = diff.changed_files
    added = diff.added_lines
    deleted = diff.deleted_lines
    assert added is not None and deleted is not None
    by_gate = {gate: [item for item in commands if item.gate is gate] for gate in GateKind}
    return RunMetrics(
        quality_gate_pass=all(item.status is CommandStatus.PASSED for item in commands),
        acceptance_tests_passed=sum(
            item.status is CommandStatus.PASSED for item in by_gate[GateKind.ACCEPTANCE]
        ),
        acceptance_tests_total=len(by_gate[GateKind.ACCEPTANCE]),
        regression_failures=sum(
            item.status is CommandStatus.FAILED for item in by_gate[GateKind.REGRESSION]
        ),
        lint_errors=sum(item.status is CommandStatus.FAILED for item in by_gate[GateKind.LINT]),
        typecheck_errors=sum(
            item.status is CommandStatus.FAILED for item in by_gate[GateKind.TYPECHECK]
        ),
        agent_duration_ms=codex.duration_ms,
        evaluation_duration_ms=evaluation_ms,
        total_duration_ms=codex.duration_ms + evaluation_ms,
        agent_call_count=1,
        retry_count=0,
        changed_files=changed,
        added_lines=added,
        deleted_lines=deleted,
        usage_metrics=codex.usage_metrics,
    )


def _write_run_outputs(
    *,
    inputs: PlanBoundInputs,
    run: WorkflowPlanRun,
    started_at: datetime,
    completed_at: datetime,
    codex: CodexExecutionEvidence,
    overall: Phase6OverallStatus,
    failure: Phase6FailureKind,
    gate_reason: GateNotExecutedReason | None,
    commands: list[CommandEvidence],
    diff: DiffEvidence,
    metrics: RunMetrics | None,
    lifecycle: WorkspaceLifecycle,
) -> None:
    spec = inputs.loaded_spec.spec
    assert isinstance(spec, WorkflowExperimentSpecV2_1)
    prompt = inputs.fixed.prompts[run.workflow]
    recording_started = Phase6RecordingStartedEvent(
        schema_version="1.2",
        sequence=0,
        event_type="run_started",
        run_id=run.run_id,
        experiment_id=plan_id(inputs),
        task_id=run.task_id,
        language=inputs.plan.language,
        workflow=run.workflow,
        provider=Provider.CODEX,
        repetition_index=run.repetition_index,
        execution_mode=ExecutionMode.LIVE,
        occurred_at=cast(datetime, _timestamp(started_at)),
        plan_sha256=inputs.plan_sha256,
        fixture_sha256=inputs.plan.fixture_sha256,
        fixture_manifest_sha256=inputs.plan.fixture_manifest_sha256,
        fixture_acceptance_sha256=inputs.plan.fixture_acceptance_sha256,
        diff_policy_sha256=inputs.plan.diff_policy_sha256,
        prompt_sha256=prompt.sha256,
        prompt_bytes=prompt.byte_count,
        prompt_redacted=True,
        requested_model=spec.model,
        requested_reasoning_effort=spec.reasoning_effort,
        cli_version=cast(str, codex.cli_version),
    )
    terminal = Phase6RecordingTerminalEvent(
        schema_version="1.2",
        sequence=1,
        event_type=(
            "run_completed"
            if overall in {Phase6OverallStatus.PASSED, Phase6OverallStatus.FAILED}
            else "run_failed"
        ),
        run_id=run.run_id,
        experiment_id=plan_id(inputs),
        occurred_at=cast(datetime, _timestamp(completed_at)),
        overall_status=overall,
        failure_kind=failure,
        codex=codex,
        gate_executed=bool(commands),
        gate_not_executed_reason=gate_reason,
        gate_commands=commands,
        diff=diff,
        metrics=metrics,
        workspace_lifecycle=lifecycle,
    )
    recording_bytes = _canonical_jsonl_line(recording_started) + _canonical_jsonl_line(terminal)
    artifact = LiveRunArtifactV1_2(
        schema_version="1.2",
        run_id=run.run_id,
        experiment_id=plan_id(inputs),
        task_id=run.task_id,
        language=inputs.plan.language,
        repetition_index=run.repetition_index,
        workflow=run.workflow,
        provider=Provider.CODEX,
        execution_mode=ExecutionMode.LIVE,
        overall_status=overall,
        failure_kind=failure,
        started_at=cast(datetime, _timestamp(started_at)),
        completed_at=cast(datetime, _timestamp(completed_at)),
        reviewed_commit=inputs.plan.reviewed_commit,
        spec_sha256=inputs.loaded_spec.sha256,
        plan_sha256=inputs.plan_sha256,
        fixture_sha256=inputs.plan.fixture_sha256,
        fixture_manifest_sha256=inputs.plan.fixture_manifest_sha256,
        fixture_acceptance_sha256=inputs.plan.fixture_acceptance_sha256,
        diff_policy_sha256=inputs.plan.diff_policy_sha256,
        toolchain_fingerprint=inputs.plan.toolchain_fingerprint,
        prompt_sha256=prompt.sha256,
        prompt_bytes=prompt.byte_count,
        prompt_redacted=True,
        runner=spec.runner,
        codex=codex,
        gate_executed=bool(commands),
        gate_not_executed_reason=gate_reason,
        gate_commands=commands,
        diff=diff,
        metrics=metrics,
        workspace_lifecycle=lifecycle,
        recording_sha256=_sha256(recording_bytes),
        raw_provider_output_persisted=False,
    )
    recording_path = inputs.spec_path.parent / run.recording_path
    evidence_path = inputs.spec_path.parent / run.evidence_path
    _validate_artifact_path(inputs, recording_path, "Recording publication")
    _validate_artifact_path(inputs, evidence_path, "Evidence publication")
    try:
        _publish_create_only_pair(
            recording_path,
            recording_bytes,
            evidence_path,
            canonical_json_bytes(artifact),
        )
    except Exception as error:
        raise Phase6CampaignError("could not publish create-only run outputs") from error
    if load_recording_contract(recording_path).terminal != terminal:  # type: ignore[union-attr]
        raise Phase6CampaignError("canonical Recording reload failed")
    if load_live_run_artifact_contract(evidence_path) != artifact:
        raise Phase6CampaignError("canonical LiveRunArtifact reload failed")


def plan_id(inputs: PlanBoundInputs) -> str:
    return inputs.plan.experiment_id


def _run_event(
    *,
    sequence: int,
    run: WorkflowPlanRun,
    status: CampaignRunStatus,
    outcome: Phase6CampaignOutcome | None,
    stop_reason: CampaignStopReason | None,
    calls: int | None,
    gate: bool,
    counted: bool,
    failure: Phase6FailureKind | None,
    occurred_at: datetime,
) -> Phase6CampaignRunEvent:
    return Phase6CampaignRunEvent(
        schema_version="1.2",
        sequence=sequence,
        event_type="run_state",
        run_id=run.run_id,
        task_id=run.task_id,
        workflow=run.workflow,
        repetition_index=run.repetition_index,
        status=status,
        outcome=outcome,
        stop_reason=stop_reason,
        provider_call_count=calls,
        gate_executed=gate,
        counted_failure=counted,
        fail_fast_applies=counted,
        max_failures_applies=counted,
        failure_kind=failure,
        occurred_at=cast(datetime, _timestamp(occurred_at)),
    )


def run_phase6_campaign(
    repository_root: Path,
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    *,
    confirm_live_codex: bool,
    confirm_provider_calls: int | None,
    provider_executor: ProviderExecutor = execute_real_codex_provider,
) -> Phase6CampaignOutcomeRecord:
    """Execute one preregistered Campaign; never retries, resumes, or falls back."""
    if not confirm_live_codex:
        raise Phase6CampaignError("--confirm-live-codex is required")
    inputs = load_plan_bound_inputs(repository_root, spec_path, plan_path)
    if confirm_provider_calls != inputs.plan.planned_provider_call_count:
        raise Phase6CampaignError("confirmed Provider-call budget differs from Plan")
    spec = inputs.loaded_spec.spec
    assert isinstance(spec, WorkflowExperimentSpecV2_1)
    _validate_artifact_reservations(inputs, check_exists=True)
    expected_campaign = spec_path.parent / spec.artifacts.root / "campaign.jsonl"
    if Path(os.path.abspath(campaign_path)) != Path(os.path.abspath(expected_campaign)):
        raise Phase6CampaignError("Campaign path must use the Plan-bound Artifact root")
    _validate_artifact_path(inputs, campaign_path, "Campaign output")
    if os.path.lexists(campaign_path):
        raise Phase6CampaignError("Campaign output already exists; resume is forbidden")
    revalidate_plan_bound_inputs(inputs)
    campaign_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_artifact_path(inputs, campaign_path, "Campaign output")
    sequence = 0
    started_at = datetime.now(UTC)
    _append_event(
        campaign_path,
        Phase6CampaignStartedEvent(
            schema_version="1.2",
            sequence=0,
            event_type="campaign_started",
            experiment_id=plan_id(inputs),
            plan_sha256=inputs.plan_sha256,
            fixture_manifest_sha256=inputs.plan.fixture_manifest_sha256,
            fixture_acceptance_sha256=inputs.plan.fixture_acceptance_sha256,
            diff_policy_sha256=inputs.plan.diff_policy_sha256,
            toolchain_fingerprint=inputs.plan.toolchain_fingerprint,
            planned_run_count=inputs.plan.planned_run_count,
            planned_provider_call_count=inputs.plan.planned_provider_call_count,
            occurred_at=cast(datetime, _timestamp(started_at)),
        ),
        create=True,
    )
    attempted = calls = failures = 0
    stop = CampaignStopReason.NONE
    next_index = 0
    for index, run in enumerate(inputs.plan.runs):
        next_index = index
        try:
            revalidate_plan_bound_inputs(inputs)
        except Exception:
            stop = CampaignStopReason.INPUT_CHANGED
            break
        run_started = datetime.now(UTC)
        sequence += 1
        _append_event(
            campaign_path,
            _run_event(
                sequence=sequence,
                run=run,
                status=CampaignRunStatus.STARTED,
                outcome=None,
                stop_reason=None,
                calls=None,
                gate=False,
                counted=False,
                failure=None,
                occurred_at=run_started,
            ),
        )
        attempted += 1
        workspace = None
        commands: list[CommandEvidence] = []
        metrics: RunMetrics | None = None
        diff = incomplete_diff_evidence("Provider-after diff collection did not complete")
        overall = Phase6OverallStatus.HARNESS_ERROR
        failure = Phase6FailureKind.EVIDENCE_ERROR
        campaign_outcome = Phase6CampaignOutcome.HARNESS_FAILURE
        gate_reason: GateNotExecutedReason | None = GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
        lifecycle = WorkspaceLifecycle.NOT_CREATED
        counted = False
        codex: CodexExecutionEvidence | None = None
        gate_harness_failure: FailureKind | None = None
        gate_workspace_error = False
        try:
            workspace = prepare_disposable_workspace(inputs.fixture_source, inputs.fixed.fixture)
            lifecycle = WorkspaceLifecycle.REMOVED
            codex = provider_executor(
                prompt=inputs.fixed.prompts[run.workflow].content,
                workspace=workspace.workspace,
                environment_root=workspace.environment_root / "provider",
                spec=spec,
            )
            run_calls = (
                1
                if codex.execution_stage is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
                else 0
            )
            calls += run_calls
            if codex.status is not ProviderExecutionStatus.SUCCEEDED:
                overall, failure, campaign_outcome, gate_reason = _provider_terminal(codex)
                counted = campaign_outcome in {
                    Phase6CampaignOutcome.PROVIDER_FAILURE,
                    Phase6CampaignOutcome.PROVIDER_TIMEOUT,
                }
                try:
                    after = snapshot_directory(workspace.workspace)
                    diff = build_diff_evidence(
                        workspace.initial_snapshot, after, max_diff_bytes=spec.runner.max_diff_bytes
                    )
                except SnapshotError:
                    diff = incomplete_diff_evidence("Provider-after diff collection failed")
            else:
                try:
                    provider_secure = secure_tree_snapshot(workspace.workspace)
                    provider_snapshot = snapshot_directory(workspace.workspace)
                    diff = build_diff_evidence(
                        workspace.initial_snapshot,
                        provider_snapshot,
                        max_diff_bytes=spec.runner.max_diff_bytes,
                    )
                    _validate_workspace_policy(
                        inputs.policy, inputs.fixture_secure, provider_secure
                    )
                except FixtureAcceptanceError:
                    if diff.collection_error is not None:
                        diff = _rejected_output_diff_evidence()
                    overall = Phase6OverallStatus.REJECTED
                    failure = Phase6FailureKind.OUTPUT_CONTRACT_VIOLATION
                    campaign_outcome = Phase6CampaignOutcome.OUTPUT_CONTRACT_VIOLATION
                    gate_reason = GateNotExecutedReason.OUTPUT_CONTRACT_VIOLATION
                    counted = True
                except (SnapshotError, OSError):
                    diff = incomplete_diff_evidence("Provider-after diff collection failed")
                else:
                    gate_environment = workspace.environment_root / "gates"
                    gate_environment.mkdir(parents=True, exist_ok=False)
                    gate_outcome = _run_gates(
                        inputs, workspace.workspace, gate_environment, workspace.temporary_root
                    )
                    commands = gate_outcome.commands
                    evaluation_ms = gate_outcome.evaluation_duration_ms
                    gate_harness_failure = gate_outcome.harness_failure
                    if gate_outcome.evidence_collection_error is not None:
                        diff = incomplete_diff_evidence(gate_outcome.evidence_collection_error)
                    try:
                        after_gate = secure_tree_snapshot(workspace.workspace)
                        if after_gate != provider_secure:
                            diff = incomplete_diff_evidence("Gate changed the Provider Workspace")
                            gate_workspace_error = True
                    except (FixtureAcceptanceError, SnapshotError, OSError):
                        diff = incomplete_diff_evidence("Gate Workspace verification failed")
                        gate_workspace_error = True
                    if gate_harness_failure is not None:
                        overall = Phase6OverallStatus.HARNESS_ERROR
                        abnormal_gate_observed = any(
                            command.status not in {CommandStatus.PASSED, CommandStatus.FAILED}
                            or not command.termination.process_group_cleared
                            for command in commands
                        )
                        if gate_harness_failure is FailureKind.PROCESS_CLEANUP_ERROR:
                            failure = Phase6FailureKind.PROCESS_CLEANUP_ERROR
                            campaign_outcome = Phase6CampaignOutcome.CLEANUP_FAILURE
                        elif abnormal_gate_observed:
                            failure = Phase6FailureKind.GATE_HARNESS_ERROR
                            campaign_outcome = Phase6CampaignOutcome.HARNESS_FAILURE
                        else:
                            failure = Phase6FailureKind.EVIDENCE_ERROR
                            campaign_outcome = Phase6CampaignOutcome.HARNESS_FAILURE
                            if diff.collection_error is None:
                                diff = incomplete_diff_evidence("Gate Evidence collection failed")
                        gate_reason = (
                            None if commands else GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
                        )
                    elif gate_workspace_error:
                        overall = Phase6OverallStatus.HARNESS_ERROR
                        failure = Phase6FailureKind.EVIDENCE_ERROR
                        campaign_outcome = Phase6CampaignOutcome.HARNESS_FAILURE
                        gate_reason = None
                    else:
                        metrics = _metrics(codex, commands, diff, evaluation_ms)
                        overall = (
                            Phase6OverallStatus.PASSED
                            if metrics.quality_gate_pass
                            else Phase6OverallStatus.FAILED
                        )
                        failure = (
                            Phase6FailureKind.NONE
                            if metrics.quality_gate_pass
                            else Phase6FailureKind.QUALITY_GATE_FAILURE
                        )
                        campaign_outcome = (
                            Phase6CampaignOutcome.SUCCESS
                            if metrics.quality_gate_pass
                            else Phase6CampaignOutcome.QUALITY_GATE_FAILURE
                        )
                        gate_reason = None
                        counted = not metrics.quality_gate_pass
        except Exception:
            if codex is None:
                raise
            diff = incomplete_diff_evidence("Post-Codex harness processing failed")
            overall = Phase6OverallStatus.HARNESS_ERROR
            abnormal_gate_observed = any(
                command.status not in {CommandStatus.PASSED, CommandStatus.FAILED}
                or not command.termination.process_group_cleared
                for command in commands
            )
            failure = (
                Phase6FailureKind.GATE_HARNESS_ERROR
                if abnormal_gate_observed
                else Phase6FailureKind.EVIDENCE_ERROR
            )
            campaign_outcome = Phase6CampaignOutcome.HARNESS_FAILURE
            gate_reason = None if commands else GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
            metrics = None
            counted = False
        finally:
            if workspace is not None:
                removed, _detail = remove_disposable_workspace(workspace)
                lifecycle = (
                    WorkspaceLifecycle.REMOVED if removed else WorkspaceLifecycle.CLEANUP_FAILED
                )
        if codex is None:
            raise Phase6CampaignError("Provider execution produced no Evidence")
        if (
            lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
            or codex.cleanup_state is CodexCleanupState.FAILED
            or not codex.termination.process_group_cleared
            or gate_harness_failure is FailureKind.PROCESS_CLEANUP_ERROR
        ):
            overall = Phase6OverallStatus.HARNESS_ERROR
            failure = Phase6FailureKind.PROCESS_CLEANUP_ERROR
            campaign_outcome = Phase6CampaignOutcome.CLEANUP_FAILURE
            gate_reason = None if commands else GateNotExecutedReason.PRE_GATE_HARNESS_FAILURE
            metrics = None
            counted = False
        completed = datetime.now(UTC)
        _write_run_outputs(
            inputs=inputs,
            run=run,
            started_at=run_started,
            completed_at=completed,
            codex=codex,
            overall=overall,
            failure=failure,
            gate_reason=gate_reason,
            commands=commands,
            diff=diff,
            metrics=metrics,
            lifecycle=lifecycle,
        )
        sequence += 1
        terminal_status = (
            CampaignRunStatus.COMPLETED
            if campaign_outcome
            in {Phase6CampaignOutcome.SUCCESS, Phase6CampaignOutcome.QUALITY_GATE_FAILURE}
            else CampaignRunStatus.FAILED
        )
        run_calls = (
            1 if codex.execution_stage is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED else 0
        )
        _append_event(
            campaign_path,
            _run_event(
                sequence=sequence,
                run=run,
                status=terminal_status,
                outcome=campaign_outcome,
                stop_reason=None,
                calls=run_calls,
                gate=bool(commands),
                counted=counted,
                failure=failure,
                occurred_at=completed,
            ),
        )
        failures += int(counted)
        next_index = index + 1
        if campaign_outcome is Phase6CampaignOutcome.CLEANUP_FAILURE:
            stop = CampaignStopReason.CLEANUP_FAILURE
            break
        if campaign_outcome is Phase6CampaignOutcome.HARNESS_FAILURE:
            stop = CampaignStopReason.HARNESS_FAILURE
            break
        if counted and spec.stop_conditions.fail_fast:
            stop = CampaignStopReason.FAIL_FAST
            break
        if (
            counted
            and spec.stop_conditions.max_failures is not None
            and failures >= spec.stop_conditions.max_failures
        ):
            stop = CampaignStopReason.MAX_FAILURES
            break
    if stop is not CampaignStopReason.NONE:
        for run in inputs.plan.runs[next_index:]:
            sequence += 1
            _append_event(
                campaign_path,
                _run_event(
                    sequence=sequence,
                    run=run,
                    status=CampaignRunStatus.NOT_RUN,
                    outcome=Phase6CampaignOutcome.STOP_CONDITION,
                    stop_reason=stop,
                    calls=0,
                    gate=False,
                    counted=False,
                    failure=None,
                    occurred_at=datetime.now(UTC),
                ),
            )
    sequence += 1
    _append_event(
        campaign_path,
        Phase6CampaignFinishedEvent(
            schema_version="1.2",
            sequence=sequence,
            event_type="campaign_finished",
            experiment_id=plan_id(inputs),
            stop_reason=stop,
            attempted_run_count=attempted,
            provider_call_count=calls,
            provider_call_count_unknown_runs=0,
            counted_failure_count=failures,
            retry_count=0,
            occurred_at=cast(datetime, _timestamp(datetime.now(UTC))),
        ),
    )
    loaded_campaign = load_campaign_contract(campaign_path)
    if not hasattr(loaded_campaign, "finished"):
        raise Phase6CampaignError("canonical Campaign reload failed")
    return Phase6CampaignOutcomeRecord(campaign_path, attempted, calls, failures, stop)
