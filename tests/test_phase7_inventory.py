from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_phase6 import COMMIT
from test_phase6_public import (
    _completed_historical_fixture,
    _historical_record_for_fixture,
)
from typer.testing import CliRunner

from agentlab.cli import app
from agentlab.phase7_inventory import (
    AuthorityReference,
    CampaignClassification,
    CampaignEntry,
    CommitVerificationMode,
    EvidenceInventoryMetadata,
    EvidenceInventoryRequest,
    ExpectedFileArtifact,
    ExpectedTree,
    ExternalCopyReceipt,
    FindingCode,
    InventoryContractError,
    InventoryPublicationError,
    InventoryReleaseEntry,
    InventorySafetyError,
    InventoryScope,
    ProviderTotalStatus,
    ReleaseClassification,
    ReleaseEntry,
    RetentionExpectation,
    RetentionState,
    _finding,
    _ObservationResult,
    _observe_file,
    _observe_tree,
    _publish_file_no_replace,
    _read_regular_file,
    _snapshot_revalidate,
    _snapshot_root,
    canonical_inventory_json_bytes,
    compute_tree_sha256,
    create_inventory_publication,
    load_inventory_request_bytes,
    publish_inventory_request_bytes,
    verify_declared_inventory_inputs,
    verify_evidence_inventory_publication_bytes,
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
    commit_mode: CommitVerificationMode = CommitVerificationMode.DECLARATION_BASIS_ONLY,
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
    assert "artifact_reviewed_commit_not_verifiable" not in codes
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
    content = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    expected_files: dict[str, tuple[int, str]] = {}
    expected_artifacts: list[ExpectedFileArtifact] = []
    for relative in directories:
        directory = bundle / relative
        directory.mkdir()
        (directory / "entry.json").write_bytes(content)
        path = f"{relative}/entry.json"
        expected_files[path] = (len(content), _sha256(content))
        expected_artifacts.append(
            ExpectedFileArtifact(
                role="report_json",
                path=path,
                byte_count=len(content),
                sha256=_sha256(content),
            )
        )
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=sorted(directories),
        file_artifacts=sorted(expected_artifacts, key=lambda item: item.path),
        expected_file_count=len(expected_artifacts),
        tree_sha256=compute_tree_sha256(expected_files, directories),
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
        assert len(snapshot.file_fds) == 10
    finally:
        snapshot.close()


def test_missing_tree_directories_do_not_cache_present_parent_fds(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    parents = [f"parent_{index}" for index in range(10)]
    missing = [f"{parent}/allowed" for parent in parents]
    for parent in parents:
        (bundle / parent).mkdir()
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=sorted([*parents, *missing]),
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=compute_tree_sha256({}, parents),
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
        assert observed.observation.integrity_state.value == "not_verifiable"
        assert len(snapshot.directory_fds) == 2
    finally:
        snapshot.close()


def test_tree_child_replacement_before_file_observation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    original_child = tmp_path / "bundle" / "child"
    original_child.mkdir(parents=True)
    (original_child / "entry.json").write_bytes(content)
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=["child"],
        file_artifacts=[
            ExpectedFileArtifact(
                role="report_json",
                path="child/entry.json",
                byte_count=len(content),
                sha256=_sha256(content),
            )
        ],
        expected_file_count=1,
        tree_sha256=compute_tree_sha256(
            {"child/entry.json": (len(content), _sha256(content))},
            ["child"],
        ),
    )
    snapshot = _snapshot_root(tmp_path)
    original_observe_file = _observe_file
    replaced = False

    def replace_child_before_file_read(*args: Any, **kwargs: Any) -> _ObservationResult:
        nonlocal replaced
        if not replaced:
            original_child.rename(tmp_path / "bundle" / "child-original")
            replacement = tmp_path / "bundle" / "child"
            replacement.mkdir()
            (replacement / "entry.json").write_bytes(content)
            replaced = True
        return original_observe_file(*args, **kwargs)

    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_file",
        replace_child_before_file_read,
    )
    try:
        with pytest.raises(InventorySafetyError, match="directory identity changed"):
            _observe_tree(
                root=tmp_path,
                snapshot=snapshot,
                subject_kind="release",
                subject_id="synthetic-release",
                tree=tree,
            )
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
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(artifact))
        except OSError as error:
            if listener is not None:
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
        commit_verification_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
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


def test_provider_strict_load_failure_drifts_campaign_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_bytes = b"provider-source\n"
    request_path = _request(
        tmp_path,
        artifact_path="unused-report.json",
        artifact_bytes=b'{}\n',
    )
    campaign = CampaignEntry(
        campaign_id="strict-provider-campaign",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
        classification=CampaignClassification.AUDIT_ONLY_FAILURE,
        included_in_primary_denominator=False,
        verification_profile="declared_artifact_set",
        declaration_basis="provider strict-load ordering regression",
        file_artifacts=[
            ExpectedFileArtifact(
                role="campaign",
                path="campaign.jsonl",
                byte_count=len(campaign_bytes),
                sha256=_sha256(campaign_bytes),
            )
        ],
    )
    request = load_inventory_request_bytes(request_path.read_bytes()).model_copy(
        update={
            "campaign_entries": [campaign],
            "retention_expectations": [
                RetentionExpectation(
                    subject_kind="campaign",
                    subject_id=campaign.campaign_id,
                    expected_retention_state=RetentionState.LOCAL_ONLY,
                    declaration_basis="provider strict-load retention regression",
                )
            ],
        }
    )
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "campaign.jsonl").write_bytes(campaign_bytes)
    from agentlab.phase6 import Phase6CampaignByteValidation

    monkeypatch.setattr(
        "agentlab.phase7_inventory._known_contract_is_valid", lambda _role, _content: True
    )
    monkeypatch.setattr(
        "agentlab.phase6.load_phase6_campaign_from_bytes",
        lambda _content: Phase6CampaignByteValidation(
            is_valid=False,
            provider_call_count=None,
            provider_call_count_unknown_runs=None,
            total_status="unknown",
            error_detail="synthetic strict-load failure",
        ),
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.campaigns[0].integrity_state.value == "drifted"
    assert inventory.campaigns[0].provider_total_status is ProviderTotalStatus.UNAVAILABLE
    assert inventory.summary.campaigns_without_total == 1
    assert inventory.retention[0].retention_state is RetentionState.UNKNOWN
    assert any(
        finding.code is FindingCode.CANONICAL_LOAD_FAILED
        and finding.subject_kind == "campaign"
        and finding.subject_id == campaign.campaign_id
        for finding in inventory.findings
    )


def _historical_inventory_request(
    root: Path,
    *,
    corrupt_record: bool = False,
) -> tuple[Path, Any, str]:
    (
        source_root,
        plan_path,
        campaign_path,
        report_json_path,
        report_markdown_path,
        plan,
    ) = _completed_historical_fixture(root / "source")
    inventory_root = root / "inventory"
    inventory_root.mkdir()
    relative_paths = {
        "historical_verification": "historical-verification-record.json",
        "plan": "plan.json",
        "campaign": "campaign.jsonl",
        "report_json": "report.json",
        "report_markdown": "report.md",
    }
    source_paths = {
        "plan": source_root / plan_path,
        "campaign": source_root / campaign_path,
        "report_json": source_root / report_json_path,
        "report_markdown": source_root / report_markdown_path,
    }
    record_bytes = _historical_record_for_fixture(
        source_root,
        plan_path,
        campaign_path,
        report_json_path,
        report_markdown_path,
        plan,
    )
    if corrupt_record:
        record = json.loads(record_bytes)
        record["plan_sha256"] = "0" * 64
        record_bytes = canonical_inventory_json_bytes(record)
    contents = {
        **{role: path.read_bytes() for role, path in source_paths.items()},
        "historical_verification": record_bytes,
    }
    artifacts = []
    for role, relative in relative_paths.items():
        path = inventory_root / relative
        path.write_bytes(contents[role])
        artifacts.append(
            ExpectedFileArtifact(
                role=role,
                path=relative,
                byte_count=len(contents[role]),
                sha256=_sha256(contents[role]),
            )
        )
    authority = b"historical authority\n"
    (inventory_root / "authority.txt").write_bytes(authority)
    campaign_id = "workflow-ab-codex-live-002"
    campaign = CampaignEntry(
        campaign_id=campaign_id,
        experiment_id=plan.experiment_id,
        artifact_reviewed_commit=COMMIT,
        commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
        classification=CampaignClassification.HISTORICAL_NON_PRIMARY,
        included_in_primary_denominator=False,
        verification_profile="historical_verification",
        declaration_basis="legacy historical byte facade regression",
        file_artifacts=artifacts,
    )
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="historical-legacy-regression",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="authority",
                kind="tracked_closeout",
                path="authority.txt",
                byte_count=len(authority),
                sha256=_sha256(authority),
                description="synthetic historical authority",
            )
        ],
        campaign_entries=[campaign],
        retention_expectations=[
            RetentionExpectation(
                subject_kind="campaign",
                subject_id=campaign_id,
                expected_retention_state=RetentionState.LOCAL_ONLY,
                declaration_basis="legacy local retention regression",
            )
        ],
    )
    request_path = inventory_root / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))

    return request_path, plan, campaign_id


def test_historical_legacy_campaign_is_verified_and_accounted_from_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, plan, _campaign_id = _historical_inventory_request(tmp_path)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=request_path.parent,
        confirm_local_execution=True,
    )

    assert inventory.verification_status.value == "verified"
    assert inventory.findings == []
    assert inventory.campaigns[0].integrity_state.value == "verified"
    assert inventory.campaigns[0].provider_total_status is ProviderTotalStatus.OBSERVED
    assert (
        inventory.campaigns[0].provider_call_count_observed
        == plan.planned_provider_call_count
    )
    assert inventory.campaigns[0].provider_call_count_unknown_runs == 0
    assert inventory.summary.campaigns_without_total == 0
    assert inventory.retention[0].retention_state is RetentionState.LOCAL_ONLY


def test_historical_facade_failure_drifts_campaign_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path, _plan, campaign_id = _historical_inventory_request(
        tmp_path,
        corrupt_record=True,
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=request_path.parent,
        confirm_local_execution=True,
    )

    assert inventory.verification_status.value == "failed"
    campaign = inventory.campaigns[0]
    assert campaign.integrity_state.value == "drifted"
    assert campaign.provider_total_status is ProviderTotalStatus.UNAVAILABLE
    assert inventory.summary.campaigns_without_total == 1
    assert inventory.retention[0].retention_state is RetentionState.UNKNOWN
    assert any(
        finding.code is FindingCode.CANONICAL_LOAD_FAILED
        and finding.subject_kind == "campaign"
        and finding.subject_id == campaign_id
        for finding in inventory.findings
    )


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
        artifact_reviewed_commits=[HEAD],
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
        artifact_reviewed_commits=[HEAD],
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
        commit_verification_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
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
    public_audit_report = (
        json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode()
        + b"\n"
    )
    public_audit_campaign = CampaignEntry(
        campaign_id="public-audit-campaign",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
        classification=CampaignClassification.AUDIT_ONLY_FAILURE,
        included_in_primary_denominator=False,
        release_id="release-current",
        verification_profile="declared_artifact_set",
        declaration_basis="public suite non-primary propagation regression",
        file_artifacts=[
            ExpectedFileArtifact(
                role="report_json",
                path="current/campaign-report.json",
                byte_count=len(public_audit_report),
                sha256=_sha256(public_audit_report),
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
        campaign_entries=[campaign, public_audit_campaign],
    )
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "historical").mkdir()
    (tmp_path / "historical/release-metadata.json").write_bytes(historical_release_bytes)
    (tmp_path / "historical/campaign-report.json").write_bytes(campaign_report)
    (tmp_path / "current").mkdir()
    (tmp_path / "current/campaign-report.json").write_bytes(public_audit_report)
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
    public_audit = next(
        entry
        for entry in inventory.campaigns
        if entry.campaign_id == "public-audit-campaign"
    )
    assert public_audit.storage_state.value == "present"
    assert public_audit.commit_verification.value == "verified"
    assert public_audit.integrity_state.value == "verified"
    assert not any(
        finding.subject_id in {"historical-campaign", "public-audit-campaign"}
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
        artifact_reviewed_commits=[HEAD],
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
                artifact_reviewed_commits=[HEAD],
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


def test_generic_reviewed_commit_is_not_typed_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = json.dumps({"reviewed_commit": HEAD}, sort_keys=True, indent=2).encode() + b"\n"
    request_path = _request(
        tmp_path,
        artifact_path="report.json",
        artifact_bytes=report,
        commit_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    assert inventory.campaigns[0].commit_verification.value == "not_verifiable"
    assert inventory.campaigns[0].integrity_state.value == "not_verifiable"


def test_release_commit_collection_rejects_legacy_duplicate_and_unsorted_shapes() -> None:
    with pytest.raises(ValueError, match="artifact_reviewed_commits"):
        ReleaseEntry(
            release_id="release",
            artifact_reviewed_commits=[OTHER_HEAD, HEAD],
            commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
            classification=ReleaseClassification.HISTORICAL,
            verification_profile="declared_artifact_set",
            declaration_basis="test",
            file_artifacts=[
                ExpectedFileArtifact(
                    role="release_metadata",
                    path="metadata.json",
                    byte_count=1,
                    sha256=_sha256(b"x"),
                )
            ],
        )
    with pytest.raises(InventoryContractError):
        load_inventory_request_bytes(
            b'{"authoritative":false,"campaign_entries":[],"expected_execution_repository_head":null,'
            b'"inventory_id":"legacy","release_entries":[{"artifact_reviewed_commit":"'
            + HEAD.encode()
            + b'"}],"retention_expectations":[],"schema_version":"1.0","scope":"phase6",'
            b'"source_of_truth_references":[]}'
        )


def test_provider_accounting_exposes_unavailable_campaign_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_campaign = b'{"event_type":"campaign_finished","provider_call_count":4}\n'
    request_path = _request(
        tmp_path,
        artifact_path="unused-report.json",
        artifact_bytes=b"{}\n",
    )
    request = load_inventory_request_bytes(request_path.read_bytes()).model_copy(
        update={
            "campaign_entries": [
                CampaignEntry(
                    campaign_id="synthetic-campaign",
                    artifact_reviewed_commit=HEAD,
                    commit_verification_mode=CommitVerificationMode.DECLARATION_BASIS_ONLY,
                    classification=CampaignClassification.AUDIT_ONLY_FAILURE,
                    included_in_primary_denominator=False,
                    verification_profile="declared_artifact_set",
                    declaration_basis="test",
                    file_artifacts=[
                        ExpectedFileArtifact(
                            role="campaign",
                            path="campaign.jsonl",
                            byte_count=len(invalid_campaign),
                            sha256=_sha256(invalid_campaign),
                        )
                    ],
                )
            ]
        }
    )
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    (tmp_path / "campaign.jsonl").write_bytes(invalid_campaign)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )

    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )

    campaign = inventory.campaigns[0]
    assert campaign.provider_total_status is ProviderTotalStatus.UNAVAILABLE
    assert campaign.provider_call_count_observed is None
    assert campaign.provider_call_count_unknown_runs is None
    assert inventory.summary.provider_accounting_scope == "declared_campaign_entries"
    assert inventory.summary.campaigns_without_total == 1


def test_publication_verifier_binds_request_and_failed_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(
        tmp_path,
        artifact_path="artifact.json",
        artifact_bytes=b"{}\n",
    )
    (tmp_path / "artifact.json").unlink()
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )
    publication = create_inventory_publication(
        request_path=request_path,
        repository_root=tmp_path,
        output_path=tmp_path / "inventory.json",
        markdown_path=tmp_path / "inventory.md",
        metadata_path=tmp_path / "inventory.metadata.json",
        confirm_local_execution=True,
    )

    assert publication.exit_code == 2
    verified = verify_evidence_inventory_publication_bytes(
        request_path.read_bytes(),
        publication.inventory_bytes,
        publication.markdown_bytes,
        publication.metadata_bytes,
    )
    assert verified.inventory.verification_status.value == "failed"


def test_publication_verifier_binds_execution_head_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )
    publication = create_inventory_publication(
        request_path=request_path,
        repository_root=tmp_path,
        output_path=tmp_path / "inventory.json",
        markdown_path=tmp_path / "inventory.md",
        metadata_path=tmp_path / "inventory.metadata.json",
        confirm_local_execution=True,
    )
    metadata = EvidenceInventoryMetadata.model_validate(json.loads(publication.metadata_bytes))
    modified_metadata = metadata.model_copy(
        update={"observed_execution_repository_head": OTHER_HEAD}
    )

    with pytest.raises(InventoryContractError, match="metadata"):
        verify_evidence_inventory_publication_bytes(
            request_path.read_bytes(),
            publication.inventory_bytes,
            publication.markdown_bytes,
            canonical_inventory_json_bytes(modified_metadata),
        )


@pytest.mark.parametrize("invalid_commit", ["not-a-commit", "A" * 40, "a" * 39])
def test_release_reviewed_commit_members_must_be_canonical(
    invalid_commit: str,
) -> None:
    release_kwargs = {
        "release_id": "release-invalid-commit",
        "artifact_reviewed_commits": [invalid_commit],
        "commit_verification_mode": CommitVerificationMode.INTERNAL_REQUIRED,
        "classification": ReleaseClassification.HISTORICAL,
        "verification_profile": "declared_artifact_set",
        "declaration_basis": "commit pattern regression",
        "file_artifacts": [
            ExpectedFileArtifact(
                role="release_metadata",
                path="metadata.json",
                byte_count=1,
                sha256=_sha256(b"x"),
            )
        ],
    }
    with pytest.raises(ValueError, match="canonical commit IDs"):
        ReleaseEntry(**release_kwargs)
    with pytest.raises(ValueError, match="canonical commit IDs"):
        InventoryReleaseEntry(
            release_id="release-invalid-commit",
            artifact_reviewed_commits=[invalid_commit],
            commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
            commit_verification="verified",
            classification=ReleaseClassification.HISTORICAL,
            verification_profile="declared_artifact_set",
            storage_state="present",
            integrity_state="verified",
            artifact_observations=[],
        )


def test_request_publisher_sha_mismatch_has_zero_mutation(tmp_path: Path) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    (tmp_path / ".artifacts").mkdir()

    with pytest.raises(InventoryContractError, match="SHA-256"):
        publish_inventory_request_bytes(
            request_path.read_bytes(),
            tmp_path,
            expected_request_sha256="0" * 64,
            confirm_local_write=True,
        )

    assert list((tmp_path / ".artifacts").iterdir()) == []


def test_request_publisher_write_failure_rolls_back_owned_file_and_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()

    def fail_write(_descriptor: int, _content: object) -> int:
        raise OSError("synthetic write failure")

    monkeypatch.setattr("agentlab.phase7_inventory.os.write", fail_write)
    with pytest.raises(InventoryPublicationError, match="failed safely"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    assert list((tmp_path / ".artifacts").iterdir()) == []


def test_request_publisher_reload_failure_rolls_back_owned_file_and_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()
    monkeypatch.setattr(
        "agentlab.phase7_inventory._read_regular_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InventoryPublicationError("synthetic descriptor reload failure")
        ),
    )

    with pytest.raises(InventoryPublicationError, match="synthetic descriptor reload failure"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    assert list((tmp_path / ".artifacts").iterdir()) == []


def test_request_publisher_rejects_same_inode_mutation_after_descriptor_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()
    from agentlab import phase7_inventory

    original_read = phase7_inventory._read_regular_file

    def truncate_after_read(*args: object, **kwargs: object) -> object:
        result = original_read(*args, **kwargs)
        request_file = (
            tmp_path
            / ".artifacts"
            / "phase7"
            / "evidence-inventory"
            / "synthetic-inventory"
            / "request.json"
        )
        with request_file.open("r+b") as handle:
            handle.truncate(0)
        return result

    monkeypatch.setattr(phase7_inventory, "_read_regular_file", truncate_after_read)
    with pytest.raises(InventoryPublicationError, match="changed before reload completed"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    assert list((tmp_path / ".artifacts").iterdir()) == []


def test_request_publisher_preserves_leaf_when_request_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()
    from agentlab import phase7_inventory

    original_read = phase7_inventory._read_regular_file

    def replace_after_read(*args: object, **kwargs: object) -> object:
        result = original_read(*args, **kwargs)
        leaf = (
            tmp_path
            / ".artifacts"
            / "phase7"
            / "evidence-inventory"
            / "synthetic-inventory"
        )
        replacement = leaf / "replacement.json"
        replacement.write_bytes(request_bytes)
        os.replace(replacement, leaf / "request.json")
        return result

    monkeypatch.setattr(phase7_inventory, "_read_regular_file", replace_after_read)
    with pytest.raises(InventoryPublicationError, match="rollback could not be verified"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    assert (
        tmp_path
        / ".artifacts"
        / "phase7"
        / "evidence-inventory"
        / "synthetic-inventory"
        / "request.json"
    ).exists()


def test_request_publisher_preserves_nonempty_leaf_on_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()

    def add_unowned_file_then_fail(_descriptor: int, _content: object) -> int:
        leaf = (
            tmp_path
            / ".artifacts"
            / "phase7"
            / "evidence-inventory"
            / "synthetic-inventory"
        )
        (leaf / "unowned.txt").write_bytes(b"do not remove\n")
        raise OSError("synthetic write failure")

    monkeypatch.setattr("agentlab.phase7_inventory.os.write", add_unowned_file_then_fail)
    with pytest.raises(InventoryPublicationError, match="rollback could not be verified"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    leaf = (
        tmp_path
        / ".artifacts"
        / "phase7"
        / "evidence-inventory"
        / "synthetic-inventory"
    )
    assert (leaf / "unowned.txt").read_bytes() == b"do not remove\n"
    assert not (leaf / "request.json").exists()


def test_request_publisher_reports_rollback_failure_without_deleting_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()
    monkeypatch.setattr(
        "agentlab.phase7_inventory.os.write",
        lambda _descriptor, _content: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._rollback_owned_request_publication",
        lambda **_kwargs: (_ for _ in ()).throw(
            InventoryPublicationError("synthetic rollback failure")
        ),
    )

    with pytest.raises(InventoryPublicationError, match="rollback could not be verified"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )

    assert (
        tmp_path
        / ".artifacts"
        / "phase7"
        / "evidence-inventory"
        / "synthetic-inventory"
    ).exists()


def test_request_publisher_is_create_only_and_declared_verifier_is_byte_based(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=b"{}\n")
    request_bytes = request_path.read_bytes()
    (tmp_path / ".artifacts").mkdir()
    published = publish_inventory_request_bytes(
        request_bytes,
        tmp_path,
        expected_request_sha256=_sha256(request_bytes),
        confirm_local_write=True,
    )
    assert published.request_path.read_bytes() == request_bytes
    with pytest.raises(InventoryPublicationError, match="already exists"):
        publish_inventory_request_bytes(
            request_bytes,
            tmp_path,
            expected_request_sha256=_sha256(request_bytes),
            confirm_local_write=True,
        )
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head", lambda _root: HEAD
    )
    verification = verify_declared_inventory_inputs(
        request_bytes,
        tmp_path,
        confirm_local_execution=True,
    )
    assert verification.request_sha256 == _sha256(request_bytes)


def test_only_matching_release_checksums_tree_alias_is_allowed() -> None:
    manifest = ExpectedFileArtifact(
        role="suite_manifest",
        path="manifest.json",
        byte_count=1,
        sha256=_sha256(b"m"),
    )
    checksums = ExpectedFileArtifact(
        role="checksums",
        path="bundle/checksums.json",
        byte_count=1,
        sha256=_sha256(b"c"),
    )
    tree_member = ExpectedFileArtifact(
        role="checksums",
        path="checksums.json",
        byte_count=1,
        sha256=_sha256(b"c"),
    )
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        file_artifacts=[tree_member],
        expected_file_count=1,
        tree_sha256=compute_tree_sha256(
            {"checksums.json": (1, _sha256(b"c"))}, []
        ),
    )
    release = ReleaseEntry(
        release_id="release-current",
        artifact_reviewed_commits=[HEAD],
        commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
        classification=ReleaseClassification.ACCEPTED_CURRENT,
        verification_profile="phase6_public_suite",
        declaration_basis="alias test",
        accepted_manifest_reference_id="manifest-ref",
        file_artifacts=[
            manifest,
            checksums,
            ExpectedFileArtifact(
                role="external_anchor",
                path="anchor.json",
                byte_count=1,
                sha256=_sha256(b"a"),
            ),
        ],
        trees=[tree],
    )
    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="checksum-alias",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="manifest-ref",
                kind="accepted_manifest",
                path="manifest.json",
                byte_count=1,
                sha256=_sha256(b"m"),
                description="manifest",
            )
        ],
        release_entries=[release],
    )
    assert request.release_entries[0].trees[0].file_artifacts[0].path == "checksums.json"

    with pytest.raises(ValueError, match="paths must be unique"):
        EvidenceInventoryRequest(
            **{
                **request.model_dump(mode="python"),
                "release_entries": [
                    release.model_copy(
                        update={
                            "trees": [
                                tree.model_copy(
                                    update={
                                        "file_artifacts": [
                                            tree_member.model_copy(
                                                update={"role": "release_metadata"}
                                            )
                                        ]
                                    }
                                )
                            ]
                        }
                    )
                ],
            }
        )
