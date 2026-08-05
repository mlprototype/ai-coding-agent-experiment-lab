from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
    InventoryContractError,
    InventoryScope,
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
    tree: ExpectedTree | None = None,
) -> Path:
    authority = b"synthetic authority\n"
    (root / "authority.txt").write_bytes(authority)
    artifact = root / artifact_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(artifact_bytes)
    files = (
        []
        if tree is not None
        else [
            ExpectedFileArtifact(
                role="report_json",
                path=artifact_path,
                byte_count=len(artifact_bytes),
                sha256=_sha256(artifact_bytes),
            )
        ]
    )
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
                trees=[] if tree is None else [tree],
            )
        ],
    )
    request_path = root / "request.json"
    request_path.write_bytes(canonical_inventory_json_bytes(request))
    return request_path


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
    monkeypatch: pytest.MonkeyPatch,
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
    request_path = _request(
        tmp_path,
        artifact_path="bundle/entry.json",
        artifact_bytes=content,
        tree=tree,
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

    assert inventory.verification_status.value == "verified"
    assert inventory.campaigns[0].artifact_observations[0].kind == "tree"


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
