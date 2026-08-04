"""Offline, pending-only Supplemental Live Campaign Approval packets.

This module validates already prepared Phase 6 inputs and records their exact
identity.  It has no Provider, Prompt transmission, Gate, or Campaign entry
point and cannot issue Live approval.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.models import ContractModel, Provider, ReasoningEffort, Workflow
from agentlab.phase6 import (
    COMMIT_PATTERN,
    SHA256_PATTERN,
    Language,
    LanguageStatus,
    ProviderEvaluationStatus,
    WorkflowExperimentSpecV2_1,
    _load_canonical_model_bytes,
    _read_stable_regular_file,
    canonical_json_bytes,
    load_public_suite_manifest,
)
from agentlab.phase6_campaign import (
    Phase6CampaignError,
    PlanBoundInputs,
    load_plan_bound_inputs,
)
from agentlab.phase6_fixtures import (
    FixtureAcceptanceError,
    _gate_contract_bytes,
    secure_tree_snapshot,
    verify_repository_provenance,
)
from agentlab.workflow import plan_publication_path

_JAVA_EXPERIMENT_ID = "phase6-java-workflow"
_JAVA_MODEL = "gpt-5.6-sol"
_JAVA_REASONING_EFFORT = ReasoningEffort.HIGH
_JAVA_PLANNED_RUNS = 2
_JAVA_EXACT_PROVIDER_CALLS = 2
_JAVA_WORKFLOW_ORDER = (Workflow.STAGED, Workflow.ONE_SHOT)
_JAVA_PER_RUN_PROVIDER_CALLS = (1, 1)
_JAVA_MAX_FAILURES = 2
_JAVA_MAX_TOTAL_DURATION_MS = 3_600_000


class SupplementalApprovalError(ValueError):
    """A fail-closed offline Supplemental Approval error."""


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
        raise ValueError(f"{field_name} must remain below repository")
    return value


def _relative_directory(value: str, field_name: str) -> str:
    normalized = _relative_file(value, field_name)
    if PurePosixPath(normalized).suffix:
        raise ValueError(f"{field_name} must name a directory")
    return normalized


class SupplementalFileBinding(ContractModel):
    path: StrictStr
    byte_count: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "Artifact binding path")


class SupplementalDirectoryBinding(ContractModel):
    path: StrictStr
    file_count: StrictInt = Field(ge=0)
    directory_count: StrictInt = Field(ge=0)
    byte_count: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_directory(value, "directory binding path")


class SupplementalDerivedBinding(ContractModel):
    """Identity derived from a named repository Artifact without persisting content."""

    path: StrictStr
    derivation: StrictStr = Field(min_length=1)
    byte_count: StrictInt = Field(ge=0)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "derived binding source path")


class SupplementalToolchainBinding(SupplementalDerivedBinding):
    fingerprint: StrictStr = Field(pattern=SHA256_PATTERN)


class SupplementalAcceptedSuiteBinding(ContractModel):
    suite_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    manifest: SupplementalFileBinding
    acceptance_basis: Literal["human_final_acceptance_external"]
    python_status: Literal[LanguageStatus.EVALUATED]
    java_status: Literal[LanguageStatus.READY_NOT_RUN]
    phase6_status: Literal["current_not_complete"]


class SupplementalJavaArtifactBindings(ContractModel):
    spec: SupplementalFileBinding
    plan: SupplementalFileBinding
    fixture_manifest: SupplementalFileBinding
    fixture_acceptance: SupplementalFileBinding
    diff_policy: SupplementalFileBinding
    fixture_snapshot: SupplementalDirectoryBinding
    reference_snapshot: SupplementalDirectoryBinding
    gate_contract: SupplementalDerivedBinding
    toolchain_fingerprint: SupplementalToolchainBinding
    task_prompt: SupplementalFileBinding
    one_shot_prompt: SupplementalDerivedBinding
    staged_prompt: SupplementalDerivedBinding
    plan_metadata: SupplementalFileBinding


class SupplementalStopPolicy(ContractModel):
    max_failures: StrictInt = Field(gt=0)
    max_total_duration_ms: StrictInt = Field(gt=0)
    fail_fast: StrictBool

    @model_validator(mode="after")
    def policy_is_fixed_for_java_live(self) -> SupplementalStopPolicy:
        if self.max_failures != _JAVA_MAX_FAILURES:
            raise ValueError("max_failures must be 2")
        if self.max_total_duration_ms != _JAVA_MAX_TOTAL_DURATION_MS:
            raise ValueError("max_total_duration_ms must be 3600000")
        if self.fail_fast:
            raise ValueError("fail_fast must be false")
        return self


class SupplementalCampaignContract(ContractModel):
    experiment_id: Literal["phase6-java-workflow"]
    output_root: StrictStr
    campaign_jsonl: StrictStr
    repository_root: Literal["."]
    provider: Literal[Provider.CODEX]
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal[ReasoningEffort.HIGH]
    planned_runs: StrictInt = Field(gt=0)
    exact_provider_calls: StrictInt = Field(gt=0)
    workflow_order: list[Workflow] = Field(min_length=2)
    per_run_provider_calls: list[StrictInt] = Field(min_length=2)
    retry_count: Literal[0]
    fallback_count: Literal[0]
    resume_count: Literal[0]
    stop_policy: SupplementalStopPolicy
    create_only_output: Literal[True]
    stop_on_collision: Literal[True]
    stop_on_input_drift: Literal[True]

    @field_validator("output_root")
    @classmethod
    def output_root_is_relative(cls, value: str) -> str:
        return _relative_directory(value, "Campaign output root")

    @field_validator("campaign_jsonl")
    @classmethod
    def campaign_path_is_relative(cls, value: str) -> str:
        return _relative_file(value, "Campaign JSONL path")

    @model_validator(mode="after")
    def run_and_call_counts_match(self) -> SupplementalCampaignContract:
        if self.planned_runs != _JAVA_PLANNED_RUNS:
            raise ValueError("planned_runs must be 2")
        if self.exact_provider_calls != _JAVA_EXACT_PROVIDER_CALLS:
            raise ValueError("exact_provider_calls must be 2")
        if tuple(self.workflow_order) != _JAVA_WORKFLOW_ORDER:
            raise ValueError("workflow_order must be staged then one_shot")
        if tuple(self.per_run_provider_calls) != _JAVA_PER_RUN_PROVIDER_CALLS:
            raise ValueError("per_run_provider_calls must be [1, 1]")
        if self.planned_runs != len(self.workflow_order):
            raise ValueError("planned_runs must match workflow_order")
        if self.planned_runs != len(self.per_run_provider_calls):
            raise ValueError("planned_runs must match per_run_provider_calls")
        if self.exact_provider_calls != sum(self.per_run_provider_calls):
            raise ValueError("exact_provider_calls must match per-run calls")
        return self


class SupplementalProviderAccounting(ContractModel):
    prior_minimum_calls: StrictInt = Field(ge=0)
    prior_maximum_calls: StrictInt = Field(ge=0)
    campaign_exact_calls: StrictInt = Field(gt=0)
    projected_minimum_calls: StrictInt = Field(ge=0)
    projected_maximum_calls: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def call_ranges_are_exact(self) -> SupplementalProviderAccounting:
        if self.prior_minimum_calls > self.prior_maximum_calls:
            raise ValueError("prior minimum calls must not exceed prior maximum calls")
        if self.projected_minimum_calls != (
            self.prior_minimum_calls + self.campaign_exact_calls
        ):
            raise ValueError("projected minimum calls must equal prior plus Campaign calls")
        if self.projected_maximum_calls != (
            self.prior_maximum_calls + self.campaign_exact_calls
        ):
            raise ValueError("projected maximum calls must equal prior plus Campaign calls")
        if self.projected_minimum_calls > self.projected_maximum_calls:
            raise ValueError("projected minimum calls must not exceed projected maximum calls")
        return self


class SupplementalHumanPreflight(ContractModel):
    authenticated_account_verification: Literal["pending"]
    quota_verification: Literal["pending"]
    host_process_verification: Literal["pending"]
    final_branch_head_worktree_verification: Literal["pending"]
    output_collision_verification: Literal["pending"]
    final_artifact_sha_verification: Literal["pending"]


class SupplementalLiveCampaignApproval(ContractModel):
    schema_version: Literal["1.0"]
    document_type: Literal["supplemental_live_campaign_approval"]
    approval_status: Literal["pending_human_live_approval"]
    approval_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    language: Literal[Language.JAVA]
    reviewed_repository_head: StrictStr = Field(pattern=COMMIT_PATTERN)
    reviewed_head_role: Literal["harness_head"]
    accepted_public_suite: SupplementalAcceptedSuiteBinding
    artifacts: SupplementalJavaArtifactBindings
    campaign: SupplementalCampaignContract
    provider_accounting: SupplementalProviderAccounting
    human_preflight: SupplementalHumanPreflight
    exact_argv: list[StrictStr] = Field(min_length=1)

    @model_validator(mode="after")
    def packet_is_internally_consistent(self) -> SupplementalLiveCampaignApproval:
        if self.provider_accounting.campaign_exact_calls != self.campaign.exact_provider_calls:
            raise ValueError("Provider accounting differs from Campaign call budget")
        expected_argv = _campaign_argv(self.campaign, self.artifacts)
        if self.exact_argv != expected_argv:
            raise ValueError("exact_argv differs from the bound Campaign contract")
        return self


@dataclass(frozen=True)
class SupplementalApprovalPublication:
    output_path: Path
    byte_count: int
    sha256: str
    packet: SupplementalLiveCampaignApproval


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _repository_relative(repository: Path, path: Path, label: str) -> str:
    try:
        relative = Path(os.path.abspath(path)).relative_to(repository).as_posix()
    except ValueError as error:
        raise SupplementalApprovalError(f"{label} must remain below repository") from error
    try:
        return _relative_file(relative, label)
    except ValueError as error:
        raise SupplementalApprovalError(str(error)) from error


def _repository_path(
    repository: Path,
    configured: Path | str,
    label: str,
    *,
    require_file: bool,
) -> Path:
    path = Path(configured)
    if ".." in path.parts:
        raise SupplementalApprovalError(f"{label} must not contain '..'")
    candidate = path if path.is_absolute() else repository / path
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(repository)
    except ValueError as error:
        raise SupplementalApprovalError(f"{label} must remain below repository") from error
    current = repository
    missing_suffix = False
    for index, component in enumerate(relative.parts):
        current /= component
        if missing_suffix:
            continue
        if not os.path.lexists(current):
            if require_file:
                raise SupplementalApprovalError(f"{label} is unavailable")
            missing_suffix = True
            continue
        try:
            metadata = current.lstat()
        except OSError as error:
            raise SupplementalApprovalError(f"{label} is unavailable") from error
        final = index == len(relative.parts) - 1
        if stat.S_ISLNK(metadata.st_mode):
            raise SupplementalApprovalError(f"{label} contains a symlink")
        if not final and not stat.S_ISDIR(metadata.st_mode):
            raise SupplementalApprovalError(f"{label} parent is not a directory")
        if final and require_file and (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
        ):
            raise SupplementalApprovalError(
                f"{label} must be a regular single-link file"
            )
    return lexical


def _file_binding(repository: Path, path: Path, label: str) -> SupplementalFileBinding:
    safe = _repository_path(repository, path, label, require_file=True)
    snapshot = _read_stable_regular_file(safe, label)
    return SupplementalFileBinding(
        path=_repository_relative(repository, safe, label),
        byte_count=len(snapshot.content),
        sha256=snapshot.sha256,
    )


def _directory_binding(
    repository: Path,
    path: Path,
    label: str,
) -> SupplementalDirectoryBinding:
    safe = _repository_path(repository, path, label, require_file=False)
    snapshot = secure_tree_snapshot(safe)
    return SupplementalDirectoryBinding(
        path=_repository_relative(repository, safe, label),
        file_count=len(snapshot.files),
        directory_count=len(snapshot.directories),
        byte_count=sum(len(content) for content in snapshot.files.values()),
        sha256=snapshot.sha256,
    )


def _derived_binding(
    repository: Path,
    source_path: Path,
    derivation: str,
    content: bytes,
) -> SupplementalDerivedBinding:
    return SupplementalDerivedBinding(
        path=_repository_relative(repository, source_path, derivation),
        derivation=derivation,
        byte_count=len(content),
        sha256=_sha256(content),
    )


def _public_suite_binding(
    repository: Path,
    manifest_path: Path,
) -> SupplementalAcceptedSuiteBinding:
    safe_path = _repository_path(
        repository,
        manifest_path,
        "accepted Public Suite Manifest",
        require_file=True,
    )
    manifest = load_public_suite_manifest(safe_path)
    statuses = {
        source.language: source.expected_language_status
        for source in manifest.primary_sources
    }
    if statuses.get(Language.PYTHON) is not LanguageStatus.EVALUATED:
        raise SupplementalApprovalError("accepted Public Suite must mark Python evaluated")
    if statuses.get(Language.JAVA) is not LanguageStatus.READY_NOT_RUN:
        raise SupplementalApprovalError("accepted Public Suite must mark Java ready_not_run")
    codex = next(
        (
            coverage
            for coverage in manifest.provider_coverage
            if coverage.provider is Provider.CODEX
        ),
        None,
    )
    if (
        codex is None
        or codex.evaluation_status is not ProviderEvaluationStatus.EVALUATED
        or codex.evaluated_languages != [Language.PYTHON]
    ):
        raise SupplementalApprovalError(
            "accepted Public Suite Codex coverage must be evaluated for Python only"
        )
    return SupplementalAcceptedSuiteBinding(
        suite_id=manifest.suite_id,
        manifest=_file_binding(repository, safe_path, "accepted Public Suite Manifest"),
        acceptance_basis="human_final_acceptance_external",
        python_status=LanguageStatus.EVALUATED,
        java_status=LanguageStatus.READY_NOT_RUN,
        phase6_status="current_not_complete",
    )


def _campaign_argv(
    campaign: SupplementalCampaignContract,
    artifacts: SupplementalJavaArtifactBindings,
) -> list[str]:
    return [
        ".venv/bin/agentlab",
        "run-phase6-campaign",
        artifacts.spec.path,
        "--plan",
        artifacts.plan.path,
        "--campaign",
        campaign.campaign_jsonl,
        "--repository-root",
        ".",
        "--confirm-live-codex",
        "--confirm-provider-calls",
        str(campaign.exact_provider_calls),
    ]


def _artifact_bindings(
    repository: Path,
    inputs: PlanBoundInputs,
) -> SupplementalJavaArtifactBindings:
    spec = inputs.loaded_spec.spec
    if not isinstance(spec, WorkflowExperimentSpecV2_1):
        raise SupplementalApprovalError("Supplemental Approval requires Workflow Spec 2.1")
    base = inputs.spec_path.parent
    manifest_path = base / spec.fixture_manifest_path
    acceptance_path = base / spec.fixture_acceptance_path
    policy_path = base / spec.diff_policy_path
    task_prompt_path = base / spec.task_prompt_path
    gate_bytes = _gate_contract_bytes(
        inputs.plan.language,
        inputs.acceptance.toolchain,
        inputs.gate_commands,
    )
    if _sha256(gate_bytes) != inputs.plan.gate_contract_sha256:
        raise SupplementalApprovalError("Gate contract differs from Plan")
    toolchain_bytes = canonical_json_bytes(inputs.acceptance.toolchain)
    task_prompt = _file_binding(repository, task_prompt_path, "Task Prompt")
    artifacts = SupplementalJavaArtifactBindings(
        spec=_file_binding(repository, inputs.spec_path, "Workflow Spec"),
        plan=_file_binding(repository, inputs.plan_path, "Workflow Plan"),
        fixture_manifest=_file_binding(repository, manifest_path, "Fixture Manifest"),
        fixture_acceptance=_file_binding(
            repository,
            acceptance_path,
            "Fixture Acceptance",
        ),
        diff_policy=_file_binding(repository, policy_path, "Diff Policy"),
        fixture_snapshot=_directory_binding(repository, inputs.fixture_source, "Fixture snapshot"),
        reference_snapshot=_directory_binding(
            repository,
            inputs.reference_source,
            "Reference snapshot",
        ),
        gate_contract=_derived_binding(
            repository,
            inputs.spec_path,
            "canonical_gate_contract_v1",
            gate_bytes,
        ),
        toolchain_fingerprint=SupplementalToolchainBinding(
            **_derived_binding(
                repository,
                acceptance_path,
                "canonical_toolchain_identity",
                toolchain_bytes,
            ).model_dump(),
            fingerprint=inputs.acceptance.toolchain.fingerprint,
        ),
        task_prompt=task_prompt,
        one_shot_prompt=_derived_binding(
            repository,
            task_prompt_path,
            "rendered_one_shot_prompt",
            inputs.fixed.prompts[Workflow.ONE_SHOT].content,
        ),
        staged_prompt=_derived_binding(
            repository,
            task_prompt_path,
            "rendered_staged_prompt",
            inputs.fixed.prompts[Workflow.STAGED].content,
        ),
        plan_metadata=_file_binding(
            repository,
            plan_publication_path(inputs.plan_path),
            "Plan publication metadata",
        ),
    )
    if (
        artifacts.fixture_snapshot.sha256 != inputs.plan.fixture_sha256
        or artifacts.reference_snapshot.sha256 != inputs.plan.reference_solution_sha256
        or artifacts.task_prompt.sha256 != inputs.plan.task_prompt_sha256
        or artifacts.one_shot_prompt.sha256 != inputs.plan.one_shot_prompt_sha256
        or artifacts.staged_prompt.sha256 != inputs.plan.staged_prompt_sha256
    ):
        raise SupplementalApprovalError("derived Java Artifact binding differs from Plan")
    return artifacts


def _build_packet(
    *,
    repository: Path,
    approval_id: str,
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    accepted_manifest_path: Path,
    prior_provider_call_minimum: int,
    prior_provider_call_maximum: int,
) -> SupplementalLiveCampaignApproval:
    commit = verify_repository_provenance(repository)
    safe_spec = _repository_path(repository, spec_path, "Workflow Spec", require_file=True)
    safe_plan = _repository_path(repository, plan_path, "Workflow Plan", require_file=True)
    try:
        inputs = load_plan_bound_inputs(repository, safe_spec, safe_plan)
    except (Phase6CampaignError, FixtureAcceptanceError, OSError, ValueError) as error:
        raise SupplementalApprovalError(f"Plan-bound Java inputs are invalid: {error}") from error
    if inputs.plan.language is not Language.JAVA:
        raise SupplementalApprovalError("Supplemental Approval requires a Java Plan")
    if inputs.plan.reviewed_commit != commit:
        raise SupplementalApprovalError("reviewed repository HEAD differs from Plan")
    _validate_java_live_contract(inputs)
    safe_campaign = _repository_path(
        repository,
        campaign_path,
        "Campaign JSONL output",
        require_file=False,
    )
    expected_campaign = inputs.artifact_root / "campaign.jsonl"
    if safe_campaign != Path(os.path.abspath(expected_campaign)):
        raise SupplementalApprovalError("Campaign JSONL path differs from Plan artifact root")
    if os.path.lexists(inputs.artifact_root) or os.path.lexists(safe_campaign):
        raise SupplementalApprovalError("Campaign output collision detected")
    artifacts = _artifact_bindings(repository, inputs)
    campaign = SupplementalCampaignContract(
        experiment_id="phase6-java-workflow",
        output_root=_repository_relative(repository, inputs.artifact_root, "Campaign output root"),
        campaign_jsonl=_repository_relative(repository, safe_campaign, "Campaign JSONL path"),
        repository_root=".",
        provider=Provider.CODEX,
        model="gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.HIGH,
        planned_runs=_JAVA_PLANNED_RUNS,
        exact_provider_calls=_JAVA_EXACT_PROVIDER_CALLS,
        workflow_order=list(_JAVA_WORKFLOW_ORDER),
        per_run_provider_calls=list(_JAVA_PER_RUN_PROVIDER_CALLS),
        retry_count=0,
        fallback_count=0,
        resume_count=0,
        stop_policy=SupplementalStopPolicy(
            max_failures=_JAVA_MAX_FAILURES,
            max_total_duration_ms=_JAVA_MAX_TOTAL_DURATION_MS,
            fail_fast=False,
        ),
        create_only_output=True,
        stop_on_collision=True,
        stop_on_input_drift=True,
    )
    accounting = SupplementalProviderAccounting(
        prior_minimum_calls=prior_provider_call_minimum,
        prior_maximum_calls=prior_provider_call_maximum,
        campaign_exact_calls=campaign.exact_provider_calls,
        projected_minimum_calls=(
            prior_provider_call_minimum + campaign.exact_provider_calls
        ),
        projected_maximum_calls=(
            prior_provider_call_maximum + campaign.exact_provider_calls
        ),
    )
    exact_argv = _campaign_argv(campaign, artifacts)
    return SupplementalLiveCampaignApproval(
        schema_version="1.0",
        document_type="supplemental_live_campaign_approval",
        approval_status="pending_human_live_approval",
        approval_id=approval_id,
        language=Language.JAVA,
        reviewed_repository_head=commit,
        reviewed_head_role="harness_head",
        accepted_public_suite=_public_suite_binding(repository, accepted_manifest_path),
        artifacts=artifacts,
        campaign=campaign,
        provider_accounting=accounting,
        human_preflight=SupplementalHumanPreflight(
            authenticated_account_verification="pending",
            quota_verification="pending",
            host_process_verification="pending",
            final_branch_head_worktree_verification="pending",
            output_collision_verification="pending",
            final_artifact_sha_verification="pending",
        ),
        exact_argv=exact_argv,
    )


def _validate_java_live_contract(inputs: PlanBoundInputs) -> None:
    """Enforce the independently approved Java Live conditions, not only Plan consistency."""
    spec = inputs.loaded_spec.spec
    if not isinstance(spec, WorkflowExperimentSpecV2_1):
        raise SupplementalApprovalError("Java Live fixed contract requires Workflow Spec 2.1")
    fixed_checks = (
        (spec.experiment_id == _JAVA_EXPERIMENT_ID, "Spec experiment ID"),
        (inputs.plan.experiment_id == _JAVA_EXPERIMENT_ID, "Plan experiment ID"),
        (spec.provider is Provider.CODEX, "Spec Provider"),
        (inputs.plan.provider is Provider.CODEX, "Plan Provider"),
        (spec.model == _JAVA_MODEL, "Spec model"),
        (inputs.plan.model == _JAVA_MODEL, "Plan model"),
        (spec.reasoning_effort is _JAVA_REASONING_EFFORT, "Spec reasoning effort"),
        (
            inputs.plan.reasoning_effort is _JAVA_REASONING_EFFORT,
            "Plan reasoning effort",
        ),
        (inputs.plan.planned_run_count == _JAVA_PLANNED_RUNS, "planned run count"),
        (
            inputs.plan.planned_provider_call_count == _JAVA_EXACT_PROVIDER_CALLS,
            "exact Provider-call count",
        ),
        (
            tuple(run.workflow for run in inputs.plan.runs) == _JAVA_WORKFLOW_ORDER,
            "workflow order",
        ),
        (
            tuple(run.planned_provider_calls for run in inputs.plan.runs)
            == _JAVA_PER_RUN_PROVIDER_CALLS,
            "per-run Provider-call count",
        ),
        (
            spec.stop_conditions.max_failures == _JAVA_MAX_FAILURES,
            "max_failures",
        ),
        (
            spec.stop_conditions.max_total_duration_ms == _JAVA_MAX_TOTAL_DURATION_MS,
            "max_total_duration_ms",
        ),
        (spec.stop_conditions.fail_fast is False, "fail_fast"),
    )
    for matches, label in fixed_checks:
        if not matches:
            raise SupplementalApprovalError(
                f"Java Live fixed contract differs: {label}"
            )


def _model_from_bytes(content: bytes) -> SupplementalLiveCampaignApproval:
    try:
        return _load_canonical_model_bytes(
            content,
            SupplementalLiveCampaignApproval,
            "Supplemental Live Campaign Approval",
        )
    except Exception as error:
        if isinstance(error, SupplementalApprovalError):
            raise
        raise SupplementalApprovalError(str(error)) from error


def _load_supplemental_live_campaign_approval(
    path: Path,
    *,
    repository_root: Path,
) -> SupplementalLiveCampaignApproval:
    """Strict-load and rederive every packet binding from current repository bytes."""
    try:
        repository = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SupplementalApprovalError("repository root is unavailable") from error
    safe_packet = _repository_path(
        repository,
        path,
        "Supplemental Approval packet",
        require_file=True,
    )
    snapshot = _read_stable_regular_file(safe_packet, "Supplemental Approval packet")
    packet = _model_from_bytes(snapshot.content)
    expected = _build_packet(
        repository=repository,
        approval_id=packet.approval_id,
        spec_path=Path(packet.artifacts.spec.path),
        plan_path=Path(packet.artifacts.plan.path),
        campaign_path=Path(packet.campaign.campaign_jsonl),
        accepted_manifest_path=Path(packet.accepted_public_suite.manifest.path),
        prior_provider_call_minimum=packet.provider_accounting.prior_minimum_calls,
        prior_provider_call_maximum=packet.provider_accounting.prior_maximum_calls,
    )
    if packet != expected:
        raise SupplementalApprovalError(
            "Supplemental Approval differs from rederived canonical bindings"
        )
    return packet


def load_supplemental_live_campaign_approval(
    path: Path,
    *,
    repository_root: Path,
) -> SupplementalLiveCampaignApproval:
    """Strict-load and rederive a packet using one consistent public error type."""
    try:
        return _load_supplemental_live_campaign_approval(
            path,
            repository_root=repository_root,
        )
    except SupplementalApprovalError:
        raise
    except Exception as error:
        raise SupplementalApprovalError(
            f"Supplemental Approval validation failed: {error}"
        ) from error


def _write_create_only(
    path: Path,
    content: bytes,
    *,
    repository: Path,
) -> tuple[tuple[Path, ...], tuple[int, int]]:
    safe = _repository_path(
        repository,
        path,
        "Supplemental Approval output",
        require_file=False,
    )
    if os.path.lexists(safe):
        raise SupplementalApprovalError("Supplemental Approval output already exists")
    created_directories: list[Path] = []
    relative_parent = safe.parent.relative_to(repository)
    current = repository
    descriptor: int | None = None
    opened_identity: tuple[int, int] | None = None
    try:
        for component in relative_parent.parts:
            current /= component
            if os.path.lexists(current):
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise SupplementalApprovalError(
                        "Supplemental Approval output parent is unsafe"
                    )
                continue
            os.mkdir(current, 0o700)
            created_directories.append(current)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(safe, flags, 0o600)
        opened = os.fstat(descriptor)
        opened_identity = (opened.st_dev, opened.st_ino)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("Supplemental Approval write made no progress")
            offset += written
        os.fsync(descriptor)
        published = safe.lstat()
        if (
            (published.st_dev, published.st_ino) != opened_identity
            or not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
        ):
            raise SupplementalApprovalError(
                "Supplemental Approval output changed during publication"
            )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if opened_identity is not None:
            with suppress(OSError):
                current_file = safe.lstat()
                if (current_file.st_dev, current_file.st_ino) == opened_identity:
                    safe.unlink()
        for directory in reversed(created_directories):
            with suppress(OSError):
                directory.rmdir()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert opened_identity is not None
    return tuple(created_directories), opened_identity


def _prepare_supplemental_live_campaign_approval(
    *,
    repository_root: Path,
    approval_id: str,
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    accepted_manifest_path: Path,
    prior_provider_call_minimum: int,
    prior_provider_call_maximum: int,
    output_path: Path,
    confirm_local_execution: bool,
) -> SupplementalApprovalPublication:
    """Create one pending packet after offline validation; never grant Live approval."""
    if not confirm_local_execution:
        raise SupplementalApprovalError("--confirm-local-execution is required")
    try:
        repository = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SupplementalApprovalError("repository root is unavailable") from error
    packet = _build_packet(
        repository=repository,
        approval_id=approval_id,
        spec_path=spec_path,
        plan_path=plan_path,
        campaign_path=campaign_path,
        accepted_manifest_path=accepted_manifest_path,
        prior_provider_call_minimum=prior_provider_call_minimum,
        prior_provider_call_maximum=prior_provider_call_maximum,
    )
    try:
        packet = SupplementalLiveCampaignApproval.model_validate(
            packet.model_dump(mode="json")
        )
    except ValidationError as error:
        raise SupplementalApprovalError(f"invalid Supplemental Approval: {error}") from error
    content = canonical_json_bytes(packet)
    _model_from_bytes(content)
    created_directories, output_identity = _write_create_only(
        output_path,
        content,
        repository=repository,
    )
    try:
        loaded = load_supplemental_live_campaign_approval(
            output_path,
            repository_root=repository,
        )
        if loaded != packet:
            raise SupplementalApprovalError("created packet failed strict reload")
    except Exception:
        safe_output = Path(os.path.abspath(
            output_path if output_path.is_absolute() else repository / output_path
        ))
        with suppress(OSError):
            current_output = safe_output.lstat()
            if (current_output.st_dev, current_output.st_ino) == output_identity:
                safe_output.unlink()
        for directory in reversed(created_directories):
            with suppress(OSError):
                directory.rmdir()
        raise
    return SupplementalApprovalPublication(
        output_path=Path(os.path.abspath(
            output_path if output_path.is_absolute() else repository / output_path
        )),
        byte_count=len(content),
        sha256=_sha256(content),
        packet=packet,
    )


def prepare_supplemental_live_campaign_approval(
    *,
    repository_root: Path,
    approval_id: str,
    spec_path: Path,
    plan_path: Path,
    campaign_path: Path,
    accepted_manifest_path: Path,
    prior_provider_call_minimum: int,
    prior_provider_call_maximum: int,
    output_path: Path,
    confirm_local_execution: bool,
) -> SupplementalApprovalPublication:
    """Create one pending packet and expose only fail-closed domain errors."""
    try:
        return _prepare_supplemental_live_campaign_approval(
            repository_root=repository_root,
            approval_id=approval_id,
            spec_path=spec_path,
            plan_path=plan_path,
            campaign_path=campaign_path,
            accepted_manifest_path=accepted_manifest_path,
            prior_provider_call_minimum=prior_provider_call_minimum,
            prior_provider_call_maximum=prior_provider_call_maximum,
            output_path=output_path,
            confirm_local_execution=confirm_local_execution,
        )
    except SupplementalApprovalError:
        raise
    except Exception as error:
        raise SupplementalApprovalError(
            f"Supplemental Approval preparation failed: {error}"
        ) from error
