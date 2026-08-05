"""Phase 7A read-only Evidence Inventory contracts and verifier.

The module intentionally only reads the repository and publishes three new
create-only files.  It never invokes a Provider, Gate, Campaign, or network
operation.  The observed repository HEAD is a checkout observation only; it
is not binary provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, TypeVar

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

from agentlab.models import ContractModel

SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$|^[0-9a-f]{64}$"
ID_PATTERN = r"^[a-z][a-z0-9_-]{0,127}$"
CANONICAL_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

# These limits bound a local snapshot without making any claim about the
# size of a real Phase 6 Artifact. They are deliberately generous for the
# synthetic-only Phase 7 verifier and are part of the read-only contract.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_FILES = 4096
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_PUBLICATION_FILE_BYTES = 64 * 1024 * 1024

TContract = TypeVar("TContract", bound=ContractModel)


class InventoryError(ValueError):
    """Base error for Phase 7 inventory operations."""


class InventoryContractError(InventoryError):
    """Request or persisted output violates its strict contract."""


class InventorySafetyError(InventoryError):
    """A safe, stable snapshot or bounded local observation was impossible."""


class InventoryPublicationError(InventoryError):
    """The create-only publication could not be safely completed."""


class InventoryScope(StrEnum):
    PHASE6 = "phase6"


class ReleaseClassification(StrEnum):
    ACCEPTED_CURRENT = "accepted_current"
    ACCEPTED_SUPERSEDED = "accepted_superseded"
    HISTORICAL = "historical"
    CANDIDATE_UNACCEPTED = "candidate_unaccepted"
    ABANDONED_PREPARATION = "abandoned_preparation"


class CampaignClassification(StrEnum):
    PRIMARY_EVALUATION = "primary_evaluation"
    AUDIT_ONLY_FAILURE = "audit_only_failure"
    ABANDONED_INCONCLUSIVE = "abandoned_inconclusive"
    HISTORICAL_NON_PRIMARY = "historical_non_primary"


class StorageState(StrEnum):
    PRESENT = "present"
    PARTIAL = "partial"
    MISSING = "missing"


class IntegrityState(StrEnum):
    VERIFIED = "verified"
    DRIFTED = "drifted"
    NOT_VERIFIABLE = "not_verifiable"


class RetentionState(StrEnum):
    LOCAL_ONLY = "local_only"
    EXTERNAL_COPY_RECEIPT_VERIFIED = "external_copy_receipt_verified"
    UNKNOWN = "unknown"


class RetentionVerificationBasis(StrEnum):
    LOCAL_ARTIFACT_ONLY = "local_artifact_only"
    RECEIPT_ONLY = "receipt_only"
    NOT_AVAILABLE = "not_available"


class RemoteLiveness(StrEnum):
    NOT_CHECKED = "not_checked"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"


class CommitVerificationMode(StrEnum):
    INTERNAL_REQUIRED = "internal_required"
    INTERNAL_IF_PRESENT = "internal_if_present"
    DECLARATION_BASIS_ONLY = "declaration_basis_only"


class FindingCode(StrEnum):
    AUTHORITY_REFERENCE_MISSING = "authority_reference_missing"
    AUTHORITY_REFERENCE_BYTES_MISMATCH = "authority_reference_bytes_mismatch"
    AUTHORITY_REFERENCE_SHA256_MISMATCH = "authority_reference_sha256_mismatch"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_BYTES_MISMATCH = "artifact_bytes_mismatch"
    ARTIFACT_SHA256_MISMATCH = "artifact_sha256_mismatch"
    UNSAFE_ARTIFACT = "unsafe_artifact"
    UNEXPECTED_PATH = "unexpected_path"
    CANONICAL_LOAD_FAILED = "canonical_load_failed"
    CROSS_ARTIFACT_MISMATCH = "cross_artifact_mismatch"
    BUNDLE_RENDERER_MISMATCH = "bundle_renderer_mismatch"
    CHECKSUM_CONTRACT_MISMATCH = "checksum_contract_mismatch"
    EXTERNAL_ANCHOR_MISMATCH = "external_anchor_mismatch"
    ARTIFACT_REVIEWED_COMMIT_MISMATCH = "artifact_reviewed_commit_mismatch"
    ARTIFACT_REVIEWED_COMMIT_NOT_VERIFIABLE = "artifact_reviewed_commit_not_verifiable"
    CLASSIFICATION_MISMATCH = "classification_mismatch"
    DENOMINATOR_MISMATCH = "denominator_mismatch"
    EXECUTION_REPOSITORY_HEAD_MISMATCH = "execution_repository_head_mismatch"
    RETENTION_RECEIPT_MISSING = "retention_receipt_missing"
    RETENTION_RECEIPT_BYTES_MISMATCH = "retention_receipt_bytes_mismatch"
    RETENTION_RECEIPT_SHA256_MISMATCH = "retention_receipt_sha256_mismatch"
    RETENTION_RECEIPT_INVALID = "retention_receipt_invalid"


class ReleaseArtifactRole(StrEnum):
    SUITE_MANIFEST = "suite_manifest"
    CHECKSUMS = "checksums"
    EXTERNAL_ANCHOR = "external_anchor"
    RELEASE_METADATA = "release_metadata"


class CampaignArtifactRole(StrEnum):
    SPEC = "spec"
    PLAN = "plan"
    CAMPAIGN = "campaign"
    RECORDING = "recording"
    EVIDENCE = "evidence"
    REPORT_JSON = "report_json"
    REPORT_MARKDOWN = "report_markdown"
    HISTORICAL_VERIFICATION = "historical_verification"
    FIXTURE_MANIFEST = "fixture_manifest"
    FIXTURE_ACCEPTANCE = "fixture_acceptance"
    DIFF_POLICY = "diff_policy"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_relative(value: str, label: str) -> str:
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
        raise ValueError(f"{label} must be a canonical relative POSIX path")
    return value


def _stable_id(value: str, label: str) -> str:
    if not re.fullmatch(ID_PATTERN, value):
        raise ValueError(f"{label} must be a stable lowercase identifier")
    return value


class AuthorityReference(ContractModel):
    reference_id: StrictStr = Field(pattern=ID_PATTERN)
    kind: Literal["accepted_manifest", "tracked_closeout", "human_acceptance_record"]
    path: StrictStr
    byte_count: StrictInt = Field(ge=0, le=MAX_ARTIFACT_FILE_BYTES)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    description: StrictStr = Field(min_length=1, max_length=240)

    @field_validator("path")
    @classmethod
    def path_is_canonical(cls, value: str) -> str:
        return _canonical_relative(value, "authority reference path")


class ExpectedFileArtifact(ContractModel):
    """One expected single-link regular file."""

    role: StrictStr = Field(pattern=ID_PATTERN)
    path: StrictStr
    byte_count: StrictInt = Field(ge=0, le=MAX_ARTIFACT_FILE_BYTES)
    sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    required: StrictBool = True

    @field_validator("path")
    @classmethod
    def path_is_canonical(cls, value: str) -> str:
        return _canonical_relative(value, "expected file path")


class ExpectedTree(ContractModel):
    """One expected real directory and its fully enumerated file set."""

    role: StrictStr = Field(pattern=ID_PATTERN)
    root_path: StrictStr
    allowed_directories: list[StrictStr] = Field(default_factory=list)
    file_artifacts: list[ExpectedFileArtifact] = Field(
        default_factory=list,
        max_length=MAX_TREE_FILES,
    )
    expected_file_count: StrictInt = Field(ge=0, le=MAX_TREE_FILES)
    tree_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    required: StrictBool = True

    @field_validator("root_path")
    @classmethod
    def root_is_canonical(cls, value: str) -> str:
        return _canonical_relative(value, "expected tree root")

    @field_validator("allowed_directories")
    @classmethod
    def directories_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = [_canonical_relative(value, "allowed tree directory") for value in values]
        if normalized != sorted(set(normalized)):
            raise ValueError("allowed tree directories must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def file_set_is_coherent(self) -> ExpectedTree:
        paths = [item.path for item in self.file_artifacts]
        if paths != sorted(set(paths)):
            raise ValueError("ExpectedTree file_artifacts must be unique and sorted")
        if self.expected_file_count != len(self.file_artifacts):
            raise ValueError("ExpectedTree file count must match file_artifacts")
        return self


class ReleaseEntry(ContractModel):
    release_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    commit_verification_mode: CommitVerificationMode
    classification: ReleaseClassification
    verification_profile: Literal[
        "phase6_public_suite",
        "phase6_campaign_complete",
        "historical_verification",
        "declared_artifact_set",
    ]
    declaration_basis: StrictStr = Field(min_length=1, max_length=240)
    file_artifacts: list[ExpectedFileArtifact] = Field(default_factory=list)
    trees: list[ExpectedTree] = Field(default_factory=list)
    superseded_by: StrictStr | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def release_roles_are_closed(self) -> ReleaseEntry:
        if not self.file_artifacts and not self.trees:
            raise ValueError("ReleaseEntry must declare at least one Artifact")
        allowed = {item.value for item in ReleaseArtifactRole}
        if any(item.role not in allowed for item in self.file_artifacts):
            raise ValueError("ReleaseEntry contains an unsupported file role")
        if any(item.role != "bundle_root" for item in self.trees):
            raise ValueError("ReleaseEntry tree role must be bundle_root")
        if self.classification in {
            ReleaseClassification.ACCEPTED_CURRENT,
            ReleaseClassification.ACCEPTED_SUPERSEDED,
        } and (
            self.verification_profile != "phase6_public_suite"
            or self.commit_verification_mode is not CommitVerificationMode.INTERNAL_REQUIRED
        ):
            raise ValueError("accepted release requires public-suite/internal commit verification")
        if self.verification_profile == "phase6_public_suite":
            required_roles = {
                ReleaseArtifactRole.SUITE_MANIFEST.value,
                ReleaseArtifactRole.CHECKSUMS.value,
                ReleaseArtifactRole.EXTERNAL_ANCHOR.value,
            }
            declared_roles = {item.role for item in self.file_artifacts}
            if not required_roles.issubset(declared_roles):
                raise ValueError(
                    "phase6_public_suite release requires Manifest, checksums, and anchor"
                )
        if self.classification is ReleaseClassification.ABANDONED_PREPARATION and (
            self.commit_verification_mode is CommitVerificationMode.INTERNAL_REQUIRED
        ):
            raise ValueError("abandoned preparation may not require an internal commit")
        return self


class CampaignEntry(ContractModel):
    campaign_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    commit_verification_mode: CommitVerificationMode
    classification: CampaignClassification
    included_in_primary_denominator: StrictBool
    release_id: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
    verification_profile: Literal[
        "phase6_public_suite",
        "phase6_campaign_complete",
        "historical_verification",
        "declared_artifact_set",
    ]
    declaration_basis: StrictStr = Field(min_length=1, max_length=240)
    file_artifacts: list[ExpectedFileArtifact] = Field(default_factory=list)
    trees: list[ExpectedTree] = Field(default_factory=list)

    @model_validator(mode="after")
    def campaign_contract_is_coherent(self) -> CampaignEntry:
        if not self.file_artifacts and not self.trees:
            raise ValueError("CampaignEntry must declare at least one Artifact")
        allowed = {item.value for item in CampaignArtifactRole}
        if any(item.role not in allowed for item in self.file_artifacts):
            raise ValueError("CampaignEntry contains an unsupported file role")
        if any(item.role != "bundle_root" for item in self.trees):
            raise ValueError("CampaignEntry tree role must be bundle_root")
        should_be_primary = self.classification is CampaignClassification.PRIMARY_EVALUATION
        if self.included_in_primary_denominator is not should_be_primary:
            raise ValueError("only primary_evaluation may enter the denominator")
        if should_be_primary and self.release_id is None:
            raise ValueError("primary_evaluation requires a release_id")
        if should_be_primary and (
            self.verification_profile != "phase6_campaign_complete"
            or self.commit_verification_mode is not CommitVerificationMode.INTERNAL_REQUIRED
        ):
            raise ValueError(
                "primary evaluation requires campaign-complete/internal commit verification"
            )
        if (
            self.classification
            in {
                CampaignClassification.ABANDONED_INCONCLUSIVE,
                CampaignClassification.HISTORICAL_NON_PRIMARY,
            }
            and self.commit_verification_mode is CommitVerificationMode.INTERNAL_REQUIRED
        ):
            raise ValueError(
                "abandoned or historical non-primary may not require an absent internal commit"
            )
        return self


class ExternalCopyReceipt(ContractModel):
    schema_version: Literal["1.0"]
    subject_kind: Literal["release", "campaign"]
    subject_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_path: StrictStr
    artifact_byte_count: StrictInt = Field(ge=0)
    artifact_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    created_at: StrictStr

    @field_validator("artifact_path")
    @classmethod
    def artifact_path_is_canonical(cls, value: str) -> str:
        return _canonical_relative(value, "receipt artifact path")

    @field_validator("created_at")
    @classmethod
    def created_at_is_canonical(cls, value: str) -> str:
        if not CANONICAL_TIMESTAMP_PATTERN.fullmatch(value):
            raise ValueError("receipt created_at must be canonical UTC")
        return value


class RetentionExpectation(ContractModel):
    subject_kind: Literal["release", "campaign"]
    subject_id: StrictStr = Field(pattern=ID_PATTERN)
    expected_retention_state: RetentionState
    external_copy_receipt: ExpectedFileArtifact | None = None
    declaration_basis: StrictStr = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def receipt_matches_state(self) -> RetentionExpectation:
        needs_receipt = (
            self.expected_retention_state is RetentionState.EXTERNAL_COPY_RECEIPT_VERIFIED
        )
        if needs_receipt and self.external_copy_receipt is None:
            raise ValueError("external_copy_receipt_verified requires a receipt Artifact")
        if not needs_receipt and self.external_copy_receipt is not None:
            raise ValueError("receipt is only allowed for verified external copy state")
        return self


class EvidenceInventoryRequest(ContractModel):
    schema_version: Literal["1.0"]
    inventory_id: StrictStr = Field(pattern=ID_PATTERN)
    authoritative: Literal[False]
    scope: Literal[InventoryScope.PHASE6]
    expected_execution_repository_head: StrictStr | None = Field(
        default=None,
        pattern=COMMIT_PATTERN,
    )
    source_of_truth_references: list[AuthorityReference] = Field(min_length=1)
    release_entries: list[ReleaseEntry] = Field(default_factory=list)
    campaign_entries: list[CampaignEntry] = Field(default_factory=list)
    retention_expectations: list[RetentionExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def request_invariants(self) -> EvidenceInventoryRequest:
        reference_ids = [item.reference_id for item in self.source_of_truth_references]
        if reference_ids != sorted(set(reference_ids)):
            raise ValueError("authority references must be unique and sorted")
        release_ids = [item.release_id for item in self.release_entries]
        if release_ids != sorted(set(release_ids)):
            raise ValueError("release entries must be unique and sorted")
        campaign_ids = [item.campaign_id for item in self.campaign_entries]
        if campaign_ids != sorted(set(campaign_ids)):
            raise ValueError("campaign entries must be unique and sorted")
        current = [
            item
            for item in self.release_entries
            if item.classification is ReleaseClassification.ACCEPTED_CURRENT
        ]
        if len(current) > 1:
            raise ValueError("at most one accepted_current release is allowed")
        release_by_id = set(release_ids)
        for entry in self.release_entries:
            if entry.superseded_by is not None and entry.superseded_by not in release_by_id:
                raise ValueError("superseded_by must name a declared release")
        for entry in self.release_entries:
            seen: set[str] = set()
            current_id: str | None = entry.release_id
            while current_id is not None:
                if current_id in seen:
                    raise ValueError("release supersession graph contains a cycle")
                seen.add(current_id)
                target: ReleaseEntry | None = next(
                    (
                        release_entry
                        for release_entry in self.release_entries
                        if release_entry.release_id == current_id
                    ),
                    None,
                )
                current_id = target.superseded_by if target is not None else None
        campaign_by_id: dict[str, CampaignEntry] = {}
        for campaign_entry in self.campaign_entries:
            if campaign_entry.campaign_id in campaign_by_id:
                raise ValueError("campaign IDs must be unique")
            campaign_by_id[campaign_entry.campaign_id] = campaign_entry
        retention_ids: set[tuple[str, str]] = set()
        for expectation in self.retention_expectations:
            key = (expectation.subject_kind, expectation.subject_id)
            if key in retention_ids:
                raise ValueError("retention expectations must be unique")
            retention_ids.add(key)
            if (
                expectation.subject_kind == "release"
                and expectation.subject_id not in release_by_id
            ):
                raise ValueError("retention release must be declared")
            if (
                expectation.subject_kind == "campaign"
                and expectation.subject_id not in campaign_by_id
            ):
                raise ValueError("retention campaign must be declared")
        for campaign_entry in self.campaign_entries:
            if (
                campaign_entry.release_id is not None
                and campaign_entry.release_id not in release_by_id
            ):
                raise ValueError("campaign release_id must name a declared release")
        all_paths: list[str] = []
        for release_entry in self.release_entries:
            all_paths.extend(item.path for item in release_entry.file_artifacts)
            all_paths.extend(tree.root_path for tree in release_entry.trees)
            all_paths.extend(
                f"{tree.root_path}/{item.path}"
                for tree in release_entry.trees
                for item in tree.file_artifacts
            )
        for campaign_entry in self.campaign_entries:
            all_paths.extend(item.path for item in campaign_entry.file_artifacts)
            all_paths.extend(tree.root_path for tree in campaign_entry.trees)
            all_paths.extend(
                f"{tree.root_path}/{item.path}"
                for tree in campaign_entry.trees
                for item in tree.file_artifacts
            )
        if len(all_paths) != len(set(all_paths)):
            raise ValueError("Request Artifact paths must be unique")
        return self


class ArtifactObservation(ContractModel):
    role: StrictStr = Field(pattern=ID_PATTERN)
    path: StrictStr
    kind: Literal["file", "tree"]
    required: StrictBool
    storage_state: StorageState
    integrity_state: IntegrityState
    expected_byte_count: StrictInt | None = Field(default=None, ge=0)
    observed_byte_count: StrictInt | None = Field(default=None, ge=0)
    expected_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    observed_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    expected_file_count: StrictInt | None = Field(default=None, ge=0)
    observed_file_count: StrictInt | None = Field(default=None, ge=0)
    expected_tree_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)
    observed_tree_sha256: StrictStr | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def observation_path_is_canonical(cls, value: str) -> str:
        return _canonical_relative(value, "observation path")


class InventoryReleaseEntry(ContractModel):
    release_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    commit_verification_mode: CommitVerificationMode
    commit_verification: IntegrityState
    classification: ReleaseClassification
    verification_profile: StrictStr
    storage_state: StorageState
    integrity_state: IntegrityState
    artifact_observations: list[ArtifactObservation]


class InventoryCampaignEntry(ContractModel):
    campaign_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    commit_verification_mode: CommitVerificationMode
    commit_verification: IntegrityState
    classification: CampaignClassification
    included_in_primary_denominator: StrictBool
    release_id: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
    verification_profile: StrictStr
    storage_state: StorageState
    integrity_state: IntegrityState
    artifact_observations: list[ArtifactObservation]


class InventoryRetention(ContractModel):
    subject_kind: Literal["release", "campaign"]
    subject_id: StrictStr = Field(pattern=ID_PATTERN)
    retention_state: RetentionState
    verification_basis: RetentionVerificationBasis
    remote_liveness: Literal[RemoteLiveness.NOT_CHECKED]
    receipt_path: StrictStr | None = None

    @field_validator("receipt_path")
    @classmethod
    def receipt_path_is_canonical(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_relative(value, "receipt path")


class InventoryFinding(ContractModel):
    subject_kind: Literal["authority", "release", "campaign", "retention", "request"]
    subject_id: StrictStr = Field(pattern=ID_PATTERN)
    code: FindingCode
    detail: StrictStr = Field(min_length=1, max_length=500)
    artifact_role: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
    path: StrictStr | None = None

    @field_validator("path")
    @classmethod
    def finding_path_is_canonical(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_relative(value, "finding path")


class InventorySummary(ContractModel):
    release_count: StrictInt = Field(ge=0)
    campaign_count: StrictInt = Field(ge=0)
    primary_denominator: StrictInt = Field(ge=0)
    classification_counts: dict[StrictStr, StrictInt]
    storage_state_counts: dict[StorageState, StrictInt]
    integrity_state_counts: dict[IntegrityState, StrictInt]
    provider_call_count_observed: StrictInt = Field(ge=0)
    provider_call_count_unknown: StrictInt = Field(ge=0)


class EvidenceInventory(ContractModel):
    schema_version: Literal["1.0"]
    inventory_id: StrictStr = Field(pattern=ID_PATTERN)
    request_correlation_id: StrictStr = Field(pattern=ID_PATTERN)
    authoritative: Literal[False]
    scope: Literal[InventoryScope.PHASE6]
    request_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_of_truth_references: list[AuthorityReference]
    releases: list[InventoryReleaseEntry]
    campaigns: list[InventoryCampaignEntry]
    retention: list[InventoryRetention]
    findings: list[InventoryFinding]
    summary: InventorySummary
    verification_status: VerificationStatus

    @model_validator(mode="after")
    def output_is_sorted(self) -> EvidenceInventory:
        if [item.release_id for item in self.releases] != sorted(
            item.release_id for item in self.releases
        ):
            raise ValueError("Inventory releases must be sorted")
        if [item.campaign_id for item in self.campaigns] != sorted(
            item.campaign_id for item in self.campaigns
        ):
            raise ValueError("Inventory campaigns must be sorted")
        if self.findings != sorted(
            self.findings,
            key=lambda item: (item.subject_kind, item.subject_id, item.code.value, item.path or ""),
        ):
            raise ValueError("Inventory findings must be deterministically sorted")
        if self.verification_status is VerificationStatus.VERIFIED and self.findings:
            raise ValueError("verified Inventory must not contain findings")
        if self.verification_status is VerificationStatus.FAILED and not self.findings:
            raise ValueError("failed Inventory requires findings")
        return self


class EvidenceInventoryMetadata(ContractModel):
    schema_version: Literal["1.0"]
    request_correlation_id: StrictStr = Field(pattern=ID_PATTERN)
    request_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    inventory_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    markdown_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    expected_execution_repository_head: StrictStr | None = Field(
        default=None,
        pattern=COMMIT_PATTERN,
    )
    observed_execution_repository_head: StrictStr = Field(pattern=COMMIT_PATTERN)
    generated_at: StrictStr
    renderer_version: StrictStr = Field(min_length=1)
    tool_version: StrictStr = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_canonical(cls, value: str) -> str:
        if not CANONICAL_TIMESTAMP_PATTERN.fullmatch(value):
            raise ValueError("generated_at must be canonical UTC")
        return value


@dataclass(frozen=True)
class InventoryPublication:
    inventory: EvidenceInventory
    metadata: EvidenceInventoryMetadata
    inventory_bytes: bytes
    markdown_bytes: bytes
    metadata_bytes: bytes
    output_path: Path
    markdown_path: Path
    metadata_path: Path
    exit_code: int


@dataclass(frozen=True)
class _FileRead:
    exists: bool
    safe: bool
    content: bytes | None
    byte_count: int | None
    sha256: str | None
    reason: str | None = None


@dataclass(frozen=True)
class _ObservationResult:
    observation: ArtifactObservation
    findings: tuple[InventoryFinding, ...]
    contents: Mapping[str, bytes]
    reviewed_commits: frozenset[str]


@dataclass(frozen=True)
class _AuthorityObservation:
    findings: tuple[InventoryFinding, ...]
    content: bytes | None


@dataclass(frozen=True)
class _VerificationResult:
    inventory: EvidenceInventory
    observed_execution_repository_head: str


def _canonical_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_inventory_json_bytes(value: ContractModel | Mapping[str, Any]) -> bytes:
    """Serialize a Phase 7 contract with deterministic JSON bytes."""
    raw = value.model_dump(mode="python") if isinstance(value, ContractModel) else value
    return (
        json.dumps(
            _canonical_value(raw),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json_bytes(content: bytes, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> NoReturn:
        raise InventoryContractError(f"{label} contains non-finite number {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InventoryContractError(f"{label} contains duplicate JSON key")
            result[key] = value
        return result

    try:
        decoded = content.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except InventoryContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InventoryContractError(f"invalid JSON in {label}") from error
    if not isinstance(value, dict):
        raise InventoryContractError(f"{label} must contain a JSON object")
    return value


def _load_canonical_bytes[TModel: ContractModel](
    content: bytes,
    model: type[TModel],
    label: str,
) -> TModel:
    raw = _strict_json_bytes(content, label)
    try:
        value = model.model_validate(raw)
    except ValidationError as error:
        raise InventoryContractError(f"invalid {label}") from error
    if content != canonical_inventory_json_bytes(value):
        raise InventoryContractError(f"{label} must use canonical JSON serialization")
    return value


def load_inventory_request_bytes(content: bytes) -> EvidenceInventoryRequest:
    return _load_canonical_bytes(content, EvidenceInventoryRequest, "Evidence Inventory Request")


def _absolute_below(root: Path, relative: str, label: str) -> Path:
    try:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        candidate.relative_to(root)
    except (ValueError, RuntimeError) as error:
        raise InventorySafetyError(f"{label} escapes repository root") from error
    return candidate


def _real_directory(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(path))
    current = Path(lexical.anchor)
    try:
        for component in lexical.parts[1:]:
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise InventorySafetyError(f"{label} contains a symlink")
        metadata = lexical.lstat()
    except InventorySafetyError:
        raise
    except OSError as error:
        raise InventorySafetyError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InventorySafetyError(f"{label} must be a real directory")
    return lexical


def _validate_parent_components(root: Path, relative: str, label: str) -> None:
    current = root
    parts = PurePosixPath(relative).parts
    for component in parts[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise InventorySafetyError(f"could not inspect {label} parent") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise InventorySafetyError(f"{label} parent contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            return


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_file(
    root: Path,
    relative: str,
    label: str,
    *,
    max_bytes: int = MAX_ARTIFACT_FILE_BYTES,
) -> _FileRead:
    """Read a final regular file without following any path component link."""
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise InventorySafetyError(f"{label} repository root changed") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise InventorySafetyError(f"{label} repository root is unsafe")
    _validate_parent_components(root, relative, label)
    path = _absolute_below(root, relative, label)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return _FileRead(False, True, None, None, None, "missing")
    except OSError as error:
        raise InventorySafetyError(f"could not inspect {label}") from error
    if stat.S_ISLNK(before.st_mode):
        return _FileRead(True, True, None, None, None, "symlink")
    if stat.S_ISREG(before.st_mode) and before.st_nlink != 1:
        return _FileRead(True, True, None, None, None, "hardlink")
    if not stat.S_ISREG(before.st_mode):
        return _FileRead(True, True, None, None, None, "special_file")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise InventorySafetyError(f"{label} changed during read")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise InventorySafetyError(f"{label} exceeds the bounded read limit")
        after = path.lstat()
        if _identity(after) != _identity(before):
            raise InventorySafetyError(f"{label} changed during read")
        _validate_parent_components(root, relative, label)
        if _identity(root.lstat()) != _identity(root_metadata):
            raise InventorySafetyError(f"{label} repository root changed during read")
        content = b"".join(chunks)
        return _FileRead(True, True, content, len(content), _sha256(content))
    except InventorySafetyError:
        raise
    except OSError as error:
        raise InventorySafetyError(f"could not read {label} safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_request_file(path: Path, root: Path) -> bytes:
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as error:
        raise InventorySafetyError("Request must remain below repository root") from error
    result = _read_regular_file(
        root,
        relative,
        "Request",
        max_bytes=MAX_REQUEST_BYTES,
    )
    if not result.exists or result.content is None or result.reason is not None:
        raise InventorySafetyError("Request must be a stable single-link regular file")
    return result.content


def _safe_json_object(content: bytes) -> dict[str, Any] | None:
    try:
        return _strict_json_bytes(content, "Artifact")
    except InventoryContractError:
        return None


def _reviewed_commits(content: bytes) -> frozenset[str]:
    raw = _safe_json_object(content)
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if (
                    key
                    in {
                        "reviewed_commit",
                        "source_reviewed_commit",
                        "acceptance_agentlab_commit",
                    }
                    and isinstance(child, str)
                    and re.fullmatch(COMMIT_PATTERN, child)
                ):
                    found.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    if raw is not None:
        visit(raw)
    else:
        for line in content.splitlines():
            try:
                line_object = _strict_json_bytes(line, "Artifact JSONL")
            except InventoryContractError:
                break
            visit(line_object)
        else:
            return frozenset(found)
        try:
            yaml_object = yaml.safe_load(content.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            yaml_object = None
        if isinstance(yaml_object, Mapping):
            visit(yaml_object)
    return frozenset(found)


def _known_contract_is_valid(role: str, content: bytes) -> bool:
    """Use Phase 6 strict loaders for roles whose contract is known."""
    if role == "report_markdown":
        return True
    try:
        from agentlab.phase6 import validate_phase6_snapshot_contract

        known_roles = {
            "suite_manifest",
            "checksums",
            "external_anchor",
            "release_metadata",
            "fixture_manifest",
            "fixture_acceptance",
            "historical_verification",
            "plan",
            "campaign",
            "recording",
            "evidence",
            "spec",
        }
        if role in known_roles:
            validate_phase6_snapshot_contract(role, content)
        elif role in {"report_json", "diff_policy"}:
            raw = _strict_json_bytes(content, role)
            if content != canonical_inventory_json_bytes(raw):
                return False
        return True
    except Exception:
        return False


_DETAIL_TEMPLATES: dict[FindingCode, str] = {
    FindingCode.AUTHORITY_REFERENCE_MISSING: "authority reference {role} is missing at {path}",
    FindingCode.AUTHORITY_REFERENCE_BYTES_MISMATCH: "authority reference {role} byte count differs",
    FindingCode.AUTHORITY_REFERENCE_SHA256_MISMATCH: "authority reference {role} SHA-256 differs",
    FindingCode.ARTIFACT_MISSING: "expected {role} is missing at {path}",
    FindingCode.ARTIFACT_BYTES_MISMATCH: "expected {role} byte count differs at {path}",
    FindingCode.ARTIFACT_SHA256_MISMATCH: "expected {role} SHA-256 differs at {path}",
    FindingCode.UNSAFE_ARTIFACT: "expected {role} at {path} is an unsafe final artifact",
    FindingCode.UNEXPECTED_PATH: "profile contains an unexpected path under {path}",
    FindingCode.CANONICAL_LOAD_FAILED: "expected {role} at {path} failed its strict contract",
    FindingCode.CROSS_ARTIFACT_MISMATCH: "Phase 6 Artifact bindings differ for {role} at {path}",
    FindingCode.BUNDLE_RENDERER_MISMATCH: (
        "stored bundle bytes differ from deterministic renderer for {role}"
    ),
    FindingCode.CHECKSUM_CONTRACT_MISMATCH: (
        "checksum coverage does not match the declared bundle for {role}"
    ),
    FindingCode.EXTERNAL_ANCHOR_MISMATCH: (
        "external checksum anchor does not match checksums for {role}"
    ),
    FindingCode.ARTIFACT_REVIEWED_COMMIT_MISMATCH: (
        "declared reviewed commit differs from observed Artifact commit for {role}"
    ),
    FindingCode.ARTIFACT_REVIEWED_COMMIT_NOT_VERIFIABLE: (
        "Artifact does not contain an internal reviewed commit for {role}"
    ),
    FindingCode.CLASSIFICATION_MISMATCH: (
        "declared classification conflicts with the Artifact profile for {role}"
    ),
    FindingCode.DENOMINATOR_MISMATCH: "primary denominator binding differs for {role}",
    FindingCode.EXECUTION_REPOSITORY_HEAD_MISMATCH: (
        "expected execution checkout HEAD differs from observed HEAD"
    ),
    FindingCode.RETENTION_RECEIPT_MISSING: "retention receipt is missing at {path}",
    FindingCode.RETENTION_RECEIPT_BYTES_MISMATCH: "retention receipt byte count differs at {path}",
    FindingCode.RETENTION_RECEIPT_SHA256_MISMATCH: "retention receipt SHA-256 differs at {path}",
    FindingCode.RETENTION_RECEIPT_INVALID: (
        "retention receipt contract or Artifact binding is invalid at {path}"
    ),
}


def _finding(
    code: FindingCode,
    *,
    subject_kind: Literal["authority", "release", "campaign", "retention", "request"],
    subject_id: str,
    role: str | None = None,
    path: str | None = None,
    expected: str | int | None = None,
    observed: str | int | None = None,
) -> InventoryFinding:
    template = _DETAIL_TEMPLATES[code]
    detail = template.format(
        role=role or "declared Artifact",
        path=path or "declared scope",
        expected=expected if expected is not None else "unknown",
        observed=observed if observed is not None else "unknown",
    )
    if (
        expected is not None
        and observed is not None
        and code
        in {
            FindingCode.ARTIFACT_BYTES_MISMATCH,
            FindingCode.ARTIFACT_SHA256_MISMATCH,
            FindingCode.AUTHORITY_REFERENCE_BYTES_MISMATCH,
            FindingCode.AUTHORITY_REFERENCE_SHA256_MISMATCH,
            FindingCode.EXECUTION_REPOSITORY_HEAD_MISMATCH,
        }
    ):
        detail = f"{detail}; expected={expected}; observed={observed}"
    return InventoryFinding(
        subject_kind=subject_kind,
        subject_id=subject_id,
        code=code,
        detail=detail,
        artifact_role=role,
        path=path,
    )


def _observe_file(
    *,
    root: Path,
    subject_kind: Literal["release", "campaign", "authority", "retention"],
    subject_id: str,
    expected: ExpectedFileArtifact,
    role_prefix: str | None = None,
) -> _ObservationResult:
    read = _read_regular_file(root, expected.path, f"{subject_id} {expected.role}")
    role = role_prefix or expected.role
    findings: list[InventoryFinding] = []
    if not read.exists:
        if expected.required:
            findings.append(
                _finding(
                    FindingCode.ARTIFACT_MISSING,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=role,
                    path=expected.path,
                )
            )
        observation = ArtifactObservation(
            role=role,
            path=expected.path,
            kind="file",
            required=expected.required,
            storage_state=StorageState.MISSING,
            integrity_state=IntegrityState.NOT_VERIFIABLE,
            expected_byte_count=expected.byte_count,
            expected_sha256=expected.sha256,
        )
        return _ObservationResult(observation, tuple(findings), {}, frozenset())
    if read.reason is not None:
        findings.append(
            _finding(
                FindingCode.UNSAFE_ARTIFACT,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
                path=expected.path,
            )
        )
        observation = ArtifactObservation(
            role=role,
            path=expected.path,
            kind="file",
            required=expected.required,
            storage_state=StorageState.PRESENT,
            integrity_state=IntegrityState.DRIFTED,
            expected_byte_count=expected.byte_count,
            expected_sha256=expected.sha256,
        )
        return _ObservationResult(observation, tuple(findings), {}, frozenset())
    assert read.content is not None and read.byte_count is not None and read.sha256 is not None
    if read.byte_count != expected.byte_count:
        findings.append(
            _finding(
                FindingCode.ARTIFACT_BYTES_MISMATCH,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
                path=expected.path,
                expected=expected.byte_count,
                observed=read.byte_count,
            )
        )
    if read.sha256 != expected.sha256:
        findings.append(
            _finding(
                FindingCode.ARTIFACT_SHA256_MISMATCH,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
                path=expected.path,
                expected=expected.sha256,
                observed=read.sha256,
            )
        )
    if not _known_contract_is_valid(expected.role, read.content):
        findings.append(
            _finding(
                FindingCode.CANONICAL_LOAD_FAILED,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
                path=expected.path,
            )
        )
    observation = ArtifactObservation(
        role=role,
        path=expected.path,
        kind="file",
        required=expected.required,
        storage_state=StorageState.PRESENT,
        integrity_state=IntegrityState.VERIFIED if not findings else IntegrityState.DRIFTED,
        expected_byte_count=expected.byte_count,
        observed_byte_count=read.byte_count,
        expected_sha256=expected.sha256,
        observed_sha256=read.sha256,
    )
    return _ObservationResult(
        observation,
        tuple(findings),
        {expected.path: read.content},
        _reviewed_commits(read.content),
    )


def compute_tree_sha256(
    files: Mapping[str, tuple[int, str]],
    directories: Iterable[str],
) -> str:
    """Compute the domain-separated digest used by ExpectedTree."""
    parts = [b"agentlab.phase7.tree.v1\0"]
    for relative in sorted(set(directories)):
        parts.append(b"D\0")
        parts.append(relative.encode("utf-8"))
        parts.append(b"\0")
    for relative in sorted(files):
        byte_count, digest = files[relative]
        parts.append(b"F\0")
        parts.append(relative.encode("utf-8"))
        parts.append(b"\0")
        parts.append(str(byte_count).encode("ascii"))
        parts.append(b"\0")
        parts.append(digest.encode("ascii"))
        parts.append(b"\0")
    return _sha256(b"".join(parts))


def _tree_relative_path(tree: ExpectedTree, file: ExpectedFileArtifact) -> str:
    return f"{tree.root_path}/{file.path}"


def _observe_tree(
    *,
    root: Path,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    tree: ExpectedTree,
) -> _ObservationResult:
    tree_path = _absolute_below(root, tree.root_path, f"{subject_id} tree")
    findings: list[InventoryFinding] = []
    try:
        root_metadata = tree_path.lstat()
    except FileNotFoundError:
        findings = (
            [
                _finding(
                    FindingCode.ARTIFACT_MISSING,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=tree.role,
                    path=tree.root_path,
                )
            ]
            if tree.required
            else []
        )
        observation = ArtifactObservation(
            role=tree.role,
            path=tree.root_path,
            kind="tree",
            required=tree.required,
            storage_state=StorageState.MISSING,
            integrity_state=IntegrityState.NOT_VERIFIABLE,
            expected_file_count=tree.expected_file_count,
            expected_tree_sha256=tree.tree_sha256,
        )
        return _ObservationResult(observation, tuple(findings), {}, frozenset())
    except OSError as error:
        raise InventorySafetyError(f"could not inspect {subject_id} tree") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        finding = _finding(
            FindingCode.UNSAFE_ARTIFACT,
            subject_kind=subject_kind,
            subject_id=subject_id,
            role=tree.role,
            path=tree.root_path,
        )
        observation = ArtifactObservation(
            role=tree.role,
            path=tree.root_path,
            kind="tree",
            required=tree.required,
            storage_state=StorageState.PRESENT,
            integrity_state=IntegrityState.DRIFTED,
            expected_file_count=tree.expected_file_count,
            expected_tree_sha256=tree.tree_sha256,
        )
        return _ObservationResult(observation, (finding,), {}, frozenset())

    expected_paths = {item.path: item for item in tree.file_artifacts}
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    unsafe_paths: set[str] = set()
    directory_identities: dict[str, tuple[int, int, int, int, int, int, int]] = {
        "": _identity(root_metadata),
    }
    stack: list[tuple[Path, str]] = [(tree_path, "")]
    while stack:
        current, relative_directory = stack.pop()
        try:
            current_metadata = current.lstat()
        except OSError as error:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}") from error
        expected_identity = directory_identities.get(relative_directory)
        if expected_identity is None or _identity(current_metadata) != expected_identity:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}")
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}") from error
        for entry in entries:
            relative = (
                entry.name if not relative_directory else f"{relative_directory}/{entry.name}"
            )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise InventorySafetyError(
                    f"tree identity could not be established for {subject_id}"
                ) from error
            unsafe = (
                stat.S_ISLNK(metadata.st_mode)
                or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
                or (not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode))
            )
            if unsafe:
                unsafe_paths.add(relative)
                if len(actual_files) + len(unsafe_paths) > MAX_TREE_FILES:
                    raise InventorySafetyError(
                        f"tree exceeds the bounded file limit for {subject_id}"
                    )
                findings.append(
                    _finding(
                        FindingCode.UNSAFE_ARTIFACT,
                        subject_kind=subject_kind,
                        subject_id=subject_id,
                        role=tree.role,
                        path=f"{tree.root_path}/{relative}",
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                actual_directories.add(relative)
                directory_identities[relative] = _identity(metadata)
                if relative not in tree.allowed_directories:
                    findings.append(
                        _finding(
                            FindingCode.UNEXPECTED_PATH,
                            subject_kind=subject_kind,
                            subject_id=subject_id,
                            role=tree.role,
                            path=f"{tree.root_path}/{relative}",
                        )
                    )
                stack.append((Path(entry.path), relative))
            else:
                actual_files.add(relative)
                if len(actual_files) + len(unsafe_paths) > MAX_TREE_FILES:
                    raise InventorySafetyError(
                        f"tree exceeds the bounded file limit for {subject_id}"
                    )
                if relative not in expected_paths:
                    findings.append(
                        _finding(
                            FindingCode.UNEXPECTED_PATH,
                            subject_kind=subject_kind,
                            subject_id=subject_id,
                            role=tree.role,
                            path=f"{tree.root_path}/{relative}",
                        )
                    )

    for relative_directory, expected_identity in directory_identities.items():
        directory_path = tree_path if not relative_directory else tree_path / relative_directory
        try:
            current_identity = _identity(directory_path.lstat())
        except OSError as error:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}") from error
        if current_identity != expected_identity:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}")

    contents: dict[str, bytes] = {}
    commits: set[str] = set()
    observed_files: dict[str, tuple[int, str]] = {}
    observed_tree_bytes = 0
    for relative, expected in expected_paths.items():
        full_expected = expected.model_copy(update={"path": _tree_relative_path(tree, expected)})
        result = _observe_file(
            root=root,
            subject_kind=subject_kind,
            subject_id=subject_id,
            expected=full_expected,
        )
        findings.extend(result.findings)
        contents.update(result.contents)
        commits.update(result.reviewed_commits)
        if result.observation.observed_byte_count is not None:
            observed_tree_bytes += result.observation.observed_byte_count
            if observed_tree_bytes > MAX_TREE_BYTES:
                raise InventorySafetyError(f"tree exceeds the bounded byte limit for {subject_id}")
        if (
            result.observation.observed_byte_count is not None
            and result.observation.observed_sha256 is not None
        ):
            observed_files[relative] = (
                result.observation.observed_byte_count,
                result.observation.observed_sha256,
            )

    for relative_directory, expected_identity in directory_identities.items():
        directory_path = tree_path if not relative_directory else tree_path / relative_directory
        try:
            current_identity = _identity(directory_path.lstat())
        except OSError as error:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}") from error
        if current_identity != expected_identity:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}")
    missing_expected_paths = set(expected_paths) - actual_files - unsafe_paths
    for relative in sorted(missing_expected_paths):
        if not expected_paths[relative].required:
            continue
        full_path = f"{tree.root_path}/{relative}"
        if not any(
            item.code is FindingCode.ARTIFACT_MISSING and item.path == full_path
            for item in findings
        ):
            findings.append(
                _finding(
                    FindingCode.ARTIFACT_MISSING,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=tree.role,
                    path=full_path,
                )
            )
    for relative_directory in sorted(set(tree.allowed_directories) - actual_directories):
        findings.append(
            _finding(
                FindingCode.ARTIFACT_MISSING,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=tree.role,
                path=f"{tree.root_path}/{relative_directory}",
            )
        )
    observed_tree_digest: str | None = None
    if not findings and len(actual_files) == tree.expected_file_count:
        observed_tree_digest = compute_tree_sha256(observed_files, actual_directories)
        if observed_tree_digest != tree.tree_sha256:
            findings.append(
                _finding(
                    FindingCode.ARTIFACT_SHA256_MISMATCH,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=tree.role,
                    path=tree.root_path,
                    expected=tree.tree_sha256,
                    observed=observed_tree_digest,
                )
            )
    elif len(actual_files) != tree.expected_file_count and not any(
        item.code is FindingCode.UNEXPECTED_PATH for item in findings
    ):
        findings.append(
            _finding(
                FindingCode.UNEXPECTED_PATH,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=tree.role,
                path=tree.root_path,
            )
        )
    storage = StorageState.PRESENT
    if (
        not actual_files
        and findings
        and all(item.code is FindingCode.ARTIFACT_MISSING for item in findings)
    ):
        storage = StorageState.MISSING
    elif any(item.code is FindingCode.ARTIFACT_MISSING for item in findings):
        storage = StorageState.PARTIAL
    observation = ArtifactObservation(
        role=tree.role,
        path=tree.root_path,
        kind="tree",
        required=tree.required,
        storage_state=storage,
        integrity_state=IntegrityState.VERIFIED if not findings else IntegrityState.DRIFTED,
        expected_file_count=tree.expected_file_count,
        observed_file_count=len(actual_files),
        expected_tree_sha256=tree.tree_sha256,
        observed_tree_sha256=observed_tree_digest,
    )
    return _ObservationResult(observation, tuple(findings), contents, frozenset(commits))


def _observe_authority(
    *,
    root: Path,
    reference: AuthorityReference,
) -> _AuthorityObservation:
    read = _read_regular_file(root, reference.path, f"authority {reference.reference_id}")
    if not read.exists:
        return _AuthorityObservation(
            (
                _finding(
                    FindingCode.AUTHORITY_REFERENCE_MISSING,
                    subject_kind="authority",
                    subject_id=reference.reference_id,
                    role=reference.kind,
                    path=reference.path,
                ),
            ),
            None,
        )
    if read.reason is not None:
        return _AuthorityObservation(
            (
                _finding(
                    FindingCode.UNSAFE_ARTIFACT,
                    subject_kind="authority",
                    subject_id=reference.reference_id,
                    role=reference.kind,
                    path=reference.path,
                ),
            ),
            None,
        )
    assert read.byte_count is not None and read.sha256 is not None
    findings: list[InventoryFinding] = []
    if read.byte_count != reference.byte_count:
        findings.append(
            _finding(
                FindingCode.AUTHORITY_REFERENCE_BYTES_MISMATCH,
                subject_kind="authority",
                subject_id=reference.reference_id,
                role=reference.kind,
                path=reference.path,
                expected=reference.byte_count,
                observed=read.byte_count,
            )
        )
    if read.sha256 != reference.sha256:
        findings.append(
            _finding(
                FindingCode.AUTHORITY_REFERENCE_SHA256_MISMATCH,
                subject_kind="authority",
                subject_id=reference.reference_id,
                role=reference.kind,
                path=reference.path,
                expected=reference.sha256,
                observed=read.sha256,
            )
        )
    return _AuthorityObservation(tuple(findings), read.content)


def _observe_entry(
    *,
    root: Path,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    file_artifacts: Sequence[ExpectedFileArtifact],
    trees: Sequence[ExpectedTree],
) -> tuple[list[ArtifactObservation], list[InventoryFinding], dict[str, bytes], frozenset[str]]:
    observations: list[ArtifactObservation] = []
    findings: list[InventoryFinding] = []
    contents: dict[str, bytes] = {}
    commits: set[str] = set()
    for expected in file_artifacts:
        result = _observe_file(
            root=root,
            subject_kind=subject_kind,
            subject_id=subject_id,
            expected=expected,
        )
        observations.append(result.observation)
        findings.extend(result.findings)
        contents.update(result.contents)
        commits.update(result.reviewed_commits)
    for tree in trees:
        result = _observe_tree(
            root=root,
            subject_kind=subject_kind,
            subject_id=subject_id,
            tree=tree,
        )
        observations.append(result.observation)
        findings.extend(result.findings)
        contents.update(result.contents)
        commits.update(result.reviewed_commits)
    return observations, findings, contents, frozenset(commits)


def _commit_observation(
    *,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    expected_commit: str,
    mode: CommitVerificationMode,
    observed_commits: frozenset[str],
    role: str,
) -> tuple[IntegrityState, tuple[InventoryFinding, ...]]:
    if expected_commit in observed_commits and len(observed_commits) == 1:
        return IntegrityState.VERIFIED, ()
    if observed_commits:
        return (
            IntegrityState.DRIFTED,
            (
                _finding(
                    FindingCode.ARTIFACT_REVIEWED_COMMIT_MISMATCH,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=role,
                ),
            ),
        )
    if mode is CommitVerificationMode.INTERNAL_REQUIRED:
        return (
            IntegrityState.DRIFTED,
            (
                _finding(
                    FindingCode.ARTIFACT_REVIEWED_COMMIT_MISMATCH,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=role,
                ),
            ),
        )
    return (
        IntegrityState.NOT_VERIFIABLE,
        (
            _finding(
                FindingCode.ARTIFACT_REVIEWED_COMMIT_NOT_VERIFIABLE,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=role,
            ),
        ),
    )


def _inventory_entry_states(
    observations: Sequence[ArtifactObservation],
) -> tuple[StorageState, IntegrityState]:
    required = [item for item in observations if item.required]
    if not required or all(item.storage_state is StorageState.MISSING for item in required):
        storage = StorageState.MISSING
    elif any(item.storage_state is StorageState.MISSING for item in required):
        storage = StorageState.PARTIAL
    else:
        storage = StorageState.PRESENT
    if any(item.integrity_state is IntegrityState.DRIFTED for item in required):
        integrity = IntegrityState.DRIFTED
    elif any(item.integrity_state is IntegrityState.NOT_VERIFIABLE for item in required):
        integrity = IntegrityState.NOT_VERIFIABLE
    else:
        integrity = IntegrityState.VERIFIED
    return storage, integrity


def _extract_json_objects(content: bytes) -> list[dict[str, Any]]:
    try:
        raw = _strict_json_bytes(content, "Artifact")
        return [raw]
    except InventoryContractError:
        objects: list[dict[str, Any]] = []
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                value = _strict_json_bytes(line, "Artifact JSONL")
            except InventoryContractError:
                return []
            objects.append(value)
        return objects


def _provider_counts(contents: Mapping[str, bytes]) -> tuple[int, int]:
    observed = 0
    unknown = 0
    for content in contents.values():
        objects = _extract_json_objects(content)
        finished = [item for item in objects if item.get("event_type") == "campaign_finished"]
        if finished:
            for raw in finished:
                value = raw.get("provider_call_count")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    observed += value
                unknown_value = raw.get("provider_call_count_unknown_runs")
                if (
                    isinstance(unknown_value, int)
                    and not isinstance(unknown_value, bool)
                    and unknown_value >= 0
                ):
                    unknown += unknown_value
            continue
        for raw in objects:
            value = raw.get("provider_call_count")
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                observed += value
            elif value is None and "provider_call_count" in raw:
                unknown += 1
    return observed, unknown


def _receipt_result(
    *,
    root: Path,
    expectation: RetentionExpectation,
    all_contents: Mapping[str, bytes],
) -> tuple[InventoryRetention, tuple[InventoryFinding, ...]]:
    receipt = expectation.external_copy_receipt
    assert receipt is not None
    read = _read_regular_file(root, receipt.path, f"retention receipt {expectation.subject_id}")
    if not read.exists:
        return (
            InventoryRetention(
                subject_kind=expectation.subject_kind,
                subject_id=expectation.subject_id,
                retention_state=RetentionState.UNKNOWN,
                verification_basis=RetentionVerificationBasis.NOT_AVAILABLE,
                remote_liveness=RemoteLiveness.NOT_CHECKED,
                receipt_path=receipt.path,
            ),
            (
                _finding(
                    FindingCode.RETENTION_RECEIPT_MISSING,
                    subject_kind="retention",
                    subject_id=expectation.subject_id,
                    role=receipt.role,
                    path=receipt.path,
                ),
            ),
        )
    if (
        read.reason is not None
        or read.content is None
        or read.byte_count is None
        or read.sha256 is None
    ):
        return (
            InventoryRetention(
                subject_kind=expectation.subject_kind,
                subject_id=expectation.subject_id,
                retention_state=RetentionState.UNKNOWN,
                verification_basis=RetentionVerificationBasis.NOT_AVAILABLE,
                remote_liveness=RemoteLiveness.NOT_CHECKED,
                receipt_path=receipt.path,
            ),
            (
                _finding(
                    FindingCode.UNSAFE_ARTIFACT,
                    subject_kind="retention",
                    subject_id=expectation.subject_id,
                    role=receipt.role,
                    path=receipt.path,
                ),
            ),
        )
    findings: list[InventoryFinding] = []
    if read.byte_count != receipt.byte_count:
        findings.append(
            _finding(
                FindingCode.RETENTION_RECEIPT_BYTES_MISMATCH,
                subject_kind="retention",
                subject_id=expectation.subject_id,
                role=receipt.role,
                path=receipt.path,
                expected=receipt.byte_count,
                observed=read.byte_count,
            )
        )
    if read.sha256 != receipt.sha256:
        findings.append(
            _finding(
                FindingCode.RETENTION_RECEIPT_SHA256_MISMATCH,
                subject_kind="retention",
                subject_id=expectation.subject_id,
                role=receipt.role,
                path=receipt.path,
                expected=receipt.sha256,
                observed=read.sha256,
            )
        )
    try:
        parsed = _load_canonical_bytes(read.content, ExternalCopyReceipt, "External copy receipt")
    except InventoryContractError:
        parsed = None
    target = all_contents.get(parsed.artifact_path) if parsed is not None else None
    if (
        parsed is None
        or parsed.subject_kind != expectation.subject_kind
        or parsed.subject_id != expectation.subject_id
        or target is None
        or parsed.artifact_byte_count != len(target)
        or parsed.artifact_sha256 != _sha256(target)
    ):
        findings.append(
            _finding(
                FindingCode.RETENTION_RECEIPT_INVALID,
                subject_kind="retention",
                subject_id=expectation.subject_id,
                role=receipt.role,
                path=receipt.path,
            )
        )
    state = (
        RetentionState.EXTERNAL_COPY_RECEIPT_VERIFIED if not findings else RetentionState.UNKNOWN
    )
    basis = (
        RetentionVerificationBasis.RECEIPT_ONLY
        if not findings
        else RetentionVerificationBasis.NOT_AVAILABLE
    )
    return (
        InventoryRetention(
            subject_kind=expectation.subject_kind,
            subject_id=expectation.subject_id,
            retention_state=state,
            verification_basis=basis,
            remote_liveness=RemoteLiveness.NOT_CHECKED,
            receipt_path=receipt.path,
        ),
        tuple(findings),
    )


def _observe_execution_repository_head(repository_root: Path) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InventorySafetyError("execution repository HEAD could not be observed") from error
    if result.returncode != 0:
        raise InventorySafetyError("execution repository HEAD could not be observed")
    try:
        head = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise InventorySafetyError("execution repository HEAD was not ASCII") from error
    if not re.fullmatch(COMMIT_PATTERN, head):
        raise InventorySafetyError("execution repository HEAD is not a full commit")
    return head


def _find_content(contents: Mapping[str, bytes], relative: str) -> bytes | None:
    if relative in contents:
        return contents[relative]
    suffix = f"/{relative}"
    matches = [content for path, content in contents.items() if path.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _release_binding_findings(
    *,
    entry: ReleaseEntry,
    contents: Mapping[str, bytes],
) -> tuple[InventoryFinding, ...]:
    findings: list[InventoryFinding] = []
    checksum_content = next(
        (
            content
            for item in entry.file_artifacts
            if item.role == ReleaseArtifactRole.CHECKSUMS.value
            for path, content in contents.items()
            if path == item.path
        ),
        None,
    )
    anchor_content = next(
        (
            content
            for item in entry.file_artifacts
            if item.role == ReleaseArtifactRole.EXTERNAL_ANCHOR.value
            for path, content in contents.items()
            if path == item.path
        ),
        None,
    )
    manifest_content = next(
        (
            content
            for item in entry.file_artifacts
            if item.role == ReleaseArtifactRole.SUITE_MANIFEST.value
            for path, content in contents.items()
            if path == item.path
        ),
        None,
    )
    release_metadata_content = _find_content(contents, "release-metadata.json")
    parsed_checksums: Any = None
    if checksum_content is not None:
        try:
            from agentlab.phase6 import PublicChecksums

            parsed_checksums = _load_canonical_bytes(
                checksum_content,
                PublicChecksums,
                "Public checksums",
            )
        except (InventoryContractError, ImportError):
            parsed_checksums = None
        if parsed_checksums is not None:
            for checksum in parsed_checksums.entries:
                observed = _find_content(contents, checksum.path)
                if observed is None:
                    findings.append(
                        _finding(
                            FindingCode.CHECKSUM_CONTRACT_MISMATCH,
                            subject_kind="release",
                            subject_id=entry.release_id,
                            role=ReleaseArtifactRole.CHECKSUMS.value,
                            path=checksum.path,
                        )
                    )
                    continue
                if len(observed) != checksum.size_bytes or _sha256(observed) != checksum.sha256:
                    findings.append(
                        _finding(
                            FindingCode.CHECKSUM_CONTRACT_MISMATCH,
                            subject_kind="release",
                            subject_id=entry.release_id,
                            role=ReleaseArtifactRole.CHECKSUMS.value,
                            path=checksum.path,
                        )
                    )
    parsed_anchor: Any = None
    if anchor_content is not None and checksum_content is not None:
        try:
            from agentlab.phase6 import ExternalChecksumAnchor

            parsed_anchor = _load_canonical_bytes(
                anchor_content, ExternalChecksumAnchor, "External anchor"
            )
        except (InventoryContractError, ImportError):
            parsed_anchor = None
        if parsed_anchor is not None and parsed_anchor.checksum_manifest_sha256 != _sha256(
            checksum_content
        ):
            findings.append(
                _finding(
                    FindingCode.EXTERNAL_ANCHOR_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role=ReleaseArtifactRole.EXTERNAL_ANCHOR.value,
                )
            )
    if (
        entry.verification_profile == "phase6_public_suite"
        and manifest_content is not None
        and release_metadata_content is not None
        and parsed_checksums is not None
    ):
        try:
            from agentlab.phase6 import (
                ExternalChecksumAnchor,
                PublicSuiteManifest,
                ReleaseMetadata,
                validate_checksum_contract,
            )

            manifest = _load_canonical_bytes(
                manifest_content,
                PublicSuiteManifest,
                "Public Suite Manifest",
            )
            release_metadata = _load_canonical_bytes(
                release_metadata_content,
                ReleaseMetadata,
                "release metadata",
            )
            if parsed_anchor is None:
                anchor = None
            else:
                anchor = _load_canonical_bytes(
                    anchor_content or b"",
                    ExternalChecksumAnchor,
                    "External anchor",
                )
            validate_checksum_contract(
                manifest=manifest,
                checksums=parsed_checksums,
                release_metadata=release_metadata,
                external_anchor=anchor,
                checksum_bytes=checksum_content,
            )
        except Exception:
            findings.append(
                _finding(
                    FindingCode.CHECKSUM_CONTRACT_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role=ReleaseArtifactRole.CHECKSUMS.value,
                )
            )
    return tuple(findings)


def _phase6_public_suite_findings(
    *,
    root: Path,
    entry: ReleaseEntry,
    contents: Mapping[str, bytes],
) -> tuple[tuple[InventoryFinding, ...], frozenset[str]]:
    if entry.verification_profile != "phase6_public_suite":
        return (), frozenset()
    manifest = next(
        (
            item
            for item in entry.file_artifacts
            if item.role == ReleaseArtifactRole.SUITE_MANIFEST.value
        ),
        None,
    )
    if manifest is None:
        return (
            (
                _finding(
                    FindingCode.CLASSIFICATION_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role="suite_manifest",
                ),
            ),
            frozenset(),
        )
    try:
        from agentlab.phase6 import load_public_suite_inputs, validate_public_suite_inputs
        from agentlab.phase6_public import render_public_suite

        loaded = load_public_suite_inputs(root / manifest.path, root=root)
        validated = validate_public_suite_inputs(loaded)
        rendered = render_public_suite(validated)
    except Exception:
        return (
            (
                _finding(
                    FindingCode.CROSS_ARTIFACT_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role=manifest.role,
                    path=manifest.path,
                ),
            ),
            frozenset(),
        )
    findings: list[InventoryFinding] = []
    for relative, rendered_bytes in rendered.files.items():
        observed = _find_content(contents, relative)
        if observed is None or observed != rendered_bytes:
            findings.append(
                _finding(
                    FindingCode.BUNDLE_RENDERER_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role="bundle_root",
                    path=relative,
                )
            )
    commits: set[str] = set()
    for content in loaded.bytes_by_path.values():
        commits.update(_reviewed_commits(content))
    return tuple(findings), frozenset(commits)


def _build_inventory(
    *,
    request: EvidenceInventoryRequest,
    request_sha256: str,
    repository_root: Path,
    observed_head: str,
) -> _VerificationResult:
    findings: list[InventoryFinding] = []
    all_contents: dict[str, bytes] = {}
    for reference in request.source_of_truth_references:
        authority = _observe_authority(root=repository_root, reference=reference)
        findings.extend(authority.findings)
        if authority.content is not None:
            all_contents[reference.path] = authority.content

    release_outputs: list[InventoryReleaseEntry] = []
    campaign_outputs: list[InventoryCampaignEntry] = []
    for release_entry in request.release_entries:
        observations, entry_findings, contents, commits = _observe_entry(
            root=repository_root,
            subject_kind="release",
            subject_id=release_entry.release_id,
            file_artifacts=release_entry.file_artifacts,
            trees=release_entry.trees,
        )
        findings.extend(entry_findings)
        findings.extend(_release_binding_findings(entry=release_entry, contents=contents))
        suite_findings, suite_commits = _phase6_public_suite_findings(
            root=repository_root,
            entry=release_entry,
            contents=contents,
        )
        findings.extend(suite_findings)
        commits = frozenset((*commits, *suite_commits))
        commit_state, commit_findings = _commit_observation(
            subject_kind="release",
            subject_id=release_entry.release_id,
            expected_commit=release_entry.artifact_reviewed_commit,
            mode=release_entry.commit_verification_mode,
            observed_commits=commits,
            role="release",
        )
        findings.extend(commit_findings)
        storage, integrity = _inventory_entry_states(observations)
        if commit_state is IntegrityState.DRIFTED:
            integrity = IntegrityState.DRIFTED
        elif commit_state is IntegrityState.NOT_VERIFIABLE and integrity is IntegrityState.VERIFIED:
            integrity = IntegrityState.NOT_VERIFIABLE
        release_outputs.append(
            InventoryReleaseEntry(
                release_id=release_entry.release_id,
                artifact_reviewed_commit=release_entry.artifact_reviewed_commit,
                commit_verification_mode=release_entry.commit_verification_mode,
                commit_verification=commit_state,
                classification=release_entry.classification,
                verification_profile=release_entry.verification_profile,
                storage_state=storage,
                integrity_state=integrity,
                artifact_observations=sorted(
                    observations, key=lambda item: (item.kind, item.path, item.role)
                ),
            )
        )
        all_contents.update(contents)

    for campaign_entry in request.campaign_entries:
        observations, entry_findings, contents, commits = _observe_entry(
            root=repository_root,
            subject_kind="campaign",
            subject_id=campaign_entry.campaign_id,
            file_artifacts=campaign_entry.file_artifacts,
            trees=campaign_entry.trees,
        )
        findings.extend(entry_findings)
        commit_state, commit_findings = _commit_observation(
            subject_kind="campaign",
            subject_id=campaign_entry.campaign_id,
            expected_commit=campaign_entry.artifact_reviewed_commit,
            mode=campaign_entry.commit_verification_mode,
            observed_commits=commits,
            role="campaign",
        )
        findings.extend(commit_findings)
        storage, integrity = _inventory_entry_states(observations)
        if commit_state is IntegrityState.DRIFTED:
            integrity = IntegrityState.DRIFTED
        elif commit_state is IntegrityState.NOT_VERIFIABLE and integrity is IntegrityState.VERIFIED:
            integrity = IntegrityState.NOT_VERIFIABLE
        campaign_outputs.append(
            InventoryCampaignEntry(
                campaign_id=campaign_entry.campaign_id,
                artifact_reviewed_commit=campaign_entry.artifact_reviewed_commit,
                commit_verification_mode=campaign_entry.commit_verification_mode,
                commit_verification=commit_state,
                classification=campaign_entry.classification,
                included_in_primary_denominator=campaign_entry.included_in_primary_denominator,
                release_id=campaign_entry.release_id,
                verification_profile=campaign_entry.verification_profile,
                storage_state=storage,
                integrity_state=integrity,
                artifact_observations=sorted(
                    observations, key=lambda item: (item.kind, item.path, item.role)
                ),
            )
        )
        all_contents.update(contents)

    primary = [
        item
        for item in request.campaign_entries
        if item.classification is CampaignClassification.PRIMARY_EVALUATION
    ]
    accepted_current_ids = {
        item.release_id
        for item in request.release_entries
        if item.classification is ReleaseClassification.ACCEPTED_CURRENT
    }
    if primary and any(item.release_id not in accepted_current_ids for item in primary):
        findings.append(
            _finding(
                FindingCode.DENOMINATOR_MISMATCH,
                subject_kind="request",
                subject_id=request.inventory_id,
            )
        )
    if request.expected_execution_repository_head is not None and (
        request.expected_execution_repository_head != observed_head
    ):
        findings.append(
            _finding(
                FindingCode.EXECUTION_REPOSITORY_HEAD_MISMATCH,
                subject_kind="request",
                subject_id=request.inventory_id,
                expected=request.expected_execution_repository_head,
                observed=observed_head,
            )
        )

    retention_outputs: list[InventoryRetention] = []
    for expectation in request.retention_expectations:
        if expectation.expected_retention_state is RetentionState.LOCAL_ONLY:
            retention_outputs.append(
                InventoryRetention(
                    subject_kind=expectation.subject_kind,
                    subject_id=expectation.subject_id,
                    retention_state=RetentionState.LOCAL_ONLY,
                    verification_basis=RetentionVerificationBasis.LOCAL_ARTIFACT_ONLY,
                    remote_liveness=RemoteLiveness.NOT_CHECKED,
                )
            )
        elif expectation.expected_retention_state is RetentionState.UNKNOWN:
            retention_outputs.append(
                InventoryRetention(
                    subject_kind=expectation.subject_kind,
                    subject_id=expectation.subject_id,
                    retention_state=RetentionState.UNKNOWN,
                    verification_basis=RetentionVerificationBasis.NOT_AVAILABLE,
                    remote_liveness=RemoteLiveness.NOT_CHECKED,
                )
            )
        else:
            result, receipt_findings = _receipt_result(
                root=repository_root,
                expectation=expectation,
                all_contents=all_contents,
            )
            retention_outputs.append(result)
            findings.extend(receipt_findings)

    observed_calls, unknown_calls = _provider_counts(all_contents)
    classification_counts: dict[str, int] = {}
    for release_entry in request.release_entries:
        classification = release_entry.classification.value
        classification_counts[classification] = classification_counts.get(classification, 0) + 1
    for campaign_entry in request.campaign_entries:
        classification = campaign_entry.classification.value
        classification_counts[classification] = classification_counts.get(classification, 0) + 1
    storage_counts: dict[StorageState, int] = {state: 0 for state in StorageState}
    integrity_counts: dict[IntegrityState, int] = {state: 0 for state in IntegrityState}
    for release_output in release_outputs:
        storage_counts[release_output.storage_state] += 1
        integrity_counts[release_output.integrity_state] += 1
    for campaign_output in campaign_outputs:
        storage_counts[campaign_output.storage_state] += 1
        integrity_counts[campaign_output.integrity_state] += 1
    request_correlation_id = (
        "rc-" + _sha256(f"{request.inventory_id}\0{request_sha256}".encode())[:32]
    )
    unique_findings: dict[tuple[str, str, str, str, str], InventoryFinding] = {}
    for finding in findings:
        key = (
            finding.subject_kind,
            finding.subject_id,
            finding.code.value,
            finding.path or "",
            finding.artifact_role or "",
        )
        unique_findings[key] = finding
    findings = sorted(
        unique_findings.values(),
        key=lambda item: (
            item.subject_kind,
            item.subject_id,
            item.code.value,
            item.path or "",
            item.artifact_role or "",
        ),
    )
    inventory = EvidenceInventory(
        schema_version="1.0",
        inventory_id=request.inventory_id,
        request_correlation_id=request_correlation_id,
        authoritative=False,
        scope=InventoryScope.PHASE6,
        request_sha256=request_sha256,
        source_of_truth_references=request.source_of_truth_references,
        releases=sorted(release_outputs, key=lambda item: item.release_id),
        campaigns=sorted(campaign_outputs, key=lambda item: item.campaign_id),
        retention=sorted(retention_outputs, key=lambda item: (item.subject_kind, item.subject_id)),
        findings=findings,
        summary=InventorySummary(
            release_count=len(release_outputs),
            campaign_count=len(campaign_outputs),
            primary_denominator=sum(
                item.included_in_primary_denominator for item in campaign_outputs
            ),
            classification_counts=classification_counts,
            storage_state_counts=storage_counts,
            integrity_state_counts=integrity_counts,
            provider_call_count_observed=observed_calls,
            provider_call_count_unknown=unknown_calls,
        ),
        verification_status=VerificationStatus.FAILED if findings else VerificationStatus.VERIFIED,
    )
    return _VerificationResult(
        inventory=inventory, observed_execution_repository_head=observed_head
    )


RENDERER_VERSION = "phase7-inventory-renderer-1.0"
TOOL_VERSION = "agentlab-phase7-1.0"


def render_inventory_markdown(inventory: EvidenceInventory) -> bytes:
    """Render Markdown solely from the canonical Inventory model."""
    lines = [
        "# Phase 7A Evidence Inventory",
        "",
        f"- schema_version: '{inventory.schema_version}'",
        f"- inventory_id: '{inventory.inventory_id}'",
        f"- request_correlation_id: '{inventory.request_correlation_id}'",
        "- authoritative: false",
        f"- scope: '{inventory.scope.value}'",
        f"- verification_status: '{inventory.verification_status.value}'",
        "",
        "## Summary",
        "",
        f"- releases: {inventory.summary.release_count}",
        f"- campaigns: {inventory.summary.campaign_count}",
        f"- primary denominator: {inventory.summary.primary_denominator}",
        f"- provider call count observed: {inventory.summary.provider_call_count_observed}",
        f"- provider call count unknown: {inventory.summary.provider_call_count_unknown}",
        "",
        "## Entries",
        "",
        "| Kind | ID | Classification | Storage | Integrity | Commit |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| release | '{entry.release_id}' | '{entry.classification.value}' | "
        f"'{entry.storage_state.value}' | '{entry.integrity_state.value}' | "
        f"'{entry.commit_verification.value}' |"
        for entry in inventory.releases
    )
    lines.extend(
        f"| campaign | '{entry.campaign_id}' | '{entry.classification.value}' | "
        f"'{entry.storage_state.value}' | '{entry.integrity_state.value}' | "
        f"'{entry.commit_verification.value}' |"
        for entry in inventory.campaigns
    )
    lines.extend(["", "## Retention", ""])
    lines.append("| Kind | ID | State | Basis | Remote liveness |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.extend(
        f"| '{item.subject_kind}' | '{item.subject_id}' | '{item.retention_state.value}' | "
        f"'{item.verification_basis.value}' | '{item.remote_liveness.value}' |"
        for item in inventory.retention
    )
    lines.extend(["", "Retention does not check remote liveness; remote_liveness=not_checked.", ""])
    lines.extend(["## Findings", ""])
    if inventory.findings:
        lines.extend(
            f"- '{finding.code.value}' '{finding.subject_kind}/{finding.subject_id}': "
            f"{finding.detail}"
            for finding in inventory.findings
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            (
                "Provider, Prompt, Gate, Campaign execution, Report regeneration, "
                "Public Suite regeneration, and network access: 0."
            ),
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


def _validate_output_paths(
    *,
    repository_root: Path,
    request_path: Path,
    output_path: Path,
    markdown_path: Path,
    metadata_path: Path,
) -> tuple[Path, Path, Path]:
    paths = [Path(os.path.abspath(item)) for item in (output_path, markdown_path, metadata_path)]
    if len(set(paths)) != 3:
        raise InventorySafetyError("output, Markdown, and metadata paths must be distinct")
    for path, label in zip(
        paths,
        ("Inventory output", "Markdown output", "metadata output"),
        strict=True,
    ):
        _real_directory(path.parent, f"{label} parent")
        try:
            path.relative_to(repository_root)
        except ValueError as error:
            raise InventorySafetyError(f"{label} must remain below repository root") from error
        if path.name in {"", ".", ".."}:
            raise InventorySafetyError(f"{label} must name a file")
    lexical_request = Path(os.path.abspath(request_path))
    if lexical_request in paths:
        raise InventorySafetyError("output must not alias Request")
    return paths[0], paths[1], paths[2]


def _same_existing_identity(first: Path, second: Path) -> bool:
    if not os.path.lexists(first) or not os.path.lexists(second):
        return False
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _preflight_outputs(
    *,
    output_path: Path,
    markdown_path: Path,
    metadata_path: Path,
) -> None:
    paths = (output_path, markdown_path, metadata_path)
    existing = [os.path.lexists(path) for path in paths]
    if not any(existing):
        return
    if not all(existing):
        raise InventoryPublicationError(
            "incomplete publication: one or two output paths already exist"
        )
    if any(
        _same_existing_identity(first, second)
        for index, first in enumerate(paths)
        for second in paths[index + 1 :]
    ):
        raise InventoryPublicationError("output paths alias an existing publication")
    for path in paths:
        try:
            entry_metadata = path.lstat()
        except OSError as error:
            raise InventoryPublicationError("existing publication path is unavailable") from error
        if not stat.S_ISREG(entry_metadata.st_mode) or entry_metadata.st_nlink != 1:
            raise InventoryPublicationError("existing publication path is unsafe")
        if entry_metadata.st_size > MAX_PUBLICATION_FILE_BYTES:
            raise InventoryPublicationError("existing publication is too large to inspect safely")
    try:
        inventory_bytes = output_path.read_bytes()
        markdown_bytes = markdown_path.read_bytes()
        metadata_bytes = metadata_path.read_bytes()
        metadata_model = _load_canonical_bytes(
            metadata_bytes,
            EvidenceInventoryMetadata,
            "Inventory metadata",
        )
        inventory = _load_canonical_bytes(
            inventory_bytes,
            EvidenceInventory,
            "Evidence Inventory",
        )
    except (OSError, InventoryContractError) as error:
        raise InventoryPublicationError(
            "incomplete publication: existing outputs are not a valid complete publication"
        ) from error
    if (
        metadata_model.inventory_sha256 != _sha256(inventory_bytes)
        or metadata_model.markdown_sha256 != _sha256(markdown_bytes)
        or metadata_model.request_correlation_id != inventory.request_correlation_id
        or render_inventory_markdown(inventory) != markdown_bytes
    ):
        raise InventoryPublicationError(
            "incomplete publication: output hashes do not agree with metadata"
        )
    raise InventoryPublicationError("output collision: complete publication already exists")


def _publish_file_no_replace(path: Path, content: bytes, label: str) -> tuple[int, int, int, int]:
    descriptor: int | None = None
    staging: Path | None = None
    linked_identity: tuple[int, int, int, int] | None = None
    published_successfully = False
    try:
        descriptor, staging_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        staging = Path(staging_name)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        staged_metadata = os.fstat(descriptor)
        linked_identity = (
            staged_metadata.st_dev,
            staged_metadata.st_ino,
            staged_metadata.st_mode,
            staged_metadata.st_size,
        )
        os.close(descriptor)
        descriptor = None
        os.link(staging, path)
        staging.unlink()
        metadata = path.lstat()
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
        ) != linked_identity:
            raise InventoryPublicationError(f"published {label} identity changed")
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InventoryPublicationError(f"published {label} identity is unsafe")
        published_successfully = True
        return linked_identity
    except FileExistsError as error:
        raise InventoryPublicationError(f"{label} already exists") from error
    except InventoryPublicationError:
        raise
    except OSError as error:
        raise InventoryPublicationError(f"could not publish {label}") from error
    finally:
        if not published_successfully and linked_identity is not None and os.path.lexists(path):
            try:
                metadata = path.lstat()
                current = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                )
                if current == linked_identity and stat.S_ISREG(metadata.st_mode):
                    path.unlink()
            except OSError:
                pass
        if descriptor is not None:
            os.close(descriptor)
        if staging is not None and os.path.lexists(staging):
            staging.unlink()


def _rollback_file(path: Path, identity: tuple[int, int, int, int]) -> None:
    if not os.path.lexists(path):
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InventoryPublicationError("owned-output rollback could not inspect path") from error
    current = (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size)
    if current != identity or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise InventoryPublicationError("owned-output rollback refused a changed path")
    try:
        path.unlink()
    except OSError as error:
        raise InventoryPublicationError("owned-output rollback failed") from error


def _load_published_outputs(
    output_path: Path,
    markdown_path: Path,
    metadata_path: Path,
) -> None:
    inventory_bytes = output_path.read_bytes()
    markdown_bytes = markdown_path.read_bytes()
    metadata = _load_canonical_bytes(
        metadata_path.read_bytes(),
        EvidenceInventoryMetadata,
        "Inventory metadata",
    )
    inventory = _load_canonical_bytes(
        inventory_bytes,
        EvidenceInventory,
        "Evidence Inventory",
    )
    if (
        metadata.inventory_sha256 != _sha256(inventory_bytes)
        or metadata.markdown_sha256 != _sha256(markdown_bytes)
        or metadata.request_correlation_id != inventory.request_correlation_id
        or render_inventory_markdown(inventory) != markdown_bytes
    ):
        raise InventoryPublicationError("published metadata hashes do not match outputs")


def create_inventory_publication(
    *,
    request_path: Path,
    repository_root: Path,
    output_path: Path,
    markdown_path: Path,
    metadata_path: Path,
    confirm_local_execution: bool,
    now: Callable[[], datetime] | None = None,
    tool_version: str = TOOL_VERSION,
) -> InventoryPublication:
    """Verify one Request and publish its three output files create-only."""
    if not confirm_local_execution:
        raise InventorySafetyError("inventory requires --confirm-local-execution")
    root = _real_directory(repository_root, "repository root")
    output_path, markdown_path, metadata_path = _validate_output_paths(
        repository_root=root,
        request_path=request_path,
        output_path=output_path,
        markdown_path=markdown_path,
        metadata_path=metadata_path,
    )
    _preflight_outputs(
        output_path=output_path,
        markdown_path=markdown_path,
        metadata_path=metadata_path,
    )
    request_bytes = _read_request_file(request_path, root)
    request = load_inventory_request_bytes(request_bytes)
    request_sha256 = _sha256(request_bytes)
    expected_paths: set[str] = set()
    for release_entry in request.release_entries:
        expected_paths.update(item.path for item in release_entry.file_artifacts)
        expected_paths.update(tree.root_path for tree in release_entry.trees)
        expected_paths.update(
            f"{tree.root_path}/{item.path}"
            for tree in release_entry.trees
            for item in tree.file_artifacts
        )
    for campaign_entry in request.campaign_entries:
        expected_paths.update(item.path for item in campaign_entry.file_artifacts)
        expected_paths.update(tree.root_path for tree in campaign_entry.trees)
        expected_paths.update(
            f"{tree.root_path}/{item.path}"
            for tree in campaign_entry.trees
            for item in tree.file_artifacts
        )
    expected_paths.update(reference.path for reference in request.source_of_truth_references)
    expected_paths.update(
        expectation.external_copy_receipt.path
        for expectation in request.retention_expectations
        if expectation.external_copy_receipt is not None
    )
    output_relative_paths = {
        path.relative_to(root).as_posix() for path in (output_path, markdown_path, metadata_path)
    }
    if output_relative_paths & expected_paths:
        raise InventorySafetyError("outputs must not alias declared input Artifacts")
    observed_head = _observe_execution_repository_head(root)
    result = _build_inventory(
        request=request,
        request_sha256=request_sha256,
        repository_root=root,
        observed_head=observed_head,
    )
    inventory_bytes = canonical_inventory_json_bytes(result.inventory)
    markdown_bytes = render_inventory_markdown(result.inventory)
    generated_at = _canonical_timestamp((now or (lambda: datetime.now(UTC)))())
    metadata = EvidenceInventoryMetadata(
        schema_version="1.0",
        request_correlation_id=result.inventory.request_correlation_id,
        request_sha256=request_sha256,
        inventory_sha256=_sha256(inventory_bytes),
        markdown_sha256=_sha256(markdown_bytes),
        expected_execution_repository_head=request.expected_execution_repository_head,
        observed_execution_repository_head=observed_head,
        generated_at=generated_at,
        renderer_version=RENDERER_VERSION,
        tool_version=tool_version,
    )
    metadata_bytes = canonical_inventory_json_bytes(metadata)
    if any(
        len(content) > MAX_PUBLICATION_FILE_BYTES
        for content in (inventory_bytes, markdown_bytes, metadata_bytes)
    ):
        raise InventoryPublicationError("generated publication exceeds the bounded output limit")
    if _read_request_file(request_path, root) != request_bytes:
        raise InventorySafetyError("Request changed during verification")
    published: list[tuple[Path, tuple[int, int, int, int]]] = []
    try:
        for path, content, label in (
            (output_path, inventory_bytes, "Inventory"),
            (markdown_path, markdown_bytes, "Markdown"),
            (metadata_path, metadata_bytes, "metadata"),
        ):
            published.append((path, _publish_file_no_replace(path, content, label)))
        _load_published_outputs(output_path, markdown_path, metadata_path)
    except Exception as original_error:
        rollback_error: Exception | None = None
        try:
            for path, identity in reversed(published):
                _rollback_file(path, identity)
        except Exception as error:
            rollback_error = error
        if rollback_error is not None:
            raise InventoryPublicationError(
                "publication failed and owned-output rollback could not be verified"
            ) from rollback_error
        if isinstance(original_error, InventoryPublicationError):
            raise
        raise InventoryPublicationError("publication failed safely") from original_error
    return InventoryPublication(
        inventory=result.inventory,
        metadata=metadata,
        inventory_bytes=inventory_bytes,
        markdown_bytes=markdown_bytes,
        metadata_bytes=metadata_bytes,
        output_path=output_path,
        markdown_path=markdown_path,
        metadata_path=metadata_path,
        exit_code=2 if result.inventory.verification_status is VerificationStatus.FAILED else 0,
    )


def verify_inventory_request(
    *,
    request_path: Path,
    repository_root: Path,
    confirm_local_execution: bool,
) -> EvidenceInventory:
    """Read and verify without publishing; useful for synthetic callers."""
    if not confirm_local_execution:
        raise InventorySafetyError("inventory requires --confirm-local-execution")
    root = _real_directory(repository_root, "repository root")
    request_bytes = _read_request_file(request_path, root)
    request = load_inventory_request_bytes(request_bytes)
    observed_head = _observe_execution_repository_head(root)
    return _build_inventory(
        request=request,
        request_sha256=_sha256(request_bytes),
        repository_root=root,
        observed_head=observed_head,
    ).inventory


# Compatibility aliases for callers using the command-oriented names.
run_inventory_phase6_evidence = create_inventory_publication
load_evidence_inventory_request = load_inventory_request_bytes
