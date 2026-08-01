"""Offline Phase 6 public reporting and create-only publication.

This module consumes only the Artifact paths enumerated by Public Suite Manifest
1.0.  It never invokes a Provider, a quality Gate, or a fixture command.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import TypeAdapter, ValidationError

from agentlab.models import (
    ContractModel,
    Provider,
    UsageMetricSource,
)
from agentlab.phase6 import (
    ArtifactReference,
    ChecksumEntry,
    ExternalChecksumAnchor,
    FixtureManifest,
    HistoricalVerificationRecord,
    Language,
    LiveRunArtifactV1_2,
    LoadedPhase6Campaign,
    PrimarySuiteSource,
    ProviderCoverage,
    PublicChecksums,
    PublicLanguageReport,
    PublicLanguageReportV1_1,
    PublicRunRecord,
    PublicSuiteReport,
    ReleaseMetadata,
    SourceClass,
    ValidatedPublicSuiteInputs,
    WorkflowPlanV1_2,
    _load_canonical_model_bytes,
    _load_live_run_artifact_1_2_bytes,
    _load_phase6_campaign_bytes,
    _load_phase6_recording_bytes,
    _load_workflow_plan_1_2_bytes,
    _provider_call_count_from_codex,
    _require_loaded_inputs_unchanged,
    canonical_json_bytes,
    derive_public_language_counts,
    load_public_suite_inputs,
    validate_checksum_contract,
    validate_public_language_report_campaign,
    validate_public_suite_inputs,
)
from agentlab.workflow import WorkflowPlan, workflow_plan_bytes
from agentlab.workflow_report import (
    WorkflowReport,
    WorkflowReportError,
    aggregate_workflow_campaign,
    workflow_report_json_bytes,
    workflow_report_markdown,
)

RENDERER_VERSION = "phase6-renderer-1.0"


class Phase6PublicError(ValueError):
    """A deterministic rendering, verification, or publication failure."""


@dataclass(frozen=True)
class RenderedPublicSuite:
    files: Mapping[str, bytes]
    checksums: PublicChecksums
    external_anchor: ExternalChecksumAnchor
    external_anchor_bytes: bytes


@dataclass(frozen=True)
class PublicSuitePublication:
    destination: Path
    external_anchor_path: Path
    checksum_manifest_sha256: str
    published_file_count: int


@dataclass(frozen=True)
class HistoricalVerification:
    record: HistoricalVerificationRecord
    record_bytes: bytes
    output_path: Path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise Phase6PublicError("publication timestamp must be timezone-aware UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _timestamp_contract_input(value: datetime) -> datetime:
    """Supply canonical text to before-validators while retaining model typing."""
    return cast(datetime, _canonical_timestamp(value))


def _canonical_sequence_bytes(values: Sequence[ContractModel]) -> bytes:
    return (
        json.dumps(
            [value.model_dump(mode="json") for value in values],
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_canonical_model[T: ContractModel](
    content: bytes,
    model: type[T],
    label: str,
) -> T:
    return _load_canonical_model_bytes(content, model, label)


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or value in {".", "./"}
    ):
        raise Phase6PublicError(f"{label} must be a canonical relative POSIX path")
    return value


def _real_directory(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            component_metadata = current.lstat()
            if stat.S_ISLNK(component_metadata.st_mode):
                raise Phase6PublicError(f"{label} path must not contain symlinks")
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except Phase6PublicError:
        raise
    except OSError as error:
        raise Phase6PublicError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase6PublicError(f"{label} must be a real directory")
    return resolved


def _stable_regular_file(
    root: Path,
    relative: str,
    label: str,
    *,
    directory_identities: dict[Path, tuple[int, int, int]] | None = None,
    file_identities: dict[Path, tuple[int, int, int, int, int, int, int]] | None = None,
) -> bytes:
    """Read one explicitly named file without following any path-component link."""
    relative = _safe_relative(relative, label)
    parts = PurePosixPath(relative).parts
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        descriptors.append(
            os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        )
        root_metadata = os.fstat(descriptors[0])
        root_identity = (
            root_metadata.st_dev,
            root_metadata.st_ino,
            root_metadata.st_mode,
        )
        if directory_identities is not None:
            previous = directory_identities.setdefault(root, root_identity)
            if previous != root_identity:
                raise Phase6PublicError(f"{label} root changed between reads")
        current_path = root
        for component in parts[:-1]:
            before = os.stat(
                component,
                dir_fd=descriptors[-1],
                follow_symlinks=False,
            )
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise Phase6PublicError(f"{label} contains a linked parent")
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptors[-1],
            )
            descriptors.append(child)
            after = os.stat(
                component,
                dir_fd=descriptors[-2],
                follow_symlinks=False,
            )
            opened = os.fstat(child)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
            ) or (before.st_dev, before.st_ino, before.st_mode) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
            ):
                raise Phase6PublicError(f"{label} parent changed during read")
            current_path /= component
            if directory_identities is not None:
                identity = (opened.st_dev, opened.st_ino, opened.st_mode)
                previous = directory_identities.setdefault(current_path, identity)
                if previous != identity:
                    raise Phase6PublicError(
                        f"{label} parent changed between reads"
                    )
        before_file = os.stat(
            parts[-1],
            dir_fd=descriptors[-1],
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(before_file.st_mode)
            or not stat.S_ISREG(before_file.st_mode)
            or before_file.st_nlink != 1
        ):
            raise Phase6PublicError(f"{label} must be a single-link regular file")
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptors[-1],
        )
        opened_file = os.fstat(file_descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_file = os.stat(
            parts[-1],
            dir_fd=descriptors[-1],
            follow_symlinks=False,
        )
        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            for item in (before_file, opened_file, after_file)
        }
        if len(identities) != 1:
            raise Phase6PublicError(f"{label} changed during read")
        if file_identities is not None:
            path = root / relative
            file_identity = next(iter(identities))
            previous_file = file_identities.setdefault(path, file_identity)
            if previous_file != file_identity:
                raise Phase6PublicError(f"{label} changed between reads")
        return b"".join(chunks)
    except Phase6PublicError:
        raise
    except OSError as error:
        raise Phase6PublicError(f"could not read {label} safely") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _revalidate_historical_inputs(
    root: Path,
    contents_by_relative: Mapping[str, bytes],
    directory_identities: dict[Path, tuple[int, int, int]],
    file_identities: dict[Path, tuple[int, int, int, int, int, int, int]],
) -> None:
    for path, expected in directory_identities.items():
        try:
            metadata = path.lstat()
        except OSError as error:
            raise Phase6PublicError("historical directory disappeared") from error
        actual = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise Phase6PublicError("historical directory changed type")
        if actual != expected:
            raise Phase6PublicError("historical directory identity changed")
    for relative, expected_content in contents_by_relative.items():
        current = _stable_regular_file(
            root,
            relative,
            f"historical input {relative}",
            directory_identities=directory_identities,
            file_identities=file_identities,
        )
        if current != expected_content:
            raise Phase6PublicError("historical input bytes changed after validation")


def _create_only_file(path: Path, content: bytes, label: str) -> None:
    parent = _real_directory(path.parent, f"{label} parent")
    target = parent / path.name
    if os.path.lexists(target):
        raise Phase6PublicError(f"{label} already exists")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=parent)
    staging = Path(name)
    published_identity: tuple[int, int, int] | None = None
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _rename_no_replace(staging, target)
        published_identity = _published_identity(target, False)
        _fsync_directory(parent)
    except OSError as error:
        if published_identity is not None:
            _rollback_owned_path(
                target,
                published_identity,
                directory=False,
            )
        raise Phase6PublicError(f"could not create {label}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(staging):
            staging.unlink()


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *arguments],
        cwd=repository,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        },
    )
    if result.returncode != 0:
        raise Phase6PublicError("read-only Git verification failed")
    return result.stdout


def _clean_git_head(repository: Path) -> str:
    repository = _real_directory(repository, "repository root")
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=no")
    if status:
        raise Phase6PublicError("historical verification requires a clean tracked tree")
    head = _git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise Phase6PublicError("Git HEAD is not a full commit hash")
    return head


def _source_reviewed_commit(
    repository: Path,
    historical_root: Path,
    plan: WorkflowPlan,
) -> str:
    spec_path = historical_root / f"{plan.experiment_id}.yaml"
    try:
        relative = spec_path.relative_to(repository).as_posix()
    except ValueError as error:
        raise Phase6PublicError("historical Spec must remain below repository root") from error
    commit = (
        _git(repository, "log", "-1", "--format=%H", "--", relative)
        .decode("ascii")
        .strip()
    )
    if len(commit) != 40:
        raise Phase6PublicError("could not derive the reviewed historical commit")
    saved_spec = _git(repository, "show", f"{commit}:{relative}")
    if _sha256(saved_spec) != plan.experiment_spec_sha256:
        raise Phase6PublicError("reviewed historical Spec does not match Plan")
    return commit


def verify_phase6_historical(
    *,
    repository: Path,
    historical_root: Path,
    plan_path: str,
    campaign_path: str,
    report_json_path: str,
    report_markdown_path: str,
    output_path: Path,
    language: Language,
    confirm_local_execution: bool,
    now: Callable[[], datetime] | None = None,
) -> HistoricalVerification:
    """Verify one saved Phase 4 Campaign without rerunning or regenerating it."""
    if not confirm_local_execution:
        raise Phase6PublicError("historical verification requires explicit confirmation")
    repository = _real_directory(repository, "repository root")
    root = _real_directory(historical_root, "historical Artifact root")
    relative_paths = {
        "plan": _safe_relative(plan_path, "historical Plan path"),
        "campaign": _safe_relative(campaign_path, "historical Campaign path"),
        "report_json": _safe_relative(report_json_path, "historical Report JSON path"),
        "report_markdown": _safe_relative(
            report_markdown_path,
            "historical Report Markdown path",
        ),
    }
    if len(set(relative_paths.values())) != len(relative_paths):
        raise Phase6PublicError("historical input paths must be distinct")
    directory_identities: dict[Path, tuple[int, int, int]] = {}
    file_identities: dict[
        Path,
        tuple[int, int, int, int, int, int, int],
    ] = {}
    contents = {
        role: _stable_regular_file(
            root,
            relative,
            f"historical {role}",
            directory_identities=directory_identities,
            file_identities=file_identities,
        )
        for role, relative in relative_paths.items()
    }
    all_contents = {
        relative_paths[role]: content for role, content in contents.items()
    }

    try:
        plan_raw = json.loads(contents["plan"])
        plan = WorkflowPlan.model_validate(plan_raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        raise Phase6PublicError("invalid historical Workflow Plan 1.1") from error
    if contents["plan"] != workflow_plan_bytes(plan):
        raise Phase6PublicError("historical Workflow Plan must be canonical JSON")
    try:
        saved_report = WorkflowReport.model_validate_json(contents["report_json"])
    except ValidationError as error:
        raise Phase6PublicError("invalid historical Workflow Report JSON") from error
    if contents["report_json"] != workflow_report_json_bytes(saved_report):
        raise Phase6PublicError("historical Workflow Report JSON is not canonical")
    expected_markdown = workflow_report_markdown(saved_report).encode("utf-8")
    if contents["report_markdown"] != expected_markdown:
        raise Phase6PublicError("historical Workflow Report Markdown differs from JSON")

    # Re-aggregate only from stable copies. Evidence and Recording paths are
    # explicitly reserved by the Plan; no directory discovery is performed.
    with tempfile.TemporaryDirectory(prefix="agentlab-phase6-historical-") as temp:
        mirror = Path(temp)
        for role, relative in relative_paths.items():
            target = mirror / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents[role])
        for run in plan.runs:
            for relative, label in (
                (run.evidence_path, "historical Evidence"),
                (run.recording_path, "historical Recording"),
            ):
                relative = _safe_relative(relative, label)
                source = root / relative
                if os.path.lexists(source):
                    data = _stable_regular_file(
                        root,
                        relative,
                        label,
                        directory_identities=directory_identities,
                        file_identities=file_identities,
                    )
                    all_contents[relative] = data
                    target = mirror / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        try:
            regenerated = aggregate_workflow_campaign(
                mirror / f"{plan.experiment_id}.yaml",
                mirror / relative_paths["plan"],
                mirror / relative_paths["campaign"],
            ).model_copy(update={"created_at": saved_report.created_at})
        except WorkflowReportError as error:
            raise Phase6PublicError("historical cross-Artifact validation failed") from error
    if workflow_report_json_bytes(regenerated) != contents["report_json"]:
        raise Phase6PublicError("historical Report does not match offline aggregation")

    verification_commit = _clean_git_head(repository)
    source_commit = _source_reviewed_commit(repository, root, plan)
    try:
        reviewed_spec_path = (
            root / f"{plan.experiment_id}.yaml"
        ).relative_to(repository).as_posix()
    except ValueError as error:
        raise Phase6PublicError(
            "historical root must remain below the repository root"
        ) from error
    _revalidate_historical_inputs(
        root,
        all_contents,
        directory_identities,
        file_identities,
    )
    verified_at = (now or (lambda: datetime.now(UTC)))()
    record = HistoricalVerificationRecord(
        schema_version="1.0",
        source_class=SourceClass.HISTORICAL,
        language=language,
        experiment_id=plan.experiment_id,
        source_reviewed_commit=source_commit,
        verification_agentlab_commit=verification_commit,
        toolchain_version_status="unknown",
        plan_sha256=_sha256(contents["plan"]),
        campaign_sha256=_sha256(contents["campaign"]),
        report_json_sha256=_sha256(contents["report_json"]),
        report_markdown_sha256=_sha256(contents["report_markdown"]),
        strict_schema_validation_passed=True,
        cross_artifact_validation_passed=True,
        artifact_regenerated=False,
        campaign_reexecuted=False,
        validation_commands=[
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
            ],
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "log",
                "-1",
                "--format=%H",
                "--",
                reviewed_spec_path,
            ],
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "show",
                f"{source_commit}:{reviewed_spec_path}",
            ],
        ],
        verified_at=_timestamp_contract_input(verified_at),
    )
    record_bytes = canonical_json_bytes(record)
    _revalidate_historical_inputs(
        root,
        all_contents,
        directory_identities,
        file_identities,
    )
    _create_only_file(output_path, record_bytes, "Historical Verification Record")
    return HistoricalVerification(record=record, record_bytes=record_bytes, output_path=output_path)


@dataclass(frozen=True)
class _PrimaryContext:
    source: PrimarySuiteSource
    plan: WorkflowPlanV1_2 | None
    campaign: LoadedPhase6Campaign | None
    fixture_manifest: FixtureManifest | None
    evidence: Mapping[str, tuple[ArtifactReference, LiveRunArtifactV1_2]]
    recordings: Mapping[str, ArtifactReference]


def _primary_context(
    validated: ValidatedPublicSuiteInputs,
    source: PrimarySuiteSource,
) -> _PrimaryContext:
    loaded = validated.loaded
    plan = (
        _load_workflow_plan_1_2_bytes(loaded.bytes_by_path[source.plan.path])
        if source.plan is not None
        else None
    )
    campaign = (
        _load_phase6_campaign_bytes(loaded.bytes_by_path[source.campaign.path])
        if source.campaign is not None
        else None
    )
    fixture_manifest = (
        _strict_canonical_model(
            loaded.bytes_by_path[source.fixture_manifest.path],
            FixtureManifest,
            "Fixture Manifest",
        )
        if source.fixture_manifest is not None
        else None
    )
    evidence: dict[str, tuple[ArtifactReference, LiveRunArtifactV1_2]] = {}
    for reference in source.evidence:
        artifact = _load_live_run_artifact_1_2_bytes(
            loaded.bytes_by_path[reference.path]
        )
        if artifact.run_id in evidence:
            raise Phase6PublicError("duplicate Evidence run ID")
        evidence[artifact.run_id] = (reference, artifact)
    recordings: dict[str, ArtifactReference] = {}
    for reference in source.recordings:
        recording = _load_phase6_recording_bytes(
            loaded.bytes_by_path[reference.path]
        )
        if recording.started.run_id in recordings:
            raise Phase6PublicError("duplicate Recording run ID")
        recordings[recording.started.run_id] = reference
    return _PrimaryContext(
        source=source,
        plan=plan,
        campaign=campaign,
        fixture_manifest=fixture_manifest,
        evidence=evidence,
        recordings=recordings,
    )


def _eligible_run_records(
    validated: ValidatedPublicSuiteInputs,
    context: _PrimaryContext,
) -> list[tuple[int, PublicRunRecord]]:
    if context.plan is None or context.campaign is None:
        return []
    assert context.source.plan is not None
    assert context.source.campaign is not None
    loaded = validated.loaded
    records: list[tuple[int, PublicRunRecord]] = []
    for index, run in enumerate(context.plan.runs):
        evidence_pair = context.evidence.get(run.run_id)
        recording_reference = context.recordings.get(run.run_id)
        if evidence_pair is None or recording_reference is None:
            continue
        evidence_reference, artifact = evidence_pair
        call_count = _provider_call_count_from_codex(artifact.codex)
        if call_count != 1:
            continue
        if artifact.codex.cli_version is None:
            raise Phase6PublicError(
                f"one-call run lacks an exact CLI version: {run.run_id}"
            )
        metrics = artifact.metrics
        metrics_available = metrics is not None
        usage = artifact.codex.usage_metrics
        usage_observed = usage.source in {
            UsageMetricSource.PROVIDER_REPORTED,
            UsageMetricSource.ESTIMATED,
        }
        usage_source: Literal["provider_reported", "estimated"] | None = None
        if usage.source is UsageMetricSource.PROVIDER_REPORTED:
            usage_source = "provider_reported"
        elif usage.source is UsageMetricSource.ESTIMATED:
            usage_source = "estimated"
        if metrics is None:
            gate_counts = (0, 0, 0, 0, 0)
            metric_values: tuple[int | None, ...] = (None,) * 6
        else:
            gate_counts = (
                metrics.acceptance_tests_passed,
                metrics.acceptance_tests_total,
                metrics.regression_failures,
                metrics.lint_errors,
                metrics.typecheck_errors,
            )
            metric_values = (
                metrics.agent_duration_ms,
                metrics.evaluation_duration_ms,
                metrics.total_duration_ms,
                len(metrics.changed_files),
                metrics.added_lines,
                metrics.deleted_lines,
            )
        record = PublicRunRecord(
            schema_version="1.0",
            reviewed_commit=artifact.reviewed_commit,
            experiment_id=artifact.experiment_id,
            run_id=artifact.run_id,
            task_id=artifact.task_id,
            language=artifact.language,
            provider=Provider.CODEX,
            workflow=artifact.workflow,
            repetition_index=artifact.repetition_index,
            exact_model_id=artifact.codex.requested_model,
            reasoning_effort=artifact.codex.requested_reasoning_effort,
            cli_profile=artifact.codex.cli_profile.value,
            cli_version=artifact.codex.cli_version,
            os=(
                context.fixture_manifest.toolchain.os
                if context.fixture_manifest is not None
                else "unknown"
            ),
            architecture=(
                context.fixture_manifest.toolchain.architecture
                if context.fixture_manifest is not None
                else "unknown"
            ),
            toolchain_fingerprint=artifact.toolchain_fingerprint,
            fixture_sha256=artifact.fixture_sha256,
            prompt_sha256=artifact.prompt_sha256,
            plan_sha256=_sha256(
                loaded.bytes_by_path[context.source.plan.path]
            ),
            campaign_sha256=_sha256(
                loaded.bytes_by_path[context.source.campaign.path]
            ),
            evidence_sha256=_sha256(
                loaded.bytes_by_path[evidence_reference.path]
            ),
            recording_sha256=_sha256(
                loaded.bytes_by_path[recording_reference.path]
            ),
            overall_status=artifact.overall_status,
            failure_kind=artifact.failure_kind,
            provider_call_count=1,
            gate_executed=artifact.gate_executed,
            gate_not_executed_reason=artifact.gate_not_executed_reason,
            run_metrics_available=metrics_available,
            acceptance_passed=gate_counts[0],
            acceptance_total=gate_counts[1],
            regression_failures=gate_counts[2],
            lint_errors=gate_counts[3],
            typecheck_errors=gate_counts[4],
            agent_duration_ms=metric_values[0],
            evaluation_duration_ms=metric_values[1],
            total_duration_ms=metric_values[2],
            changed_file_count=metric_values[3],
            added_lines=metric_values[4],
            deleted_lines=metric_values[5],
            usage_status="observed" if usage_observed else "missing",
            usage_source=usage_source,
            input_tokens=usage.input_tokens if usage_observed else None,
            cached_input_tokens=(
                usage.cached_input_tokens if usage_observed else None
            ),
            output_tokens=usage.output_tokens if usage_observed else None,
            reasoning_output_tokens=(
                usage.reasoning_output_tokens if usage_observed else None
            ),
            started_at=_timestamp_contract_input(artifact.started_at),
            completed_at=_timestamp_contract_input(artifact.completed_at),
        )
        records.append((index, record))
    return records


def _language_report(
    validated: ValidatedPublicSuiteInputs,
    context: _PrimaryContext,
) -> PublicLanguageReportV1_1:
    status = validated.derived_language_status[context.source.language]
    if context.campaign is None:
        return PublicLanguageReportV1_1(
            schema_version="1.1",
            language=context.source.language,
            status=status,
            scheduled_runs=0,
            attempted_runs=0,
            completed_runs=0,
            failed_runs=0,
            interrupted_runs=0,
            not_run_runs=0,
            output_rejected_runs=0,
            gate_not_executed_runs=0,
            gate_not_executed_reason={},
            scheduled_pair_count=0,
            complete_pair_count=0,
            estimability="not_estimable",
            zero_call_runs=0,
            provider_call_count_unknown_runs=0,
        )
    assert context.plan is not None
    counts = derive_public_language_counts(context.campaign)
    report = PublicLanguageReportV1_1(
        schema_version="1.1",
        language=context.source.language,
        status=status,
        **{
            field: getattr(counts, field)
            for field in (
                "scheduled_runs",
                "attempted_runs",
                "completed_runs",
                "failed_runs",
                "interrupted_runs",
                "not_run_runs",
                "output_rejected_runs",
                "gate_not_executed_runs",
                "gate_not_executed_reason",
                "scheduled_pair_count",
                "complete_pair_count",
                "estimability",
                "zero_call_runs",
                "provider_call_count_unknown_runs",
            )
        },
    )
    validate_public_language_report_campaign(
        report,
        context.campaign,
        source=context.source,
        plan=context.plan,
        evidence_run_ids=set(context.evidence),
    )
    return report


def _escape_markdown(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def render_public_language_markdown(report: PublicLanguageReportV1_1) -> bytes:
    reasons = ", ".join(
        f"{reason.value}={count}"
        for reason, count in sorted(
            report.gate_not_executed_reason.items(),
            key=lambda item: item[0].value,
        )
    ) or "none"
    lines = [
        f"# Phase 6 Language Report: {_escape_markdown(report.language.value)}",
        "",
        f"- Status: `{_escape_markdown(report.status.value)}`",
        f"- Estimability: `{report.estimability}`",
        f"- Complete pairs: {report.complete_pair_count}/{report.scheduled_pair_count}",
        "",
        "| Scheduled | Attempted | Completed | Failed | Interrupted | Not run |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {report.scheduled_runs} | {report.attempted_runs} | "
        f"{report.completed_runs} | {report.failed_runs} | "
        f"{report.interrupted_runs} | {report.not_run_runs} |",
        "",
        f"- Output-rejected runs: {report.output_rejected_runs}",
        f"- Gate-not-executed runs: {report.gate_not_executed_runs} ({_escape_markdown(reasons)})",
        f"- Zero-call runs: {report.zero_call_runs}",
        f"- Unknown Provider-call-count runs: {report.provider_call_count_unknown_runs}",
        "",
        "No language winner, leaderboard, or statistical-significance claim is produced.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_public_suite_markdown(report: PublicSuiteReport) -> bytes:
    lines = [
        f"# Phase 6 Public Suite: {_escape_markdown(report.suite_id)}",
        "",
        f"- Renderer: `{_escape_markdown(report.renderer_version)}`",
        f"- Data cutoff: `{_canonical_timestamp(report.data_cutoff_at)}`",
        "- Comparison scope: workflow within each language; no cross-language winner.",
        "",
        "| Language | Status | Scheduled | Attempted | Completed | Failed | Complete pairs |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for language in report.languages:
        lines.append(
            f"| {_escape_markdown(language.language.value)} | "
            f"{_escape_markdown(language.status.value)} | {language.scheduled_runs} | "
            f"{language.attempted_runs} | {language.completed_runs} | "
            f"{language.failed_runs} | {language.complete_pair_count} |"
        )
    lines.extend(
        [
            "",
            "## Provider coverage",
            "",
            "| Provider | Evaluation | Languages | Blocker |",
            "|---|---|---|---|",
        ]
    )
    for coverage in report.provider_coverage:
        languages = ", ".join(item.value for item in coverage.evaluated_languages) or "none"
        lines.append(
            f"| {_escape_markdown(coverage.provider.value)} | "
            f"{_escape_markdown(coverage.evaluation_status.value)} | "
            f"{_escape_markdown(languages)} | "
            f"{_escape_markdown(coverage.blocker or 'none')} |"
        )
    lines.extend(
        [
            "",
            "Antigravity is not evaluated because the upstream artifact signature was invalid.",
            "Live outputs are not claimed to be fully reproducible; saved normalized "
            "inputs and evaluation evidence are replay-auditable.",
            "No overall winner, leaderboard, or statistical-significance claim is produced.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _planned_output_paths(
    contexts: Sequence[_PrimaryContext],
    historical_count: int,
    records_by_language: Mapping[Language, list[tuple[int, PublicRunRecord]]],
) -> list[str]:
    outputs = {
        "suite.json",
        "suite.md",
        "provider-coverage.json",
        "release-metadata.json",
        "checksums.json",
    }
    for context in contexts:
        language = context.source.language.value
        outputs.add(f"languages/{language}/report.json")
        outputs.add(f"languages/{language}/report.md")
        for index, _ in records_by_language[context.source.language]:
            outputs.add(f"runs/{language}/{index:03d}.json")
    for index in range(historical_count):
        outputs.add(f"historical/{index:03d}/verification.json")
    return sorted(outputs)


def _strict_reload_generated_json(path: str, content: bytes) -> None:
    if path == "suite.json":
        model: ContractModel = _strict_canonical_model(
            content, PublicSuiteReport, "Public Suite Report"
        )
    elif path.endswith("/report.json"):
        model = _strict_canonical_model(
            content, PublicLanguageReportV1_1, "Public Language Report"
        )
    elif path.startswith("runs/"):
        model = _strict_canonical_model(content, PublicRunRecord, "Public Run Record")
    elif path.startswith("historical/"):
        model = _strict_canonical_model(
            content, HistoricalVerificationRecord, "Historical Verification Record"
        )
    elif path == "release-metadata.json":
        model = _strict_canonical_model(content, ReleaseMetadata, "Release Metadata")
    elif path == "checksums.json":
        model = _strict_canonical_model(content, PublicChecksums, "Public Checksums")
    elif path == "provider-coverage.json":
        try:
            coverage = TypeAdapter(list[ProviderCoverage]).validate_json(content)
        except ValidationError as error:
            raise Phase6PublicError("invalid generated Provider coverage") from error
        if content != _canonical_sequence_bytes(coverage):
            raise Phase6PublicError("Provider coverage is not canonical")
        return
    else:
        return
    if content != canonical_json_bytes(model):
        raise Phase6PublicError(f"generated JSON is not canonical: {path}")


_FORBIDDEN_PUBLIC_BYTES = (
    b'"prompt"',
    b'"raw_output"',
    b'"raw_provider_output"',
    b'"agent_message"',
    b'"unified_diff"',
    b'"stdout"',
    b'"stderr"',
    b'"thread_id"',
    b'"session_id"',
    b"CODEX_HOME",
    b"OPENAI_API_KEY",
    b"CODEX_API_KEY",
    b"SECRET_SENTINEL",
    b"THREAD_SENTINEL",
    b"sk-",
    b"Authorization:",
    b"Bearer ",
    b"/Users/",
    b"/home/",
    b"/private/",
)


def _scan_public_bytes(files: Mapping[str, bytes], anchor: bytes) -> None:
    for path, content in (*files.items(), ("external-anchor", anchor)):
        for forbidden in _FORBIDDEN_PUBLIC_BYTES:
            if forbidden in content:
                raise Phase6PublicError(
                    f"public allowlist leak scan rejected {path}"
                )


def _validate_rendered_files(rendered: RenderedPublicSuite) -> None:
    checksum_entries = {entry.path: entry for entry in rendered.checksums.entries}
    expected_checksum_paths = set(rendered.files) - {"checksums.json"}
    if set(checksum_entries) != expected_checksum_paths:
        raise Phase6PublicError("staged checksum path set changed")
    for path, content in rendered.files.items():
        _strict_reload_generated_json(path, content)
        if path == "checksums.json":
            continue
        entry = checksum_entries[path]
        if entry.size_bytes != len(content) or entry.sha256 != _sha256(content):
            raise Phase6PublicError(f"staged checksum or size differs: {path}")
    checksum_bytes = rendered.files["checksums.json"]
    if (
        rendered.external_anchor.checksum_manifest_sha256
        != _sha256(checksum_bytes)
        or rendered.external_anchor_bytes
        != canonical_json_bytes(rendered.external_anchor)
    ):
        raise Phase6PublicError("external anchor differs from checksums.json")
    suite = _strict_canonical_model(
        rendered.files["suite.json"],
        PublicSuiteReport,
        "Public Suite Report",
    )
    try:
        coverage = TypeAdapter(list[ProviderCoverage]).validate_json(
            rendered.files["provider-coverage.json"]
        )
    except ValidationError as error:
        raise Phase6PublicError("invalid staged Provider coverage") from error
    if coverage != suite.provider_coverage:
        raise Phase6PublicError("Provider coverage differs from Suite JSON")
    if rendered.files["suite.md"] != render_public_suite_markdown(suite):
        raise Phase6PublicError("Suite Markdown does not match strict-loaded JSON")
    for path in sorted(rendered.files):
        if not path.startswith("languages/") or not path.endswith("/report.json"):
            continue
        report = _strict_canonical_model(
            rendered.files[path],
            PublicLanguageReportV1_1,
            "Public Language Report",
        )
        markdown_path = path.removesuffix(".json") + ".md"
        if rendered.files[markdown_path] != render_public_language_markdown(report):
            raise Phase6PublicError(
                "Language Markdown does not match strict-loaded JSON"
            )
    _scan_public_bytes(rendered.files, rendered.external_anchor_bytes)


def render_public_suite(
    validated: ValidatedPublicSuiteInputs,
) -> RenderedPublicSuite:
    """Render a byte-deterministic Suite entirely from validated saved inputs."""
    manifest = validated.loaded.manifest
    if manifest.renderer_version != RENDERER_VERSION:
        raise Phase6PublicError("unsupported Public Suite renderer version")
    contexts = [
        _primary_context(validated, source)
        for source in manifest.primary_sources
    ]
    records_by_language = {
        context.source.language: _eligible_run_records(validated, context)
        for context in contexts
    }
    historical = sorted(
        manifest.historical_sources,
        key=lambda source: (
            source.language.value,
            source.verification_record.sha256,
            source.plan.sha256,
            source.campaign.sha256,
        ),
    )
    expected_outputs = _planned_output_paths(
        contexts,
        len(historical),
        records_by_language,
    )
    if manifest.planned_outputs != expected_outputs:
        raise Phase6PublicError(
            "Manifest planned_outputs differs from the deterministic renderer layout"
        )

    language_reports: list[PublicLanguageReport | PublicLanguageReportV1_1] = [
        _language_report(validated, context) for context in contexts
    ]
    suite_report = PublicSuiteReport(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        renderer_version=manifest.renderer_version,
        generated_at=_timestamp_contract_input(manifest.data_cutoff_at),
        data_cutoff_at=_timestamp_contract_input(manifest.data_cutoff_at),
        languages=language_reports,
        provider_coverage=manifest.provider_coverage,
        automatic_winner_selected=False,
        leaderboard_generated=False,
        statistical_significance_claimed=False,
    )
    files: dict[str, bytes] = {}
    # Generation order is contractually fixed: Run Records, language reports,
    # Suite reports, coverage, historical copies, then release metadata.
    for context in contexts:
        language = context.source.language.value
        for index, record in records_by_language[context.source.language]:
            files[f"runs/{language}/{index:03d}.json"] = canonical_json_bytes(record)
    for context, generic_report in zip(contexts, language_reports, strict=True):
        if not isinstance(generic_report, PublicLanguageReportV1_1):
            raise Phase6PublicError("renderer requires Public Language Report 1.1")
        report = generic_report
        language = context.source.language.value
        files[f"languages/{language}/report.json"] = canonical_json_bytes(report)
        files[f"languages/{language}/report.md"] = render_public_language_markdown(report)
    files["suite.json"] = canonical_json_bytes(suite_report)
    files["suite.md"] = render_public_suite_markdown(suite_report)
    files["provider-coverage.json"] = _canonical_sequence_bytes(
        manifest.provider_coverage
    )
    for index, source in enumerate(historical):
        record_bytes = validated.loaded.bytes_by_path[
            source.verification_record.path
        ]
        _strict_canonical_model(
            record_bytes,
            HistoricalVerificationRecord,
            "Historical Verification Record",
        )
        files[f"historical/{index:03d}/verification.json"] = record_bytes

    release = ReleaseMetadata(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        renderer_version=manifest.renderer_version,
        data_cutoff_at=_timestamp_contract_input(manifest.data_cutoff_at),
        checksum_manifest_path="checksums.json",
        checksum_digest_anchored_externally=True,
        authenticity_claimed=False,
    )
    files["release-metadata.json"] = canonical_json_bytes(release)
    for path, content in files.items():
        _strict_reload_generated_json(path, content)
    checksums = PublicChecksums(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        entries=[
            ChecksumEntry(
                path=path,
                size_bytes=len(content),
                sha256=_sha256(content),
            )
            for path, content in sorted(files.items())
        ],
        excluded_paths=["checksums.json"],
        authenticity_claimed=False,
    )
    _scan_public_bytes(files, b"")
    checksum_bytes = canonical_json_bytes(checksums)
    files["checksums.json"] = checksum_bytes
    anchor = ExternalChecksumAnchor(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        checksum_manifest_path="checksums.json",
        checksum_manifest_sha256=_sha256(checksum_bytes),
        authenticity_claimed=False,
    )
    anchor_bytes = canonical_json_bytes(anchor)
    _strict_reload_generated_json("checksums.json", checksum_bytes)
    _strict_canonical_model(anchor_bytes, ExternalChecksumAnchor, "External anchor")
    validate_checksum_contract(
        manifest=manifest,
        checksums=checksums,
        release_metadata=release,
        external_anchor=anchor,
        checksum_bytes=checksum_bytes,
    )
    if set(files) != set(manifest.planned_outputs):
        raise Phase6PublicError("rendered output set differs from Manifest")
    rendered = RenderedPublicSuite(
        files=dict(sorted(files.items())),
        checksums=checksums,
        external_anchor=anchor,
        external_anchor_bytes=anchor_bytes,
    )
    _validate_rendered_files(rendered)
    _require_loaded_inputs_unchanged(validated.loaded)
    return rendered


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Use the platform's atomic no-replace rename primitive."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(source_bytes, destination_bytes, 0x00000004)
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            result = rename(
                -100,
                source_bytes,
                -100,
                destination_bytes,
                0x00000001,
            )
        else:
            raise Phase6PublicError(
                "atomic no-replace publication is unsupported on this platform"
            )
    except AttributeError as error:
        raise Phase6PublicError(
            "atomic no-replace publication is unavailable"
        ) from error
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise Phase6PublicError("publication destination already exists")
    raise Phase6PublicError(
        f"atomic no-replace publication failed: {os.strerror(error_number)}"
    )


def _write_staging_bundle(staging: Path, rendered: RenderedPublicSuite) -> None:
    _validate_rendered_files(rendered)
    created_directories = {staging}
    for relative, content in rendered.files.items():
        relative = _safe_relative(relative, "rendered output path")
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.parent
        while current != staging.parent:
            created_directories.add(current)
            if current == staging:
                break
            current = current.parent
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if target.read_bytes() != content:
            raise Phase6PublicError(f"staged output bytes changed: {relative}")
        _strict_reload_generated_json(relative, content)
    for directory in sorted(
        created_directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    # Required second staging-root sync occurs after the last file and all
    # strict reload/checksum validation work.
    _validate_rendered_files(rendered)
    _fsync_directory(staging)


def _write_anchor_staging(parent: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".phase6-anchor-", dir=parent)
    path = Path(name)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if path.read_bytes() != content:
        raise Phase6PublicError("staged external anchor bytes changed")
    _strict_canonical_model(content, ExternalChecksumAnchor, "External anchor")
    _fsync_directory(parent)
    return path


def _published_identity(path: Path, directory: bool) -> tuple[int, int, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or (
        directory and not stat.S_ISDIR(metadata.st_mode)
    ) or (not directory and not stat.S_ISREG(metadata.st_mode)):
        raise Phase6PublicError("published output changed type")
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _rollback_owned_path(
    path: Path,
    identity: tuple[int, int, int],
    *,
    directory: bool,
) -> None:
    if not os.path.lexists(path):
        return
    current = _published_identity(path, directory)
    if current != identity:
        raise Phase6PublicError("refusing to remove a replaced publication path")
    if directory:
        shutil.rmtree(path)
    else:
        path.unlink()


def publish_public_suite(
    *,
    manifest_path: Path,
    root: Path,
    destination: Path,
    external_anchor_path: Path,
    confirm_publication: bool,
) -> PublicSuitePublication:
    """Create one immutable public bundle and its bundle-external digest anchor."""
    if not confirm_publication:
        raise Phase6PublicError("public Suite publication requires explicit confirmation")
    loaded = load_public_suite_inputs(manifest_path, root=root)
    validated = validate_public_suite_inputs(loaded)
    rendered = render_public_suite(validated)
    _require_loaded_inputs_unchanged(loaded)

    destination_parent = _real_directory(destination.parent, "destination parent")
    anchor_parent = _real_directory(external_anchor_path.parent, "anchor parent")
    destination = destination_parent / destination.name
    external_anchor_path = anchor_parent / external_anchor_path.name
    if destination.name in {"", ".", ".."} or external_anchor_path.name in {
        "",
        ".",
        "..",
    }:
        raise Phase6PublicError("publication paths must name files below real parents")
    if destination == external_anchor_path:
        raise Phase6PublicError("bundle and external anchor paths must differ")
    if os.path.lexists(destination) or os.path.lexists(external_anchor_path):
        raise Phase6PublicError("publication destination already exists")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-staging-", dir=destination_parent)
    )
    lock_path = destination_parent / f".{destination.name}.publish.lock"
    anchor_staging: Path | None = None
    lock_descriptor: int | None = None
    lock_identity: tuple[int, int, int] | None = None
    published_bundle: tuple[int, int, int] | None = None
    published_anchor: tuple[int, int, int] | None = None
    try:
        _write_staging_bundle(staging, rendered)
        anchor_staging = _write_anchor_staging(
            anchor_parent,
            rendered.external_anchor_bytes,
        )
        _require_loaded_inputs_unchanged(loaded)
        try:
            lock_descriptor = os.open(
                lock_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError as error:
            raise Phase6PublicError("Public Suite publish lock is held") from error
        lock_metadata = os.fstat(lock_descriptor)
        lock_identity = (
            lock_metadata.st_dev,
            lock_metadata.st_ino,
            lock_metadata.st_mode,
        )
        os.fsync(lock_descriptor)
        _fsync_directory(destination_parent)
        if os.path.lexists(destination) or os.path.lexists(external_anchor_path):
            raise Phase6PublicError("publication destination appeared after lock")
        _require_loaded_inputs_unchanged(loaded)

        _rename_no_replace(staging, destination)
        published_bundle = _published_identity(destination, True)
        _rename_no_replace(anchor_staging, external_anchor_path)
        published_anchor = _published_identity(external_anchor_path, False)
        anchor_staging = None
        _fsync_directory(destination_parent)
        if anchor_parent != destination_parent:
            _fsync_directory(anchor_parent)
    except Exception as original_error:
        rollback_error: Exception | None = None
        try:
            if published_anchor is not None:
                _rollback_owned_path(
                    external_anchor_path,
                    published_anchor,
                    directory=False,
                )
            if published_bundle is not None:
                _rollback_owned_path(
                    destination,
                    published_bundle,
                    directory=True,
                )
            if published_anchor is not None or published_bundle is not None:
                _fsync_directory(destination_parent)
                if anchor_parent != destination_parent:
                    _fsync_directory(anchor_parent)
        except Exception as error:  # pragma: no cover - exceptional safety report
            rollback_error = error
        if rollback_error is not None:
            raise Phase6PublicError(
                "publication failed and owned-output rollback could not be verified"
            ) from rollback_error
        if isinstance(original_error, Phase6PublicError):
            raise
        raise Phase6PublicError("Public Suite publication failed safely") from original_error
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if lock_identity is not None and os.path.lexists(lock_path):
            try:
                metadata = lock_path.lstat()
                current_lock_identity = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                )
                if (
                    current_lock_identity == lock_identity
                    and stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink == 1
                ):
                    lock_path.unlink()
                    _fsync_directory(destination_parent)
            except OSError:
                pass
        if os.path.lexists(staging):
            shutil.rmtree(staging)
        if anchor_staging is not None and os.path.lexists(anchor_staging):
            anchor_staging.unlink()
    return PublicSuitePublication(
        destination=destination,
        external_anchor_path=external_anchor_path,
        checksum_manifest_sha256=rendered.external_anchor.checksum_manifest_sha256,
        published_file_count=len(rendered.files),
    )
