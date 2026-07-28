"""Bounded POSIX subprocess execution for trusted Phase 2 quality gates."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, cast

from agentlab.models import (
    CommandEvidence,
    CommandStatus,
    FailureKind,
    GateKind,
    RunnerSettings,
    TerminationEvidence,
    TerminationReason,
)

_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)
_READ_CHUNK_BYTES = 64 * 1024
_POLL_INTERVAL_SECONDS = 0.01


class UnsupportedRunnerPlatformError(RuntimeError):
    """Raised before command execution when process-group guarantees are unavailable."""


@dataclass(frozen=True)
class CommandRunResult:
    evidence: CommandEvidence
    harness_failure: FailureKind | None


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._content = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        remaining = self._limit - len(self._content)
        if remaining > 0:
            self._content.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def decode(self) -> tuple[str, bool]:
        content = bytes(self._content)
        try:
            return content.decode("utf-8"), False
        except UnicodeDecodeError:
            return content.decode("utf-8", errors="replace"), True


class _PipeDrainer:
    def __init__(
        self,
        stdout: IO[bytes],
        stderr: IO[bytes],
        *,
        stdout_collector: _BoundedBytes,
        stderr_collector: _BoundedBytes,
    ) -> None:
        self._selector = selectors.DefaultSelector()
        self.stdout = stdout_collector
        self.stderr = stderr_collector
        self.error: str | None = None
        self._register(stdout, self.stdout)
        self._register(stderr, self.stderr)

    def _register(self, stream: IO[bytes], collector: _BoundedBytes) -> None:
        os.set_blocking(stream.fileno(), False)
        self._selector.register(stream, selectors.EVENT_READ, collector)

    @property
    def has_open_streams(self) -> bool:
        return bool(self._selector.get_map())

    def drain(self, timeout: float) -> None:
        if not self.has_open_streams:
            return
        try:
            ready = self._selector.select(max(timeout, 0.0))
        except OSError as error:
            self.error = f"output selector failed: {type(error).__name__}"
            return

        for key, _events in ready:
            stream = cast(IO[bytes], key.fileobj)
            collector = cast(_BoundedBytes, key.data)
            while True:
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                except BlockingIOError:
                    break
                except OSError as error:
                    self.error = f"output drain failed: {type(error).__name__}"
                    self._close_stream(stream)
                    break
                if not chunk:
                    self._close_stream(stream)
                    break
                collector.feed(chunk)

    def _close_stream(self, stream: IO[bytes]) -> None:
        with suppress(KeyError, ValueError):
            self._selector.unregister(stream)
        with suppress(OSError):
            stream.close()

    def close(self) -> None:
        for key in list(self._selector.get_map().values()):
            self._close_stream(cast(IO[bytes], key.fileobj))
        self._selector.close()


def ensure_runner_platform_supported() -> None:
    if (
        (sys.platform != "darwin" and not sys.platform.startswith("linux"))
        or os.name != "posix"
        or not hasattr(os, "killpg")
        or not hasattr(os, "setsid")
        or not hasattr(signal, "SIGKILL")
        or not hasattr(signal, "SIGTERM")
    ):
        raise UnsupportedRunnerPlatformError(
            "Phase 2 Safe Runner supports macOS/Linux POSIX sessions "
            "and process-group signals only"
        )


def build_child_environment(
    environment_root: Path,
    *,
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment without inheriting arbitrary parent secrets."""
    parent = os.environ if parent_environment is None else parent_environment
    environment = {
        name: parent[name]
        for name in _CHILD_ENV_ALLOWLIST
        if name in parent
    }
    environment.update(
        {
            "HOME": str(environment_root / "home"),
            "TMPDIR": str(environment_root / "tmp"),
            "XDG_CACHE_HOME": str(environment_root / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _normalize_text(
    value: str,
    *,
    workspace: Path,
    environment_root: Path,
    temporary_root: Path,
) -> str:
    replacements = {
        str(workspace): "<WORKSPACE>",
        str(environment_root / "home"): "<TEMP_HOME>",
        str(environment_root / "tmp"): "<TEMP_DIR>",
        str(environment_root / "cache"): "<TEMP_CACHE>",
        str(temporary_root): "<RUN_TEMP>",
    }
    normalized = value
    for raw_path, placeholder in sorted(
        replacements.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        normalized = normalized.replace(raw_path, placeholder)
    return normalized


def _render_collected_text(
    collector: _BoundedBytes,
    *,
    max_output_bytes: int,
    workspace: Path,
    environment_root: Path,
    temporary_root: Path,
) -> tuple[str, bool, bool]:
    decoded, decode_replaced = collector.decode()
    normalized = _normalize_text(
        decoded,
        workspace=workspace,
        environment_root=environment_root,
        temporary_root=temporary_root,
    )
    encoded = normalized.encode()
    if len(encoded) <= max_output_bytes:
        return normalized, collector.truncated, decode_replaced
    return (
        encoded[:max_output_bytes].decode("utf-8", errors="ignore"),
        True,
        decode_replaced,
    )


def _group_exists(process_group_id: int) -> tuple[bool, str | None]:
    try:
        os.killpg(process_group_id, 0)
        return True, None
    except ProcessLookupError:
        return False, None
    except PermissionError:
        # Darwin can transiently report EPERM while a killed process group is
        # becoming unobservable. Treat it as "still present" and keep waiting;
        # a persistent EPERM remains fail-closed at the bounded deadline.
        return True, None
    except OSError as error:
        return True, f"could not inspect process group: {type(error).__name__}"


def _wait_for_group_to_clear(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    deadline: float,
    drain: Callable[[float], None],
) -> tuple[bool, str | None]:
    while True:
        process.poll()
        drain(0.0)
        exists, error = _group_exists(process_group_id)
        if error is not None:
            return False, error
        if not exists:
            return True, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, None
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _signal_group(process_group_id: int, selected_signal: signal.Signals) -> str | None:
    try:
        os.killpg(process_group_id, selected_signal)
        return None
    except ProcessLookupError:
        return None
    except OSError as error:
        return f"{selected_signal.name} failed: {type(error).__name__}"


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    reason: TerminationReason,
    grace_seconds: float,
    drain: Callable[[float], None],
) -> TerminationEvidence:
    process_group_id = process.pid
    exists, inspection_error = _group_exists(process_group_id)
    if inspection_error is not None:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=False,
            sigkill_sent=False,
            process_group_cleared=False,
            error=inspection_error,
        )
    if not exists:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=False,
            sigkill_sent=False,
            process_group_cleared=True,
            error=None,
        )

    term_error = _signal_group(process_group_id, signal.SIGTERM)
    if term_error is not None:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=False,
            sigkill_sent=False,
            process_group_cleared=False,
            error=term_error,
        )

    cleared, wait_error = _wait_for_group_to_clear(
        process,
        process_group_id,
        time.monotonic() + grace_seconds,
        drain,
    )
    if wait_error is not None:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=False,
            error=wait_error,
        )
    if cleared:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=True,
            error=None,
        )

    kill_error = _signal_group(process_group_id, signal.SIGKILL)
    if kill_error is not None:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=False,
            error=kill_error,
        )
    cleared, wait_error = _wait_for_group_to_clear(
        process,
        process_group_id,
        time.monotonic() + max(grace_seconds, 0.5),
        drain,
    )
    if wait_error is not None:
        return TerminationEvidence(
            reason=reason,
            sigterm_sent=True,
            sigkill_sent=True,
            process_group_cleared=False,
            error=wait_error,
        )
    return TerminationEvidence(
        reason=reason,
        sigterm_sent=True,
        sigkill_sent=True,
        process_group_cleared=cleared,
        error=None if cleared else "process group remained after SIGKILL",
    )


def _termination_without_signal() -> TerminationEvidence:
    return TerminationEvidence(
        reason=TerminationReason.NONE,
        sigterm_sent=False,
        sigkill_sent=False,
        process_group_cleared=True,
        error=None,
    )


def _merge_termination_error(
    termination: TerminationEvidence,
    error: str,
) -> TerminationEvidence:
    combined = error if termination.error is None else f"{termination.error}; {error}"
    return termination.model_copy(
        update={
            "reason": (
                TerminationReason.EMERGENCY_CLEANUP
                if termination.reason is TerminationReason.NONE
                else termination.reason
            ),
            "process_group_cleared": False,
            "error": combined,
        }
    )


def _append_collection_error(existing: str | None, detail: str) -> str:
    return detail if existing is None else f"{existing}; {detail}"


def process_group_exists(process_group_id: int) -> tuple[bool, str | None]:
    """Expose the Phase 2 process-group inspection contract to Provider runners."""
    return _group_exists(process_group_id)


def terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    reason: TerminationReason,
    grace_seconds: float,
    drain: Callable[[float], None],
) -> TerminationEvidence:
    """Use the same bounded SIGTERM/SIGKILL policy for Gate and Provider processes."""
    return _terminate_process_group(
        process,
        reason=reason,
        grace_seconds=grace_seconds,
        drain=drain,
    )


def termination_without_signal() -> TerminationEvidence:
    return _termination_without_signal()


def merge_termination_error(
    termination: TerminationEvidence,
    error: str,
) -> TerminationEvidence:
    return _merge_termination_error(termination, error)


class LocalCommandRunner:
    """Execute one already-validated argv in a disposable workspace."""

    def __init__(self, settings: RunnerSettings) -> None:
        self._settings = settings

    def run(
        self,
        *,
        gate: GateKind,
        command_index: int,
        argv: Sequence[str],
        workspace: Path,
        environment_root: Path,
        temporary_root: Path,
        parent_environment: Mapping[str, str] | None = None,
    ) -> CommandRunResult:
        ensure_runner_platform_supported()
        command = list(argv)
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        child_environment = build_child_environment(
            environment_root,
            parent_environment=parent_environment,
        )

        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            completed_at = datetime.now(UTC)
            failure_kind = (
                FailureKind.COMMAND_UNAVAILABLE
                if isinstance(error, FileNotFoundError)
                else FailureKind.SPAWN_ERROR
            )
            spawn_error_message = f"{failure_kind.value}: {type(error).__name__}"
            return CommandRunResult(
                evidence=CommandEvidence(
                    gate=gate,
                    command_index=command_index,
                    argv=command,
                    status=CommandStatus.SPAWN_ERROR,
                    return_code=None,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    stdout_decode_replaced=False,
                    stderr_decode_replaced=False,
                    termination=_termination_without_signal(),
                    error=spawn_error_message,
                ),
                harness_failure=failure_kind,
            )

        assert process.stdout is not None
        assert process.stderr is not None
        drainer: _PipeDrainer | None = None
        stdout_collector = _BoundedBytes(self._settings.max_output_bytes)
        stderr_collector = _BoundedBytes(self._settings.max_output_bytes)
        timed_out = False
        internal_error: str | None = None
        termination = _termination_without_signal()
        return_code: int | None = None

        def emergency_drain(timeout: float) -> None:
            if drainer is not None:
                with suppress(Exception):
                    drainer.drain(timeout)

        try:
            drainer = _PipeDrainer(
                process.stdout,
                process.stderr,
                stdout_collector=stdout_collector,
                stderr_collector=stderr_collector,
            )
            deadline = started_monotonic + (self._settings.command_timeout_ms / 1000)
            while True:
                now = time.monotonic()
                drainer.drain(min(0.05, max(0.0, deadline - now)))
                return_code = process.poll()
                if return_code is not None:
                    group_exists, group_error = _group_exists(process.pid)
                    if group_error is not None:
                        termination = TerminationEvidence(
                            reason=TerminationReason.RESIDUAL_PROCESS,
                            sigterm_sent=False,
                            sigkill_sent=False,
                            process_group_cleared=False,
                            error=group_error,
                        )
                    elif group_exists:
                        termination = _terminate_process_group(
                            process,
                            reason=TerminationReason.RESIDUAL_PROCESS,
                            grace_seconds=self._settings.termination_grace_ms / 1000,
                            drain=drainer.drain,
                        )
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    termination = _terminate_process_group(
                        process,
                        reason=TerminationReason.TIMEOUT,
                        grace_seconds=self._settings.termination_grace_ms / 1000,
                        drain=drainer.drain,
                    )
                    return_code = process.poll()
                    break

            if process.poll() is None:
                # A process that escaped its new group is outside the Phase 2
                # guarantee, but still receives a bounded direct cleanup attempt.
                process.kill()
                try:
                    process.wait(timeout=max(self._settings.termination_grace_ms / 1000, 0.5))
                except subprocess.TimeoutExpired:
                    termination = _merge_termination_error(
                        termination,
                        "parent process remained after direct kill",
                    )
                else:
                    termination = _merge_termination_error(
                        termination,
                        "parent process escaped its process group",
                    )
                return_code = process.poll()

            output_deadline = time.monotonic() + max(
                self._settings.termination_grace_ms / 1000,
                0.5,
            )
            while drainer.has_open_streams and time.monotonic() < output_deadline:
                drainer.drain(0.05)
            if drainer.has_open_streams:
                internal_error = _append_collection_error(
                    internal_error,
                    "output pipes remained open after process cleanup",
                )
            if drainer.error is not None:
                internal_error = _append_collection_error(internal_error, drainer.error)
        except Exception as error:
            internal_error = f"runner collection failed: {type(error).__name__}"
            termination = _terminate_process_group(
                process,
                reason=TerminationReason.EMERGENCY_CLEANUP,
                grace_seconds=self._settings.termination_grace_ms / 1000,
                drain=emergency_drain,
            )
            return_code = process.poll()
        finally:
            if drainer is not None:
                try:
                    drainer.close()
                except Exception as error:
                    internal_error = _append_collection_error(
                        internal_error,
                        f"runner pipe cleanup failed: {type(error).__name__}",
                    )
            else:
                for stream in (process.stdout, process.stderr):
                    try:
                        stream.close()
                    except Exception as error:
                        internal_error = _append_collection_error(
                            internal_error,
                            f"runner pipe cleanup failed: {type(error).__name__}",
                        )

        completed_at = datetime.now(UTC)
        harness_failure: FailureKind | None
        if not termination.process_group_cleared:
            status = (
                CommandStatus.COLLECTION_ERROR
                if internal_error is not None
                else (
                    CommandStatus.TIMED_OUT
                    if timed_out
                    else CommandStatus.SIGNAL_TERMINATED
                    if return_code is not None and return_code < 0
                    else CommandStatus.PASSED
                    if return_code == 0
                    else CommandStatus.FAILED
                )
            )
            evidence_return_code = return_code
            harness_failure = FailureKind.PROCESS_CLEANUP_ERROR
        elif internal_error is not None:
            status = CommandStatus.COLLECTION_ERROR
            evidence_return_code = return_code
            harness_failure = FailureKind.EVIDENCE_ERROR
        elif timed_out:
            status = CommandStatus.TIMED_OUT
            evidence_return_code = return_code
            harness_failure = (
                FailureKind.TIMEOUT
                if termination.process_group_cleared
                else FailureKind.PROCESS_CLEANUP_ERROR
            )
        elif return_code is not None and return_code < 0:
            status = CommandStatus.SIGNAL_TERMINATED
            evidence_return_code = return_code
            harness_failure = (
                FailureKind.SIGNAL_TERMINATION
                if termination.process_group_cleared
                else FailureKind.PROCESS_CLEANUP_ERROR
            )
        else:
            status = CommandStatus.PASSED if return_code == 0 else CommandStatus.FAILED
            evidence_return_code = return_code
            harness_failure = (
                None
                if termination.process_group_cleared
                else FailureKind.PROCESS_CLEANUP_ERROR
            )

        stdout, stdout_truncated, stdout_decode_replaced = _render_collected_text(
            stdout_collector,
            max_output_bytes=self._settings.max_output_bytes,
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
        )
        stderr, stderr_truncated, stderr_decode_replaced = _render_collected_text(
            stderr_collector,
            max_output_bytes=self._settings.max_output_bytes,
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
        )
        safe_error: str | None = (
            _normalize_text(
                internal_error,
                workspace=workspace,
                environment_root=environment_root,
                temporary_root=temporary_root,
            )
            if internal_error is not None
            else None
        )
        return CommandRunResult(
            evidence=CommandEvidence(
                gate=gate,
                command_index=command_index,
                argv=command,
                status=status,
                return_code=evidence_return_code,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                stdout_decode_replaced=stdout_decode_replaced,
                stderr_decode_replaced=stderr_decode_replaced,
                termination=termination,
                error=safe_error,
            ),
            harness_failure=harness_failure,
        )
