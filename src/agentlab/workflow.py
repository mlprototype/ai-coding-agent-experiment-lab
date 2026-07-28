"""Phase 4 Workflow A/B specification, Prompt, and preregistered Plan contracts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, NoReturn

import yaml
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agentlab.live import LiveCodexError, PromptInput, load_prompt
from agentlab.models import (
    ContractModel,
    Provider,
    QualityGate,
    ReasoningEffort,
    RunnerSettings,
    Workflow,
)
from agentlab.workspace import (
    DirectorySnapshot,
    paths_refer_to_same_file,
    validate_fixture_source,
)


class WorkflowSpecError(ValueError):
    """A safe error while loading a Phase 4 Workflow experiment."""


class WorkflowPlanError(ValueError):
    """A safe error while creating or loading a preregistered Plan."""


def _validate_relative_path(value: str, field_name: str, *, file: bool) -> str:
    if not value.strip() or value != value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty relative POSIX path")
    if "\\" in value:
        raise ValueError(f"{field_name} must use POSIX separators")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError(f"{field_name} must be relative")
    if value in {".", "./"} or ".." in posix.parts:
        raise ValueError(f"{field_name} must remain below the Spec directory")
    if not file and posix.suffix:
        raise ValueError(f"{field_name} must name a directory")
    return value


class WorkflowArtifactSettings(ContractModel):
    root: StrictStr = Field(min_length=1)

    @field_validator("root")
    @classmethod
    def root_is_bounded_relative_directory(cls, value: str) -> str:
        return _validate_relative_path(value, "artifacts.root", file=False)


class WorkflowStopConditions(ContractModel):
    max_failures: StrictInt | None = Field(default=None, gt=0)
    max_total_duration_ms: StrictInt | None = Field(default=None, gt=0)
    fail_fast: StrictBool


class WorkflowExperimentSpec(ContractModel):
    """Strict Phase 4 contract; ExperimentSpec 1.0 remains unchanged."""

    schema_version: Literal["2.0"]
    experiment_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    title: StrictStr = Field(min_length=1)
    research_question: StrictStr = Field(min_length=1)
    hypothesis: StrictStr = Field(min_length=1)
    comparison_axis: Literal["workflow"]
    control: Literal[Workflow.ONE_SHOT]
    treatment: Literal[Workflow.STAGED]
    provider: Literal[Provider.CODEX]
    model: StrictStr = Field(min_length=1, max_length=128)
    reasoning_effort: ReasoningEffort
    task_ids: list[StrictStr] = Field(min_length=1)
    repetitions: StrictInt = Field(gt=0, le=100)
    random_seed: StrictInt
    task_prompt_path: StrictStr = Field(min_length=1)
    task_prompt_revision: StrictStr = Field(min_length=1, max_length=128)
    one_shot_revision: StrictStr = Field(min_length=1, max_length=128)
    staged_revision: StrictStr = Field(min_length=1, max_length=128)
    fixture_revision: StrictStr = Field(min_length=1, max_length=128)
    provider_timeout_ms: StrictInt = Field(gt=0, le=1_800_000)
    max_prompt_bytes: StrictInt = Field(gt=0, le=1024 * 1024)
    max_event_line_bytes: StrictInt = Field(gt=0, le=4 * 1024 * 1024)
    max_provider_output_bytes: StrictInt = Field(gt=0, le=64 * 1024 * 1024)
    sandbox: Literal["workspace-write"]
    network_access: Literal[False]
    quality_gate: QualityGate
    runner: RunnerSettings
    stop_conditions: WorkflowStopConditions
    artifacts: WorkflowArtifactSettings

    @field_validator("model")
    @classmethod
    def model_is_exact(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or "\x00" in value:
            raise ValueError("model must be an explicit model identifier")
        mutable = {"latest", "default", "auto"}
        normalized = value.casefold().replace("_", "-").replace("/", "-").split("-")
        if mutable.intersection(normalized):
            raise ValueError("model must not use a mutable alias")
        return value

    @field_validator("task_ids")
    @classmethod
    def task_ids_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("task_ids must be unique")
        for value in values:
            allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
            if not value or any(character not in allowed for character in value):
                raise ValueError("task_ids must use lowercase letters, digits, '_' or '-'")
        return values

    @field_validator("task_prompt_path")
    @classmethod
    def task_prompt_is_bounded_relative_file(cls, value: str) -> str:
        return _validate_relative_path(value, "task_prompt_path", file=True)

    @model_validator(mode="after")
    def fixed_execution_contract_is_coherent(self) -> WorkflowExperimentSpec:
        if self.max_event_line_bytes > self.max_provider_output_bytes:
            raise ValueError("max_event_line_bytes must not exceed max_provider_output_bytes")
        return self


class WorkflowPlanRun(ContractModel):
    run_id: StrictStr = Field(pattern=r"^[a-z0-9_-]+$")
    task_id: StrictStr = Field(min_length=1)
    workflow: Workflow
    repetition_index: StrictInt = Field(ge=0)
    initial_state: Literal["planned"]
    planned_provider_calls: Literal[1]
    task_prompt_revision: StrictStr
    workflow_revision: StrictStr
    fixture_revision: StrictStr
    recording_path: StrictStr
    evidence_path: StrictStr
    diagnostic_path: StrictStr

    @field_validator("recording_path", "evidence_path", "diagnostic_path")
    @classmethod
    def artifact_path_is_relative(cls, value: str, info: Any) -> str:
        return _validate_relative_path(value, str(info.field_name), file=True)


class WorkflowPlan(ContractModel):
    """Deterministic canonical Plan; publication time lives in a sidecar."""

    schema_version: Literal["1.1"]
    experiment_spec_schema_version: Literal["2.0"]
    experiment_id: StrictStr
    experiment_spec_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    task_prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    one_shot_prompt_bytes: StrictInt = Field(gt=0)
    staged_prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    staged_prompt_bytes: StrictInt = Field(gt=0)
    random_seed: StrictInt
    comparison_axis: Literal["workflow"]
    provider: Literal[Provider.CODEX]
    model: StrictStr
    reasoning_effort: ReasoningEffort
    provider_timeout_ms: StrictInt = Field(gt=0)
    task_prompt_revision: StrictStr
    one_shot_revision: StrictStr
    staged_revision: StrictStr
    fixture_revision: StrictStr
    planned_run_count: StrictInt = Field(gt=0)
    planned_provider_call_count: StrictInt = Field(gt=0)
    runs: list[WorkflowPlanRun] = Field(min_length=2)

    @model_validator(mode="after")
    def plan_is_a_complete_blocked_pairing(self) -> WorkflowPlan:
        if self.planned_run_count != len(self.runs):
            raise ValueError("planned_run_count must match runs")
        if self.planned_provider_call_count != sum(run.planned_provider_calls for run in self.runs):
            raise ValueError("planned_provider_call_count must match runs")
        run_ids = [run.run_id for run in self.runs]
        artifact_paths = [
            path
            for run in self.runs
            for path in (run.recording_path, run.evidence_path, run.diagnostic_path)
        ]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run IDs must be unique")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("Artifact reservation paths must be unique")
        blocks: dict[tuple[str, int], list[Workflow]] = {}
        for run in self.runs:
            blocks.setdefault((run.task_id, run.repetition_index), []).append(run.workflow)
        expected = sorted([Workflow.ONE_SHOT, Workflow.STAGED], key=lambda item: item.value)
        malformed_block = any(
            sorted(values, key=lambda item: item.value) != expected for values in blocks.values()
        )
        if malformed_block:
            raise ValueError("each task/repetition block must contain one run per Workflow")
        for index in range(0, len(self.runs), 2):
            pair = self.runs[index : index + 2]
            if len(pair) != 2 or len({(item.task_id, item.repetition_index) for item in pair}) != 1:
                raise ValueError("each randomized block must remain contiguous")
        return self


class WorkflowPlanPublication(ContractModel):
    schema_version: Literal["1.0"]
    plan_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("created_at must be timezone-aware UTC")
        return value


@dataclass(frozen=True)
class LoadedWorkflowSpec:
    spec: WorkflowExperimentSpec
    sha256: str


@dataclass(frozen=True)
class BuiltWorkflowPrompt:
    workflow: Workflow
    content: bytes
    sha256: str
    byte_count: int
    task_sha256: str
    task_byte_count: int
    workflow_revision: str


@dataclass(frozen=True)
class FixedWorkflowInputs:
    """Campaign inputs captured once before the first Provider call."""

    fixture: DirectorySnapshot
    task_prompt: PromptInput
    prompts: Mapping[Workflow, BuiltWorkflowPrompt]


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


def _strict_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError as error:
        raise WorkflowPlanError(f"{label} contains duplicate key {error}") from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkflowPlanError(f"could not read strict {label} JSON") from error
    if not isinstance(raw, dict):
        raise WorkflowPlanError(f"{label} must be a JSON object")
    return raw


def load_workflow_spec(path: Path) -> LoadedWorkflowSpec:
    try:
        source = path.read_bytes()
        raw: Any = yaml.safe_load(source.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WorkflowSpecError(f"could not read Workflow Spec YAML: {error}") from error
    if not isinstance(raw, dict):
        raise WorkflowSpecError("Workflow Spec must be a YAML mapping")
    try:
        spec = WorkflowExperimentSpec.model_validate(raw)
    except ValidationError as error:
        raise WorkflowSpecError(str(error)) from error
    return LoadedWorkflowSpec(spec, hashlib.sha256(source).hexdigest())


def _workflow_instruction(spec: WorkflowExperimentSpec, workflow: Workflow) -> tuple[str, str]:
    if workflow is Workflow.ONE_SHOT:
        return (
            spec.one_shot_revision,
            "Implement the task requirements and perform the checks needed to deliver "
            "a correct result. "
            "Choose the detailed working order yourself.",
        )
    return (
        spec.staged_revision,
        "Within this single Provider turn, follow these logical stages without requesting "
        "additional human input or starting another session:\n"
        "1. Investigate\n"
        "2. Plan\n"
        "3. Check and add tests as needed\n"
        "4. Implement\n"
        "5. Self-review and make necessary corrections\n"
        "Do not treat the stages as separate Provider turns.",
    )


def _build_workflow_prompt_from_task(
    spec: WorkflowExperimentSpec,
    workflow: Workflow,
    task: PromptInput,
) -> BuiltWorkflowPrompt:
    revision, instruction = _workflow_instruction(spec, workflow)
    separator = (
        f"\n\n---\n\nWorkflow template revision: {revision}\nWorkflow instructions:\n"
    ).encode()
    content = task.content + separator + instruction.encode("utf-8") + b"\n"
    if len(content) > spec.max_prompt_bytes:
        raise WorkflowSpecError("generated Prompt exceeds max_prompt_bytes")
    return BuiltWorkflowPrompt(
        workflow=workflow,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        task_sha256=task.sha256,
        task_byte_count=task.byte_count,
        workflow_revision=revision,
    )


def build_workflow_prompt(
    spec_path: Path,
    spec: WorkflowExperimentSpec,
    workflow: Workflow,
) -> BuiltWorkflowPrompt:
    try:
        task = load_prompt(
            spec_path,
            spec.task_prompt_path,
            max_prompt_bytes=spec.max_prompt_bytes,
        )
    except LiveCodexError as error:
        raise WorkflowSpecError(str(error)) from error
    return _build_workflow_prompt_from_task(spec, workflow, task)


def capture_workflow_inputs(
    spec_path: Path,
    spec: WorkflowExperimentSpec,
) -> FixedWorkflowInputs:
    """Read Prompt and Fixture exactly once into immutable Campaign inputs."""
    try:
        task = load_prompt(
            spec_path,
            spec.task_prompt_path,
            max_prompt_bytes=spec.max_prompt_bytes,
        )
        _source, fixture = validate_fixture_source(
            spec_path,
            spec.runner.fixture_path,
        )
    except (LiveCodexError, ValueError) as error:
        raise WorkflowSpecError(str(error)) from error
    prompts = {
        workflow: _build_workflow_prompt_from_task(spec, workflow, task)
        for workflow in (Workflow.ONE_SHOT, Workflow.STAGED)
    }
    return FixedWorkflowInputs(
        fixture=fixture,
        task_prompt=task,
        prompts=prompts,
    )


def workflow_inputs_unchanged(
    spec_path: Path,
    spec: WorkflowExperimentSpec,
    fixed: FixedWorkflowInputs,
) -> bool:
    """Re-read only for integrity; execution always uses the fixed bytes."""
    try:
        task = load_prompt(
            spec_path,
            spec.task_prompt_path,
            max_prompt_bytes=spec.max_prompt_bytes,
        )
        _source, fixture = validate_fixture_source(
            spec_path,
            spec.runner.fixture_path,
        )
    except (LiveCodexError, ValueError):
        return False
    return (
        task.sha256 == fixed.task_prompt.sha256
        and task.byte_count == fixed.task_prompt.byte_count
        and fixture.sha256 == fixed.fixture.sha256
    )


def _stable_digest(*parts: object) -> str:
    encoded = "\0".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_workflow_plan_from_inputs(
    spec_path: Path,
    loaded: LoadedWorkflowSpec,
    fixed: FixedWorkflowInputs,
) -> WorkflowPlan:
    spec = loaded.spec
    one_shot = fixed.prompts[Workflow.ONE_SHOT]
    staged = fixed.prompts[Workflow.STAGED]
    if one_shot.task_sha256 != staged.task_sha256:
        raise WorkflowPlanError("Workflow task requirements are not identical")
    source = spec_path.parent / spec.runner.fixture_path
    artifact_root = spec_path.parent / spec.artifacts.root
    prompt_path = spec_path.parent / spec.task_prompt_path
    try:
        resolved_source = source.resolve(strict=True)
        resolved_prompt = prompt_path.resolve(strict=True)
        resolved_artifact_root = artifact_root.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise WorkflowPlanError("could not resolve protected Plan inputs") from error
    if (
        resolved_artifact_root in {resolved_source, resolved_prompt}
        or resolved_artifact_root.is_relative_to(resolved_source)
    ):
        raise WorkflowPlanError("Artifact root must not overwrite Fixture or Prompt")

    blocks = [
        (task_id, repetition) for task_id in spec.task_ids for repetition in range(spec.repetitions)
    ]
    blocks.sort(key=lambda item: _stable_digest(spec.random_seed, "block", *item))
    runs: list[WorkflowPlanRun] = []
    root = PurePosixPath(spec.artifacts.root)
    for task_id, repetition in blocks:
        workflows = [Workflow.ONE_SHOT, Workflow.STAGED]
        workflows.sort(
            key=lambda workflow: _stable_digest(
                spec.random_seed,
                "workflow",
                task_id,
                repetition,
                workflow.value,
            )
        )
        for workflow in workflows:
            suffix = _stable_digest(
                loaded.sha256,
                task_id,
                repetition,
                workflow.value,
            )[:12]
            run_id = (
                f"{spec.experiment_id}_{task_id}_r{repetition + 1:03d}_{workflow.value}_{suffix}"
            )
            revision = (
                spec.one_shot_revision if workflow is Workflow.ONE_SHOT else spec.staged_revision
            )
            runs.append(
                WorkflowPlanRun(
                    run_id=run_id,
                    task_id=task_id,
                    workflow=workflow,
                    repetition_index=repetition,
                    initial_state="planned",
                    planned_provider_calls=1,
                    task_prompt_revision=spec.task_prompt_revision,
                    workflow_revision=revision,
                    fixture_revision=spec.fixture_revision,
                    recording_path=str(root / "recordings" / f"{run_id}.jsonl"),
                    evidence_path=str(root / "evidence" / f"{run_id}.json"),
                    diagnostic_path=str(root / "diagnostics" / f"{run_id}.json"),
                )
            )
    return WorkflowPlan(
        schema_version="1.1",
        experiment_spec_schema_version="2.0",
        experiment_id=spec.experiment_id,
        experiment_spec_sha256=loaded.sha256,
        task_prompt_sha256=one_shot.task_sha256,
        fixture_sha256=fixed.fixture.sha256,
        one_shot_prompt_sha256=one_shot.sha256,
        one_shot_prompt_bytes=one_shot.byte_count,
        staged_prompt_sha256=staged.sha256,
        staged_prompt_bytes=staged.byte_count,
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
        planned_run_count=len(runs),
        planned_provider_call_count=len(runs),
        runs=runs,
    )


def build_workflow_plan_from_inputs(
    spec_path: Path,
    loaded: LoadedWorkflowSpec,
    fixed: FixedWorkflowInputs,
) -> WorkflowPlan:
    """Build a Plan from the exact inputs captured for a Campaign."""
    return _build_workflow_plan_from_inputs(spec_path, loaded, fixed)


def build_workflow_plan(spec_path: Path) -> WorkflowPlan:
    loaded = load_workflow_spec(spec_path)
    fixed = capture_workflow_inputs(spec_path, loaded.spec)
    return _build_workflow_plan_from_inputs(spec_path, loaded, fixed)


def workflow_prompt_fingerprint(
    plan: WorkflowPlan,
    workflow: Workflow,
) -> tuple[str, int]:
    if workflow is Workflow.ONE_SHOT:
        return plan.one_shot_prompt_sha256, plan.one_shot_prompt_bytes
    return plan.staged_prompt_sha256, plan.staged_prompt_bytes


def workflow_plan_bytes(plan: WorkflowPlan) -> bytes:
    return (
        json.dumps(
            plan.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _publication_bytes(publication: WorkflowPlanPublication) -> bytes:
    return (
        json.dumps(
            publication.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def plan_publication_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.stem}.metadata.json")


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise WorkflowPlanError(f"could not inspect {label}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkflowPlanError(f"{label} path must not contain symlinks")


def _stage(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_create_only_pair(
    first_path: Path,
    first_bytes: bytes,
    second_path: Path,
    second_bytes: bytes,
) -> None:
    for label, path in (("first output", first_path), ("second output", second_path)):
        _reject_symlink_components(path, label)
        if os.path.lexists(path):
            raise WorkflowPlanError(f"{label} already exists")
    if paths_refer_to_same_file(first_path, second_path):
        raise WorkflowPlanError("output paths must not alias")
    first_temporary: Path | None = None
    second_temporary: Path | None = None
    first_published = False
    try:
        first_temporary = _stage(first_path, first_bytes)
        second_temporary = _stage(second_path, second_bytes)
        os.link(first_temporary, first_path)
        first_published = True
        os.link(second_temporary, second_path)
    except (OSError, WorkflowPlanError) as error:
        if first_published:
            with suppress(OSError):
                first_path.unlink()
        if isinstance(error, WorkflowPlanError):
            raise
        raise WorkflowPlanError("could not publish create-only output pair") from error
    finally:
        for temporary in (first_temporary, second_temporary):
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


def create_workflow_plan(spec_path: Path, output_path: Path) -> WorkflowPlan:
    plan = build_workflow_plan(spec_path)
    canonical = workflow_plan_bytes(plan)
    publication = WorkflowPlanPublication(
        schema_version="1.0",
        plan_sha256=hashlib.sha256(canonical).hexdigest(),
        created_at=datetime.now(UTC),
    )
    metadata_path = plan_publication_path(output_path)
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
        raise WorkflowPlanError("could not resolve protected Plan inputs") from error
    reservations = [
        spec_path.parent / configured
        for run in plan.runs
        for configured in (run.recording_path, run.evidence_path, run.diagnostic_path)
    ]
    for reservation in reservations:
        _reject_symlink_components(reservation, "Artifact reservation")
    for candidate in (output_path, metadata_path):
        try:
            resolved_candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise WorkflowPlanError("could not resolve Plan output") from error
        if (
            paths_refer_to_same_file(candidate, spec_path)
            or paths_refer_to_same_file(candidate, prompt_path)
            or resolved_candidate in {resolved_source, resolved_prompt}
            or resolved_candidate.is_relative_to(resolved_source)
        ):
            raise WorkflowPlanError(
                "Plan outputs must not alias or modify protected inputs"
            )
    _publish_create_only_pair(
        output_path,
        canonical,
        metadata_path,
        _publication_bytes(publication),
    )
    return plan


def load_workflow_plan(path: Path) -> WorkflowPlan:
    try:
        return WorkflowPlan.model_validate(_strict_json(path, "Workflow Plan"))
    except ValidationError as error:
        raise WorkflowPlanError(f"invalid Workflow Plan: {error}") from error


def load_plan_publication(path: Path) -> WorkflowPlanPublication:
    try:
        return WorkflowPlanPublication.model_validate(
            _strict_json(plan_publication_path(path), "Plan publication metadata")
        )
    except ValidationError as error:
        raise WorkflowPlanError(f"invalid Plan publication metadata: {error}") from error
