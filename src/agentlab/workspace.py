"""Trusted-fixture validation, disposable workspaces, and filesystem Evidence."""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentlab.models import DiffEvidence


class WorkspaceError(ValueError):
    """Raised when a fixture or disposable workspace cannot be handled safely."""


class SnapshotError(ValueError):
    """Raised when a directory cannot be represented as regular-file Evidence."""


@dataclass(frozen=True)
class DirectorySnapshot:
    """A deterministic in-memory snapshot for the small trusted Phase 2 fixtures."""

    files: dict[str, bytes]
    directories: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class DisposableWorkspace:
    """Paths belonging to one system-temporary runner invocation."""

    temporary_root: Path
    workspace: Path
    environment_root: Path
    initial_snapshot: DirectorySnapshot


def _relative_posix_path(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SnapshotError("fixture paths must be valid UTF-8") from error
    if any(character in relative for character in ("\x00", "\n", "\r")):
        raise SnapshotError("fixture paths must not contain control characters")
    return relative


def _read_regular_file(path: Path, relative: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError(f"fixture entry changed to a special file: {relative}")
        with os.fdopen(file_descriptor, "rb") as source_file:
            file_descriptor = None
            return source_file.read()
    except SnapshotError:
        raise
    except OSError as error:
        raise SnapshotError(
            f"could not read fixture file {relative!r}: {type(error).__name__}"
        ) from error
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)


def _snapshot_tree(root: Path) -> tuple[dict[str, bytes], tuple[str, ...]]:
    files: dict[str, bytes] = {}
    directories: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise SnapshotError(f"could not scan fixture tree: {type(error).__name__}") from error

        for entry in entries:
            path = Path(entry.path)
            relative = _relative_posix_path(root, path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SnapshotError(
                    f"could not inspect fixture entry {relative!r}: {type(error).__name__}"
                ) from error

            if stat.S_ISLNK(metadata.st_mode):
                raise SnapshotError(f"fixture symlink is not allowed: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotError(f"fixture special file is not allowed: {relative}")

            files[relative] = _read_regular_file(path, relative)

    visit(root)
    return files, tuple(sorted(directories))


def _snapshot_hash(files: dict[str, bytes], directories: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in directories:
        encoded_path = relative.encode("utf-8")
        digest.update(b"D\0")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
    for relative, content in sorted(files.items()):
        encoded_path = relative.encode("utf-8")
        digest.update(b"F\0")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def snapshot_directory(root: Path) -> DirectorySnapshot:
    """Snapshot only regular files/directories without following symlinks."""
    try:
        metadata = root.lstat()
    except OSError as error:
        raise SnapshotError(f"could not inspect fixture root: {type(error).__name__}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise SnapshotError("fixture root symlink is not allowed")
    if not stat.S_ISDIR(metadata.st_mode):
        raise SnapshotError("fixture root must be a directory")

    files, directories = _snapshot_tree(root)
    return DirectorySnapshot(
        files=files,
        directories=directories,
        sha256=_snapshot_hash(files, directories),
    )


def validate_fixture_source(
    spec_path: Path,
    configured_path: str,
) -> tuple[Path, DirectorySnapshot]:
    """Resolve and validate a relative fixture without traversing symlink components."""
    base = spec_path.parent
    current = base
    for component in PurePosixPath(configured_path).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise WorkspaceError(
                f"fixture path is unavailable at {component!r}: {type(error).__name__}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceError(f"fixture path component is a symlink: {component}")

    try:
        source = current.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkspaceError(f"could not resolve fixture path: {type(error).__name__}") from error

    try:
        snapshot = snapshot_directory(source)
    except SnapshotError as error:
        raise WorkspaceError(str(error)) from error
    return source, snapshot


def _remove_temporary_root(path: Path) -> tuple[bool, str | None]:
    try:
        shutil.rmtree(path)
        return True, None
    except OSError as first_error:
        # A trusted command may have made its own files read-only. Make one bounded
        # recovery attempt inside the exact temporary root.
        try:
            with suppress(OSError):
                os.chmod(path, 0o700)
            for directory, child_directories, _files in os.walk(
                path,
                topdown=True,
                followlinks=False,
            ):
                current = Path(directory)
                if current.is_symlink():
                    child_directories.clear()
                    continue
                with suppress(OSError):
                    os.chmod(current, 0o700)
                child_directories[:] = [
                    name
                    for name in child_directories
                    if not (current / name).is_symlink()
                ]
            shutil.rmtree(path)
            return True, None
        except OSError as second_error:
            return (
                False,
                "temporary workspace cleanup failed: "
                f"{type(first_error).__name__}/{type(second_error).__name__}",
            )
def prepare_disposable_workspace(
    source: Path,
    source_snapshot: DirectorySnapshot,
) -> DisposableWorkspace:
    """Copy a validated fixture and allocate isolated HOME/cache/temp directories."""
    try:
        temporary_root = Path(tempfile.mkdtemp(prefix="agentlab-run-")).resolve()
    except OSError as error:
        raise WorkspaceError(
            f"could not create temporary workspace: {type(error).__name__}"
        ) from error

    workspace = temporary_root / "workspace"
    environment_root = temporary_root / "environment"
    try:
        shutil.copytree(source, workspace, symlinks=True)
        copied_snapshot = snapshot_directory(workspace)
        if copied_snapshot.sha256 != source_snapshot.sha256:
            raise WorkspaceError("fixture changed while it was copied")
        for name in ("home", "tmp", "cache"):
            (environment_root / name).mkdir(parents=True, exist_ok=True)
    except (OSError, SnapshotError) as error:
        _remove_temporary_root(temporary_root)
        raise WorkspaceError(
            f"could not prepare disposable workspace: {type(error).__name__}: {error}"
        ) from error
    except WorkspaceError:
        _remove_temporary_root(temporary_root)
        raise

    return DisposableWorkspace(
        temporary_root=temporary_root,
        workspace=workspace,
        environment_root=environment_root,
        initial_snapshot=copied_snapshot,
    )


def remove_disposable_workspace(workspace: DisposableWorkspace) -> tuple[bool, str | None]:
    """Remove the exact system-temporary root allocated for one run."""
    return _remove_temporary_root(workspace.temporary_root)


def _decode_text(content: bytes) -> str | None:
    if b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _line_delta(old_lines: list[str], new_lines: list[str]) -> tuple[int, int]:
    added = 0
    deleted = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation in {"replace", "delete"}:
            deleted += old_end - old_start
        if operation in {"replace", "insert"}:
            added += new_end - new_start
    return added, deleted


def _render_unified_diff(
    relative: str,
    old_text: str,
    new_text: str,
    *,
    existed_before: bool,
    exists_after: bool,
) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    from_file = f"a/{relative}" if existed_before else "/dev/null"
    to_file = f"b/{relative}" if exists_after else "/dev/null"
    rendered: list[str] = []
    for line in difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=from_file,
        tofile=to_file,
        lineterm="",
    ):
        rendered.append(line if line.endswith("\n") else f"{line}\n")
    return "".join(rendered)


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def build_diff_evidence(
    before: DirectorySnapshot,
    after: DirectorySnapshot,
    *,
    max_diff_bytes: int,
) -> DiffEvidence:
    """Build stable, bounded content diff Evidence without invoking Git."""
    changed_files = sorted(
        relative
        for relative in set(before.files) | set(after.files)
        if before.files.get(relative) != after.files.get(relative)
    )
    binary_files: list[str] = []
    added_lines = 0
    deleted_lines = 0
    patches: list[str] = []

    for relative in changed_files:
        old_content = before.files.get(relative, b"")
        new_content = after.files.get(relative, b"")
        old_text = _decode_text(old_content)
        new_text = _decode_text(new_content)
        if old_text is None or new_text is None:
            binary_files.append(relative)
            from_file = f"a/{relative}" if relative in before.files else "/dev/null"
            to_file = f"b/{relative}" if relative in after.files else "/dev/null"
            patches.append(f"Binary files {from_file} and {to_file} differ\n")
            continue

        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        file_added, file_deleted = _line_delta(old_lines, new_lines)
        added_lines += file_added
        deleted_lines += file_deleted
        patches.append(
            _render_unified_diff(
                relative,
                old_text,
                new_text,
                existed_before=relative in before.files,
                exists_after=relative in after.files,
            )
        )

    unified_diff, diff_truncated = _truncate_utf8("".join(patches), max_diff_bytes)
    line_counts_complete = not binary_files
    return DiffEvidence(
        changed_files=changed_files,
        binary_files=binary_files,
        added_lines=added_lines if line_counts_complete else None,
        deleted_lines=deleted_lines if line_counts_complete else None,
        unified_diff=unified_diff,
        diff_truncated=diff_truncated,
        line_counts_complete=line_counts_complete,
        collection_error=None,
    )


def incomplete_diff_evidence(error: str) -> DiffEvidence:
    """Represent a diff collection failure without inventing file or line counts."""
    return DiffEvidence(
        changed_files=[],
        binary_files=[],
        added_lines=None,
        deleted_lines=None,
        unified_diff="",
        diff_truncated=False,
        line_counts_complete=False,
        collection_error=error,
    )


def paths_refer_to_same_file(first: Path, second: Path) -> bool:
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
    except (OSError, RuntimeError):
        pass
    try:
        return first.samefile(second)
    except OSError:
        return False


def protect_evidence_inputs(
    output_path: Path,
    *,
    spec_path: Path,
    recording_path: Path | None,
    fixture_source: Path,
    fixture_snapshot: DirectorySnapshot,
) -> None:
    """Reject lexical, symlink, and hard-link aliases of all Phase 2 inputs."""
    try:
        resolved_output = output_path.resolve(strict=False)
        resolved_fixture = fixture_source.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkspaceError(
            f"could not resolve Evidence output path: {type(error).__name__}"
        ) from error

    if resolved_output == resolved_fixture or resolved_output.is_relative_to(resolved_fixture):
        raise WorkspaceError("Evidence output must not be inside the fixture source")

    protected_inputs: list[tuple[str, Path]] = [("ExperimentSpec", spec_path)]
    if recording_path is not None:
        protected_inputs.append(("Replay Recording", recording_path))
    protected_inputs.extend(
        ("fixture source", fixture_source / relative)
        for relative in fixture_snapshot.files
    )

    for input_name, input_path in protected_inputs:
        if paths_refer_to_same_file(output_path, input_path):
            raise WorkspaceError(
                f"Evidence output must not overwrite or alias the input {input_name}: {input_path}"
            )
