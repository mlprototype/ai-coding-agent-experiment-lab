from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import pytest

from agentlab.models import WorkspaceLifecycle
from agentlab.workspace import (
    SnapshotError,
    WorkspaceError,
    build_diff_evidence,
    prepare_disposable_workspace,
    protect_evidence_inputs,
    remove_disposable_workspace,
    snapshot_directory,
    validate_fixture_source,
)


def _fixture_case(tmp_path: Path) -> tuple[Path, Path]:
    case = tmp_path / "case"
    fixture = case / "fixtures" / "sample"
    fixture.mkdir(parents=True)
    (fixture / "input.txt").write_text("before\n", encoding="utf-8")
    spec_path = case / "experiment.yaml"
    spec_path.write_text("schema_version: '1.0'\n", encoding="utf-8")
    return spec_path, fixture


def test_fixture_is_copied_and_source_is_not_modified(tmp_path: Path) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")

    disposable = prepare_disposable_workspace(source, source_snapshot)
    (disposable.workspace / "input.txt").write_text("after\n", encoding="utf-8")
    (disposable.workspace / "created.txt").write_text("new\n", encoding="utf-8")

    assert (fixture / "input.txt").read_text(encoding="utf-8") == "before\n"
    assert not (fixture / "created.txt").exists()
    temporary_root = disposable.temporary_root
    removed, error = remove_disposable_workspace(disposable)
    assert removed is True
    assert error is None
    assert not temporary_root.exists()


def test_prepare_failure_reports_removed_workspace_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")
    temporary_root = tmp_path / "controlled-run-root"
    temporary_root.mkdir()

    monkeypatch.setattr(
        "agentlab.workspace.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root),
    )

    def fail_copy(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError("synthetic copy failure")

    monkeypatch.setattr("agentlab.workspace.shutil.copytree", fail_copy)

    with pytest.raises(WorkspaceError) as error:
        prepare_disposable_workspace(source, source_snapshot)

    assert error.value.lifecycle is WorkspaceLifecycle.REMOVED
    assert not temporary_root.exists()


def test_workspace_creation_failure_reports_not_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")

    def fail_create(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError("synthetic create failure")

    monkeypatch.setattr("agentlab.workspace.tempfile.mkdtemp", fail_create)

    with pytest.raises(WorkspaceError) as error:
        prepare_disposable_workspace(source, source_snapshot)

    assert error.value.lifecycle is WorkspaceLifecycle.NOT_CREATED


def test_workspace_resolve_failure_removes_created_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")
    temporary_root = tmp_path / "unresolved-run-root"
    resolve = Path.resolve

    monkeypatch.setattr(
        "agentlab.workspace.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root.mkdir() or temporary_root),
    )

    def fail_created_root_resolve(
        path: Path,
        strict: bool = False,
    ) -> Path:
        if path == temporary_root:
            raise OSError("synthetic workspace root resolve failure")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_created_root_resolve)

    with pytest.raises(WorkspaceError) as error:
        prepare_disposable_workspace(source, source_snapshot)

    assert error.value.lifecycle is WorkspaceLifecycle.REMOVED
    assert not temporary_root.exists()


def test_workspace_resolve_and_cleanup_failure_reports_cleanup_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")
    temporary_root = tmp_path / "unresolved-uncleaned-run-root"
    resolve = Path.resolve

    monkeypatch.setattr(
        "agentlab.workspace.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root.mkdir() or temporary_root),
    )

    def fail_created_root_resolve(
        path: Path,
        strict: bool = False,
    ) -> Path:
        if path == temporary_root:
            raise OSError("synthetic workspace root resolve failure")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_created_root_resolve)
    monkeypatch.setattr(
        "agentlab.workspace._remove_temporary_root",
        lambda _path: (False, "synthetic cleanup failure"),
    )

    with pytest.raises(WorkspaceError) as error:
        prepare_disposable_workspace(source, source_snapshot)

    assert error.value.lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
    assert temporary_root.is_dir()
    temporary_root.rmdir()


def test_prepare_cleanup_failure_reports_cleanup_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture = _fixture_case(tmp_path)
    source, source_snapshot = validate_fixture_source(spec_path, "fixtures/sample")
    temporary_root = tmp_path / "uncleaned-run-root"
    temporary_root.mkdir()
    monkeypatch.setattr(
        "agentlab.workspace.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root),
    )

    def fail_copy(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError("synthetic copy failure")

    monkeypatch.setattr("agentlab.workspace.shutil.copytree", fail_copy)
    monkeypatch.setattr(
        "agentlab.workspace._remove_temporary_root",
        lambda _path: (False, "synthetic cleanup failure"),
    )

    with pytest.raises(WorkspaceError) as error:
        prepare_disposable_workspace(source, source_snapshot)

    assert error.value.lifecycle is WorkspaceLifecycle.CLEANUP_FAILED
    temporary_root.rmdir()


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    alias = fixture.parent / "alias"
    alias.symlink_to(fixture, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symlink"):
        validate_fixture_source(spec_path, "fixtures/alias")


def test_nested_symlink_is_rejected(tmp_path: Path) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    (fixture / "alias.txt").symlink_to(fixture / "input.txt")

    with pytest.raises(WorkspaceError, match="symlink"):
        validate_fixture_source(spec_path, "fixtures/sample")


@pytest.mark.skipif(os.name != "posix", reason="FIFO creation is POSIX-only")
def test_special_file_is_rejected(tmp_path: Path) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    os.mkfifo(fixture / "events.fifo")

    with pytest.raises(WorkspaceError, match="special file"):
        validate_fixture_source(spec_path, "fixtures/sample")


def test_snapshot_rejects_non_directory_root(tmp_path: Path) -> None:
    regular_file = tmp_path / "fixture.txt"
    regular_file.write_text("content\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match="directory"):
        snapshot_directory(regular_file)


def test_text_diff_has_stable_paths_and_exact_line_counts(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "b.txt").write_text("old\n", encoding="utf-8")
    (fixture / "z.txt").write_text("delete\n", encoding="utf-8")
    before = snapshot_directory(fixture)
    (fixture / "b.txt").write_text("new\nextra\n", encoding="utf-8")
    (fixture / "a.txt").write_text("added\n", encoding="utf-8")
    (fixture / "z.txt").unlink()
    after = snapshot_directory(fixture)

    diff = build_diff_evidence(before, after, max_diff_bytes=65536)

    assert diff.changed_files == ["a.txt", "b.txt", "z.txt"]
    assert diff.added_lines == 3
    assert diff.deleted_lines == 2
    assert diff.line_counts_complete is True
    assert "--- /dev/null" in diff.unified_diff
    assert "+++ b/a.txt" in diff.unified_diff


def test_binary_diff_preserves_path_without_inventing_line_counts(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    binary = fixture / "binary.dat"
    binary.write_bytes(b"\x00old")
    before = snapshot_directory(fixture)
    binary.write_bytes(b"\x00new")
    after = snapshot_directory(fixture)

    diff = build_diff_evidence(before, after, max_diff_bytes=65536)

    assert diff.changed_files == ["binary.dat"]
    assert diff.binary_files == ["binary.dat"]
    assert diff.line_counts_complete is False
    assert diff.added_lines is None
    assert diff.deleted_lines is None
    assert "Binary files" in diff.unified_diff


def test_unified_diff_is_bounded_without_losing_complete_counts(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    changed = fixture / "large.txt"
    changed.write_text("old\n", encoding="utf-8")
    before = snapshot_directory(fixture)
    changed.write_text("".join(f"new-{index}\n" for index in range(100)), encoding="utf-8")
    after = snapshot_directory(fixture)

    diff = build_diff_evidence(before, after, max_diff_bytes=64)

    assert diff.diff_truncated is True
    assert len(diff.unified_diff.encode()) <= 64
    assert diff.line_counts_complete is True
    assert diff.added_lines == 100
    assert diff.deleted_lines == 1


def test_evidence_output_cannot_be_inside_fixture_source(tmp_path: Path) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    source, snapshot = validate_fixture_source(spec_path, "fixtures/sample")

    with pytest.raises(WorkspaceError, match="inside the fixture"):
        protect_evidence_inputs(
            fixture / "evidence.json",
            spec_path=spec_path,
            recording_path=None,
            fixture_source=source,
            fixture_snapshot=snapshot,
        )


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_evidence_output_alias_cannot_overwrite_fixture_file(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    spec_path, fixture = _fixture_case(tmp_path)
    source, snapshot = validate_fixture_source(spec_path, "fixtures/sample")
    output = tmp_path / "evidence.json"
    if alias_kind == "symlink":
        output.symlink_to(fixture / "input.txt")
    else:
        output.hardlink_to(fixture / "input.txt")

    with pytest.raises(WorkspaceError, match="fixture source"):
        protect_evidence_inputs(
            output,
            spec_path=spec_path,
            recording_path=None,
            fixture_source=source,
            fixture_snapshot=snapshot,
        )
