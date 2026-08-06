from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from typer.testing import CliRunner

from agentlab.cli import app
from agentlab.phase7_inventory import (
    AuthorityReference,
    CampaignClassification,
    CampaignEntry,
    CommitVerificationMode,
    EvidenceInventoryRequest,
    ExpectedFileArtifact,
    ExpectedTree,
    ExternalCopyReceipt,
    FindingCode,
    InventoryContractError,
    InventoryPublicationError,
    InventorySafetyError,
    InventoryScope,
    ReleaseClassification,
    ReleaseEntry,
    RetentionExpectation,
    RetentionState,
    _finding,
    _ObservationResult,
    _observe_tree,
    _publish_file_no_replace,
    _read_regular_file,
    _snapshot_revalidate,
    _snapshot_root,
    canonical_inventory_json_bytes,
    compute_tree_sha256,
    create_inventory_publication,
    load_inventory_request_bytes,
    verify_inventory_request,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _request(
    root: Path,
    *,
    artifact_path: str,
    artifact_bytes: bytes,
    commit_mode: CommitVerificationMode = CommitVerificationMode.INTERNAL_IF_PRESENT,
    commit: str = HEAD,
) -> Path:
    authority = b"synthetic authority\n"
    (root / "authority.txt").write_bytes(authority)
    artifact = root / artifact_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(artifact_bytes)
    files = [
        ExpectedFileArtifact(
            role="report_json",
            path=artifact_path,
            byte_count=len(artifact_bytes),
            sha256=_sha256(artifact_bytes),
        )
    ]
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="synthetic-inventory",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="authority",
                kind="tracked_closeout",
                path="authority.txt",
                byte_count=len(authority),
                sha256=_sha256(authority),
                description="synthetic authority",
            )
        ],
        campaign_entries=[
            CampaignEntry(
                campaign_id="synthetic-campaign",
                artifact_reviewed_commit=commit,
                commit_verification_mode=commit_mode,
                classification=CampaignClassification.AUDIT_ONLY_FAILURE,
                included_in_primary_denominator=False,
                verification_profile="declared_artifact_set",
                declaration_basis="synthetic declaration",
                file_artifacts=files,
            )
        ],
    )
    request_path = root / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    return request_path


def _observe_test_tree(root: Path, tree: ExpectedTree) -> _ObservationResult:
    snapshot = _snapshot_root(root)
    try:
        return _observe_tree(
            root=root,
            snapshot=snapshot,
            subject_kind="release",
            subject_id="synthetic-release",
            tree=tree,
        )
    finally:
        snapshot.close()


def test_request_loader_rejects_duplicate_keys() -> None:
    content = (
        b'{"authoritative":false,"authoritative":false,"campaign_entries":[],'
        b'"inventory_id":"synthetic-inventory","release_entries":[],'
        b'"retention_expectations":[],"schema_version":"1.0","scope":"phase6",'
        b'"source_of_truth_references":[]}'
    )
    with pytest.raises(InventoryContractError):
        load_inventory_request_bytes(content)


def test_synthetic_valid_file_inventory_and_commit_separation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = (
        json.dumps(
            {"provider_call_count": 0, "reviewed_commit": HEAD},
            sort_keys=True,
            indent=2,
        ).encode()
        + b"\n"
    )
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=artifact)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: OTHER_HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.verification_status.value == "verified"
    assert inventory.campaigns[0].commit_verification.value == "verified"
    assert inventory.findings == []


def test_abandoned_missing_internal_commit_is_not_verifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_bytes = b'{"provider_call_count":0}\n'
    request_path = _request(
        tmp_path,
        artifact_path="missing.json",
        artifact_bytes=missing_bytes,
        commit_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
    )
    (tmp_path / "missing.json").unlink()
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    codes = {finding.code.value for finding in inventory.findings}
    assert "artifact_missing" in codes
    assert "artifact_reviewed_commit_not_verifiable" in codes
    assert "artifact_reviewed_commit_mismatch" not in codes


def test_expected_tree_is_verified_separately_from_files(
    tmp_path: Path,
) -> None:
    content = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    (tmp_path / "bundle" / "entry.json").parent.mkdir(parents=True)
    (tmp_path / "bundle" / "entry.json").write_bytes(content)
    digest = compute_tree_sha256(
        {"entry.json": (len(content), _sha256(content))},
        [],
    )
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=[],
        file_artifacts=[
            ExpectedFileArtifact(
                role="report_json",
                path="entry.json",
                byte_count=len(content),
                sha256=_sha256(content),
            )
        ],
        expected_file_count=1,
        tree_sha256=digest,
    )
    observed = _observe_test_tree(tmp_path, tree)

    assert observed.observation.integrity_state.value == "verified"
    assert observed.observation.kind == "tree"


def test_same_request_correlation_changes_only_content_hash_on_artifact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=original)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    first = create_inventory_publication(
        request_path=request_path,
        repository_root=tmp_path,
        output_path=tmp_path / "one.json",
        markdown_path=tmp_path / "one.md",
        metadata_path=tmp_path / "one.metadata.json",
        confirm_local_execution=True,
    )
    (tmp_path / "artifact.json").write_bytes(b'{"reviewed_commit":"drift"}\n')
    second = create_inventory_publication(
        request_path=request_path,
        repository_root=tmp_path,
        output_path=tmp_path / "two.json",
        markdown_path=tmp_path / "two.md",
        metadata_path=tmp_path / "two.metadata.json",
        confirm_local_execution=True,
    )

    assert first.inventory.request_correlation_id == second.inventory.request_correlation_id
    assert first.metadata.inventory_sha256 != second.metadata.inventory_sha256
    assert first.inventory.verification_status.value == "verified"
    assert second.inventory.verification_status.value == "failed"


def test_cli_requires_confirmation_without_reading_or_publishing(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "inventory-phase6-evidence",
            str(tmp_path / "request.json"),
            "--output",
            str(tmp_path / "inventory.json"),
            "--markdown",
            str(tmp_path / "inventory.md"),
            "--metadata",
            str(tmp_path / "inventory.metadata.json"),
        ],
    )

    assert result.exit_code == 1
    assert "confirm-local-execution" in result.stderr
    assert not (tmp_path / "inventory.json").exists()


def test_cli_verified_publication_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=(
            json.dumps(
                {"provider_call_count": 0, "reviewed_commit": HEAD},
                sort_keys=True,
                indent=2,
            )
            .encode()
            + b"\n"
        ),
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    result = CliRunner().invoke(
        app,
        [
            "inventory-phase6-evidence",
            str(request_path),
            "--repository-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "inventory.json"),
            "--markdown",
            str(tmp_path / "inventory.md"),
            "--metadata",
            str(tmp_path / "inventory.metadata.json"),
            "--confirm-local-execution",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "inventory.json").exists()


def test_cli_finding_publication_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    (tmp_path / "artifact.json").write_bytes(b'{"reviewed_commit":"drift"}\n')
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    result = CliRunner().invoke(
        app,
        [
            "inventory-phase6-evidence",
            str(request_path),
            "--repository-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "inventory.json"),
            "--markdown",
            str(tmp_path / "inventory.md"),
            "--metadata",
            str(tmp_path / "inventory.metadata.json"),
            "--confirm-local-execution",
        ],
    )

    assert result.exit_code == 2
    assert (tmp_path / "inventory.json").exists()


def test_final_symlink_is_observed_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    outside = tmp_path.parent / "phase7-outside-final"
    outside.write_bytes(b"outside\n")
    (tmp_path / "artifact.json").unlink()
    (tmp_path / "artifact.json").symlink_to(outside)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.verification_status.value == "failed"
    assert any(finding.code.value == "unsafe_artifact" for finding in inventory.findings)
    assert outside.read_bytes() == b"outside\n"


def test_parent_symlink_is_a_snapshot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="nested/artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    outside = tmp_path.parent / "phase7-outside-parent"
    outside.mkdir()
    (outside / "artifact.json").write_bytes(b"outside\n")
    (tmp_path / "nested").rename(tmp_path / "nested-real")
    (tmp_path / "nested").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    with pytest.raises(InventorySafetyError):
        verify_inventory_request(
            request_path=request_path,
            repository_root=tmp_path,
            confirm_local_execution=True,
        )


def test_parent_swap_after_observation_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="nested/artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    outside = tmp_path.parent / "phase7-parent-swap-outside"
    outside.mkdir()
    (outside / "artifact.json").write_bytes(b"outside\n")
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    original_revalidate = _snapshot_revalidate
    swapped = False

    def swap_then_revalidate(snapshot: Any) -> None:
        nonlocal swapped
        if not swapped:
            (tmp_path / "nested").rename(tmp_path / "nested-held")
            (tmp_path / "nested").symlink_to(outside, target_is_directory=True)
            swapped = True
        original_revalidate(snapshot)

    monkeypatch.setattr(
        "agentlab.phase7_inventory._snapshot_revalidate",
        swap_then_revalidate,
    )
    with pytest.raises(InventorySafetyError):
        verify_inventory_request(
            request_path=request_path,
            repository_root=tmp_path,
            confirm_local_execution=True,
        )


def test_bounded_file_read_limit_is_a_safety_failure(
    tmp_path: Path,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b"12",
    )
    snapshot = _snapshot_root(tmp_path)
    try:
        with pytest.raises(InventorySafetyError):
            _read_regular_file(
                tmp_path,
                "artifact.json",
                "bounded artifact",
                max_bytes=1,
                snapshot=snapshot,
            )
    finally:
        snapshot.close()
    assert request_path.exists()


def test_tree_file_and_byte_limits_are_safety_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"12"
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "entry.txt").write_bytes(content)
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        file_artifacts=[
            ExpectedFileArtifact(
                role="release_metadata",
                path="entry.txt",
                byte_count=len(content),
                sha256=_sha256(content),
            )
        ],
        expected_file_count=1,
        tree_sha256=compute_tree_sha256(
            {"entry.txt": (len(content), _sha256(content))}, []
        ),
    )
    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_FILES", 0)
    with pytest.raises(InventorySafetyError):
        _observe_test_tree(tmp_path, tree)

    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_FILES", 4096)
    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_BYTES", 1)
    with pytest.raises(InventorySafetyError):
        _observe_test_tree(tmp_path, tree)


def test_tree_directory_limit_is_a_safety_failure(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    directories = [f"dir_{index}" for index in range(257)]
    for relative in directories:
        (bundle / relative).mkdir()
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=sorted(directories),
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=compute_tree_sha256({}, directories),
    )

    with pytest.raises(InventorySafetyError, match="bounded directory limit"):
        _observe_test_tree(tmp_path, tree)


def test_tree_scan_does_not_cache_child_directory_fds(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    directories = [f"dir_{index}" for index in range(10)]
    for relative in directories:
        (bundle / relative).mkdir()
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=sorted(directories),
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=compute_tree_sha256({}, directories),
    )
    snapshot = _snapshot_root(tmp_path)
    try:
        observed = _observe_tree(
            root=tmp_path,
            snapshot=snapshot,
            subject_kind="release",
            subject_id="synthetic-release",
            tree=tree,
        )
        assert observed.observation.integrity_state.value == "verified"
        assert len(snapshot.directory_fds) == 2
    finally:
        snapshot.close()


def test_tree_entry_replacement_is_caught_by_snapshot_revalidation(
    tmp_path: Path,
) -> None:
    content = b"stable"
    (tmp_path / "bundle").mkdir()
    entry = tmp_path / "bundle" / "entry.txt"
    entry.write_bytes(content)
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        file_artifacts=[
            ExpectedFileArtifact(
                role="release_metadata",
                path="entry.txt",
                byte_count=len(content),
                sha256=_sha256(content),
            )
        ],
        expected_file_count=1,
        tree_sha256=compute_tree_sha256(
            {"entry.txt": (len(content), _sha256(content))}, []
        ),
    )
    snapshot = _snapshot_root(tmp_path)
    try:
        _observe_tree(
            root=tmp_path,
            snapshot=snapshot,
            subject_kind="release",
            subject_id="synthetic-release",
            tree=tree,
        )
        entry.write_bytes(b"changed")
        with pytest.raises(InventorySafetyError):
            _snapshot_revalidate(snapshot)
    finally:
        snapshot.close()


def test_publication_output_limit_is_a_safety_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    monkeypatch.setattr("agentlab.phase7_inventory.MAX_PUBLICATION_FILE_BYTES", 1)
    with pytest.raises(InventoryPublicationError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=tmp_path / "inventory.json",
            markdown_path=tmp_path / "inventory.md",
            metadata_path=tmp_path / "inventory.metadata.json",
            confirm_local_execution=True,
        )
    assert not (tmp_path / "inventory.json").exists()


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "socket"])
def test_final_hardlink_and_special_files_are_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    content = b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n'
    root = tmp_path
    temporary_root: Any = None
    if kind == "socket" and len(str(root / "artifact.json")) >= 100:
        temporary_root = TemporaryDirectory(prefix="p7-", dir="/private/tmp")
        root = Path(temporary_root.name)
    request_path = _request(
        root,
        artifact_path="artifact.json",
        artifact_bytes=content,
    )
    artifact = root / "artifact.json"
    artifact.unlink()
    outside = root.parent / f"phase7-{kind}-outside"
    listener: socket.socket | None = None
    if kind == "hardlink":
        outside.write_bytes(content)
        os.link(outside, artifact)
    elif kind == "fifo":
        os.mkfifo(artifact)
    else:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(artifact))
        except OSError as error:
            listener.close()
            if temporary_root is not None:
                temporary_root.cleanup()
            pytest.skip(f"filesystem UNIX sockets unavailable: {error}")
        listener.listen(1)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    try:
        inventory = verify_inventory_request(
            request_path=request_path,
            repository_root=root,
            confirm_local_execution=True,
        )
    finally:
        if listener is not None:
            listener.close()
        if artifact.exists() or artifact.is_symlink():
            artifact.unlink()
        if outside.exists():
            outside.unlink()
        if temporary_root is not None:
            temporary_root.cleanup()

    assert inventory.verification_status.value == "failed"
    assert any(finding.code.value == "unsafe_artifact" for finding in inventory.findings)


def test_optional_tree_file_is_rejected_by_schema() -> None:
    required = (
        json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode()
        + b"\n"
    )
    optional = b"optional\n"
    digest = compute_tree_sha256(
        {
            "entry.json": (len(required), _sha256(required)),
            "optional.txt": (len(optional), _sha256(optional)),
        },
        [],
    )
    with pytest.raises(ValueError, match="all have required=True"):
        ExpectedTree(
            role="bundle_root",
            root_path="bundle",
            file_artifacts=[
                ExpectedFileArtifact(
                    role="report_json",
                    path="entry.json",
                    byte_count=len(required),
                    sha256=_sha256(required),
                ),
                ExpectedFileArtifact(
                    role="release_metadata",
                    path="optional.txt",
                    byte_count=len(optional),
                    sha256=_sha256(optional),
                    required=False,
                ),
            ],
            expected_file_count=2,
            tree_sha256=digest,
        )


def test_invalid_campaign_remains_visible_as_without_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_campaign = b'{"event_type":"campaign_finished","provider_call_count":4}\n'
    request_path = _request(
        tmp_path,
        artifact_path="unused-report.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    campaign = CampaignEntry(
        campaign_id="synthetic-campaign",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
        classification=CampaignClassification.AUDIT_ONLY_FAILURE,
        included_in_primary_denominator=False,
        verification_profile="declared_artifact_set",
        declaration_basis="invalid campaign accounting regression",
        file_artifacts=[
            ExpectedFileArtifact(
                role="campaign",
                path="campaign.jsonl",
                byte_count=len(invalid_campaign),
                sha256=_sha256(invalid_campaign),
            )
        ],
    )
    request = load_inventory_request_bytes(request_path.read_bytes()).model_copy(
        update={"campaign_entries": [campaign]}
    )
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "campaign.jsonl").write_bytes(invalid_campaign)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.campaigns[0].integrity_state.value == "drifted"
    assert inventory.summary.provider_call_count_observed == 0
    assert inventory.summary.provider_call_count_unknown_runs == 0
    assert inventory.summary.campaigns_without_total == 1


def test_non_public_release_does_not_propagate_manifest_drift_to_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = b"missing manifest\n"
    historical_release_bytes = b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n'
    campaign_report = (
        json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode()
        + b"\n"
    )
    current_release = ReleaseEntry(
        release_id="release-current",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
        classification=ReleaseClassification.ACCEPTED_CURRENT,
        verification_profile="phase6_public_suite",
        declaration_basis="public release binding regression",
        accepted_manifest_reference_id="accepted-manifest",
        file_artifacts=[
            ExpectedFileArtifact(
                role="suite_manifest",
                path="current/manifest.json",
                byte_count=len(manifest),
                sha256=_sha256(manifest),
            ),
            ExpectedFileArtifact(
                role="checksums",
                path="current/checksums.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
            ExpectedFileArtifact(
                role="external_anchor",
                path="current/anchor.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
        ],
        trees=[
            ExpectedTree(
                role="bundle_root",
                root_path="current/bundle",
                file_artifacts=[],
                expected_file_count=0,
                tree_sha256=_sha256(b"empty bundle"),
            )
        ],
    )
    historical_release = ReleaseEntry(
        release_id="release-historical",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
        classification=ReleaseClassification.HISTORICAL,
        verification_profile="declared_artifact_set",
        declaration_basis="historical release binding regression",
        file_artifacts=[
            ExpectedFileArtifact(
                role="release_metadata",
                path="historical/release-metadata.json",
                byte_count=len(historical_release_bytes),
                sha256=_sha256(historical_release_bytes),
            )
        ],
    )
    campaign = CampaignEntry(
        campaign_id="historical-campaign",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
        classification=CampaignClassification.AUDIT_ONLY_FAILURE,
        included_in_primary_denominator=False,
        release_id="release-historical",
        verification_profile="declared_artifact_set",
        declaration_basis="non-public release propagation regression",
        file_artifacts=[
            ExpectedFileArtifact(
                role="report_json",
                path="historical/campaign-report.json",
                byte_count=len(campaign_report),
                sha256=_sha256(campaign_report),
            )
        ],
    )
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="non-public-release-binding",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="accepted-manifest",
                kind="accepted_manifest",
                path="current/manifest.json",
                byte_count=len(manifest),
                sha256=_sha256(manifest),
                description="accepted manifest",
            )
        ],
        release_entries=[current_release, historical_release],
        campaign_entries=[campaign],
    )
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "historical").mkdir()
    (tmp_path / "historical/release-metadata.json").write_bytes(historical_release_bytes)
    (tmp_path / "historical/campaign-report.json").write_bytes(campaign_report)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    historical = next(
        entry for entry in inventory.campaigns if entry.campaign_id == "historical-campaign"
    )
    assert historical.storage_state.value == "present"
    assert historical.commit_verification.value == "verified"
    assert historical.integrity_state.value == "verified"
    assert not any(
        finding.subject_id == "historical-campaign"
        and finding.code is FindingCode.CROSS_ARTIFACT_MISMATCH
        for finding in inventory.findings
    )


def test_output_parent_symlink_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    outside = tmp_path.parent / "phase7-outside-output"
    outside.mkdir()
    (tmp_path / "outputs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    with pytest.raises(InventorySafetyError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=tmp_path / "outputs" / "inventory.json",
            markdown_path=tmp_path / "outputs" / "inventory.md",
            metadata_path=tmp_path / "outputs" / "inventory.metadata.json",
            confirm_local_execution=True,
        )
    assert not (outside / "inventory.json").exists()


def test_output_alias_partial_publication_and_collision_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    with pytest.raises(InventorySafetyError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=request_path,
            markdown_path=tmp_path / "inventory.md",
            metadata_path=tmp_path / "inventory.metadata.json",
            confirm_local_execution=True,
        )

    output_paths = (
        tmp_path / "inventory.json",
        tmp_path / "inventory.md",
        tmp_path / "inventory.metadata.json",
    )
    output_paths[0].write_bytes(b"partial")
    with pytest.raises(InventoryPublicationError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=output_paths[0],
            markdown_path=output_paths[1],
            metadata_path=output_paths[2],
            confirm_local_execution=True,
        )
    assert output_paths[0].read_bytes() == b"partial"

    output_paths[0].unlink()
    first = create_inventory_publication(
        request_path=request_path,
        repository_root=tmp_path,
        output_path=output_paths[0],
        markdown_path=output_paths[1],
        metadata_path=output_paths[2],
        confirm_local_execution=True,
    )
    original_inventory = output_paths[0].read_bytes()
    with pytest.raises(InventoryPublicationError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=output_paths[0],
            markdown_path=output_paths[1],
            metadata_path=output_paths[2],
            confirm_local_execution=True,
        )
    assert first.inventory_bytes == original_inventory
    assert output_paths[0].read_bytes() == original_inventory


def test_publication_rolls_back_owned_outputs_after_later_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    calls = 0

    def fail_on_second(
        snapshot: Any,
        relative: str,
        content: bytes,
        label: str,
    ) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InventoryPublicationError("synthetic publish failure")
        return _publish_file_no_replace(snapshot, relative, content, label)

    monkeypatch.setattr(
        "agentlab.phase7_inventory._publish_file_no_replace",
        fail_on_second,
    )
    with pytest.raises(InventoryPublicationError):
        create_inventory_publication(
            request_path=request_path,
            repository_root=tmp_path,
            output_path=tmp_path / "inventory.json",
            markdown_path=tmp_path / "inventory.md",
            metadata_path=tmp_path / "inventory.metadata.json",
            confirm_local_execution=True,
        )
    assert not (tmp_path / "inventory.json").exists()
    assert not (tmp_path / "inventory.md").exists()
    assert not (tmp_path / "inventory.metadata.json").exists()


def test_release_request_requires_one_current_and_manifest_binding() -> None:
    manifest = b"manifest\n"
    manifest_artifact = ExpectedFileArtifact(
        role="suite_manifest",
        path="public/manifest.json",
        byte_count=len(manifest),
        sha256=_sha256(manifest),
    )
    release = ReleaseEntry(
        release_id="release-current",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
        classification=ReleaseClassification.ACCEPTED_CURRENT,
        verification_profile="phase6_public_suite",
        declaration_basis="reviewed acceptance",
        accepted_manifest_reference_id="accepted-manifest",
        file_artifacts=[
            manifest_artifact,
            ExpectedFileArtifact(
                role="checksums",
                path="public/checksums.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
            ExpectedFileArtifact(
                role="external_anchor",
                path="public/anchor.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
        ],
        trees=[
            ExpectedTree(
                role="bundle_root",
                root_path="public/bundle",
                file_artifacts=[],
                expected_file_count=0,
                tree_sha256=_sha256(b"bundle"),
            )
        ],
    )
    accepted_reference = AuthorityReference(
        reference_id="accepted-manifest",
        kind="accepted_manifest",
        path=manifest_artifact.path,
        byte_count=manifest_artifact.byte_count,
        sha256=manifest_artifact.sha256,
        description="accepted manifest",
    )
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="release-request",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[accepted_reference],
        release_entries=[release],
    )
    assert request.release_entries[0].accepted_manifest_reference_id == "accepted-manifest"

    with pytest.raises(ValueError, match="exactly one accepted_current"):
        EvidenceInventoryRequest(
            schema_version="1.0",
            inventory_id="release-without-current",
            authoritative=False,
            scope=InventoryScope.PHASE6,
            source_of_truth_references=[accepted_reference],
            release_entries=[
                ReleaseEntry(
                    **{
                        **release.model_dump(mode="python"),
                        "classification": "candidate_unaccepted",
                        "accepted_manifest_reference_id": None,
                    }
                )
            ],
        )


def test_accepted_current_and_superseded_release_form_a_valid_pair() -> None:
    def accepted_release(
        release_id: str,
        classification: ReleaseClassification,
        manifest_path: str,
        reference_id: str,
        superseded_by: str | None = None,
    ) -> tuple[ReleaseEntry, AuthorityReference]:
        manifest = b"manifest-" + release_id.encode()
        files = [
            ExpectedFileArtifact(
                role="suite_manifest",
                path=manifest_path,
                byte_count=len(manifest),
                sha256=_sha256(manifest),
            ),
            ExpectedFileArtifact(
                role="checksums",
                path=f"{release_id}/checksums.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
            ExpectedFileArtifact(
                role="external_anchor",
                path=f"{release_id}/anchor.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            ),
        ]
        return (
            ReleaseEntry(
                release_id=release_id,
                artifact_reviewed_commit=HEAD,
                commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
                classification=classification,
                verification_profile="phase6_public_suite",
                declaration_basis="accepted pair regression",
                accepted_manifest_reference_id=reference_id,
                superseded_by=superseded_by,
                file_artifacts=files,
                trees=[
                    ExpectedTree(
                        role="bundle_root",
                        root_path=f"{release_id}/bundle",
                        file_artifacts=[],
                        expected_file_count=0,
                        tree_sha256=_sha256(b"tree"),
                    )
                ],
            ),
            AuthorityReference(
                reference_id=reference_id,
                kind="accepted_manifest",
                path=manifest_path,
                byte_count=len(manifest),
                sha256=_sha256(manifest),
                description="accepted pair regression",
            ),
        )

    current, current_reference = accepted_release(
        "release-current",
        ReleaseClassification.ACCEPTED_CURRENT,
        "release-current/manifest.json",
        "accepted-current",
    )
    superseded, superseded_reference = accepted_release(
        "release-superseded",
        ReleaseClassification.ACCEPTED_SUPERSEDED,
        "release-superseded/manifest.json",
        "accepted-superseded",
        superseded_by="release-current",
    )

    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="accepted-pair",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[current_reference, superseded_reference],
        release_entries=[current, superseded],
    )

    assert [entry.classification for entry in request.release_entries] == [
        ReleaseClassification.ACCEPTED_CURRENT,
        ReleaseClassification.ACCEPTED_SUPERSEDED,
    ]
    assert request.release_entries[1].superseded_by == "release-current"


def test_semantic_finding_propagates_to_entry_and_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    request = load_inventory_request_bytes(request_path.read_bytes()).model_copy(
        update={
            "retention_expectations": [
                RetentionExpectation(
                    subject_kind="campaign",
                    subject_id="synthetic-campaign",
                    expected_retention_state=RetentionState.LOCAL_ONLY,
                    declaration_basis="semantic finding regression",
                )
            ]
        }
    )
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    from agentlab import phase7_inventory

    original = phase7_inventory._commit_observation

    def add_semantic_finding(**kwargs: Any) -> tuple[Any, tuple[Any, ...]]:
        state, findings = original(**kwargs)
        if kwargs["subject_kind"] == "campaign":
            findings = (
                *findings,
                _finding(
                    FindingCode.CROSS_ARTIFACT_MISMATCH,
                    subject_kind="campaign",
                    subject_id=kwargs["subject_id"],
                    role="semantic_binding",
                ),
            )
        return state, findings

    monkeypatch.setattr(phase7_inventory, "_commit_observation", add_semantic_finding)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.campaigns[0].integrity_state.value == "drifted"
    assert inventory.retention[0].retention_state is RetentionState.UNKNOWN
    assert any(
        finding.subject_kind == "campaign"
        and finding.code is FindingCode.CROSS_ARTIFACT_MISMATCH
        for finding in inventory.findings
    )


def test_complete_campaign_profile_requires_all_phase6_roles() -> None:
    with pytest.raises(ValueError, match="missing required file roles"):
        CampaignEntry(
            campaign_id="primary-campaign",
            artifact_reviewed_commit=HEAD,
            commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
            classification=CampaignClassification.PRIMARY_EVALUATION,
            included_in_primary_denominator=True,
            release_id="release-current",
            verification_profile="phase6_campaign_complete",
            declaration_basis="reviewed primary",
            file_artifacts=[
                ExpectedFileArtifact(
                    role="campaign",
                    path="campaign.jsonl",
                    byte_count=1,
                    sha256=_sha256(b"x"),
                )
            ],
        )


def test_campaign_tree_is_rejected_by_profile_contract() -> None:
    with pytest.raises(ValueError, match="file Artifacts only"):
        CampaignEntry(
            campaign_id="audit-campaign",
            artifact_reviewed_commit=HEAD,
            commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
            classification=CampaignClassification.AUDIT_ONLY_FAILURE,
            included_in_primary_denominator=False,
            verification_profile="declared_artifact_set",
            declaration_basis="declared",
            trees=[
                ExpectedTree(
                    role="bundle_root",
                    root_path="bundle",
                    expected_file_count=0,
                    tree_sha256=_sha256(b"tree"),
                )
            ],
        )


def test_local_only_requires_verified_local_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b'{"reviewed_commit":"' + HEAD.encode() + b'"}\n',
    )
    request = load_inventory_request_bytes(request_path.read_bytes())
    request = request.model_copy(
        update={
            "retention_expectations": [
                RetentionExpectation(
                    subject_kind="campaign",
                    subject_id="synthetic-campaign",
                    expected_retention_state=RetentionState.LOCAL_ONLY,
                    declaration_basis="local-only declaration",
                )
            ]
        }
    )
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "artifact.json").unlink()
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    retention = inventory.retention[0]
    assert retention.retention_state is RetentionState.UNKNOWN
    assert retention.verification_basis.value == "not_available"
    assert any(finding.subject_kind == "retention" for finding in inventory.findings)


def test_receipt_requires_a_subject_wide_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = b"authority\n"
    (tmp_path / "authority.txt").write_bytes(authority)
    a_bytes = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    b_bytes = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    (tmp_path / "a.json").write_bytes(a_bytes)
    (tmp_path / "b.json").write_bytes(b_bytes)
    a_markdown = b"# campaign-a\n"
    (tmp_path / "a.md").write_bytes(a_markdown)
    receipt = ExternalCopyReceipt(
        schema_version="1.0",
        subject_kind="campaign",
        subject_id="campaign-a",
        # A digest of only one declared file must not claim the subject.
        subject_digest=_sha256(a_bytes),
        created_at="2026-01-01T00:00:00.000000Z",
    )
    receipt_bytes = canonical_inventory_json_bytes(receipt)
    (tmp_path / "receipt.json").write_bytes(receipt_bytes)
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="retention-binding",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="authority",
                kind="tracked_closeout",
                path="authority.txt",
                byte_count=len(authority),
                sha256=_sha256(authority),
                description="authority",
            )
        ],
        campaign_entries=[
            CampaignEntry(
                campaign_id=campaign_id,
                artifact_reviewed_commit=HEAD,
                commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
                classification=CampaignClassification.AUDIT_ONLY_FAILURE,
                included_in_primary_denominator=False,
                verification_profile="declared_artifact_set",
                declaration_basis="declared",
                file_artifacts=(
                    [
                        ExpectedFileArtifact(
                            role="report_json",
                            path=f"{campaign_id[9]}.json",
                            byte_count=len(data),
                            sha256=_sha256(data),
                        )
                    ]
                    + (
                        [
                            ExpectedFileArtifact(
                                role="report_markdown",
                                path="a.md",
                                byte_count=len(a_markdown),
                                sha256=_sha256(a_markdown),
                            )
                        ]
                        if campaign_id == "campaign-a"
                        else []
                    )
                ),
            )
            for campaign_id, data in (("campaign-a", a_bytes), ("campaign-b", b_bytes))
        ],
        retention_expectations=[
            RetentionExpectation(
                subject_kind="campaign",
                subject_id="campaign-a",
                expected_retention_state=RetentionState.EXTERNAL_COPY_RECEIPT_VERIFIED,
                external_copy_receipt=ExpectedFileArtifact(
                    role="receipt",
                    path="receipt.json",
                    byte_count=len(receipt_bytes),
                    sha256=_sha256(receipt_bytes),
                ),
                declaration_basis="receipt declaration",
            )
        ],
    )
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.retention[0].retention_state is RetentionState.UNKNOWN
    assert any(finding.code.value == "retention_receipt_invalid" for finding in inventory.findings)
