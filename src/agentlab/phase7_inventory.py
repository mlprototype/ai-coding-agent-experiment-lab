"""Phase 7A read-only Evidence Inventory contracts and verifier.

The module intentionally only reads the repository and publishes three new
create-only files.  It never invokes a Provider, Gate, Campaign, or network
operation.  The observed repository HEAD is a checkout observation only; it
is not binary provenance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, TypeVar

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
MAX_TREE_DIRECTORIES = 256
MAX_TREE_ENTRIES = 4096
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


class ProviderTotalStatus(StrEnum):
    OBSERVED = "observed"
    PARTIALLY_UNKNOWN = "partially_unknown"
    UNAVAILABLE = "unavailable"


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


# These tables are part of the Request contract.  A profile is never inferred
# from an Artifact filename or from the order in which files happen to be
# observed.
RELEASE_PROFILE_REQUIRED_FILE_ROLES: dict[str, frozenset[str]] = {
    "phase6_public_suite": frozenset(
        {
            ReleaseArtifactRole.SUITE_MANIFEST.value,
            ReleaseArtifactRole.CHECKSUMS.value,
            ReleaseArtifactRole.EXTERNAL_ANCHOR.value,
        }
    ),
    "phase6_campaign_complete": frozenset(),
    "historical_verification": frozenset(),
    "declared_artifact_set": frozenset(),
}
CAMPAIGN_PROFILE_REQUIRED_FILE_ROLES: dict[str, frozenset[str]] = {
    "phase6_public_suite": frozenset(),
    "phase6_campaign_complete": frozenset(
        {
            CampaignArtifactRole.SPEC.value,
            CampaignArtifactRole.FIXTURE_MANIFEST.value,
            CampaignArtifactRole.FIXTURE_ACCEPTANCE.value,
            CampaignArtifactRole.DIFF_POLICY.value,
            CampaignArtifactRole.PLAN.value,
            CampaignArtifactRole.CAMPAIGN.value,
            CampaignArtifactRole.RECORDING.value,
            CampaignArtifactRole.EVIDENCE.value,
        }
    ),
    "historical_verification": frozenset(
        {
            CampaignArtifactRole.HISTORICAL_VERIFICATION.value,
            CampaignArtifactRole.PLAN.value,
            CampaignArtifactRole.CAMPAIGN.value,
            CampaignArtifactRole.REPORT_JSON.value,
            CampaignArtifactRole.REPORT_MARKDOWN.value,
        }
    ),
    "declared_artifact_set": frozenset(),
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _execution_head_attestation_sha256(observed_head: str) -> str:
    """Bind an execution HEAD without disclosing it in the Inventory payload."""
    if not re.fullmatch(COMMIT_PATTERN, observed_head):
        raise InventoryContractError("observed execution repository HEAD is invalid")
    return _sha256(b"agentlab.phase7.execution-head.v1\0" + observed_head.encode("ascii"))


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
    kind: Literal[
        "accepted_manifest",
        "tracked_closeout",
        "human_acceptance_record",
        "provider_accounting_declaration",
    ]
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
        if any(not item.required for item in self.file_artifacts):
            raise ValueError("ExpectedTree file_artifacts must all have required=True")
        if self.expected_file_count != len(self.file_artifacts):
            raise ValueError("ExpectedTree file count must match file_artifacts")
        return self


class ReleaseEntry(ContractModel):
    release_id: StrictStr = Field(pattern=ID_PATTERN)
    artifact_reviewed_commits: list[StrictStr] = Field(default_factory=list)
    commit_verification_mode: CommitVerificationMode
    classification: ReleaseClassification
    verification_profile: Literal[
        "phase6_public_suite",
        "phase6_campaign_complete",
        "historical_verification",
        "declared_artifact_set",
    ]
    declaration_basis: StrictStr = Field(min_length=1, max_length=240)
    accepted_manifest_reference_id: StrictStr | None = Field(
        default=None,
        pattern=ID_PATTERN,
    )
    file_artifacts: list[ExpectedFileArtifact] = Field(default_factory=list)
    trees: list[ExpectedTree] = Field(default_factory=list)
    superseded_by: StrictStr | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def release_roles_are_closed(self) -> ReleaseEntry:
        if any(
            not re.fullmatch(COMMIT_PATTERN, commit)
            for commit in self.artifact_reviewed_commits
        ):
            raise ValueError("artifact_reviewed_commits must contain canonical commit IDs")
        if self.artifact_reviewed_commits != sorted(set(self.artifact_reviewed_commits)):
            raise ValueError("artifact_reviewed_commits must be sorted and unique")
        if (
            self.commit_verification_mode is CommitVerificationMode.INTERNAL_REQUIRED
            and not self.artifact_reviewed_commits
        ):
            raise ValueError("internal_required release requires reviewed commits")
        if not self.file_artifacts and not self.trees:
            raise ValueError("ReleaseEntry must declare at least one Artifact")
        allowed = {item.value for item in ReleaseArtifactRole}
        if any(item.role not in allowed for item in self.file_artifacts):
            raise ValueError("ReleaseEntry contains an unsupported file role")
        if any(item.role != "bundle_root" for item in self.trees):
            raise ValueError("ReleaseEntry tree role must be bundle_root")
        file_roles = [item.role for item in self.file_artifacts]
        if len(file_roles) != len(set(file_roles)):
            raise ValueError("ReleaseEntry file roles must be unique")
        required_roles = RELEASE_PROFILE_REQUIRED_FILE_ROLES[self.verification_profile]
        declared_required_roles = {
            item.role for item in self.file_artifacts if item.required
        }
        if not required_roles.issubset(declared_required_roles):
            raise ValueError(
                f"{self.verification_profile} release is missing required file roles"
            )
        if self.verification_profile == "phase6_public_suite" and (
            len(self.trees) != 1 or not self.trees[0].required
        ):
            raise ValueError(
                "phase6_public_suite release requires one required bundle_root"
            )
        if self.classification in {
            ReleaseClassification.ACCEPTED_CURRENT,
            ReleaseClassification.ACCEPTED_SUPERSEDED,
        } and (
            self.verification_profile != "phase6_public_suite"
            or self.commit_verification_mode is not CommitVerificationMode.INTERNAL_REQUIRED
            or self.accepted_manifest_reference_id is None
        ):
            raise ValueError(
                "accepted release requires public-suite/internal commit verification "
                "and an accepted_manifest reference"
            )
        if self.classification not in {
            ReleaseClassification.ACCEPTED_CURRENT,
            ReleaseClassification.ACCEPTED_SUPERSEDED,
        } and self.accepted_manifest_reference_id is not None:
            raise ValueError("only accepted releases may bind an accepted Manifest")
        if (
            self.classification is ReleaseClassification.ACCEPTED_CURRENT
            and self.superseded_by is not None
        ):
            raise ValueError("accepted_current must not declare superseded_by")
        if (
            self.classification is ReleaseClassification.ACCEPTED_SUPERSEDED
            and self.superseded_by is None
        ):
            raise ValueError("accepted_superseded requires superseded_by")
        if (
            self.classification not in {
                ReleaseClassification.ACCEPTED_CURRENT,
                ReleaseClassification.ACCEPTED_SUPERSEDED,
            }
            and self.superseded_by is not None
        ):
            raise ValueError("only accepted_superseded may declare superseded_by")
        if self.classification is ReleaseClassification.ABANDONED_PREPARATION and (
            self.commit_verification_mode is CommitVerificationMode.INTERNAL_REQUIRED
        ):
            raise ValueError("abandoned preparation may not require an internal commit")
        return self


class CampaignEntry(ContractModel):
    campaign_id: StrictStr = Field(pattern=ID_PATTERN)
    experiment_id: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
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
        if self.trees:
            raise ValueError(
                "CampaignEntry must declare file Artifacts only; bundle_root is a Release tree"
            )
        file_roles = [item.role for item in self.file_artifacts]
        if len(file_roles) != len(set(file_roles)):
            # Evidence and Recording are collections, so repeated roles are
            # allowed only when their paths are independently declared.
            repeated = {role for role in file_roles if file_roles.count(role) > 1}
            if repeated - {
                CampaignArtifactRole.EVIDENCE.value,
                CampaignArtifactRole.RECORDING.value,
            }:
                raise ValueError("CampaignEntry scalar file roles must be unique")
        required_roles = CAMPAIGN_PROFILE_REQUIRED_FILE_ROLES[self.verification_profile]
        declared_required_roles = {
            item.role for item in self.file_artifacts if item.required
        }
        if not required_roles.issubset(declared_required_roles):
            raise ValueError(
                f"{self.verification_profile} campaign is missing required file roles"
            )
        if self.verification_profile == "phase6_public_suite":
            raise ValueError("phase6_public_suite is a Release verification profile")
        if (
            self.verification_profile in {"phase6_campaign_complete", "historical_verification"}
            and self.experiment_id is None
        ):
            raise ValueError("typed Phase 6 Campaign profiles require experiment_id")
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
            self.verification_profile == "phase6_campaign_complete"
            and self.classification is not CampaignClassification.PRIMARY_EVALUATION
        ):
            raise ValueError("phase6_campaign_complete is reserved for primary_evaluation")
        if (
            self.verification_profile == "historical_verification"
            and self.classification is not CampaignClassification.HISTORICAL_NON_PRIMARY
        ):
            raise ValueError("historical_verification is reserved for historical_non_primary")
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
    subject_digest: StrictStr = Field(pattern=SHA256_PATTERN)
    created_at: StrictStr

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
        if self.external_copy_receipt is not None and (
            self.external_copy_receipt.role != "receipt"
            or not self.external_copy_receipt.required
        ):
            raise ValueError("external_copy_receipt must be a required receipt Artifact")
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
        # Campaign-only declared-artifact Requests remain useful for isolated
        # synthetic verifier tests.  Once a Release scope is declared, the
        # accepted-current relation is total and never selected implicitly.
        if self.release_entries and len(current) != 1:
            raise ValueError("exactly one accepted_current release is required")
        release_by_id = set(release_ids)
        for entry in self.release_entries:
            if entry.superseded_by is not None and entry.superseded_by not in release_by_id:
                raise ValueError("superseded_by must name a declared release")
            if entry.classification is ReleaseClassification.ACCEPTED_SUPERSEDED:
                superseded_target = next(
                    item
                    for item in self.release_entries
                    if item.release_id == entry.superseded_by
                )
                if superseded_target.classification is not ReleaseClassification.ACCEPTED_CURRENT:
                    raise ValueError("accepted_superseded must point to accepted_current")
            if (
                entry.classification is ReleaseClassification.ACCEPTED_CURRENT
                and entry.superseded_by is not None
            ):
                raise ValueError("accepted_current must not point to a successor")
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
        references_by_id = {
            reference.reference_id: reference
            for reference in self.source_of_truth_references
        }
        accepted_releases = [
            entry
            for entry in self.release_entries
            if entry.classification
            in {
                ReleaseClassification.ACCEPTED_CURRENT,
                ReleaseClassification.ACCEPTED_SUPERSEDED,
            }
        ]
        accepted_manifest_references = [
            reference
            for reference in self.source_of_truth_references
            if reference.kind == "accepted_manifest"
        ]
        if self.release_entries and len(accepted_manifest_references) != len(accepted_releases):
            raise ValueError(
                "each accepted release requires exactly one unique accepted_manifest reference"
            )
        bound_reference_ids: set[str] = set()
        for entry in self.release_entries:
            if entry.classification not in {
                ReleaseClassification.ACCEPTED_CURRENT,
                ReleaseClassification.ACCEPTED_SUPERSEDED,
            }:
                if entry.accepted_manifest_reference_id is not None:
                    raise ValueError(
                        "non-accepted release must not specify accepted_manifest_reference_id"
                    )
                continue
            assert entry.accepted_manifest_reference_id is not None
            if entry.accepted_manifest_reference_id in bound_reference_ids:
                raise ValueError(
                    "accepted_manifest reference cannot be shared across multiple releases"
                )
            bound_reference_ids.add(entry.accepted_manifest_reference_id)
            reference = references_by_id.get(entry.accepted_manifest_reference_id)
            if reference is None or reference.kind != "accepted_manifest":
                raise ValueError(
                    "accepted release must reference an accepted_manifest AuthorityReference"
                )
            suite_manifest = next(
                (
                    artifact
                    for artifact in entry.file_artifacts
                    if artifact.role == ReleaseArtifactRole.SUITE_MANIFEST.value
                ),
                None,
            )
            if suite_manifest is None or (
                suite_manifest.path != reference.path
                or suite_manifest.byte_count != reference.byte_count
                or suite_manifest.sha256 != reference.sha256
            ):
                raise ValueError(
                    "accepted release Manifest must match its accepted_manifest reference"
                )
        if {
            reference.reference_id for reference in accepted_manifest_references
        } != bound_reference_ids:
            raise ValueError("orphan accepted_manifest references are not allowed")
        # A Request normally has one owner per input path.  The sole exception
        # is a Release's checksums scalar, which may intentionally name the
        # same bytes as that Release's bundle_root/checksums.json tree member.
        # This narrow exception keeps the checksum contract explicit without
        # allowing suffix-based or cross-subject input aliasing.
        declared: dict[str, tuple[str, str, ExpectedFileArtifact | None]] = {}

        def add_declared(
            path: str,
            owner: str,
            kind: str,
            artifact: ExpectedFileArtifact | None,
        ) -> None:
            previous = declared.get(path)
            if previous is None:
                declared[path] = (owner, kind, artifact)
                return
            previous_owner, previous_kind, previous_artifact = previous
            pair = {
                (previous_kind, previous_artifact.role if previous_artifact else None),
                (kind, artifact.role if artifact else None),
            }
            allowed_pair = pair == {
                ("scalar", ReleaseArtifactRole.CHECKSUMS.value),
                ("tree", "checksums"),
            }
            if (
                not allowed_pair
                or previous_owner != owner
                or not path.endswith("/checksums.json")
                or previous_artifact is None
                or artifact is None
                or previous_artifact.byte_count != artifact.byte_count
                or previous_artifact.sha256 != artifact.sha256
                or not previous_artifact.required
                or not artifact.required
            ):
                raise ValueError("Request Artifact paths must be unique")

        for release_entry in self.release_entries:
            owner = f"release:{release_entry.release_id}"
            for item in release_entry.file_artifacts:
                add_declared(item.path, owner, "scalar", item)
            for tree in release_entry.trees:
                add_declared(tree.root_path, owner, "tree_root", None)
                for item in tree.file_artifacts:
                    add_declared(
                        f"{tree.root_path}/{item.path}",
                        owner,
                        "tree",
                        item.model_copy(update={"path": f"{tree.root_path}/{item.path}"}),
                    )
        for campaign_entry in self.campaign_entries:
            owner = f"campaign:{campaign_entry.campaign_id}"
            for item in campaign_entry.file_artifacts:
                add_declared(item.path, owner, "scalar", item)
        declared_artifact_paths = set(declared)
        accepted_manifest_bindings = {
            entry.accepted_manifest_reference_id
            for entry in self.release_entries
            if entry.accepted_manifest_reference_id is not None
        }
        for reference in self.source_of_truth_references:
            if reference.path in declared_artifact_paths:
                if not (
                    reference.kind == "accepted_manifest"
                    and reference.reference_id in accepted_manifest_bindings
                ):
                    raise ValueError("AuthorityReference path aliases an Artifact")
            elif reference.path in declared:
                raise ValueError("AuthorityReference path aliases an Artifact")
            else:
                declared[reference.path] = ("authority", "authority", None)
        for expectation in self.retention_expectations:
            receipt = expectation.external_copy_receipt
            if receipt is None:
                continue
            if receipt.path in declared_artifact_paths or receipt.path in {
                reference.path for reference in self.source_of_truth_references
            }:
                raise ValueError("retention receipt path aliases an input Artifact")
            if receipt.path in declared:
                raise ValueError("retention receipt path aliases an input Artifact")
            declared[receipt.path] = ("receipt", "receipt", None)
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
    artifact_reviewed_commits: list[StrictStr]
    commit_verification_mode: CommitVerificationMode
    commit_verification: IntegrityState
    classification: ReleaseClassification
    verification_profile: StrictStr
    storage_state: StorageState
    integrity_state: IntegrityState
    artifact_observations: list[ArtifactObservation]

    @field_validator("artifact_reviewed_commits")
    @classmethod
    def reviewed_commits_are_sorted(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(COMMIT_PATTERN, commit) for commit in values):
            raise ValueError("artifact_reviewed_commits must contain canonical commit IDs")
        if values != sorted(set(values)):
            raise ValueError("artifact_reviewed_commits must be sorted and unique")
        return values


class InventoryCampaignEntry(ContractModel):
    campaign_id: StrictStr = Field(pattern=ID_PATTERN)
    experiment_id: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
    artifact_reviewed_commit: StrictStr = Field(pattern=COMMIT_PATTERN)
    commit_verification_mode: CommitVerificationMode
    commit_verification: IntegrityState
    classification: CampaignClassification
    included_in_primary_denominator: StrictBool
    release_id: StrictStr | None = Field(default=None, pattern=ID_PATTERN)
    verification_profile: StrictStr
    storage_state: StorageState
    integrity_state: IntegrityState
    provider_total_status: ProviderTotalStatus
    provider_call_count_observed: StrictInt | None = Field(default=None, ge=0)
    provider_call_count_unknown_runs: StrictInt | None = Field(default=None, ge=0)
    artifact_observations: list[ArtifactObservation]

    @model_validator(mode="after")
    def provider_total_is_coherent(self) -> InventoryCampaignEntry:
        if self.provider_total_status is ProviderTotalStatus.OBSERVED:
            if (
                self.provider_call_count_observed is None
                or self.provider_call_count_unknown_runs != 0
            ):
                raise ValueError(
                    "observed provider total requires integer observed and zero unknown"
                )
        elif self.provider_total_status is ProviderTotalStatus.PARTIALLY_UNKNOWN:
            if (
                self.provider_call_count_observed is None
                or self.provider_call_count_unknown_runs is None
                or self.provider_call_count_unknown_runs < 1
            ):
                raise ValueError(
                    "partially_unknown provider total requires known and unknown counts"
                )
        elif (
            self.provider_call_count_observed is not None
            or self.provider_call_count_unknown_runs is not None
        ):
            raise ValueError("unavailable provider total must not invent numeric counts")
        return self


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
    primary_campaign_count: StrictInt = Field(ge=0)
    classification_counts: dict[StrictStr, StrictInt]
    storage_state_counts: dict[StorageState, StrictInt]
    integrity_state_counts: dict[IntegrityState, StrictInt]
    provider_accounting_scope: Literal["declared_campaign_entries"]
    provider_call_count_observed: StrictInt = Field(ge=0)
    provider_call_count_unknown_runs: StrictInt = Field(ge=0)
    campaigns_without_total: StrictInt = Field(ge=0)


class EvidenceInventory(ContractModel):
    schema_version: Literal["1.0"]
    inventory_id: StrictStr = Field(pattern=ID_PATTERN)
    request_correlation_id: StrictStr = Field(pattern=ID_PATTERN)
    authoritative: Literal[False]
    scope: Literal[InventoryScope.PHASE6]
    request_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    execution_head_attestation_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
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
class EvidenceInventoryPublicationVerification:
    """Strictly reloaded publication bound to its canonical Request bytes."""

    request: EvidenceInventoryRequest
    inventory: EvidenceInventory
    metadata: EvidenceInventoryMetadata


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


@dataclass(frozen=True)
class _AuthorityObservation:
    findings: tuple[InventoryFinding, ...]
    content: bytes | None


@dataclass(frozen=True)
class _VerificationResult:
    inventory: EvidenceInventory
    observed_execution_repository_head: str


@dataclass
class _InventorySnapshot:
    """One descriptor-backed, bounded snapshot shared by the whole verifier."""

    root: Path
    root_fd: int
    root_identity: tuple[int, int, int, int, int, int, int]
    directory_fds: dict[str, int]
    directory_identities: dict[str, tuple[int, int, int, int, int, int, int]]
    file_fds: dict[str, int]
    file_identities: dict[str, tuple[int, int, int, int, int, int, int] | None]

    def close(self) -> None:
        descriptors = [*self.file_fds.values(), *self.directory_fds.values()]
        self.file_fds.clear()
        self.directory_fds.clear()
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _snapshot_root(root: Path) -> _InventorySnapshot:
    """Open and identity-check the repository root exactly once."""
    descriptor: int | None = None
    try:
        before = root.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise InventorySafetyError("repository root must be a real directory")
        descriptor = os.open(root, _directory_open_flags())
        opened = os.fstat(descriptor)
        after = root.lstat()
    except InventorySafetyError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise InventorySafetyError("repository root could not be opened safely") from error
    identities = {_identity(before), _identity(opened), _identity(after)}
    if len(identities) != 1:
        os.close(descriptor)
        raise InventorySafetyError("repository root changed while being opened")
    identity = _identity(opened)
    return _InventorySnapshot(
        root=root,
        root_fd=descriptor,
        root_identity=identity,
        directory_fds={"": descriptor},
        directory_identities={"": identity},
        file_fds={},
        file_identities={},
    )


def _relative_parent(relative: str) -> tuple[str, str]:
    parts = PurePosixPath(relative).parts
    if not parts:
        raise InventorySafetyError("relative path must name a file or directory")
    return "/".join(parts[:-1]), parts[-1]


def _open_snapshot_directory(
    snapshot: _InventorySnapshot,
    relative: str,
    label: str,
    *,
    final_kind: Literal["directory", "parent"] = "directory",
) -> tuple[int | None, Literal["missing", "unsafe"] | None]:
    """Open each component from a fixed descriptor, never through a Path."""
    if relative == "":
        return snapshot.root_fd, None
    parts = PurePosixPath(relative).parts
    current_relative = ""
    parent_fd = snapshot.root_fd
    for index, component in enumerate(parts):
        child_relative = (
            component if not current_relative else f"{current_relative}/{component}"
        )
        is_final = index == len(parts) - 1
        try:
            before = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None, "missing"
        except OSError as error:
            raise InventorySafetyError(f"could not inspect {label} parent") from error
        if stat.S_ISLNK(before.st_mode):
            if is_final and final_kind == "directory":
                return None, "unsafe"
            raise InventorySafetyError(f"{label} contains a symlinked parent")
        if not stat.S_ISDIR(before.st_mode):
            if is_final and final_kind == "directory":
                return None, "unsafe"
            return None, "missing"
        cached = snapshot.directory_fds.get(child_relative)
        if cached is None:
            try:
                cached = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=parent_fd,
                )
            except OSError as error:
                raise InventorySafetyError(f"could not open {label} directory") from error
            opened = os.fstat(cached)
        else:
            opened = os.fstat(cached)
        try:
            after = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            if cached not in snapshot.directory_fds.values():
                os.close(cached)
            raise InventorySafetyError(f"{label} directory changed while opening") from error
        if not (
            _identity(before) == _identity(opened) == _identity(after)
        ):
            if cached not in snapshot.directory_fds.values():
                os.close(cached)
            raise InventorySafetyError(f"{label} directory changed while opening")
        identity = _identity(opened)
        previous = snapshot.directory_identities.get(child_relative)
        if previous is not None and previous != identity:
            if cached not in snapshot.directory_fds.values():
                os.close(cached)
            raise InventorySafetyError(f"{label} directory identity changed")
        if child_relative not in snapshot.directory_fds:
            snapshot.directory_fds[child_relative] = cached
        snapshot.directory_identities[child_relative] = identity
        parent_fd = cached
        current_relative = child_relative
    return parent_fd, None


def _open_snapshot_directory_ephemeral(
    snapshot: _InventorySnapshot,
    relative: str,
    label: str,
    *,
    final_kind: Literal["directory", "parent"] = "directory",
) -> tuple[int | None, Literal["missing", "unsafe"] | None, tuple[int, ...]]:
    """Open a directory path without retaining newly opened descriptors.

    The fixed root descriptor and any already-cached parents are reused.  Every
    new component is kept only for the duration of this call (and the caller's
    scan), while its identity is retained for final snapshot revalidation.
    """
    if relative == "":
        return snapshot.root_fd, None, ()
    owned: list[int] = []

    def close_owned() -> None:
        for descriptor in reversed(owned):
            with suppress(OSError):
                os.close(descriptor)
        owned.clear()

    parts = PurePosixPath(relative).parts
    current_relative = ""
    parent_fd = snapshot.root_fd
    try:
        for index, component in enumerate(parts):
            child_relative = (
                component if not current_relative else f"{current_relative}/{component}"
            )
            is_final = index == len(parts) - 1
            try:
                before = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                close_owned()
                return None, "missing", ()
            except OSError as error:
                raise InventorySafetyError(f"could not inspect {label} parent") from error
            if stat.S_ISLNK(before.st_mode):
                if is_final and final_kind == "directory":
                    close_owned()
                    return None, "unsafe", ()
                raise InventorySafetyError(f"{label} contains a symlinked parent")
            if not stat.S_ISDIR(before.st_mode):
                if is_final and final_kind == "directory":
                    close_owned()
                    return None, "unsafe", ()
                close_owned()
                return None, "missing", ()

            descriptor = snapshot.directory_fds.get(child_relative)
            if descriptor is None:
                try:
                    descriptor = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=parent_fd,
                    )
                except OSError as error:
                    raise InventorySafetyError(f"could not open {label} directory") from error
                owned.append(descriptor)
            opened = os.fstat(descriptor)
            try:
                after = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise InventorySafetyError(f"{label} directory changed while opening") from error
            if _identity(before) != _identity(opened) or _identity(after) != _identity(opened):
                raise InventorySafetyError(f"{label} directory changed while opening")
            identity = _identity(opened)
            previous = snapshot.directory_identities.get(child_relative)
            if previous is not None and previous != identity:
                raise InventorySafetyError(f"{label} directory identity changed")
            snapshot.directory_identities[child_relative] = identity
            parent_fd = descriptor
            current_relative = child_relative
    except InventorySafetyError:
        close_owned()
        raise
    except OSError as error:
        close_owned()
        raise InventorySafetyError(f"{label} directory could not be revalidated") from error
    return parent_fd, None, tuple(owned)


def _watch_path(
    snapshot: _InventorySnapshot,
    relative: str,
    identity: tuple[int, int, int, int, int, int, int] | None,
) -> None:
    if relative in snapshot.file_identities and snapshot.file_identities[relative] != identity:
        raise InventorySafetyError(f"{relative} changed between snapshot reads")
    snapshot.file_identities[relative] = identity


def _watch_snapshot_entry(
    snapshot: _InventorySnapshot,
    relative: str,
    label: str,
) -> None:
    """Remember a present or absent non-file entry for final revalidation."""
    parent_relative, filename = _relative_parent(relative)
    parent_fd, parent_state, owned_descriptors = _open_snapshot_directory_ephemeral(
        snapshot,
        parent_relative,
        f"{label} parent",
        final_kind="parent",
    )
    try:
        if parent_state == "missing" or parent_fd is None:
            _watch_path(snapshot, relative, None)
            return
        try:
            metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _watch_path(snapshot, relative, None)
        except OSError as error:
            raise InventorySafetyError(f"could not inspect {label}") from error
        else:
            _watch_path(snapshot, relative, _identity(metadata))
    finally:
        for owned_descriptor in reversed(owned_descriptors):
            with suppress(OSError):
                os.close(owned_descriptor)


def _read_open_file(
    snapshot: _InventorySnapshot,
    parent_fd: int,
    filename: str,
    relative: str,
    label: str,
    before: os.stat_result,
    *,
    max_bytes: int,
    track: bool,
) -> _FileRead:
    if before.st_size > max_bytes:
        raise InventorySafetyError(f"{label} exceeds the bounded read limit")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise InventorySafetyError(f"{label} changed before reading")
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
        after_open = os.fstat(descriptor)
        after_path = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if not (
            _identity(before) == _identity(opened) == _identity(after_open) == _identity(after_path)
        ):
            raise InventorySafetyError(f"{label} changed while reading")
        content = b"".join(chunks)
        if len(content) != after_open.st_size:
            raise InventorySafetyError(f"{label} size changed while reading")
        identity = _identity(after_open)
        if track:
            _watch_path(snapshot, relative, identity)
            old = snapshot.file_fds.pop(relative, None)
            if old is not None:
                os.close(old)
            snapshot.file_fds[relative] = descriptor
            descriptor = None
        return _FileRead(True, True, content, len(content), _sha256(content))
    except InventorySafetyError:
        raise
    except OSError as error:
        raise InventorySafetyError(f"could not read {label} safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def verify_evidence_inventory_publication_bytes(
    request_bytes: bytes,
    inventory_bytes: bytes,
    markdown_bytes: bytes,
    metadata_bytes: bytes,
) -> EvidenceInventoryPublicationVerification:
    """Strictly verify a complete publication, including its Request binding.

    This facade is byte-only and is valid for both verified (exit 0) and
    findings-bearing (exit 2) publications.  It performs no filesystem,
    Provider, Gate, Campaign, or network operation.
    """
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise InventoryContractError("Evidence Inventory Request exceeds the bounded size")
    if any(len(value) > MAX_PUBLICATION_FILE_BYTES for value in (
        inventory_bytes,
        markdown_bytes,
        metadata_bytes,
    )):
        raise InventoryContractError("Evidence Inventory publication exceeds the bounded size")
    request = load_inventory_request_bytes(request_bytes)
    inventory = _load_canonical_bytes(
        inventory_bytes, EvidenceInventory, "Evidence Inventory"
    )
    metadata = _load_canonical_bytes(
        metadata_bytes, EvidenceInventoryMetadata, "Inventory metadata"
    )
    request_sha256 = _sha256(request_bytes)
    correlation_id = "rc-" + _sha256(
        f"{request.inventory_id}\0{request_sha256}".encode()
    )[:32]
    if (
        inventory.inventory_id != request.inventory_id
        or inventory.scope is not request.scope
        or inventory.authoritative is not request.authoritative
        or inventory.request_sha256 != request_sha256
        or inventory.request_correlation_id != correlation_id
        or inventory.source_of_truth_references != request.source_of_truth_references
        or [entry.release_id for entry in inventory.releases]
        != [entry.release_id for entry in request.release_entries]
        or [entry.campaign_id for entry in inventory.campaigns]
        != [entry.campaign_id for entry in request.campaign_entries]
        or [
            (entry.release_id, entry.artifact_reviewed_commits)
            for entry in inventory.releases
        ]
        != [
            (entry.release_id, entry.artifact_reviewed_commits)
            for entry in request.release_entries
        ]
        or [
            (
                entry.campaign_id,
                entry.experiment_id,
                entry.artifact_reviewed_commit,
                entry.release_id,
            )
            for entry in inventory.campaigns
        ]
        != [
            (
                entry.campaign_id,
                entry.experiment_id,
                entry.artifact_reviewed_commit,
                entry.release_id,
            )
            for entry in request.campaign_entries
        ]
    ):
        raise InventoryContractError("publication does not bind to the supplied Request")
    if (
        metadata.request_correlation_id != correlation_id
        or metadata.request_sha256 != request_sha256
        or metadata.inventory_sha256 != _sha256(inventory_bytes)
        or metadata.markdown_sha256 != _sha256(markdown_bytes)
        or metadata.expected_execution_repository_head
        != request.expected_execution_repository_head
        or metadata.renderer_version != RENDERER_VERSION
        or metadata.tool_version != TOOL_VERSION
        or inventory.execution_head_attestation_sha256
        != _execution_head_attestation_sha256(
            metadata.observed_execution_repository_head
        )
    ):
        raise InventoryContractError("publication metadata does not bind to the supplied Request")
    if render_inventory_markdown(inventory) != markdown_bytes:
        raise InventoryContractError("publication Markdown differs from canonical renderer output")
    return EvidenceInventoryPublicationVerification(
        request=request,
        inventory=inventory,
        metadata=metadata,
    )


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


def _same_object_identity(
    current: os.stat_result,
    expected: tuple[int, int, int, int, int, int, int],
) -> bool:
    """Compare immutable ownership identity while permitting expected dir mtime changes."""
    return (
        current.st_dev == expected[0]
        and current.st_ino == expected[1]
        and stat.S_IFMT(current.st_mode) == stat.S_IFMT(expected[2])
    )


def _read_regular_file(
    root: Path,
    relative: str,
    label: str,
    *,
    max_bytes: int = MAX_ARTIFACT_FILE_BYTES,
    snapshot: _InventorySnapshot | None = None,
    track: bool = True,
) -> _FileRead:
    """Read a final file through descriptor-fixed parent components."""
    _canonical_relative(relative, label)
    owned_snapshot = snapshot is None
    if snapshot is None:
        root = _real_directory(root, "repository root")
        snapshot = _snapshot_root(root)
    parent_relative, filename = _relative_parent(relative)
    owned_descriptors: tuple[int, ...] = ()
    try:
        parent_fd, parent_state, owned_descriptors = _open_snapshot_directory_ephemeral(
            snapshot,
            parent_relative,
            label,
            final_kind="parent",
        )
        try:
            if parent_state == "missing" or parent_fd is None:
                if track:
                    _watch_path(snapshot, relative, None)
                return _FileRead(False, True, None, None, None, "missing")
            try:
                before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if track:
                    _watch_path(snapshot, relative, None)
                return _FileRead(False, True, None, None, None, "missing")
            except OSError as error:
                raise InventorySafetyError(f"could not inspect {label}") from error
            final_identity = _identity(before)
            if stat.S_ISLNK(before.st_mode):
                if track:
                    _watch_path(snapshot, relative, final_identity)
                return _FileRead(True, True, None, None, None, "symlink")
            if stat.S_ISREG(before.st_mode) and before.st_nlink != 1:
                if track:
                    _watch_path(snapshot, relative, final_identity)
                return _FileRead(True, True, None, None, None, "hardlink")
            if not stat.S_ISREG(before.st_mode):
                if track:
                    _watch_path(snapshot, relative, final_identity)
                return _FileRead(True, True, None, None, None, "special_file")
            return _read_open_file(
                snapshot,
                parent_fd,
                filename,
                relative,
                label,
                before,
                max_bytes=max_bytes,
                track=track,
            )
        finally:
            for owned_descriptor in reversed(owned_descriptors):
                with suppress(OSError):
                    os.close(owned_descriptor)
    finally:
        if owned_snapshot:
            snapshot.close()


def _read_request_file(
    path: Path,
    root: Path,
    *,
    snapshot: _InventorySnapshot | None = None,
) -> bytes:
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
        snapshot=snapshot,
    )
    if not result.exists or result.content is None or result.reason is not None:
        raise InventorySafetyError("Request must be a stable single-link regular file")
    return result.content


def _snapshot_revalidate(snapshot: _InventorySnapshot) -> None:
    """Revalidate every observed identity before any publication begins."""
    try:
        root_path_identity = _identity(snapshot.root.lstat())
        if root_path_identity != snapshot.root_identity:
            raise InventorySafetyError("repository root changed after snapshot")
        if _identity(os.fstat(snapshot.root_fd)) != snapshot.root_identity:
            raise InventorySafetyError("repository root descriptor changed")
    except OSError as error:
        raise InventorySafetyError("repository root could not be revalidated") from error

    # Re-open each named directory from the fixed root descriptor.  This checks
    # the name-to-inode relation as well as the held descriptor identity, so a
    # parent swap cannot silently redirect a later read or publish.
    for relative, expected_identity in sorted(
        snapshot.directory_identities.items(),
        key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]),
    ):
        descriptor, state, owned_descriptors = _open_snapshot_directory_ephemeral(
            snapshot,
            relative,
            f"snapshot directory {relative or '.'}",
            final_kind="directory",
        )
        if state is not None or descriptor is None:
            raise InventorySafetyError("observed directory disappeared during snapshot")
        try:
            if _identity(os.fstat(descriptor)) != expected_identity:
                raise InventorySafetyError("observed directory identity changed")
        except OSError as error:
            raise InventorySafetyError("observed directory could not be revalidated") from error
        finally:
            for owned_descriptor in reversed(owned_descriptors):
                with suppress(OSError):
                    os.close(owned_descriptor)

    for relative, expected_file_identity in snapshot.file_identities.items():
        parent_relative, filename = _relative_parent(relative)
        parent_fd, parent_state, owned_descriptors = _open_snapshot_directory_ephemeral(
            snapshot,
            parent_relative,
            f"snapshot file {relative}",
            final_kind="parent",
        )
        try:
            if parent_state == "missing" or parent_fd is None:
                if expected_file_identity is None:
                    continue
                raise InventorySafetyError("observed file parent changed during snapshot")
            try:
                current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if expected_file_identity is None:
                    continue
                raise InventorySafetyError("observed file disappeared during snapshot") from None
            except OSError as error:
                raise InventorySafetyError("observed file could not be revalidated") from error
            current_identity = _identity(current)
            if expected_file_identity is None or current_identity != expected_file_identity:
                raise InventorySafetyError("observed file identity changed during snapshot")
            held = snapshot.file_fds.get(relative)
            if held is not None:
                try:
                    if _identity(os.fstat(held)) != expected_file_identity:
                        raise InventorySafetyError("held file descriptor identity changed")
                except OSError as error:
                    raise InventorySafetyError(
                        "held file descriptor could not be revalidated"
                    ) from error
        finally:
            for owned_descriptor in reversed(owned_descriptors):
                with suppress(OSError):
                    os.close(owned_descriptor)


def _known_contract_is_valid(
    role: str,
    content: bytes,
    *,
    verification_profile: str | None = None,
) -> bool:
    """Use Phase 6 strict loaders for roles whose contract is known."""
    if role == "report_markdown" and verification_profile != "historical_verification":
        return True
    try:
        from agentlab.phase6 import (
            validate_historical_phase6_artifact_contract,
            validate_phase6_snapshot_contract,
        )

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
        if verification_profile == "historical_verification" and role in {
            "plan",
            "campaign",
            "historical_verification",
            "report_json",
        }:
            validate_historical_phase6_artifact_contract(role, content)
        elif role in known_roles:
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
        "declared reviewed commit set differs from observed Artifact commit set for {role}"
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
            FindingCode.ARTIFACT_REVIEWED_COMMIT_MISMATCH,
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
    snapshot: _InventorySnapshot,
    subject_kind: Literal["release", "campaign", "authority", "retention"],
    subject_id: str,
    expected: ExpectedFileArtifact,
    role_prefix: str | None = None,
    verification_profile: str | None = None,
) -> _ObservationResult:
    read = _read_regular_file(
        root,
        expected.path,
        f"{subject_id} {expected.role}",
        snapshot=snapshot,
    )
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
        return _ObservationResult(observation, tuple(findings), {})
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
        return _ObservationResult(observation, tuple(findings), {})
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
    if verification_profile == "historical_verification":
        contract_valid = _known_contract_is_valid(
            expected.role,
            read.content,
            verification_profile=verification_profile,
        )
    else:
        contract_valid = _known_contract_is_valid(expected.role, read.content)
    if not contract_valid:
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
    return _ObservationResult(observation, tuple(findings), {expected.path: read.content})


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
    snapshot: _InventorySnapshot,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    tree: ExpectedTree,
    verification_profile: str | None = None,
) -> _ObservationResult:
    findings: list[InventoryFinding] = []
    _watch_snapshot_entry(snapshot, tree.root_path, f"{subject_id} tree")
    tree_fd, tree_state = _open_snapshot_directory(
        snapshot,
        tree.root_path,
        f"{subject_id} tree",
        final_kind="directory",
    )
    if tree_state == "missing" or tree_fd is None:
        if tree.required:
            findings.append(
                _finding(
                    FindingCode.ARTIFACT_MISSING,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=tree.role,
                    path=tree.root_path,
                )
            )
        return _ObservationResult(
            ArtifactObservation(
                role=tree.role,
                path=tree.root_path,
                kind="tree",
                required=tree.required,
                storage_state=StorageState.MISSING,
                integrity_state=IntegrityState.NOT_VERIFIABLE,
                expected_file_count=tree.expected_file_count,
                expected_tree_sha256=tree.tree_sha256,
            ),
            tuple(findings),
            {},
        )
    if tree_state == "unsafe":
        finding = _finding(
            FindingCode.UNSAFE_ARTIFACT,
            subject_kind=subject_kind,
            subject_id=subject_id,
            role=tree.role,
            path=tree.root_path,
        )
        return _ObservationResult(
            ArtifactObservation(
                role=tree.role,
                path=tree.root_path,
                kind="tree",
                required=tree.required,
                storage_state=StorageState.PRESENT,
                integrity_state=IntegrityState.DRIFTED,
                expected_file_count=tree.expected_file_count,
                expected_tree_sha256=tree.tree_sha256,
            ),
            (finding,),
            {},
        )

    expected_paths = {item.path: item for item in tree.file_artifacts}
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    unsafe_paths: set[str] = set()
    scanned_tree_bytes = 0
    stack: list[str] = [""]
    while stack:
        relative_directory = stack.pop()
        owned_descriptors: tuple[int, ...] = ()
        if relative_directory:
            current_fd, child_state, owned_descriptors = _open_snapshot_directory_ephemeral(
                snapshot,
                f"{tree.root_path}/{relative_directory}",
                f"{subject_id} tree child",
                final_kind="directory",
            )
            if child_state is not None or current_fd is None:
                raise InventorySafetyError(
                    f"tree changed while opening {subject_id} child"
                )
        else:
            current_fd = tree_fd
        try:
            before_directory = os.fstat(current_fd)
            scan_fd = os.dup(current_fd)
            with os.scandir(scan_fd) as entries:
                for entry in entries:
                    relative = (
                        entry.name
                        if not relative_directory
                        else f"{relative_directory}/{entry.name}"
                    )
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise InventorySafetyError(
                            f"tree identity could not be established for {subject_id}"
                        ) from error
                    _watch_path(
                        snapshot,
                        f"{tree.root_path}/{relative}",
                        _identity(metadata),
                    )
                    if stat.S_ISREG(metadata.st_mode):
                        if metadata.st_size > MAX_ARTIFACT_FILE_BYTES:
                            raise InventorySafetyError(
                                f"tree file exceeds the bounded read limit for {subject_id}"
                            )
                        scanned_tree_bytes += metadata.st_size
                        if scanned_tree_bytes > MAX_TREE_BYTES:
                            raise InventorySafetyError(
                                f"tree exceeds the bounded byte limit for {subject_id}"
                            )
                    unsafe = (
                        stat.S_ISLNK(metadata.st_mode)
                        or (
                            stat.S_ISREG(metadata.st_mode)
                            and metadata.st_nlink != 1
                        )
                        or (
                            not stat.S_ISREG(metadata.st_mode)
                            and not stat.S_ISDIR(metadata.st_mode)
                        )
                    )
                    if unsafe:
                        unsafe_paths.add(relative)
                        if len(actual_files) + len(unsafe_paths) > MAX_TREE_FILES:
                            raise InventorySafetyError(
                                f"tree exceeds the bounded file limit for {subject_id}"
                            )
                        if (
                            len(actual_files) + len(actual_directories) + len(unsafe_paths)
                            > MAX_TREE_ENTRIES
                        ):
                            raise InventorySafetyError(
                                f"tree exceeds the bounded entry limit for {subject_id}"
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
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        actual_directories.add(relative)
                        if len(actual_directories) > MAX_TREE_DIRECTORIES:
                            raise InventorySafetyError(
                                f"tree exceeds the bounded directory limit for {subject_id}"
                            )
                        if (
                            len(actual_files) + len(actual_directories) + len(unsafe_paths)
                            > MAX_TREE_ENTRIES
                        ):
                            raise InventorySafetyError(
                                f"tree exceeds the bounded entry limit for {subject_id}"
                            )
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
                        stack.append(relative)
                    else:
                        actual_files.add(relative)
                        if len(actual_files) + len(unsafe_paths) > MAX_TREE_FILES:
                            raise InventorySafetyError(
                                f"tree exceeds the bounded file limit for {subject_id}"
                            )
                        if (
                            len(actual_files) + len(actual_directories) + len(unsafe_paths)
                            > MAX_TREE_ENTRIES
                        ):
                            raise InventorySafetyError(
                                f"tree exceeds the bounded entry limit for {subject_id}"
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
            after_directory = os.fstat(current_fd)
            if _identity(before_directory) != _identity(after_directory):
                raise InventorySafetyError(f"tree changed during scan for {subject_id}")
        except InventorySafetyError:
            raise
        except OSError as error:
            raise InventorySafetyError(f"tree changed during scan for {subject_id}") from error
        finally:
            for owned_descriptor in reversed(owned_descriptors):
                with suppress(OSError):
                    os.close(owned_descriptor)

    for relative_directory in sorted(
        set(tree.allowed_directories) - actual_directories - unsafe_paths
    ):
        _watch_snapshot_entry(
            snapshot,
            f"{tree.root_path}/{relative_directory}",
            f"{subject_id} tree directory",
        )
        findings.append(
            _finding(
                FindingCode.ARTIFACT_MISSING,
                subject_kind=subject_kind,
                subject_id=subject_id,
                role=tree.role,
                path=f"{tree.root_path}/{relative_directory}",
            )
        )

    contents: dict[str, bytes] = {}
    observed_files: dict[str, tuple[int, str]] = {}
    observed_tree_bytes = 0
    for relative, expected in expected_paths.items():
        full_expected = expected.model_copy(
            update={"path": _tree_relative_path(tree, expected)}
        )
        result = _observe_file(
            root=root,
            snapshot=snapshot,
            subject_kind=subject_kind,
            subject_id=subject_id,
            expected=full_expected,
            verification_profile=verification_profile,
        )
        findings.extend(result.findings)
        contents.update(result.contents)
        if result.observation.observed_byte_count is not None:
            observed_tree_bytes += result.observation.observed_byte_count
            if observed_tree_bytes > MAX_TREE_BYTES:
                raise InventorySafetyError(
                    f"tree exceeds the bounded byte limit for {subject_id}"
                )
        if (
            result.observation.observed_byte_count is not None
            and result.observation.observed_sha256 is not None
        ):
            observed_files[relative] = (
                result.observation.observed_byte_count,
                result.observation.observed_sha256,
            )

    required_missing = any(
        item.required
        and item.path not in actual_files
        and item.path not in unsafe_paths
        for item in tree.file_artifacts
    ) or any(
        item.code is FindingCode.ARTIFACT_MISSING
        and item.path is not None
        and item.path.startswith(f"{tree.root_path}/")
        for item in findings
    )
    optional_missing = any(
        not item.required and item.path not in actual_files
        for item in tree.file_artifacts
    )
    has_drift = any(
        item.code
        in {
            FindingCode.UNSAFE_ARTIFACT,
            FindingCode.UNEXPECTED_PATH,
            FindingCode.ARTIFACT_BYTES_MISMATCH,
            FindingCode.ARTIFACT_SHA256_MISMATCH,
            FindingCode.CANONICAL_LOAD_FAILED,
        }
        for item in findings
    )
    observed_tree_digest: str | None = None
    if not required_missing and not has_drift and not optional_missing:
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
            has_drift = True
    incomplete = required_missing or optional_missing
    storage = StorageState.PARTIAL if incomplete else StorageState.PRESENT
    if incomplete:
        integrity = IntegrityState.NOT_VERIFIABLE
    elif has_drift or any(
        item.code is FindingCode.ARTIFACT_MISSING
        and item.path == tree.root_path
        for item in findings
    ):
        integrity = IntegrityState.DRIFTED
    else:
        integrity = IntegrityState.VERIFIED
    observation = ArtifactObservation(
        role=tree.role,
        path=tree.root_path,
        kind="tree",
        required=tree.required,
        storage_state=storage,
        integrity_state=integrity,
        expected_file_count=tree.expected_file_count,
        observed_file_count=len(actual_files),
        expected_tree_sha256=tree.tree_sha256,
        observed_tree_sha256=observed_tree_digest,
    )
    return _ObservationResult(observation, tuple(findings), contents)


def compute_subject_digest(
    *,
    subject_kind: str,
    subject_id: str,
    experiment_id: str | None,
    reviewed_commits: Sequence[str],
    file_artifacts: Sequence[ExpectedFileArtifact],
    trees: Sequence[ExpectedTree],
    observations: Sequence[ArtifactObservation],
) -> str | None:
    """Compute a deterministic digest over the complete required subject set."""
    observations_by_key = {
        (observation.kind, observation.role, observation.path): observation
        for observation in observations
    }
    for artifact in file_artifacts:
        if not artifact.required:
            continue
        observation = observations_by_key.get(("file", artifact.role, artifact.path))
        if (
            observation is None
            or observation.storage_state is not StorageState.PRESENT
            or observation.integrity_state is not IntegrityState.VERIFIED
            or observation.observed_byte_count is None
            or observation.observed_sha256 is None
        ):
            return None
    for tree in trees:
        if not tree.required:
            continue
        observation = observations_by_key.get(("tree", tree.role, tree.root_path))
        if (
            observation is None
            or observation.storage_state is not StorageState.PRESENT
            or observation.integrity_state is not IntegrityState.VERIFIED
            or observation.observed_file_count is None
            or observation.observed_tree_sha256 is None
        ):
            return None

    files_payload = [
        {
            "kind": "file",
            "role": artifact.role,
            "path": artifact.path,
            "byte_count": observations_by_key[("file", artifact.role, artifact.path)]
            .observed_byte_count,
            "sha256": observations_by_key[("file", artifact.role, artifact.path)]
            .observed_sha256,
        }
        for artifact in sorted(
            (item for item in file_artifacts if item.required),
            key=lambda item: (item.role, item.path),
        )
    ]
    trees_payload = [
        {
            "kind": "tree",
            "role": tree.role,
            "path": tree.root_path,
            "file_count": observations_by_key[("tree", tree.role, tree.root_path)]
            .observed_file_count,
            "tree_sha256": observations_by_key[("tree", tree.role, tree.root_path)]
            .observed_tree_sha256,
        }
        for tree in sorted(
            (item for item in trees if item.required),
            key=lambda item: (item.role, item.root_path),
        )
    ]
    canonical_commits = list(reviewed_commits)
    if canonical_commits != sorted(set(canonical_commits)):
        raise InventoryContractError("subject digest reviewed commits must be sorted and unique")
    payload = {
        "artifact_reviewed_commits": canonical_commits,
        "files": files_payload,
        "trees": trees_payload,
    }
    raw = (
        b"agentlab.phase7.subject.v2\x00"
        + subject_kind.encode("utf-8")
        + b"\x00"
        + subject_id.encode("utf-8")
        + b"\x00"
        + (experiment_id or "").encode("utf-8")
        + b"\x00"
        + canonical_inventory_json_bytes(payload)
    )
    return _sha256(raw)


def _observe_authority(
    *,
    root: Path,
    snapshot: _InventorySnapshot,
    reference: AuthorityReference,
) -> _AuthorityObservation:
    read = _read_regular_file(
        root,
        reference.path,
        f"authority {reference.reference_id}",
        snapshot=snapshot,
    )
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
    snapshot: _InventorySnapshot,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    file_artifacts: Sequence[ExpectedFileArtifact],
    trees: Sequence[ExpectedTree],
    verification_profile: str | None = None,
) -> tuple[list[ArtifactObservation], list[InventoryFinding], dict[str, bytes]]:
    observations: list[ArtifactObservation] = []
    findings: list[InventoryFinding] = []
    contents: dict[str, bytes] = {}
    for expected in file_artifacts:
        result = _observe_file(
            root=root,
            snapshot=snapshot,
            subject_kind=subject_kind,
            subject_id=subject_id,
            expected=expected,
            verification_profile=verification_profile,
        )
        observations.append(result.observation)
        findings.extend(result.findings)
        contents.update(result.contents)
    for tree in trees:
        result = _observe_tree(
            root=root,
            snapshot=snapshot,
            subject_kind=subject_kind,
            subject_id=subject_id,
            tree=tree,
            verification_profile=verification_profile,
        )
        observations.append(result.observation)
        findings.extend(result.findings)
        contents.update(result.contents)
    return observations, findings, contents


def _commit_observation(
    *,
    subject_kind: Literal["release", "campaign"],
    subject_id: str,
    expected_commits: frozenset[str],
    mode: CommitVerificationMode,
    observed_commits: frozenset[str],
    role: str,
) -> tuple[IntegrityState, tuple[InventoryFinding, ...]]:
    if mode is CommitVerificationMode.DECLARATION_BASIS_ONLY:
        return IntegrityState.VERIFIED, ()
    expected = ",".join(sorted(expected_commits)) or "<none>"
    observed = ",".join(sorted(observed_commits)) or "<none>"
    if expected_commits and expected_commits == observed_commits:
        return IntegrityState.VERIFIED, ()
    if observed_commits or mode is CommitVerificationMode.INTERNAL_REQUIRED:
        return (
            IntegrityState.DRIFTED,
            (
                _finding(
                    FindingCode.ARTIFACT_REVIEWED_COMMIT_MISMATCH,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    role=role,
                    expected=expected,
                    observed=observed,
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


def _single_role_content(
    entry: CampaignEntry,
    contents: Mapping[str, bytes],
    role: CampaignArtifactRole,
) -> bytes:
    artifacts = [item for item in entry.file_artifacts if item.role == role.value]
    if len(artifacts) != 1:
        raise InventoryContractError(f"{entry.campaign_id} requires exactly one {role.value}")
    content = contents.get(artifacts[0].path)
    if content is None:
        raise InventoryContractError(f"{entry.campaign_id} {role.value} is unavailable")
    return content


def _campaign_typed_provenance(
    entry: CampaignEntry,
    contents: Mapping[str, bytes],
) -> tuple[frozenset[str], str | None, bool]:
    """Derive only role-authorized Campaign provenance from stable bytes."""
    try:
        from agentlab.phase6 import (
            derive_phase6_campaign_experiment_id_from_bytes,
            derive_primary_reviewed_commit_from_bytes,
            validate_historical_phase6_snapshot,
        )

        if entry.verification_profile == "phase6_campaign_complete":
            experiment_id = derive_phase6_campaign_experiment_id_from_bytes(
                _single_role_content(entry, contents, CampaignArtifactRole.CAMPAIGN)
            )
            reviewed_commit = derive_primary_reviewed_commit_from_bytes(
                spec_bytes=_single_role_content(entry, contents, CampaignArtifactRole.SPEC),
                fixture_manifest_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.FIXTURE_MANIFEST
                ),
                fixture_acceptance_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.FIXTURE_ACCEPTANCE
                ),
                diff_policy_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.DIFF_POLICY
                ),
                plan_bytes=_single_role_content(entry, contents, CampaignArtifactRole.PLAN),
            )
            return frozenset({reviewed_commit}), experiment_id, True
        if entry.verification_profile == "historical_verification":
            historical = validate_historical_phase6_snapshot(
                record_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.HISTORICAL_VERIFICATION
                ),
                plan_bytes=_single_role_content(entry, contents, CampaignArtifactRole.PLAN),
                campaign_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.CAMPAIGN
                ),
                report_json_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.REPORT_JSON
                ),
                report_markdown_bytes=_single_role_content(
                    entry, contents, CampaignArtifactRole.REPORT_MARKDOWN
                ),
            )
            return (
                frozenset({historical.source_reviewed_commit}),
                historical.experiment_id,
                True,
            )
        experiment_id = derive_phase6_campaign_experiment_id_from_bytes(
            _single_role_content(entry, contents, CampaignArtifactRole.CAMPAIGN)
        )
        return frozenset(), experiment_id, True
    except Exception:
        return frozenset(), None, False


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


def _provider_counts(
    *,
    request: EvidenceInventoryRequest,
    campaign_outputs: Sequence[InventoryCampaignEntry],
    subject_contents: Mapping[tuple[str, str], Mapping[str, bytes]],
) -> tuple[list[InventoryCampaignEntry], list[InventoryFinding], int, int, int]:
    """Account provider calls from each declared Campaign exactly once.

    Only the canonical Campaign finished event is an accounting source.
    Evidence, reports, and public-bundle mirrors are validation witnesses.  A
    Campaign that is missing, drifted, or cannot be canonically loaded is
    exposed as ``campaigns_without_total`` instead of being silently removed
    or converted into a numeric zero.
    """
    from agentlab.phase6 import (
        load_phase6_campaign_from_bytes,
        validate_historical_phase6_snapshot,
    )

    observed = 0
    unknown_runs = 0
    campaigns_without_total = 0
    provider_findings: list[InventoryFinding] = []
    outputs_by_id = {item.campaign_id: item for item in campaign_outputs}
    updated_outputs: list[InventoryCampaignEntry] = []
    for campaign_entry in request.campaign_entries:
        output = outputs_by_id.get(campaign_entry.campaign_id)
        if (
            output is None
            or output.storage_state is not StorageState.PRESENT
            or output.integrity_state is not IntegrityState.VERIFIED
        ):
            campaigns_without_total += 1
            if output is not None:
                updated_outputs.append(
                    output.model_copy(
                        update={
                            "provider_total_status": ProviderTotalStatus.UNAVAILABLE,
                            "provider_call_count_observed": None,
                            "provider_call_count_unknown_runs": None,
                        }
                    )
                )
            continue
        campaign_artifact = next(
            (
                artifact
                for artifact in campaign_entry.file_artifacts
                if artifact.role == CampaignArtifactRole.CAMPAIGN.value
            ),
            None,
        )
        if campaign_artifact is None:
            campaigns_without_total += 1
            assert output is not None
            updated_outputs.append(
                output.model_copy(
                    update={
                        "provider_total_status": ProviderTotalStatus.UNAVAILABLE,
                        "provider_call_count_observed": None,
                        "provider_call_count_unknown_runs": None,
                    }
                )
            )
            continue
        content = subject_contents.get(
            ("campaign", campaign_entry.campaign_id), {}
        ).get(campaign_artifact.path)
        if content is None:
            campaigns_without_total += 1
            assert output is not None
            updated_outputs.append(
                output.model_copy(
                    update={
                        "provider_total_status": ProviderTotalStatus.UNAVAILABLE,
                        "provider_call_count_observed": None,
                        "provider_call_count_unknown_runs": None,
                    }
                )
            )
            continue
        if campaign_entry.verification_profile == "historical_verification":
            try:
                historical = validate_historical_phase6_snapshot(
                    record_bytes=_single_role_content(
                        campaign_entry,
                        subject_contents[("campaign", campaign_entry.campaign_id)],
                        CampaignArtifactRole.HISTORICAL_VERIFICATION,
                    ),
                    plan_bytes=_single_role_content(
                        campaign_entry,
                        subject_contents[("campaign", campaign_entry.campaign_id)],
                        CampaignArtifactRole.PLAN,
                    ),
                    campaign_bytes=content,
                    report_json_bytes=_single_role_content(
                        campaign_entry,
                        subject_contents[("campaign", campaign_entry.campaign_id)],
                        CampaignArtifactRole.REPORT_JSON,
                    ),
                    report_markdown_bytes=_single_role_content(
                        campaign_entry,
                        subject_contents[("campaign", campaign_entry.campaign_id)],
                        CampaignArtifactRole.REPORT_MARKDOWN,
                    ),
                )
                validation_is_valid = True
                validation_total_status = "determined"
                validation_provider_call_count = historical.provider_call_count
                validation_provider_call_count_unknown_runs = (
                    historical.provider_call_count_unknown_runs
                )
            except Exception:
                validation_is_valid = False
                validation_total_status = "unknown"
                validation_provider_call_count = None
                validation_provider_call_count_unknown_runs = None
        else:
            validation = load_phase6_campaign_from_bytes(content)
            validation_is_valid = validation.is_valid
            validation_total_status = validation.total_status
            validation_provider_call_count = validation.provider_call_count
            validation_provider_call_count_unknown_runs = (
                validation.provider_call_count_unknown_runs
            )
        if (
            not validation_is_valid
            or validation_total_status != "determined"
            or validation_provider_call_count is None
            or validation_provider_call_count_unknown_runs is None
        ):
            campaigns_without_total += 1
            assert output is not None
            updates: dict[str, object] = {
                "provider_total_status": ProviderTotalStatus.UNAVAILABLE,
                "provider_call_count_observed": None,
                "provider_call_count_unknown_runs": None,
            }
            if not validation_is_valid:
                # A Campaign accounting source which cannot satisfy its strict
                # contract invalidates the subject itself.  This must happen
                # before subject digests and retention are derived.
                updates["integrity_state"] = IntegrityState.DRIFTED
                provider_findings.append(
                    _finding(
                        FindingCode.CANONICAL_LOAD_FAILED,
                        subject_kind="campaign",
                        subject_id=campaign_entry.campaign_id,
                        path=campaign_artifact.path,
                        role=CampaignArtifactRole.CAMPAIGN.value,
                    )
                )
            updated_outputs.append(
                output.model_copy(update=updates)
            )
            continue
        assert validation_provider_call_count is not None
        assert validation_provider_call_count_unknown_runs is not None
        observed += validation_provider_call_count
        unknown_runs += validation_provider_call_count_unknown_runs
        assert output is not None
        total_status = (
            ProviderTotalStatus.OBSERVED
            if validation_provider_call_count_unknown_runs == 0
            else ProviderTotalStatus.PARTIALLY_UNKNOWN
        )
        updated_outputs.append(
            output.model_copy(
                update={
                    "provider_total_status": total_status,
                    "provider_call_count_observed": validation_provider_call_count,
                    "provider_call_count_unknown_runs": validation_provider_call_count_unknown_runs,
                }
            )
        )
    return (
        updated_outputs,
        provider_findings,
        observed,
        unknown_runs,
        campaigns_without_total,
    )


def _receipt_result(
    *,
    root: Path,
    snapshot: _InventorySnapshot,
    expectation: RetentionExpectation,
    expected_subject_digest: str | None,
) -> tuple[InventoryRetention, tuple[InventoryFinding, ...]]:
    receipt = expectation.external_copy_receipt
    assert receipt is not None
    read = _read_regular_file(
        root,
        receipt.path,
        f"retention receipt {expectation.subject_id}",
        snapshot=snapshot,
    )
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
    if expected_subject_digest is None:
        findings.append(
            _finding(
                FindingCode.CROSS_ARTIFACT_MISMATCH,
                subject_kind="retention",
                subject_id=expectation.subject_id,
                role="subject",
            )
        )
    if (
        parsed is None
        or parsed.subject_kind != expectation.subject_kind
        or parsed.subject_id != expectation.subject_id
        or expected_subject_digest is None
        or parsed.subject_digest != expected_subject_digest
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


def _resolve_exact_child(parent_path: str, relative: str, label: str) -> str:
    """Resolve a contract-relative path below one declared repository path."""
    _canonical_relative(relative, label)
    candidate = (PurePosixPath(parent_path).parent / PurePosixPath(relative)).as_posix()
    return _canonical_relative(candidate, label)


def _resolve_bundle_member(entry: ReleaseEntry, relative: str) -> str:
    if len(entry.trees) != 1:
        raise InventoryContractError("bundle member lookup requires one declared bundle_root")
    _canonical_relative(relative, "bundle member path")
    return _canonical_relative(
        (PurePosixPath(entry.trees[0].root_path) / PurePosixPath(relative)).as_posix(),
        "bundle member path",
    )


def _find_content(contents: Mapping[str, bytes], path: str) -> bytes | None:
    """Look up one already-captured exact repository-relative path only."""
    return contents.get(_canonical_relative(path, "snapshot content path"))


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
    try:
        release_metadata_content = _find_content(
            contents, _resolve_bundle_member(entry, "release-metadata.json")
        )
    except InventoryContractError:
        release_metadata_content = None
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
                try:
                    observed = _find_content(
                        contents, _resolve_bundle_member(entry, checksum.path)
                    )
                except InventoryContractError:
                    observed = None
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
    snapshot: _InventorySnapshot,
    entry: ReleaseEntry,
    contents: Mapping[str, bytes],
) -> tuple[tuple[InventoryFinding, ...], tuple[Any, ...], Any | None]:
    if entry.verification_profile != "phase6_public_suite":
        return (), (), None
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
            (),
            None,
        )
    try:
        from agentlab.phase6 import (
            ArtifactReference,
            HistoricalSuiteSource,
            PrimarySuiteSource,
            PublicSuiteManifest,
            canonical_json_bytes,
            derive_public_suite_source_provenance,
            validate_public_suite_snapshot,
        )
        from agentlab.phase6_public import render_public_suite

        manifest_content = _find_content(contents, manifest.path)
        if manifest_content is None:
            raise ValueError("Public Suite Manifest is not in the stable snapshot")
        manifest_raw = _strict_json_bytes(manifest_content, "Public Suite Manifest")
        manifest_model = PublicSuiteManifest.model_validate(manifest_raw)
        if manifest_content != canonical_json_bytes(manifest_model):
            raise ValueError("Public Suite Manifest is not canonical")
        snapshot_bytes: dict[str, bytes] = {}
        references: list[ArtifactReference] = []
        for source in manifest_model.primary_sources:
            if not isinstance(source, PrimarySuiteSource):
                raise ValueError("Manifest primary source has an invalid type")
            references.extend(
                item
                for item in (
                    source.spec,
                    source.fixture_manifest,
                    source.fixture_acceptance,
                    source.diff_policy,
                    source.plan,
                    source.campaign,
                )
                if item is not None
            )
            references.extend(source.evidence)
            references.extend(source.recordings)
        for historical_source in manifest_model.historical_sources:
            if not isinstance(historical_source, HistoricalSuiteSource):
                raise ValueError("Manifest historical source has an invalid type")
            references.extend(
                (
                    historical_source.verification_record,
                    historical_source.plan,
                    historical_source.campaign,
                    historical_source.report_json,
                    historical_source.report_markdown,
                )
            )
        for reference in references:
            resolved_path = _resolve_exact_child(
                manifest.path,
                reference.path,
                "Manifest Artifact reference path",
            )
            value = _find_content(contents, resolved_path)
            if value is None:
                raise ValueError(f"listed Public Suite input is missing: {reference.path}")
            snapshot_bytes[reference.path] = value
        validated = validate_public_suite_snapshot(manifest_model, snapshot_bytes)
        rendered = render_public_suite(validated)
        provenance = derive_public_suite_source_provenance(validated)
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
            (),
            None,
        )
    findings: list[InventoryFinding] = []
    for relative, rendered_bytes in rendered.files.items():
        observed = _find_content(contents, _resolve_bundle_member(entry, relative))
        if observed is None or observed != rendered_bytes:
            findings.append(
                _finding(
                    FindingCode.BUNDLE_RENDERER_MISMATCH,
                    subject_kind="release",
                    subject_id=entry.release_id,
                    role="bundle_root",
                    path=_resolve_bundle_member(entry, relative),
                )
            )
    return tuple(findings), provenance, validated


def _build_inventory(
    *,
    request: EvidenceInventoryRequest,
    request_sha256: str,
    repository_root: Path,
    observed_head: str,
    snapshot: _InventorySnapshot,
) -> _VerificationResult:
    findings: list[InventoryFinding] = []
    all_contents: dict[str, bytes] = {}
    subject_contents: dict[tuple[str, str], Mapping[str, bytes]] = {}
    subject_observations: dict[tuple[str, str], list[ArtifactObservation]] = {}
    validated_releases: dict[str, Any] = {}
    release_commits_by_id: dict[str, frozenset[str]] = {}
    for reference in request.source_of_truth_references:
        authority = _observe_authority(
            root=repository_root,
            snapshot=snapshot,
            reference=reference,
        )
        findings.extend(authority.findings)
        if authority.content is not None:
            all_contents[reference.path] = authority.content

    release_outputs: list[InventoryReleaseEntry] = []
    campaign_outputs: list[InventoryCampaignEntry] = []
    for release_entry in request.release_entries:
        observations, entry_findings, contents = _observe_entry(
            root=repository_root,
            snapshot=snapshot,
            subject_kind="release",
            subject_id=release_entry.release_id,
            file_artifacts=release_entry.file_artifacts,
            trees=release_entry.trees,
            verification_profile=release_entry.verification_profile,
        )
        findings.extend(entry_findings)
        findings.extend(_release_binding_findings(entry=release_entry, contents=contents))
        release_commits_by_id[release_entry.release_id] = frozenset()
        commit_state = IntegrityState.NOT_VERIFIABLE
        storage, integrity = _inventory_entry_states(observations)
        if commit_state is IntegrityState.DRIFTED:
            integrity = IntegrityState.DRIFTED
        elif commit_state is IntegrityState.NOT_VERIFIABLE and integrity is IntegrityState.VERIFIED:
            integrity = IntegrityState.NOT_VERIFIABLE
        release_outputs.append(
            InventoryReleaseEntry(
                release_id=release_entry.release_id,
                artifact_reviewed_commits=release_entry.artifact_reviewed_commits,
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
        subject_contents[("release", release_entry.release_id)] = contents
        subject_observations[("release", release_entry.release_id)] = observations

    for campaign_entry in request.campaign_entries:
        observations, entry_findings, contents = _observe_entry(
            root=repository_root,
            snapshot=snapshot,
            subject_kind="campaign",
            subject_id=campaign_entry.campaign_id,
            file_artifacts=campaign_entry.file_artifacts,
            trees=campaign_entry.trees,
            verification_profile=campaign_entry.verification_profile,
        )
        findings.extend(entry_findings)
        commits, observed_experiment_id, provenance_valid = _campaign_typed_provenance(
            campaign_entry, contents
        )
        commit_state, commit_findings = _commit_observation(
            subject_kind="campaign",
            subject_id=campaign_entry.campaign_id,
            expected_commits=frozenset({campaign_entry.artifact_reviewed_commit}),
            mode=campaign_entry.commit_verification_mode,
            observed_commits=commits,
            role="campaign",
        )
        findings.extend(commit_findings)
        if observed_experiment_id != campaign_entry.experiment_id:
            findings.append(
                _finding(
                    FindingCode.CROSS_ARTIFACT_MISMATCH,
                    subject_kind="campaign",
                    subject_id=campaign_entry.campaign_id,
                    role="experiment_id",
                )
            )
        storage, integrity = _inventory_entry_states(observations)
        if (
            campaign_entry.verification_profile == "historical_verification"
            and not provenance_valid
        ):
            integrity = IntegrityState.DRIFTED
            if not any(
                finding.code is FindingCode.CANONICAL_LOAD_FAILED
                and finding.subject_kind == "campaign"
                and finding.subject_id == campaign_entry.campaign_id
                for finding in findings
            ):
                findings.append(
                    _finding(
                        FindingCode.CANONICAL_LOAD_FAILED,
                        subject_kind="campaign",
                        subject_id=campaign_entry.campaign_id,
                        role="historical_verification",
                    )
                )
        if commit_state is IntegrityState.DRIFTED:
            integrity = IntegrityState.DRIFTED
        elif commit_state is IntegrityState.NOT_VERIFIABLE and integrity is IntegrityState.VERIFIED:
            integrity = IntegrityState.NOT_VERIFIABLE
        campaign_outputs.append(
            InventoryCampaignEntry(
                campaign_id=campaign_entry.campaign_id,
                experiment_id=campaign_entry.experiment_id,
                artifact_reviewed_commit=campaign_entry.artifact_reviewed_commit,
                commit_verification_mode=campaign_entry.commit_verification_mode,
                commit_verification=commit_state,
                classification=campaign_entry.classification,
                included_in_primary_denominator=campaign_entry.included_in_primary_denominator,
                release_id=campaign_entry.release_id,
                verification_profile=campaign_entry.verification_profile,
                storage_state=storage,
                integrity_state=integrity,
                provider_total_status=ProviderTotalStatus.UNAVAILABLE,
                artifact_observations=sorted(
                    observations, key=lambda item: (item.kind, item.path, item.role)
                ),
            )
        )
        all_contents.update(contents)
        subject_contents[("campaign", campaign_entry.campaign_id)] = contents
        subject_observations[("campaign", campaign_entry.campaign_id)] = observations

    # Public Suite validation consumes the aggregate of the same already-read
    # bytes.  It must not reopen Manifest inputs through their paths after the
    # per-entry scan.
    manifest_binding_campaign_drift: set[str] = set()
    for release_entry in request.release_entries:
        suite_findings, suite_provenance, validated = _phase6_public_suite_findings(
            root=repository_root,
            snapshot=snapshot,
            entry=release_entry,
            contents=all_contents,
        )
        findings.extend(suite_findings)
        if validated is not None:
            validated_releases[release_entry.release_id] = validated
        elif release_entry.verification_profile == "phase6_public_suite":
            manifest_binding_campaign_drift.update(
                campaign.campaign_id
                for campaign in request.campaign_entries
                if campaign.release_id == release_entry.release_id
                and campaign.classification is CampaignClassification.PRIMARY_EVALUATION
            )
        observed_commits = frozenset(
            provenance.reviewed_commit for provenance in suite_provenance
        )
        release_commits_by_id[release_entry.release_id] = observed_commits
        commit_state, commit_findings = _commit_observation(
            subject_kind="release",
            subject_id=release_entry.release_id,
            expected_commits=frozenset(release_entry.artifact_reviewed_commits),
            mode=release_entry.commit_verification_mode,
            observed_commits=observed_commits,
            role="release",
        )
        findings.extend(commit_findings)
        for index, output in enumerate(release_outputs):
            if output.release_id != release_entry.release_id:
                continue
            integrity = output.integrity_state
            if commit_state is IntegrityState.DRIFTED:
                integrity = IntegrityState.DRIFTED
            elif (
                commit_state is IntegrityState.NOT_VERIFIABLE
                and integrity is IntegrityState.VERIFIED
            ):
                integrity = IntegrityState.NOT_VERIFIABLE
            release_outputs[index] = output.model_copy(
                update={
                    "commit_verification": commit_state,
                    "integrity_state": integrity,
                }
            )
            break

    # Semantic findings are part of the entry state, not merely a global
    # appendix.  Retention and accounting consume these folded states below.
    for index, release_output in enumerate(release_outputs):
        release_findings = [
            finding
            for finding in findings
            if finding.subject_kind == "release"
            and finding.subject_id == release_output.release_id
        ]
        integrity = release_output.integrity_state
        if any(finding.code is FindingCode.ARTIFACT_MISSING for finding in release_findings):
            integrity = IntegrityState.NOT_VERIFIABLE
        elif release_findings and integrity is IntegrityState.VERIFIED:
            integrity = IntegrityState.DRIFTED
        release_outputs[index] = release_output.model_copy(update={"integrity_state": integrity})

    for index, campaign_output in enumerate(campaign_outputs):
        campaign_findings = [
            finding
            for finding in findings
            if finding.subject_kind == "campaign"
            and finding.subject_id == campaign_output.campaign_id
        ]
        integrity = campaign_output.integrity_state
        if any(finding.code is FindingCode.ARTIFACT_MISSING for finding in campaign_findings):
            integrity = IntegrityState.NOT_VERIFIABLE
        elif (
            (campaign_findings or campaign_output.campaign_id in manifest_binding_campaign_drift)
            and integrity is IntegrityState.VERIFIED
        ):
            integrity = IntegrityState.DRIFTED
        campaign_outputs[index] = campaign_output.model_copy(
            update={"integrity_state": integrity}
        )

    primary = [
        item
        for item in request.campaign_entries
        if item.classification is CampaignClassification.PRIMARY_EVALUATION
    ]
    expected_primary: set[
        tuple[str, str, str, str, frozenset[str], frozenset[tuple[str, int]]]
    ] = set()
    expected_primary_valid = True
    invalid_campaign_ids: set[str] = set(manifest_binding_campaign_drift)
    from agentlab.phase6 import derive_primary_snapshot_binding

    for release_entry in request.release_entries:
        if release_entry.classification is not ReleaseClassification.ACCEPTED_CURRENT:
            continue
        validated = validated_releases.get(release_entry.release_id)
        if validated is None:
            expected_primary_valid = False
            continue
        for source in validated.loaded.manifest.primary_sources:
            try:
                if source.campaign is None:
                    invalid_campaign_ids.update(
                        item.campaign_id
                        for item in primary
                        if item.release_id == release_entry.release_id
                    )
                    raise ValueError("Manifest primary source has no Campaign")
                binding = derive_primary_snapshot_binding(
                    source,
                    validated.loaded.bytes_by_path,
                )
                candidates = [
                    item
                    for item in primary
                    if item.release_id == release_entry.release_id
                    and any(
                        artifact.role == CampaignArtifactRole.CAMPAIGN.value
                        and artifact.path
                        == _resolve_exact_child(
                            next(
                                artifact.path
                                for artifact in release_entry.file_artifacts
                                if artifact.role == ReleaseArtifactRole.SUITE_MANIFEST.value
                            ),
                            source.campaign.path,
                            "Manifest Campaign path",
                        )
                        for artifact in item.file_artifacts
                    )
                ]
                if len(candidates) != 1:
                    invalid_campaign_ids.update(item.campaign_id for item in candidates)
                    raise ValueError("Manifest primary Campaign set differs from Request")
                candidate = candidates[0]
                if candidate.experiment_id != binding.experiment_id:
                    invalid_campaign_ids.add(candidate.campaign_id)
                    raise ValueError("Manifest Experiment ID differs from Request")
                expected_primary.add(
                    (
                        candidate.campaign_id,
                        candidate.experiment_id,
                        release_entry.release_id,
                        binding.language.value,
                        binding.planned_run_ids,
                        binding.complete_pairs,
                    )
                )
            except Exception:
                expected_primary_valid = False

    request_primary: set[
        tuple[str, str, str, str, frozenset[str], frozenset[tuple[str, int]]]
    ] = set()
    request_primary_valid = True
    for item in primary:
        try:
            campaign_artifact = next(
                artifact
                for artifact in item.file_artifacts
                if artifact.role == CampaignArtifactRole.CAMPAIGN.value
            )
            release_validated = validated_releases.get(item.release_id or "")
            if release_validated is None:
                raise ValueError("primary Campaign is not bound to a validated Manifest")
            source = next(
                source
                for source in release_validated.loaded.manifest.primary_sources
                if source.campaign is not None
                and _resolve_exact_child(
                    next(
                        artifact.path
                        for release_entry in request.release_entries
                        if release_entry.release_id == item.release_id
                        for artifact in release_entry.file_artifacts
                        if artifact.role == ReleaseArtifactRole.SUITE_MANIFEST.value
                    ),
                    source.campaign.path,
                    "Manifest Campaign path",
                )
                == campaign_artifact.path
            )
            binding = derive_primary_snapshot_binding(
                source,
                release_validated.loaded.bytes_by_path,
            )
            if item.experiment_id is None:
                raise ValueError("primary Campaign Experiment ID is unavailable")
            request_primary.add(
                (
                    item.campaign_id,
                    item.experiment_id,
                    item.release_id or "",
                    binding.language.value,
                    binding.planned_run_ids,
                    binding.complete_pairs,
                )
            )
        except Exception:
            invalid_campaign_ids.add(item.campaign_id)
            request_primary_valid = False
    expected_campaign_ids = {item[0] for item in expected_primary}
    invalid_campaign_ids.update(
        item.campaign_id for item in primary if item.campaign_id not in expected_campaign_ids
    )
    if (
        not expected_primary_valid
        or not request_primary_valid
        or request_primary != expected_primary
    ):
        findings.append(
            _finding(
                FindingCode.DENOMINATOR_MISMATCH,
                subject_kind="request",
                subject_id=request.inventory_id,
            )
        )
    for campaign_id in sorted(invalid_campaign_ids):
        findings.append(
            _finding(
                FindingCode.CROSS_ARTIFACT_MISMATCH,
                subject_kind="campaign",
                subject_id=campaign_id,
                role="manifest_binding",
            )
    )
    for index, campaign_output in enumerate(campaign_outputs):
        if campaign_output.campaign_id in invalid_campaign_ids:
            integrity = campaign_output.integrity_state
            if integrity is IntegrityState.VERIFIED:
                integrity = IntegrityState.DRIFTED
            campaign_outputs[index] = campaign_output.model_copy(
                update={"integrity_state": integrity}
            )
    (
        campaign_outputs,
        provider_findings,
        observed_calls,
        unknown_runs,
        campaigns_without_total,
    ) = _provider_counts(
        request=request,
        campaign_outputs=campaign_outputs,
        subject_contents=subject_contents,
    )
    findings.extend(provider_findings)

    # Provider strict-load failures are folded before either subject digests or
    # retention.  Receipt binding must never use a digest for an invalid
    # Campaign, and local-only retention must reflect that invalidity.
    entry_states = {
        ("release", item.release_id): (item.storage_state, item.integrity_state)
        for item in release_outputs
    }
    entry_states.update(
        {
            ("campaign", item.campaign_id): (item.storage_state, item.integrity_state)
            for item in campaign_outputs
        }
    )
    subject_digests: dict[tuple[str, str], str] = {}
    for release_entry in request.release_entries:
        subject_key = ("release", release_entry.release_id)
        if entry_states[subject_key][1] is IntegrityState.VERIFIED:
            digest = compute_subject_digest(
                subject_kind="release",
                subject_id=release_entry.release_id,
                experiment_id=None,
                reviewed_commits=release_entry.artifact_reviewed_commits,
                file_artifacts=release_entry.file_artifacts,
                trees=release_entry.trees,
                observations=subject_observations[subject_key],
            )
            if digest is not None:
                subject_digests[subject_key] = digest
    for campaign_entry in request.campaign_entries:
        subject_key = ("campaign", campaign_entry.campaign_id)
        if entry_states[subject_key][1] is IntegrityState.VERIFIED:
            digest = compute_subject_digest(
                subject_kind="campaign",
                subject_id=campaign_entry.campaign_id,
                experiment_id=campaign_entry.experiment_id,
                reviewed_commits=[campaign_entry.artifact_reviewed_commit],
                file_artifacts=campaign_entry.file_artifacts,
                trees=campaign_entry.trees,
                observations=subject_observations[subject_key],
            )
            if digest is not None:
                subject_digests[subject_key] = digest
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
    entry_states = {
        ("release", item.release_id): (item.storage_state, item.integrity_state)
        for item in release_outputs
    }
    entry_states.update(
        {
            ("campaign", item.campaign_id): (item.storage_state, item.integrity_state)
            for item in campaign_outputs
        }
    )
    for expectation in request.retention_expectations:
        if expectation.expected_retention_state is RetentionState.LOCAL_ONLY:
            storage, integrity = entry_states[(expectation.subject_kind, expectation.subject_id)]
            if storage is StorageState.PRESENT and integrity is IntegrityState.VERIFIED:
                retention_outputs.append(
                    InventoryRetention(
                        subject_kind=expectation.subject_kind,
                        subject_id=expectation.subject_id,
                        retention_state=RetentionState.LOCAL_ONLY,
                        verification_basis=RetentionVerificationBasis.LOCAL_ARTIFACT_ONLY,
                        remote_liveness=RemoteLiveness.NOT_CHECKED,
                    )
                )
            else:
                retention_outputs.append(
                    InventoryRetention(
                        subject_kind=expectation.subject_kind,
                        subject_id=expectation.subject_id,
                        retention_state=RetentionState.UNKNOWN,
                        verification_basis=RetentionVerificationBasis.NOT_AVAILABLE,
                        remote_liveness=RemoteLiveness.NOT_CHECKED,
                    )
                )
                findings.append(
                    _finding(
                        FindingCode.CROSS_ARTIFACT_MISMATCH,
                        subject_kind="retention",
                        subject_id=expectation.subject_id,
                        role="local_artifact",
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
                snapshot=snapshot,
                expectation=expectation,
                expected_subject_digest=subject_digests.get(
                    (expectation.subject_kind, expectation.subject_id)
                ),
            )
            retention_outputs.append(result)
            findings.extend(receipt_findings)

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
        execution_head_attestation_sha256=_execution_head_attestation_sha256(observed_head),
        source_of_truth_references=request.source_of_truth_references,
        releases=sorted(release_outputs, key=lambda item: item.release_id),
        campaigns=sorted(campaign_outputs, key=lambda item: item.campaign_id),
        retention=sorted(retention_outputs, key=lambda item: (item.subject_kind, item.subject_id)),
        findings=findings,
        summary=InventorySummary(
            release_count=len(release_outputs),
            campaign_count=len(campaign_outputs),
            primary_campaign_count=sum(
                item.included_in_primary_denominator for item in campaign_outputs
            ),
            classification_counts=classification_counts,
            storage_state_counts=storage_counts,
            integrity_state_counts=integrity_counts,
            provider_accounting_scope="declared_campaign_entries",
            provider_call_count_observed=observed_calls,
            provider_call_count_unknown_runs=unknown_runs,
            campaigns_without_total=campaigns_without_total,
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
        "- execution_head_attestation_sha256: "
        f"'{inventory.execution_head_attestation_sha256}'",
        "- authoritative: false",
        f"- scope: '{inventory.scope.value}'",
        f"- verification_status: '{inventory.verification_status.value}'",
        "",
        "## Summary",
        "",
        f"- releases: {inventory.summary.release_count}",
        f"- campaigns: {inventory.summary.campaign_count}",
        f"- primary campaigns: {inventory.summary.primary_campaign_count}",
        f"- provider accounting scope: {inventory.summary.provider_accounting_scope}",
        f"- provider call count observed: {inventory.summary.provider_call_count_observed}",
        "- provider call count unknown runs: "
        f"{inventory.summary.provider_call_count_unknown_runs}",
        f"- campaigns without total: {inventory.summary.campaigns_without_total}",
        "",
        "## Entries",
        "",
        "| Kind | Authority ID | Experiment ID | Classification | Storage | Integrity | "
        "Commit verification | Reviewed commits |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| release | '{entry.release_id}' | '' | '{entry.classification.value}' | "
        f"'{entry.storage_state.value}' | '{entry.integrity_state.value}' | "
        f"'{entry.commit_verification.value}' | '{','.join(entry.artifact_reviewed_commits)}' |"
        for entry in inventory.releases
    )
    lines.extend(
        f"| campaign | '{entry.campaign_id}' | '{entry.experiment_id or ''}' | "
        f"'{entry.classification.value}' | '{entry.storage_state.value}' | "
        f"'{entry.integrity_state.value}' | '{entry.commit_verification.value}' | "
        f"'{entry.artifact_reviewed_commit}' |"
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
                "Provider accounting is scoped to Campaign entries declared by this Request "
                "and does not represent total project-wide Provider consumption."
            ),
            "",
            (
                "Provider, Prompt, Gate, Campaign execution, Report regeneration, "
                "Public Suite regeneration, and network access: 0."
            ),
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


def _repository_relative_path(root: Path, path: Path, label: str) -> tuple[Path, str]:
    raw = os.fspath(path)
    if "\x00" in raw or "\\" in raw or ".." in PurePosixPath(raw).parts:
        raise InventorySafetyError(f"{label} must use a canonical repository path")
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise InventorySafetyError(f"{label} must remain below repository root") from error
    _canonical_relative(relative, label)
    return absolute, relative


def _validate_output_paths(
    *,
    repository_root: Path,
    snapshot: _InventorySnapshot,
    request_path: Path,
    output_path: Path,
    markdown_path: Path,
    metadata_path: Path,
) -> tuple[tuple[Path, str], tuple[Path, str], tuple[Path, str]]:
    values = []
    for path, label in zip(
        (output_path, markdown_path, metadata_path),
        ("Inventory output", "Markdown output", "metadata output"),
        strict=True,
    ):
        absolute, relative = _repository_relative_path(repository_root, path, label)
        parent_relative, filename = _relative_parent(relative)
        if not filename:
            raise InventorySafetyError(f"{label} must name a file")
        parent_fd, parent_state = _open_snapshot_directory(
            snapshot,
            parent_relative,
            f"{label} parent",
            final_kind="parent",
        )
        if parent_state is not None or parent_fd is None:
            raise InventorySafetyError(f"{label} parent must be an existing real directory")
        values.append((absolute, relative))
    paths = [absolute for absolute, _ in values]
    relatives = [relative for _, relative in values]
    if len(set(paths)) != 3 or len(set(relatives)) != 3:
        raise InventorySafetyError("output, Markdown, and metadata paths must be distinct")
    _, request_relative = _repository_relative_path(repository_root, request_path, "Request")
    if request_relative in relatives:
        raise InventorySafetyError("output must not alias Request")
    return values[0], values[1], values[2]


def _lstat_at(
    snapshot: _InventorySnapshot,
    relative: str,
    label: str,
) -> os.stat_result | None:
    parent_relative, filename = _relative_parent(relative)
    parent_fd, state = _open_snapshot_directory(
        snapshot,
        parent_relative,
        f"{label} parent",
        final_kind="parent",
    )
    if state == "missing" or parent_fd is None:
        return None
    try:
        return os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InventoryPublicationError(f"could not inspect {label}") from error


def _preflight_outputs(
    *,
    snapshot: _InventorySnapshot,
    output_relatives: tuple[str, str, str],
) -> None:
    entries = [_lstat_at(snapshot, relative, "existing output") for relative in output_relatives]
    existing = [entry is not None for entry in entries]
    if not any(existing):
        return
    if not all(existing):
        raise InventoryPublicationError(
            "incomplete publication: one or two output paths already exist"
        )
    assert all(entry is not None for entry in entries)
    identities = [
        (entry.st_dev, entry.st_ino)
        for entry in entries
        if entry is not None
    ]
    if len(identities) != len(set(identities)):
        raise InventoryPublicationError("output paths alias an existing publication")
    for entry in entries:
        assert entry is not None
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise InventoryPublicationError("existing publication path is unsafe")
        if entry.st_size > MAX_PUBLICATION_FILE_BYTES:
            raise InventoryPublicationError("existing publication is too large to inspect safely")
    reads = [
        _read_regular_file(
            snapshot.root,
            relative,
            "existing output",
            max_bytes=MAX_PUBLICATION_FILE_BYTES,
            snapshot=snapshot,
            track=False,
        )
        for relative in output_relatives
    ]
    if any(read.content is None or read.reason is not None for read in reads):
        raise InventoryPublicationError("existing publication path is unsafe")
    inventory_bytes = reads[0].content
    markdown_bytes = reads[1].content
    metadata_bytes = reads[2].content
    assert inventory_bytes is not None
    assert markdown_bytes is not None
    assert metadata_bytes is not None
    try:
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
    except InventoryContractError as error:
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


def _publication_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size


def _publish_file_no_replace(
    snapshot: _InventorySnapshot,
    relative: str,
    content: bytes,
    label: str,
) -> tuple[int, int, int, int]:
    parent_relative, filename = _relative_parent(relative)
    parent_fd, state = _open_snapshot_directory(
        snapshot,
        parent_relative,
        f"{label} parent",
        final_kind="parent",
    )
    if state is not None or parent_fd is None:
        raise InventoryPublicationError(f"{label} parent is unavailable")
    descriptor: int | None = None
    staging_name: str | None = None
    linked_identity: tuple[int, int, int, int] | None = None
    published_successfully = False
    try:
        for _ in range(32):
            candidate = f".{filename}.phase7-{secrets.token_hex(10)}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if descriptor is None or staging_name is None:
            raise InventoryPublicationError(f"could not create {label} staging file")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        linked_identity = _publication_identity(staged)
        os.close(descriptor)
        descriptor = None
        os.link(
            staging_name,
            filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(staging_name, dir_fd=parent_fd)
        staging_name = None
        published = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _publication_identity(published) != linked_identity
            or not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
        ):
            raise InventoryPublicationError(f"published {label} identity is unsafe")
        os.fsync(parent_fd)
        published_successfully = True
        return linked_identity
    except FileExistsError as error:
        raise InventoryPublicationError(f"{label} already exists") from error
    except InventoryPublicationError:
        raise
    except OSError as error:
        raise InventoryPublicationError(f"could not publish {label}") from error
    finally:
        if not published_successfully and linked_identity is not None:
            try:
                current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    _publication_identity(current) == linked_identity
                    and stat.S_ISREG(current.st_mode)
                    and current.st_nlink == 1
                ):
                    os.unlink(filename, dir_fd=parent_fd)
            except OSError:
                pass
        if descriptor is not None:
            os.close(descriptor)
        if staging_name is not None:
            with suppress(OSError):
                os.unlink(staging_name, dir_fd=parent_fd)


def _rollback_file(
    snapshot: _InventorySnapshot,
    relative: str,
    identity: tuple[int, int, int, int],
) -> None:
    parent_relative, filename = _relative_parent(relative)
    parent_fd, state = _open_snapshot_directory(
        snapshot,
        parent_relative,
        "owned-output rollback parent",
        final_kind="parent",
    )
    if state == "missing" or parent_fd is None:
        return
    try:
        metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InventoryPublicationError("owned-output rollback could not inspect path") from error
    if (
        _publication_identity(metadata) != identity
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise InventoryPublicationError("owned-output rollback refused a changed path")
    try:
        os.unlink(filename, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise InventoryPublicationError("owned-output rollback failed") from error


def _load_published_outputs(
    snapshot: _InventorySnapshot,
    output_relatives: tuple[str, str, str],
    request_bytes: bytes,
) -> None:
    reads = [
        _read_regular_file(
            snapshot.root,
            relative,
            "published output",
            max_bytes=MAX_PUBLICATION_FILE_BYTES,
            snapshot=snapshot,
            track=False,
        )
        for relative in output_relatives
    ]
    if any(read.content is None or read.reason is not None for read in reads):
        raise InventoryPublicationError("published output is unavailable")
    inventory_bytes = reads[0].content
    markdown_bytes = reads[1].content
    metadata_bytes = reads[2].content
    assert inventory_bytes is not None
    assert markdown_bytes is not None
    assert metadata_bytes is not None
    try:
        verify_evidence_inventory_publication_bytes(
            request_bytes,
            inventory_bytes,
            markdown_bytes,
            metadata_bytes,
        )
    except InventoryContractError as error:
        raise InventoryPublicationError("published outputs do not bind to the Request") from error


@dataclass(frozen=True)
class DeclaredInventoryInputVerification:
    """One read-only verification of Request-declared bytes at call time.

    The result proves only that the observed inputs matched the Request during
    this descriptor-backed call.  It intentionally makes no cross-call inode
    immutability claim.
    """

    request_sha256: str
    observed_execution_repository_head: str
    inventory: EvidenceInventory


@dataclass(frozen=True)
class InventoryRequestPublication:
    """A canonical create-only Request written below the fixed Phase 7 root."""

    request: EvidenceInventoryRequest
    request_sha256: str
    request_path: Path


def _verify_inventory_snapshot_bytes(
    *,
    request_bytes: bytes,
    repository_root: Path,
    snapshot: _InventorySnapshot,
) -> tuple[EvidenceInventoryRequest, _VerificationResult]:
    """Shared stable-snapshot verifier for all Request consumers."""
    request = load_inventory_request_bytes(request_bytes)
    observed_head = _observe_execution_repository_head(repository_root)
    result = _build_inventory(
        request=request,
        request_sha256=_sha256(request_bytes),
        repository_root=repository_root,
        observed_head=observed_head,
        snapshot=snapshot,
    )
    _snapshot_revalidate(snapshot)
    return request, result


def verify_declared_inventory_inputs(
    request_bytes: bytes,
    repository_root: Path,
    *,
    confirm_local_execution: bool,
) -> DeclaredInventoryInputVerification:
    """Read-only verify the exact inputs declared by canonical Request bytes."""
    if not confirm_local_execution:
        raise InventorySafetyError("inventory requires --confirm-local-execution")
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise InventoryContractError("Evidence Inventory Request exceeds the bounded size")
    root = _real_directory(repository_root, "repository root")
    snapshot = _snapshot_root(root)
    try:
        _, result = _verify_inventory_snapshot_bytes(
            request_bytes=request_bytes,
            repository_root=root,
            snapshot=snapshot,
        )
        return DeclaredInventoryInputVerification(
            request_sha256=_sha256(request_bytes),
            observed_execution_repository_head=result.observed_execution_repository_head,
            inventory=result.inventory,
        )
    finally:
        snapshot.close()


def _open_directory_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    create: bool,
) -> tuple[int, tuple[int, int, int, int, int, int, int], bool]:
    """Open one real directory component by descriptor, optionally create-once."""
    created = False
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise InventoryPublicationError(
                f"{label} must be an existing real directory"
            ) from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError as error:
            raise InventoryPublicationError(f"{label} changed while being created") from error
        created = True
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise InventoryPublicationError(
                f"{label} could not be inspected after creation"
            ) from error
    except OSError as error:
        raise InventoryPublicationError(f"{label} could not be inspected safely") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise InventoryPublicationError(f"{label} must be a real directory")
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(opened) or _identity(after) != _identity(opened):
            raise InventoryPublicationError(f"{label} changed while opening")
        return descriptor, _identity(opened), created
    except InventoryPublicationError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise InventoryPublicationError(f"{label} could not be opened safely") from error


def _directory_is_empty(fd: int) -> bool:
    try:
        with os.scandir(fd) as entries:
            return next(entries, None) is None
    except OSError as error:
        raise InventoryPublicationError("owned Request directory could not be scanned") from error


def _require_directory_entries_stable(
    entries: Sequence[tuple[int, str, tuple[int, int, int, int, int, int, int]]],
) -> None:
    """Ensure descriptor-fixed publisher path components still name the same objects."""
    for parent_fd, name, identity in entries:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise InventoryPublicationError(
                "Request publication parent changed before completion"
            ) from error
        if not _same_object_identity(current, identity) or not stat.S_ISDIR(current.st_mode):
            raise InventoryPublicationError("Request publication parent identity changed")


def _require_publisher_root_stable(snapshot: _InventorySnapshot) -> None:
    try:
        current_path = snapshot.root.lstat()
        current_fd = os.fstat(snapshot.root_fd)
    except OSError as error:
        raise InventoryPublicationError(
            "repository root changed during Request publication"
        ) from error
    if (
        _identity(current_path) != snapshot.root_identity
        or _identity(current_fd) != snapshot.root_identity
    ):
        raise InventoryPublicationError(
            "repository root identity changed during Request publication"
        )


def _rollback_owned_request_publication(
    *,
    request_parent_fd: int | None,
    request_identity: tuple[int, int, int, int, int, int, int] | None,
    created_directories: Sequence[
        tuple[int, str, tuple[int, int, int, int, int, int, int]]
    ],
) -> None:
    """Remove only this process's owned Request file and empty directories."""
    if request_parent_fd is not None and request_identity is not None:
        try:
            current = os.stat("request.json", dir_fd=request_parent_fd, follow_symlinks=False)
            if (
                not _same_object_identity(current, request_identity)
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
            ):
                raise InventoryPublicationError("owned Request rollback refused a changed file")
            os.unlink("request.json", dir_fd=request_parent_fd)
            os.fsync(request_parent_fd)
        except FileNotFoundError:
            pass
        except InventoryPublicationError:
            raise
        except OSError as error:
            raise InventoryPublicationError("owned Request rollback failed") from error
    for parent_fd, name, identity in reversed(created_directories):
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_object_identity(current, identity) or not stat.S_ISDIR(current.st_mode):
                raise InventoryPublicationError(
                    "owned Request directory rollback refused a changed path"
                )
            directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            try:
                if not _directory_is_empty(directory_fd):
                    raise InventoryPublicationError("owned Request directory is not empty")
            finally:
                os.close(directory_fd)
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except InventoryPublicationError:
            raise
        except OSError as error:
            raise InventoryPublicationError("owned Request directory rollback failed") from error


def publish_inventory_request_bytes(
    request_bytes: bytes,
    repository_root: Path,
    *,
    expected_request_sha256: str,
    confirm_local_write: bool,
) -> InventoryRequestPublication:
    """Create one reviewed Request below the fixed Phase 7 evidence root.

    The bytes are fully bounded, strictly loaded, and SHA-bound before any
    filesystem mutation.  Only the inventory-id leaf is create-only; existing
    real ``.artifacts/phase7/evidence-inventory`` parents may be safely reused.
    """
    if not confirm_local_write:
        raise InventorySafetyError("Request publication requires --confirm-local-write")
    if len(request_bytes) == 0 or len(request_bytes) > MAX_REQUEST_BYTES:
        raise InventoryContractError("Evidence Inventory Request has an invalid bounded size")
    request = load_inventory_request_bytes(request_bytes)
    actual_sha256 = _sha256(request_bytes)
    if not re.fullmatch(SHA256_PATTERN, expected_request_sha256) or not hmac.compare_digest(
        actual_sha256, expected_request_sha256
    ):
        raise InventoryContractError("Request SHA-256 does not match human-approved value")

    root = _real_directory(repository_root, "repository root")
    snapshot = _snapshot_root(root)
    descriptors: list[int] = []
    created_directories: list[tuple[int, str, tuple[int, int, int, int, int, int, int]]] = []
    directory_entries: list[tuple[int, str, tuple[int, int, int, int, int, int, int]]] = []
    request_parent_fd: int | None = None
    owned_request_identity: tuple[int, int, int, int, int, int, int] | None = None
    committed_request_identity: tuple[int, int, int, int, int, int, int] | None = None
    request_relative = (
        PurePosixPath(".artifacts")
        / "phase7"
        / "evidence-inventory"
        / request.inventory_id
        / "request.json"
    ).as_posix()
    try:
        artifacts_fd, _, _ = _open_directory_at(
            snapshot.root_fd, ".artifacts", ".artifacts", create=False
        )
        descriptors.append(artifacts_fd)
        artifacts_identity = _identity(os.fstat(artifacts_fd))
        directory_entries.append((snapshot.root_fd, ".artifacts", artifacts_identity))
        phase7_fd, phase7_identity, phase7_created = _open_directory_at(
            artifacts_fd, "phase7", ".artifacts/phase7", create=True
        )
        descriptors.append(phase7_fd)
        directory_entries.append((artifacts_fd, "phase7", phase7_identity))
        if phase7_created:
            created_directories.append((artifacts_fd, "phase7", phase7_identity))
        inventory_fd, inventory_identity, inventory_created = _open_directory_at(
            phase7_fd,
            "evidence-inventory",
            ".artifacts/phase7/evidence-inventory",
            create=True,
        )
        descriptors.append(inventory_fd)
        directory_entries.append(
            (phase7_fd, "evidence-inventory", inventory_identity)
        )
        if inventory_created:
            created_directories.append(
                (phase7_fd, "evidence-inventory", inventory_identity)
            )
        try:
            leaf_fd, leaf_identity, _ = _open_directory_at(
                inventory_fd,
                request.inventory_id,
                f"Inventory leaf {request.inventory_id}",
                create=False,
            )
        except InventoryPublicationError:
            # A missing leaf is the only normal creation case.  Any existing
            # leaf (including an unsafe one) is a permanent collision.
            try:
                os.stat(request.inventory_id, dir_fd=inventory_fd, follow_symlinks=False)
            except FileNotFoundError:
                leaf_fd, leaf_identity, leaf_created = _open_directory_at(
                    inventory_fd,
                    request.inventory_id,
                    f"Inventory leaf {request.inventory_id}",
                    create=True,
                )
                assert leaf_created
            else:
                raise InventoryPublicationError("Inventory Request leaf already exists")
        else:
            os.close(leaf_fd)
            raise InventoryPublicationError("Inventory Request leaf already exists")
        descriptors.append(leaf_fd)
        directory_entries.append((inventory_fd, request.inventory_id, leaf_identity))
        created_directories.append((inventory_fd, request.inventory_id, leaf_identity))
        request_parent_fd = leaf_fd
        for output_name in (
            "evidence-inventory.json",
            "evidence-inventory.md",
            "evidence-inventory.metadata.json",
        ):
            try:
                os.stat(output_name, dir_fd=leaf_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InventoryPublicationError(
                    "future output path could not be inspected"
                ) from error
            raise InventoryPublicationError("future output path already exists")
        descriptor = os.open(
            "request.json",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=leaf_fd,
        )
        try:
            # Ownership begins at O_EXCL open, rather than only after a
            # successful write.  This permits a safe rollback if write,
            # fsync, or descriptor reload fails midway through publication.
            owned_request_identity = _identity(os.fstat(descriptor))
            view = memoryview(request_bytes)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise InventoryPublicationError("published Request write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            written_stat = os.fstat(descriptor)
            if (
                not _same_object_identity(written_stat, owned_request_identity)
                or not stat.S_ISREG(written_stat.st_mode)
                or written_stat.st_nlink != 1
            ):
                raise InventoryPublicationError("published Request file is unsafe")
            committed_request_identity = _identity(written_stat)
        finally:
            os.close(descriptor)
        reread = _read_regular_file(
            root,
            request_relative,
            "published Request",
            max_bytes=MAX_REQUEST_BYTES,
            snapshot=snapshot,
            track=False,
        )
        if (
            reread.content != request_bytes
            or reread.sha256 is None
            or not hmac.compare_digest(reread.sha256, actual_sha256)
        ):
            raise InventoryPublicationError("published Request did not survive descriptor reload")
        assert owned_request_identity is not None
        assert committed_request_identity is not None
        try:
            reread_stat = os.stat("request.json", dir_fd=leaf_fd, follow_symlinks=False)
        except OSError as error:
            raise InventoryPublicationError(
                "published Request identity could not be rechecked"
            ) from error
        if (
            not _same_object_identity(reread_stat, owned_request_identity)
            or not stat.S_ISREG(reread_stat.st_mode)
            or reread_stat.st_nlink != 1
            or _identity(reread_stat) != committed_request_identity
        ):
            raise InventoryPublicationError("published Request changed before reload completed")
        _require_directory_entries_stable(directory_entries)
        _require_publisher_root_stable(snapshot)
        os.fsync(leaf_fd)
        return InventoryRequestPublication(
            request=request,
            request_sha256=actual_sha256,
            request_path=root / request_relative,
        )
    except Exception as original_error:
        rollback_error: Exception | None = None
        try:
            _rollback_owned_request_publication(
                request_parent_fd=request_parent_fd,
                request_identity=owned_request_identity,
                created_directories=created_directories,
            )
        except Exception as error:
            rollback_error = error
        if rollback_error is not None:
            raise InventoryPublicationError(
                "Request publication failed; owned rollback could not be verified "
                "and paths were preserved"
            ) from rollback_error
        if isinstance(original_error, InventoryError):
            raise
        raise InventoryPublicationError("Request publication failed safely") from original_error
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
        snapshot.close()


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
    snapshot = _snapshot_root(root)
    try:
        output_values = _validate_output_paths(
            repository_root=root,
            snapshot=snapshot,
            request_path=request_path,
            output_path=output_path,
            markdown_path=markdown_path,
            metadata_path=metadata_path,
        )
        output_paths: tuple[Path, Path, Path] = (
            output_values[0][0],
            output_values[1][0],
            output_values[2][0],
        )
        output_relatives: tuple[str, str, str] = (
            output_values[0][1],
            output_values[1][1],
            output_values[2][1],
        )
        _preflight_outputs(snapshot=snapshot, output_relatives=output_relatives)
        request_bytes = _read_request_file(request_path, root, snapshot=snapshot)
        request = load_inventory_request_bytes(request_bytes)
        request_sha256 = _sha256(request_bytes)
        expected_paths: set[str] = set()
        input_tree_roots: set[str] = set()
        for release_entry in request.release_entries:
            expected_paths.update(item.path for item in release_entry.file_artifacts)
            expected_paths.update(tree.root_path for tree in release_entry.trees)
            input_tree_roots.update(tree.root_path for tree in release_entry.trees)
            expected_paths.update(
                f"{tree.root_path}/{item.path}"
                for tree in release_entry.trees
                for item in tree.file_artifacts
            )
        for campaign_entry in request.campaign_entries:
            expected_paths.update(item.path for item in campaign_entry.file_artifacts)
            expected_paths.update(tree.root_path for tree in campaign_entry.trees)
            input_tree_roots.update(tree.root_path for tree in campaign_entry.trees)
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
        if set(output_relatives) & expected_paths:
            raise InventorySafetyError("outputs must not alias declared input Artifacts")
        if any(
            output == tree_root or output.startswith(f"{tree_root}/")
            for output in output_relatives
            for tree_root in input_tree_roots
        ):
            raise InventorySafetyError("outputs must not be inside an input tree")
        request, result = _verify_inventory_snapshot_bytes(
            request_bytes=request_bytes,
            repository_root=root,
            snapshot=snapshot,
        )
        observed_head = result.observed_execution_repository_head
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
            raise InventoryPublicationError(
                "generated publication exceeds the bounded output limit"
            )
        if _read_request_file(request_path, root, snapshot=snapshot) != request_bytes:
            raise InventorySafetyError("Request changed during verification")
        _snapshot_revalidate(snapshot)
        published: list[tuple[str, tuple[int, int, int, int]]] = []
        try:
            for relative, content, label in (
                (output_relatives[0], inventory_bytes, "Inventory"),
                (output_relatives[1], markdown_bytes, "Markdown"),
                (output_relatives[2], metadata_bytes, "metadata"),
            ):
                published.append(
                    (relative, _publish_file_no_replace(snapshot, relative, content, label))
                )
            _load_published_outputs(snapshot, output_relatives, request_bytes)
        except Exception as original_error:
            rollback_error: Exception | None = None
            try:
                for relative, identity in reversed(published):
                    _rollback_file(snapshot, relative, identity)
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
            output_path=output_paths[0],
            markdown_path=output_paths[1],
            metadata_path=output_paths[2],
            exit_code=2 if result.inventory.verification_status is VerificationStatus.FAILED else 0,
        )
    finally:
        snapshot.close()


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
    snapshot = _snapshot_root(root)
    try:
        request_bytes = _read_request_file(request_path, root, snapshot=snapshot)
        _, result = _verify_inventory_snapshot_bytes(
            request_bytes=request_bytes,
            repository_root=root,
            snapshot=snapshot,
        )
        return result.inventory
    finally:
        snapshot.close()


# Compatibility aliases for callers using the command-oriented names.
run_inventory_phase6_evidence = create_inventory_publication
load_evidence_inventory_request = load_inventory_request_bytes
