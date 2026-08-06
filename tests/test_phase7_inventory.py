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
from typer.testing import CliRunner

from agentlab.cli import app
from agentlab.phase7_inventory import (
    ArtifactObservation,
    AuthorityReference,
    CampaignClassification,
    CampaignEntry,
    CommitVerificationMode,
    EvidenceInventoryRequest,
    ExpectedFileArtifact,
    ExpectedTree,
    ExternalCopyReceipt,
    IntegrityState,
    InventoryCampaignEntry,
    InventoryContractError,
    InventoryPublicationError,
    InventorySafetyError,
    InventoryScope,
    ReleaseClassification,
    ReleaseEntry,
    RetentionExpectation,
    RetentionState,
    StorageState,
    _ObservationResult,
    _observe_tree,
    _provider_counts,
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
    from test_phase6_public import _ready_suite

    from agentlab.phase6 import (
        ExternalChecksumAnchor,
        PublicSuiteManifest,
        canonical_json_bytes,
        validate_public_suite_snapshot,
    )
    from agentlab.phase6_public import render_public_suite

    authority = b"synthetic authority\n"
    (root / "authority.txt").write_bytes(authority)

    inputs_dir, manifest_path = _ready_suite(root)
    manifest = PublicSuiteManifest.model_validate_json(manifest_path.read_bytes())
    snapshot: dict[str, bytes] = {}
    for p in inputs_dir.glob("**/*"):
        if p.is_file() and p.name != "suite-manifest.json":
            snapshot[p.relative_to(inputs_dir).as_posix()] = p.read_bytes()
    validated = validate_public_suite_snapshot(manifest, snapshot)
    rendered = render_public_suite(validated)

    pub = root / "public"
    pub.mkdir(exist_ok=True)
    manifest_bytes = manifest_path.read_bytes()
    (pub / "manifest.json").write_bytes(manifest_bytes)

    for rel_path, content in rendered.files.items():
        dest = pub / "bundle" / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    for ref_path, content in snapshot.items():
        dest = pub / "bundle" / ref_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    checksums_content = rendered.files["checksums.json"]
    (pub / "checksums.json").write_bytes(checksums_content)

    tree_files: dict[str, tuple[int, str]] = {}
    tree_dirs: set[str] = set()
    for p in (pub / "bundle").glob("**/*"):
        rel = p.relative_to(pub / "bundle").as_posix()
        if p.is_file():
            tree_files[rel] = (p.stat().st_size, _sha256(p.read_bytes()))
        elif p.is_dir():
            tree_dirs.add(rel)

    bundle_sha = compute_tree_sha256(tree_files, tree_dirs)

    anchor_data = ExternalChecksumAnchor(
        schema_version="1.0",
        suite_id=manifest.suite_id,
        checksum_manifest_path="checksums.json",
        checksum_manifest_sha256=_sha256(checksums_content),
        authenticity_claimed=False,
    )
    anchor_bytes = canonical_json_bytes(anchor_data)
    (pub / "anchor.json").write_bytes(anchor_bytes)

    role_map = {
        "checksums.json": "checksums",
        "release-metadata.json": "release_metadata",
        "fixture.manifest.json": "fixture_manifest",
        "fixture.acceptance.json": "fixture_acceptance",
        "diff-policy.json": "diff_policy",
        "plan.json": "plan",
        "workflow.yaml": "spec",
        "provider-coverage.json": "other_json",
        "suite.json": "other_json",
        "suite.md": "report_markdown",
    }
    tree_artifacts = [
        ExpectedFileArtifact(
            role=role_map.get(rel, "report_markdown" if rel.endswith(".md") else "other_json"),
            path=rel,
            byte_count=size,
            sha256=sha,
        )
        for rel, (size, sha) in tree_files.items()
    ]
    tree_artifacts.sort(key=lambda x: x.path)

    if artifact_path.endswith(".jsonl"):
        role = "campaign"
    elif artifact_path.endswith(".json"):
        role = "report_json"
        try:
            data = json.loads(artifact_bytes)
            artifact_bytes = canonical_inventory_json_bytes(data)
        except Exception:
            role = "report_markdown"
    else:
        role = "report_markdown"

    artifact = root / artifact_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(artifact_bytes)

    files = [
        ExpectedFileArtifact(
            role=role,
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
                reference_id="accepted-manifest-ref",
                kind="accepted_manifest",
                path="public/manifest.json",
                byte_count=len(manifest_bytes),
                sha256=_sha256(manifest_bytes),
                description="accepted manifest",
            ),
            AuthorityReference(
                reference_id="authority",
                kind="tracked_closeout",
                path="authority.txt",
                byte_count=len(authority),
                sha256=_sha256(authority),
                description="synthetic authority",
            ),
        ],
        release_entries=[
            ReleaseEntry(
                release_id="release-current",
                artifact_reviewed_commit=COMMIT,
                commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
                classification=ReleaseClassification.ACCEPTED_CURRENT,
                verification_profile="phase6_public_suite",
                declaration_basis="synthetic accepted release",
                accepted_manifest_reference_id="accepted-manifest-ref",
                file_artifacts=[
                    ExpectedFileArtifact(
                        role="suite_manifest",
                        path="public/manifest.json",
                        byte_count=len(manifest_bytes),
                        sha256=_sha256(manifest_bytes),
                    ),
                    ExpectedFileArtifact(
                        role="checksums",
                        path="public/checksums.json",
                        byte_count=len(checksums_content),
                        sha256=_sha256(checksums_content),
                    ),
                    ExpectedFileArtifact(
                        role="external_anchor",
                        path="public/anchor.json",
                        byte_count=len(anchor_bytes),
                        sha256=_sha256(anchor_bytes),
                    ),
                ],
                trees=[
                    ExpectedTree(
                        role="bundle_root",
                        root_path="public/bundle",
                        allowed_directories=sorted(tree_dirs),
                        file_artifacts=tree_artifacts,
                        expected_file_count=len(tree_artifacts),
                        tree_sha256=bundle_sha,
                    )
                ],
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
    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_ENTRIES", 0)
    with pytest.raises(InventorySafetyError):
        _observe_test_tree(tmp_path, tree)

    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_ENTRIES", 4096)
    monkeypatch.setattr("agentlab.phase7_inventory.MAX_TREE_BYTES", 1)
    with pytest.raises(InventorySafetyError):
        _observe_test_tree(tmp_path, tree)


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


def test_expected_tree_forbids_optional_file_artifacts() -> None:
    with pytest.raises(ValueError, match="required=True"):
        ExpectedTree(
            role="bundle_root",
            root_path="bundle",
            file_artifacts=[
                ExpectedFileArtifact(
                    role="report_json",
                    path="entry.json",
                    byte_count=10,
                    sha256=_sha256(b"content"),
                    required=False,
                )
            ],
            expected_file_count=1,
            tree_sha256=_sha256(b"digest"),
        )


def _valid_campaign_bytes(provider_call_count: int = 3, unknown_runs: int = 0) -> bytes:
    events = [
        {
            "schema_version": "1.2",
            "event_type": "campaign_started",
            "sequence": 0,
            "occurred_at": "2026-01-01T00:00:00.000000Z",
            "planned_provider_call_count": 3,
        },
        {
            "schema_version": "1.2",
            "event_type": "campaign_finished",
            "sequence": 1,
            "occurred_at": "2026-01-01T00:01:00.000000Z",
            "stop_reason": "completed",
            "provider_call_count": provider_call_count,
            "provider_call_count_unknown_runs": unknown_runs,
        },
    ]
    return b"".join(json.dumps(e, sort_keys=True).encode("utf-8") + b"\n" for e in events)


def test_provider_accounting_uses_phase6_public_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_phase6_public import _canonical_jsonl_line, _cross_artifact_case

    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    _source, _spec, _plan, campaign, _artifacts, _recordings = _cross_artifact_case(case_dir)
    campaign_bytes = b"".join(_canonical_jsonl_line(e) for e in campaign.events)
    report_bytes = canonical_inventory_json_bytes({"reviewed_commit": COMMIT})
    (tmp_path / "report.json").write_bytes(report_bytes)

    request_path = _request(
        tmp_path,
        artifact_path="campaign.jsonl",
        artifact_bytes=campaign_bytes,
        commit=COMMIT,
    )
    raw_req = json.loads(request_path.read_text())
    raw_req["campaign_entries"][0]["file_artifacts"].append(
        {
            "role": "report_json",
            "path": "report.json",
            "byte_count": len(report_bytes),
            "sha256": _sha256(report_bytes),
        }
    )
    request_path.write_bytes(
        canonical_inventory_json_bytes(EvidenceInventoryRequest.model_validate(raw_req))
    )

    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: COMMIT,
    )
    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )
    assert inventory.summary.provider_call_count_observed == 2
    assert inventory.summary.provider_call_count_unknown_runs == 0
    assert inventory.summary.campaigns_without_total == 0


def test_provider_accounting_uses_one_canonical_campaign_finished_event(tmp_path: Path) -> None:
    from test_phase6_public import _canonical_jsonl_line, _cross_artifact_case

    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    _source, _spec, _plan, campaign, _artifacts, _recordings = _cross_artifact_case(case_dir)
    campaign_bytes = b"".join(_canonical_jsonl_line(e) for e in campaign.events)
    evidence_mirror = campaign_bytes.replace(b"completed", b"failed")

    manifest_art = ExpectedFileArtifact(
        role="suite_manifest",
        path="public/manifest.json",
        byte_count=10,
        sha256=_sha256(b"manifest"),
    )
    checksums_art = ExpectedFileArtifact(
        role="checksums",
        path="public/checksums.json",
        byte_count=10,
        sha256=_sha256(b"checksums"),
    )
    anchor_art = ExpectedFileArtifact(
        role="external_anchor",
        path="public/anchor.json",
        byte_count=10,
        sha256=_sha256(b"anchor"),
    )
    tree = ExpectedTree(
        role="bundle_root",
        root_path="public/bundle",
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=_sha256(b"bundle"),
    )

    request = EvidenceInventoryRequest(
        schema_version="1.0",
        inventory_id="inv",
        authoritative=False,
        scope=InventoryScope.PHASE6,
        source_of_truth_references=[
            AuthorityReference(
                reference_id="accepted-ref",
                kind="accepted_manifest",
                path="public/manifest.json",
                byte_count=10,
                sha256=_sha256(b"manifest"),
                description="accepted manifest",
            )
        ],
        release_entries=[
            ReleaseEntry(
                release_id="rel-current",
                artifact_reviewed_commit=HEAD,
                commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
                classification=ReleaseClassification.ACCEPTED_CURRENT,
                verification_profile="phase6_public_suite",
                declaration_basis="basis",
                accepted_manifest_reference_id="accepted-ref",
                file_artifacts=[manifest_art, checksums_art, anchor_art],
                trees=[tree],
            )
        ],
        campaign_entries=[
            CampaignEntry(
                campaign_id="campaign-a",
                artifact_reviewed_commit=HEAD,
                commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
                classification=CampaignClassification.AUDIT_ONLY_FAILURE,
                included_in_primary_denominator=False,
                verification_profile="declared_artifact_set",
                declaration_basis="basis",
                file_artifacts=[
                    ExpectedFileArtifact(
                        role="campaign",
                        path="campaign.jsonl",
                        byte_count=len(campaign_bytes),
                        sha256=_sha256(campaign_bytes),
                    )
                ],
            )
        ],
    )
    campaign_outputs = [
        InventoryCampaignEntry(
            campaign_id="campaign-a",
            artifact_reviewed_commit=HEAD,
            commit_verification_mode=CommitVerificationMode.INTERNAL_IF_PRESENT,
            commit_verification=IntegrityState.VERIFIED,
            classification=CampaignClassification.AUDIT_ONLY_FAILURE,
            included_in_primary_denominator=False,
            verification_profile="declared_artifact_set",
            storage_state=StorageState.PRESENT,
            integrity_state=IntegrityState.VERIFIED,
            artifact_observations=[
                ArtifactObservation(
                    role="campaign",
                    path="campaign.jsonl",
                    kind="file",
                    required=True,
                    storage_state=StorageState.PRESENT,
                    integrity_state=IntegrityState.VERIFIED,
                    expected_byte_count=len(campaign_bytes),
                    observed_byte_count=len(campaign_bytes),
                    expected_sha256=_sha256(campaign_bytes),
                    observed_sha256=_sha256(campaign_bytes),
                )
            ],
        )
    ]
    subject_contents = {
        ("campaign", "campaign-a"): {
            "campaign.jsonl": campaign_bytes,
            "evidence.json": evidence_mirror,
        }
    }

    assert _provider_counts(
        request=request,
        campaign_outputs=campaign_outputs,
        subject_contents=subject_contents,
    ) == (2, 0, 0)


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


def test_receipt_cannot_bind_another_campaign_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a_bytes = canonical_inventory_json_bytes({"reviewed_commit": HEAD})
    b_bytes = canonical_inventory_json_bytes({"reviewed_commit": HEAD})
    (tmp_path / "a.json").write_bytes(a_bytes)
    (tmp_path / "b.json").write_bytes(b_bytes)
    receipt = ExternalCopyReceipt(
        schema_version="1.0",
        subject_kind="campaign",
        subject_id="campaign-a",
        subject_digest=_sha256(b"wrong_subject_digest"),
        created_at="2026-01-01T00:00:00.000000Z",
    )
    receipt_bytes = canonical_inventory_json_bytes(receipt)
    (tmp_path / "receipt.json").write_bytes(receipt_bytes)

    request_path = _request(tmp_path, artifact_path="a.json", artifact_bytes=a_bytes)
    raw_req = json.loads(request_path.read_text(encoding="utf-8"))
    raw_req["campaign_entries"] = [
        {
            "campaign_id": "campaign-a",
            "artifact_reviewed_commit": HEAD,
            "commit_verification_mode": "internal_if_present",
            "classification": "audit_only_failure",
            "included_in_primary_denominator": False,
            "verification_profile": "declared_artifact_set",
            "declaration_basis": "declared",
            "file_artifacts": [
                {
                    "role": "report_json",
                    "path": "a.json",
                    "byte_count": len(a_bytes),
                    "sha256": _sha256(a_bytes),
                }
            ],
        },
        {
            "campaign_id": "campaign-b",
            "artifact_reviewed_commit": HEAD,
            "commit_verification_mode": "internal_if_present",
            "classification": "audit_only_failure",
            "included_in_primary_denominator": False,
            "verification_profile": "declared_artifact_set",
            "declaration_basis": "declared",
            "file_artifacts": [
                {
                    "role": "report_json",
                    "path": "b.json",
                    "byte_count": len(b_bytes),
                    "sha256": _sha256(b_bytes),
                }
            ],
        },
    ]
    raw_req["retention_expectations"] = [
        {
            "subject_kind": "campaign",
            "subject_id": "campaign-a",
            "expected_retention_state": "external_copy_receipt_verified",
            "external_copy_receipt": {
                "role": "receipt",
                "path": "receipt.json",
                "byte_count": len(receipt_bytes),
                "sha256": _sha256(receipt_bytes),
            },
            "declaration_basis": "receipt declaration",
        }
    ]
    request_path.write_bytes(
        canonical_inventory_json_bytes(EvidenceInventoryRequest.model_validate(raw_req))
    )

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


def test_release_only_finding_does_not_propagate_to_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_bytes = canonical_inventory_json_bytes({"reviewed_commit": HEAD})
    request_path = _request(tmp_path, artifact_path="artifact.json", artifact_bytes=artifact_bytes)
    monkeypatch.setattr(
        "agentlab.phase7_inventory._observe_execution_repository_head",
        lambda _root: HEAD,
    )
    inventory = verify_inventory_request(
        request_path=request_path,
        repository_root=tmp_path,
        confirm_local_execution=True,
    )
    assert inventory.campaigns[0].integrity_state.value == "verified"


def test_accepted_manifest_one_to_one_and_orphan_rejection() -> None:
    manifest_art = ExpectedFileArtifact(
        role="suite_manifest",
        path="public/manifest.json",
        byte_count=10,
        sha256=_sha256(b"manifest"),
    )
    checksums_art = ExpectedFileArtifact(
        role="checksums",
        path="public/checksums.json",
        byte_count=10,
        sha256=_sha256(b"checksums"),
    )
    anchor_art = ExpectedFileArtifact(
        role="external_anchor",
        path="public/anchor.json",
        byte_count=10,
        sha256=_sha256(b"anchor"),
    )
    rel_files = [manifest_art, checksums_art, anchor_art]
    ref1 = AuthorityReference(
        reference_id="accepted-1",
        kind="accepted_manifest",
        path="public/manifest.json",
        byte_count=10,
        sha256=_sha256(b"manifest"),
        description="ref 1",
    )
    ref2 = AuthorityReference(
        reference_id="accepted-2",
        kind="accepted_manifest",
        path="public/manifest2.json",
        byte_count=10,
        sha256=_sha256(b"manifest2"),
        description="ref 2",
    )
    tree = ExpectedTree(
        role="bundle_root",
        root_path="public/bundle",
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=_sha256(b"bundle"),
    )
    rel1 = ReleaseEntry(
        release_id="rel-current",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
        classification=ReleaseClassification.ACCEPTED_CURRENT,
        verification_profile="phase6_public_suite",
        declaration_basis="basis",
        accepted_manifest_reference_id="accepted-1",
        file_artifacts=rel_files,
        trees=[tree],
    )
    # Reject sharing accepted_manifest reference across two accepted releases
    rel2_shared = ReleaseEntry(
        release_id="rel-superseded",
        artifact_reviewed_commit=HEAD,
        commit_verification_mode=CommitVerificationMode.INTERNAL_REQUIRED,
        classification=ReleaseClassification.ACCEPTED_SUPERSEDED,
        superseded_by="rel-current",
        verification_profile="phase6_public_suite",
        declaration_basis="basis",
        accepted_manifest_reference_id="accepted-1",
        file_artifacts=rel_files,
        trees=[tree],
    )
    with pytest.raises(ValueError, match="unique accepted_manifest"):
        EvidenceInventoryRequest(
            schema_version="1.0",
            inventory_id="req-shared",
            authoritative=False,
            scope=InventoryScope.PHASE6,
            source_of_truth_references=[ref1],
            release_entries=[rel1, rel2_shared],
        )

    # Reject orphan accepted_manifest reference
    with pytest.raises(ValueError, match="unique accepted_manifest"):
        EvidenceInventoryRequest(
            schema_version="1.0",
            inventory_id="req-orphan",
            authoritative=False,
            scope=InventoryScope.PHASE6,
            source_of_truth_references=[ref1, ref2],
            release_entries=[rel1],
        )


def test_tree_directory_limits_and_fd_safety(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for i in range(300):
        (bundle / f"dir_{i}").mkdir()
    tree = ExpectedTree(
        role="bundle_root",
        root_path="bundle",
        allowed_directories=sorted([f"dir_{i}" for i in range(300)]),
        file_artifacts=[],
        expected_file_count=0,
        tree_sha256=_sha256(b"empty"),
    )
    with pytest.raises(InventorySafetyError, match="bounded directory limit"):
        _observe_test_tree(tmp_path, tree)
