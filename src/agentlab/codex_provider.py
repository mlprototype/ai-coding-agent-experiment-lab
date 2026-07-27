"""Redacted Codex CLI preflight, JSONL parsing, and bounded process execution."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, NoReturn, cast

from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    CODEX_REQUIRED_EXEC_FLAGS,
    CodexApprovalBasis,
    CodexCleanupState,
    CodexCliProfile,
    CodexExecutionEvidence,
    CodexExecutionStage,
    CodexFailureStage,
    CodexInvocationState,
    CodexItemType,
    CodexRunnerState,
    CodexTerminalEvent,
    CommandStatus,
    FailureKind,
    GateKind,
    LiveFailureKind,
    LiveSettings,
    Provider,
    ProviderExecutionStatus,
    ReasoningEffort,
    RunnerSettings,
    TerminationEvidence,
    TerminationReason,
    UsageMetrics,
    UsageMetricSource,
)
from agentlab.runner import (
    LocalCommandRunner,
    UnsupportedRunnerPlatformError,
    ensure_runner_platform_supported,
    merge_termination_error,
    process_group_exists,
    terminate_process_group,
    termination_without_signal,
)
from agentlab.workspace import remove_temporary_root

PREFLIGHT_TIMEOUT_SECONDS = 5.0
PREFLIGHT_MAX_OUTPUT_BYTES = 64 * 1024
PREFLIGHT_TERMINATION_GRACE_MS = 100
_READ_CHUNK_BYTES = 64 * 1024
_POLL_INTERVAL_SECONDS = 0.01
_PROVIDER_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)
REQUIRED_CODEX_EXEC_FLAGS = CODEX_REQUIRED_EXEC_FLAGS
CODEX_EVIDENCE_SCHEMA_VERSION: Literal["1.4"] = "1.4"
_SAFE_ITEM_TYPES = frozenset(
    item_type.value
    for item_type in CodexItemType
    if item_type is not CodexItemType.UNKNOWN
)


class CodexPreflightError(ValueError):
    """A read-only compatibility check failed before a Live process could start."""

    def __init__(
        self,
        failure_kind: LiveFailureKind,
        message: str,
        *,
        checked_at: datetime,
        cli_version: str | None = None,
        verified_flags: tuple[str, ...] = (),
        termination: TerminationEvidence | None = None,
    ) -> None:
        super().__init__(message)
        resolved_termination = termination or termination_without_signal()
        if (
            failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
            and resolved_termination.process_group_cleared
        ):
            raise ValueError(
                "preflight process_cleanup_error requires uncleared termination Evidence"
            )
        self.failure_kind = failure_kind
        self.checked_at = checked_at
        self.cli_version = cli_version
        self.verified_flags = verified_flags
        self.termination = resolved_termination


class CodexProtocolError(ValueError):
    """A bounded JSONL stream did not satisfy the minimal lifecycle contract."""

    def __init__(self, failure_kind: LiveFailureKind, message: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


class CodexRunnerError(RuntimeError):
    """A Provider runner boundary failed after preserving its safe lifecycle."""

    def __init__(self, lifecycle: CodexLifecycleTracker) -> None:
        super().__init__("Codex Provider runner failed at a redacted lifecycle boundary")
        self.lifecycle = lifecycle


class _PreflightHarnessError(subprocess.SubprocessError):
    """A read-only probe hit an Evidence/process-cleanup Harness failure."""

    def __init__(
        self,
        failure_kind: LiveFailureKind,
        message: str,
        *,
        termination: TerminationEvidence | None = None,
    ) -> None:
        super().__init__(message)
        resolved_termination = termination or termination_without_signal()
        if (
            failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
            and resolved_termination.process_group_cleared
        ):
            raise ValueError(
                "preflight process_cleanup_error requires uncleared termination Evidence"
            )
        self.failure_kind = failure_kind
        self.termination = resolved_termination


@dataclass(frozen=True)
class CodexPreflight:
    executable: str
    cli_version: str
    cli_profile: Literal[CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2]
    checked_at: datetime
    verified_flags: tuple[str, ...]


@dataclass(frozen=True)
class CodexParseSummary:
    event_count: int
    unknown_event_count: int
    thread_started_count: int
    turn_started_count: int
    terminal_event: CodexTerminalEvent
    turn_completed_count: int
    turn_failed_count: int
    error_event_count: int
    item_type_counts: dict[CodexItemType, int]
    usage_metrics: UsageMetrics
    turn_failed: bool


@dataclass(frozen=True)
class CodexRunResult:
    evidence: CodexExecutionEvidence


@dataclass
class CodexLifecycleTracker:
    """In-memory Provider lifecycle used to construct truthful failure Evidence."""

    runner_state: CodexRunnerState = CodexRunnerState.NOT_STARTED
    invocation_state: CodexInvocationState = CodexInvocationState.NOT_ATTEMPTED
    cleanup_state: CodexCleanupState | None = CodexCleanupState.NOT_APPLICABLE
    failure_stage: CodexFailureStage = CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION
    termination: TerminationEvidence = field(default_factory=termination_without_signal)
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    parser: CodexJsonlParser | None = field(default=None, repr=False)
    started_at: datetime | None = None
    started_monotonic: float | None = None
    return_code: int | None = None
    stderr_bytes: int = 0
    stderr_truncated: bool = False
    stdout_limit_exceeded: bool = False

    def mark_runner_started(self) -> None:
        self.runner_state = CodexRunnerState.STARTED
        self.failure_stage = CodexFailureStage.PROVIDER_RUNNER_ENTRY

    def mark_spawn_attempted(self) -> None:
        self.invocation_state = CodexInvocationState.SPAWN_ATTEMPTED
        self.cleanup_state = CodexCleanupState.NOT_APPLICABLE
        self.failure_stage = CodexFailureStage.PROVIDER_PROCESS_SPAWN

    def mark_process_started(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.invocation_state = CodexInvocationState.PROCESS_STARTED
        self.cleanup_state = None

    def observe_termination(self, termination: TerminationEvidence) -> None:
        self.termination = termination
        if self.invocation_state is CodexInvocationState.PROCESS_STARTED:
            self.cleanup_state = (
                CodexCleanupState.CLEARED
                if termination.process_group_cleared
                else CodexCleanupState.FAILED
            )

    def persisted_cleanup_state(self) -> CodexCleanupState:
        if self.cleanup_state is None:
            raise RuntimeError("Provider cleanup state was not observed")
        return self.cleanup_state


class _DuplicateKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value}")
    return parsed


def _not_available_usage() -> UsageMetrics:
    return UsageMetrics(source=UsageMetricSource.NOT_AVAILABLE)


class CodexJsonlParser:
    """Incrementally normalize only safe counts and Usage from vendor JSONL."""

    def __init__(self, *, max_line_bytes: int, max_total_bytes: int) -> None:
        self._max_line_bytes = max_line_bytes
        self._max_total_bytes = max_total_bytes
        self._buffer = bytearray()
        self.total_bytes = 0
        self.event_count = 0
        self.unknown_event_count = 0
        self.item_type_counts: dict[CodexItemType, int] = {}
        self.usage_metrics = _not_available_usage()
        self._thread_started = False
        self._turn_started = False
        self._terminal_seen = False
        self._turn_failed = False
        self.thread_started_count = 0
        self.turn_started_count = 0
        self.terminal_event = CodexTerminalEvent.NONE
        self.turn_completed_count = 0
        self.turn_failed_count = 0
        self.error_event_count = 0

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.total_bytes > self._max_total_bytes:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
                "Codex JSONL exceeded the configured total byte limit",
            )

        remaining = chunk
        while remaining:
            newline_index = remaining.find(b"\n")
            if newline_index < 0:
                self._append_line_bytes(remaining)
                break
            self._append_line_bytes(remaining[:newline_index])
            encoded_line = bytes(self._buffer)
            self._buffer.clear()
            self._consume_line(encoded_line)
            remaining = remaining[newline_index + 1 :]

    def finish(self) -> CodexParseSummary:
        if self._buffer:
            encoded_line = bytes(self._buffer)
            self._buffer.clear()
            self._consume_line(encoded_line)
        if not self._terminal_seen:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL did not contain a terminal event",
            )
        if not self._turn_failed and not (
            self._thread_started and self._turn_started
        ):
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex success lifecycle is incomplete",
            )
        return self.summary()

    def summary(self) -> CodexParseSummary:
        self._assert_normalized_event_count()
        return CodexParseSummary(
            event_count=self.event_count,
            unknown_event_count=self.unknown_event_count,
            thread_started_count=self.thread_started_count,
            turn_started_count=self.turn_started_count,
            terminal_event=self.terminal_event,
            turn_completed_count=self.turn_completed_count,
            turn_failed_count=self.turn_failed_count,
            error_event_count=self.error_event_count,
            item_type_counts=dict(sorted(self.item_type_counts.items())),
            usage_metrics=self.usage_metrics,
            turn_failed=self._turn_failed,
        )

    def _append_line_bytes(self, value: bytes) -> None:
        if len(self._buffer) + len(value) > self._max_line_bytes:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
                "Codex JSONL line exceeded the configured byte limit",
            )
        self._buffer.extend(value)

    def _consume_line(self, encoded_line: bytes) -> None:
        if not encoded_line:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL contains an empty line",
            )
        try:
            line = encoded_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL contains invalid UTF-8",
            ) from error
        if not line.strip():
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL contains an empty line",
            )
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_non_finite,
                parse_float=_parse_finite_float,
            )
        except _DuplicateKeyError as error:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                f"Codex JSONL contains duplicate key {error}",
            ) from error
        except (json.JSONDecodeError, ValueError) as error:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                f"Codex JSONL is invalid: {type(error).__name__}",
            ) from error
        if not isinstance(raw, dict):
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL event must be an object",
            )
        event_type = raw.get("type")
        if not isinstance(event_type, str):
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL event type must be a string",
            )
        if self._terminal_seen:
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "Codex JSONL contains an event after its terminal event",
            )

        safe_item_type: CodexItemType | None = None
        parsed_usage: UsageMetrics | None = None
        if event_type == "thread.started":
            if self._thread_started or self._turn_started:
                self._invalid_order("thread.started")
        elif event_type == "turn.started":
            if not self._thread_started or self._turn_started:
                self._invalid_order("turn.started")
        elif event_type == "turn.completed":
            if (
                not self._thread_started
                or not self._turn_started
                or self.error_event_count > 0
            ):
                self._invalid_order("turn.completed")
            parsed_usage = self._parse_usage(raw.get("usage"))
        elif event_type == "turn.failed":
            if not self._thread_started or not self._turn_started:
                self._invalid_order("turn.failed")
        elif event_type == "error":
            if not self._thread_started or not self._turn_started:
                self._invalid_order("error")
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            item = raw.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            safe_item_type = CodexItemType(
                item_type
                if isinstance(item_type, str) and item_type in _SAFE_ITEM_TYPES
                else CodexItemType.UNKNOWN
            )
            pre_turn_warning = (
                self._thread_started
                and not self._turn_started
                and event_type == "item.completed"
                and safe_item_type is CodexItemType.ERROR
            )
            if not self._thread_started or (
                not self._turn_started and not pre_turn_warning
            ):
                self._invalid_order(event_type)

        # Apply the fully validated event as one state transition. No validation
        # below this point may fail, so a rejected event cannot partially mutate
        # normalized lifecycle, item, Usage, or terminal state.
        self.event_count += 1
        if event_type == "thread.started":
            self._thread_started = True
            self.thread_started_count += 1
        elif event_type == "turn.started":
            self._turn_started = True
            self.turn_started_count += 1
        elif event_type == "turn.completed":
            assert parsed_usage is not None
            self._terminal_seen = True
            self.terminal_event = CodexTerminalEvent.TURN_COMPLETED
            self.turn_completed_count += 1
            self.usage_metrics = parsed_usage
        elif event_type == "turn.failed":
            self._terminal_seen = True
            self._turn_failed = True
            self.terminal_event = CodexTerminalEvent.TURN_FAILED
            self.turn_failed_count += 1
        elif event_type == "error":
            self.error_event_count += 1
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            assert safe_item_type is not None
            self.item_type_counts[safe_item_type] = (
                self.item_type_counts.get(safe_item_type, 0) + 1
            )
        else:
            self.unknown_event_count += 1
        self._assert_normalized_event_count()

    def _assert_normalized_event_count(self) -> None:
        normalized_count = (
            self.thread_started_count
            + self.turn_started_count
            + self.turn_completed_count
            + self.turn_failed_count
            + self.error_event_count
            + sum(self.item_type_counts.values())
            + self.unknown_event_count
        )
        if normalized_count != self.event_count:
            raise RuntimeError("Codex parser normalized event count invariant failed")

    def _invalid_order(self, event_type: str) -> NoReturn:
        raise CodexProtocolError(
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            f"Codex JSONL lifecycle order is invalid at {event_type}",
        )

    def _parse_usage(self, value: object) -> UsageMetrics:
        if value is None:
            return _not_available_usage()
        if not isinstance(value, dict):
            raise CodexProtocolError(
                LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                "turn.completed usage must be an object",
            )
        names = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        parsed: dict[str, int] = {}
        for name in names:
            if name not in value:
                continue
            raw_value = value[name]
            if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
                raise CodexProtocolError(
                    LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
                    f"turn.completed usage.{name} must be a non-negative integer",
                )
            parsed[name] = raw_value
        if not parsed:
            return _not_available_usage()
        return UsageMetrics(
            **parsed,
            source=UsageMetricSource.PROVIDER_REPORTED,
        )


def _parser_snapshot(parser: CodexJsonlParser | None) -> CodexParseSummary:
    if parser is None:
        return CodexParseSummary(
            event_count=0,
            unknown_event_count=0,
            thread_started_count=0,
            turn_started_count=0,
            terminal_event=CodexTerminalEvent.NONE,
            turn_completed_count=0,
            turn_failed_count=0,
            error_event_count=0,
            item_type_counts={},
            usage_metrics=_not_available_usage(),
            turn_failed=False,
        )
    return CodexParseSummary(
        event_count=parser.event_count,
        unknown_event_count=parser.unknown_event_count,
        thread_started_count=parser.thread_started_count,
        turn_started_count=parser.turn_started_count,
        terminal_event=parser.terminal_event,
        turn_completed_count=parser.turn_completed_count,
        turn_failed_count=parser.turn_failed_count,
        error_event_count=parser.error_event_count,
        item_type_counts=dict(sorted(parser.item_type_counts.items())),
        usage_metrics=parser.usage_metrics,
        turn_failed=parser.terminal_event
        in {CodexTerminalEvent.TURN_FAILED, CodexTerminalEvent.ERROR},
    )


def _probe_environment(parent_environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: parent_environment[name]
        for name in _PROVIDER_ENV_ALLOWLIST
        if name in parent_environment
    }


def _run_preflight_command(
    executable: str,
    args: list[str],
    *,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    """Run one bounded read-only probe with the Safe Runner process policy."""
    temporary_root: Path | None = None
    harness_error: _PreflightHarnessError | None = None
    try:
        try:
            created_root = Path(
                tempfile.mkdtemp(prefix="agentlab-codex-preflight-")
            )
            temporary_root = created_root
            temporary_root = created_root.resolve()
            workspace = temporary_root / "workspace"
            environment_root = temporary_root / "environment"
            workspace.mkdir()
            for name in ("home", "tmp", "cache"):
                (environment_root / name).mkdir(parents=True, exist_ok=True)
        except Exception as error:
            raise _PreflightHarnessError(
                LiveFailureKind.EVIDENCE_ERROR,
                f"preflight workspace preparation failed: {type(error).__name__}",
            ) from error
        result = LocalCommandRunner(
            RunnerSettings(
                fixture_path="preflight",
                command_timeout_ms=int(PREFLIGHT_TIMEOUT_SECONDS * 1000),
                termination_grace_ms=PREFLIGHT_TERMINATION_GRACE_MS,
                max_output_bytes=PREFLIGHT_MAX_OUTPUT_BYTES,
                max_diff_bytes=1,
            )
        ).run(
            gate=GateKind.ACCEPTANCE,
            command_index=0,
            argv=[executable, *args],
            workspace=workspace,
            environment_root=environment_root,
            temporary_root=temporary_root,
            parent_environment=environment,
        )
        evidence = result.evidence
        if result.harness_failure in {
            FailureKind.PROCESS_CLEANUP_ERROR,
            FailureKind.EVIDENCE_ERROR,
        }:
            raise _PreflightHarnessError(
                (
                    LiveFailureKind.PROCESS_CLEANUP_ERROR
                    if result.harness_failure is FailureKind.PROCESS_CLEANUP_ERROR
                    else LiveFailureKind.EVIDENCE_ERROR
                ),
                f"bounded preflight Harness failure: {result.harness_failure.value}",
                termination=evidence.termination,
            )
        if (
            result.harness_failure is not None
            or evidence.status is not CommandStatus.PASSED
            or evidence.stdout_truncated
            or evidence.stderr_truncated
            or evidence.stdout_decode_replaced
            or evidence.stderr_decode_replaced
            or not evidence.termination.process_group_cleared
        ):
            raise subprocess.SubprocessError(
                f"bounded preflight command failed: {evidence.status.value}"
            )
        return evidence.stdout, evidence.stderr
    except _PreflightHarnessError as error:
        harness_error = error
        raise
    finally:
        if temporary_root is not None:
            cleanup_succeeded, cleanup_error = remove_temporary_root(temporary_root)
            if not cleanup_succeeded and (
                harness_error is None
                or harness_error.failure_kind
                is not LiveFailureKind.PROCESS_CLEANUP_ERROR
            ):
                raise _PreflightHarnessError(
                    LiveFailureKind.EVIDENCE_ERROR,
                    cleanup_error or "preflight temporary cleanup failed",
                )


def _first_nonempty_line(stdout: str, stderr: str) -> str | None:
    for output in (stdout, stderr):
        for line in output.splitlines():
            if line.strip():
                return line.strip()
    return None


def preflight_codex(
    *,
    parent_environment: Mapping[str, str] | None = None,
) -> CodexPreflight:
    """Run only version/help commands and fail closed on an incompatible CLI."""
    checked_at = datetime.now(UTC)
    parent = os.environ if parent_environment is None else parent_environment
    try:
        ensure_runner_platform_supported()
    except UnsupportedRunnerPlatformError as error:
        raise CodexPreflightError(
            LiveFailureKind.UNSUPPORTED_PLATFORM,
            "Codex preflight requires POSIX process-group support",
            checked_at=checked_at,
        ) from error
    executable = shutil.which("codex", path=parent.get("PATH"))
    if executable is None:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            "codex was not found on PATH",
            checked_at=checked_at,
        )
    environment = _probe_environment(parent)
    try:
        version_stdout, version_stderr = _run_preflight_command(
            executable,
            ["--version"],
            environment=environment,
        )
    except _PreflightHarnessError as error:
        raise CodexPreflightError(
            error.failure_kind,
            "codex version preflight Harness failed",
            checked_at=checked_at,
            termination=error.termination,
        ) from error
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            f"codex version preflight failed: {type(error).__name__}",
            checked_at=checked_at,
        ) from error
    version = _first_nonempty_line(version_stdout, version_stderr)
    if version is None or len(version.encode("utf-8")) > 256:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            "codex --version did not succeed",
            checked_at=checked_at,
        )
    if version not in CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            f"Codex CLI version is not allowlisted for the explicit-never profile: {version}",
            checked_at=checked_at,
            cli_version=version,
        )

    try:
        help_stdout, help_stderr = _run_preflight_command(
            executable,
            ["exec", "--help"],
            environment=environment,
        )
    except _PreflightHarnessError as error:
        raise CodexPreflightError(
            error.failure_kind,
            "codex exec help preflight Harness failed",
            checked_at=checked_at,
            cli_version=version,
            termination=error.termination,
        ) from error
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            f"codex exec help preflight failed: {type(error).__name__}",
            checked_at=checked_at,
            cli_version=version,
        ) from error
    help_text = f"{help_stdout}\n{help_stderr}"
    verified_flags = tuple(
        sorted(
            flag
            for flag in REQUIRED_CODEX_EXEC_FLAGS
            if re.search(
                rf"(?<![\w-]){re.escape(flag)}(?![\w-])",
                help_text,
            )
        )
    )
    missing_flags = sorted(set(REQUIRED_CODEX_EXEC_FLAGS) - set(verified_flags))
    if missing_flags:
        raise CodexPreflightError(
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
            f"codex exec help is missing required flags: {', '.join(missing_flags)}",
            checked_at=checked_at,
            cli_version=version,
            verified_flags=verified_flags,
        )
    return CodexPreflight(
        executable=executable,
        cli_version=version,
        cli_profile=CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2,
        checked_at=checked_at,
        verified_flags=verified_flags,
    )


def build_codex_argv(
    preflight: CodexPreflight,
    *,
    model: str,
    reasoning_effort: ReasoningEffort,
) -> list[str]:
    """Construct the fixed safe invocation; Prompt content is deliberately absent."""
    return [
        preflight.executable,
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--model",
        model,
        "--config",
        'approval_policy="never"',
        "--config",
        f'model_reasoning_effort="{reasoning_effort.value}"',
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        'web_search="disabled"',
        "-",
    ]


def build_codex_environment(
    environment_root: Path,
    *,
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Pass only basic locale/PATH plus the existing managed-auth CODEX_HOME path."""
    parent = os.environ if parent_environment is None else parent_environment
    environment = _probe_environment(parent)
    environment.update(
        {
            "HOME": str(environment_root / "home"),
            "TMPDIR": str(environment_root / "tmp"),
            "XDG_CACHE_HOME": str(environment_root / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment["CODEX_HOME"] = str(resolve_codex_home(parent))
    return environment


def resolve_codex_home(parent_environment: Mapping[str, str]) -> Path:
    """Require an explicit existing absolute managed-auth directory."""
    configured = parent_environment.get("CODEX_HOME")
    if not configured:
        raise ValueError("CODEX_HOME must be explicitly configured for Live Codex")
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("CODEX_HOME must be an absolute path")
    try:
        path.stat()
    except OSError as error:
        raise ValueError(
            f"CODEX_HOME is unavailable: {type(error).__name__}"
        ) from error
    if not path.is_dir():
        raise ValueError("CODEX_HOME must be an existing directory")
    return path


def _preflight_failure_evidence(
    error: CodexPreflightError,
    *,
    live: LiveSettings,
) -> CodexExecutionEvidence:
    assert live.model is not None
    assert live.reasoning_effort is not None
    now = datetime.now(UTC)
    return CodexExecutionEvidence(
        schema_version=CODEX_EVIDENCE_SCHEMA_VERSION,
        provider=Provider.CODEX,
        cli_version=error.cli_version,
        cli_profile=CodexCliProfile.NOT_SELECTED,
        execution_stage=CodexExecutionStage.PREFLIGHT_NOT_COMPLETED,
        failure_stage=CodexFailureStage.PREFLIGHT,
        runner_state=CodexRunnerState.NOT_STARTED,
        invocation_state=CodexInvocationState.NOT_ATTEMPTED,
        cleanup_state=CodexCleanupState.NOT_APPLICABLE,
        preflight_checked_at=error.checked_at,
        verified_flags=sorted(error.verified_flags),
        requested_model=live.model,
        requested_reasoning_effort=live.reasoning_effort,
        sandbox_mode="workspace-write",
        approval_policy=None,
        approval_basis=None,
        web_search_disabled=True,
        command_network_disabled=True,
        raw_stream_persisted=False,
        process_started=False,
        status=ProviderExecutionStatus.FAILED,
        failure_kind=error.failure_kind,
        exit_code=None,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        event_count=0,
        unknown_event_count=0,
        thread_started_count=0,
        turn_started_count=0,
        terminal_event=CodexTerminalEvent.NONE,
        turn_completed_count=0,
        turn_failed_count=0,
        error_event_count=0,
        item_type_counts={},
        usage_metrics=_not_available_usage(),
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_limit_exceeded=False,
        stderr_truncated=False,
        termination=error.termination,
    )


def preflight_failure_evidence(
    error: CodexPreflightError,
    *,
    live: LiveSettings,
) -> CodexExecutionEvidence:
    return _preflight_failure_evidence(error, live=live)


def post_preflight_failure_evidence(
    preflight: CodexPreflight,
    *,
    live: LiveSettings,
    failure_kind: LiveFailureKind = LiveFailureKind.EVIDENCE_ERROR,
    failure_stage: CodexFailureStage,
    runner_state: CodexRunnerState = CodexRunnerState.NOT_STARTED,
) -> CodexExecutionEvidence:
    """Record selected compatibility metadata before any Provider invocation."""
    assert live.model is not None
    assert live.reasoning_effort is not None
    now = datetime.now(UTC)
    return CodexExecutionEvidence(
        schema_version=CODEX_EVIDENCE_SCHEMA_VERSION,
        provider=Provider.CODEX,
        cli_version=preflight.cli_version,
        cli_profile=preflight.cli_profile,
        execution_stage=CodexExecutionStage.PREFLIGHT_COMPLETED,
        failure_stage=failure_stage,
        runner_state=runner_state,
        invocation_state=CodexInvocationState.NOT_ATTEMPTED,
        cleanup_state=CodexCleanupState.NOT_APPLICABLE,
        preflight_checked_at=preflight.checked_at,
        verified_flags=sorted(preflight.verified_flags),
        requested_model=live.model,
        requested_reasoning_effort=live.reasoning_effort,
        sandbox_mode="workspace-write",
        approval_policy=None,
        approval_basis=None,
        web_search_disabled=True,
        command_network_disabled=True,
        raw_stream_persisted=False,
        process_started=False,
        status=ProviderExecutionStatus.FAILED,
        failure_kind=failure_kind,
        exit_code=None,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        event_count=0,
        unknown_event_count=0,
        thread_started_count=0,
        turn_started_count=0,
        terminal_event=CodexTerminalEvent.NONE,
        turn_completed_count=0,
        turn_failed_count=0,
        error_event_count=0,
        item_type_counts={},
        usage_metrics=_not_available_usage(),
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_limit_exceeded=False,
        stderr_truncated=False,
        termination=termination_without_signal(),
    )


def lifecycle_failure_evidence(
    preflight: CodexPreflight,
    *,
    live: LiveSettings,
    lifecycle: CodexLifecycleTracker,
) -> CodexExecutionEvidence:
    """Build redacted 1.3 Evidence from observations shared with the runner."""
    assert live.model is not None
    assert live.reasoning_effort is not None
    now = datetime.now(UTC)
    started_at = lifecycle.started_at or now
    parsed = _parser_snapshot(lifecycle.parser)
    invocation_attempted = (
        lifecycle.invocation_state is not CodexInvocationState.NOT_ATTEMPTED
    )
    process_started = (
        lifecycle.invocation_state is CodexInvocationState.PROCESS_STARTED
    )
    cleanup_state = lifecycle.persisted_cleanup_state()
    failure_kind = (
        LiveFailureKind.PROCESS_CLEANUP_ERROR
        if cleanup_state is CodexCleanupState.FAILED
        else LiveFailureKind.EVIDENCE_ERROR
    )
    duration_ms = 0
    if process_started and lifecycle.started_monotonic is not None:
        duration_ms = max(
            0,
            int((time.monotonic() - lifecycle.started_monotonic) * 1000),
        )
    return CodexExecutionEvidence(
        schema_version=CODEX_EVIDENCE_SCHEMA_VERSION,
        provider=Provider.CODEX,
        cli_version=preflight.cli_version,
        cli_profile=preflight.cli_profile,
        execution_stage=(
            CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
            if invocation_attempted
            else CodexExecutionStage.PREFLIGHT_COMPLETED
        ),
        failure_stage=lifecycle.failure_stage,
        runner_state=lifecycle.runner_state,
        invocation_state=lifecycle.invocation_state,
        cleanup_state=cleanup_state,
        preflight_checked_at=preflight.checked_at,
        verified_flags=sorted(preflight.verified_flags),
        requested_model=live.model,
        requested_reasoning_effort=live.reasoning_effort,
        sandbox_mode="workspace-write",
        approval_policy="never" if invocation_attempted else None,
        approval_basis=(
            CodexApprovalBasis.EXPLICIT_CONFIG_NEVER
            if invocation_attempted
            else None
        ),
        web_search_disabled=True,
        command_network_disabled=True,
        raw_stream_persisted=False,
        process_started=process_started,
        status=ProviderExecutionStatus.FAILED,
        failure_kind=failure_kind,
        exit_code=lifecycle.return_code if process_started else None,
        started_at=started_at,
        completed_at=now,
        duration_ms=duration_ms,
        event_count=parsed.event_count,
        unknown_event_count=parsed.unknown_event_count,
        thread_started_count=parsed.thread_started_count,
        turn_started_count=parsed.turn_started_count,
        terminal_event=parsed.terminal_event,
        turn_completed_count=parsed.turn_completed_count,
        turn_failed_count=parsed.turn_failed_count,
        error_event_count=parsed.error_event_count,
        item_type_counts=parsed.item_type_counts,
        usage_metrics=parsed.usage_metrics,
        stdout_bytes=0 if lifecycle.parser is None else lifecycle.parser.total_bytes,
        stderr_bytes=lifecycle.stderr_bytes,
        stdout_limit_exceeded=lifecycle.stdout_limit_exceeded,
        stderr_truncated=lifecycle.stderr_truncated,
        termination=lifecycle.termination,
    )


class CodexProcessRunner:
    """Run one preflighted Codex process without retaining raw stdout or stderr."""

    def __init__(
        self,
        *,
        live: LiveSettings,
        runner: RunnerSettings,
        lifecycle: CodexLifecycleTracker | None = None,
    ) -> None:
        self._live = live
        self._runner = runner
        self.lifecycle = lifecycle or CodexLifecycleTracker()

    def run(
        self,
        *,
        preflight: CodexPreflight,
        prompt: bytes,
        workspace: Path,
        environment_root: Path,
        parent_environment: Mapping[str, str] | None = None,
    ) -> CodexRunResult:
        self.lifecycle.mark_runner_started()
        try:
            return self._run(
                preflight=preflight,
                prompt=prompt,
                workspace=workspace,
                environment_root=environment_root,
                parent_environment=parent_environment,
            )
        except (KeyboardInterrupt, SystemExit):
            self._emergency_cleanup()
            raise
        except UnsupportedRunnerPlatformError:
            raise
        except Exception as error:
            self._emergency_cleanup()
            raise CodexRunnerError(self.lifecycle) from error

    def _run(
        self,
        *,
        preflight: CodexPreflight,
        prompt: bytes,
        workspace: Path,
        environment_root: Path,
        parent_environment: Mapping[str, str] | None = None,
    ) -> CodexRunResult:
        live = self._live
        assert live.model is not None
        assert live.reasoning_effort is not None
        assert live.provider_timeout_ms is not None
        assert live.max_event_line_bytes is not None
        assert live.max_provider_output_bytes is not None
        max_provider_output_bytes = live.max_provider_output_bytes
        self.lifecycle.failure_stage = CodexFailureStage.PROVIDER_RUNTIME_PRECHECK
        ensure_runner_platform_supported()
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        self.lifecycle.started_at = started_at
        self.lifecycle.started_monotonic = started_monotonic
        self.lifecycle.failure_stage = CodexFailureStage.JSONL_PARSER_INITIALIZATION
        try:
            parser = CodexJsonlParser(
                max_line_bytes=live.max_event_line_bytes,
                max_total_bytes=live.max_provider_output_bytes,
            )
        except Exception:
            return self._result_from_evidence(
                lambda: post_preflight_failure_evidence(
                    preflight,
                    live=live,
                    failure_stage=CodexFailureStage.JSONL_PARSER_INITIALIZATION,
                    runner_state=CodexRunnerState.STARTED,
                )
            )
        self.lifecycle.parser = parser
        self.lifecycle.failure_stage = CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION
        try:
            argv = build_codex_argv(
                preflight,
                model=live.model,
                reasoning_effort=live.reasoning_effort,
            )
        except Exception:
            return self._result_from_evidence(
                lambda: post_preflight_failure_evidence(
                    preflight,
                    live=live,
                    failure_stage=CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
                    runner_state=CodexRunnerState.STARTED,
                )
            )
        self.lifecycle.failure_stage = CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION
        try:
            environment = build_codex_environment(
                environment_root,
                parent_environment=parent_environment,
            )
        except Exception:
            return self._result_from_evidence(
                lambda: post_preflight_failure_evidence(
                    preflight,
                    live=live,
                    failure_stage=CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
                    runner_state=CodexRunnerState.STARTED,
                )
            )
        self.lifecycle.mark_spawn_attempted()
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except Exception as error:
            failure = (
                LiveFailureKind.PROVIDER_UNAVAILABLE
                if isinstance(error, FileNotFoundError)
                else LiveFailureKind.PROVIDER_SPAWN_ERROR
            )
            return self._result_from_evidence(
                lambda: self._build_evidence(
                    preflight=preflight,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    status=ProviderExecutionStatus.FAILED,
                    failure_kind=failure,
                    failure_stage=CodexFailureStage.PROVIDER_PROCESS_SPAWN,
                    process_started=False,
                    exit_code=None,
                    parser=parser,
                    stderr_bytes=0,
                    stderr_truncated=False,
                    termination=termination_without_signal(),
                )
            )
        self.lifecycle.mark_process_started(process)
        self.lifecycle.failure_stage = (
            CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION
        )

        streams: dict[str, IO[bytes]] = {}
        try:
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise RuntimeError("Provider pipes were not created")
            streams = {
                "stdin": process.stdin,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
            selector = selectors.DefaultSelector()
        except Exception:
            termination = terminate_process_group(
                process,
                reason=TerminationReason.EMERGENCY_CLEANUP,
                grace_seconds=self._runner.termination_grace_ms / 1000,
                drain=lambda _timeout: None,
            )
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()
            failure = (
                LiveFailureKind.EVIDENCE_ERROR
                if termination.process_group_cleared
                else LiveFailureKind.PROCESS_CLEANUP_ERROR
            )
            self.lifecycle.observe_termination(termination)
            self.lifecycle.return_code = process.poll()
            return self._result_from_evidence(
                lambda: self._build_evidence(
                    preflight=preflight,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    status=ProviderExecutionStatus.FAILED,
                    failure_kind=failure,
                    failure_stage=(
                        CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION
                    ),
                    exit_code=process.poll(),
                    parser=parser,
                    stderr_bytes=0,
                    stderr_truncated=False,
                    termination=termination,
                )
            )

        try:
            for name, stream in streams.items():
                os.set_blocking(stream.fileno(), False)
                event = selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ
                selector.register(stream, event, name)
        except Exception:
            termination = terminate_process_group(
                process,
                reason=TerminationReason.EMERGENCY_CLEANUP,
                grace_seconds=self._runner.termination_grace_ms / 1000,
                drain=lambda _timeout: None,
            )
            for stream in streams.values():
                with suppress(OSError):
                    stream.close()
            with suppress(Exception):
                selector.close()
            failure = (
                LiveFailureKind.EVIDENCE_ERROR
                if termination.process_group_cleared
                else LiveFailureKind.PROCESS_CLEANUP_ERROR
            )
            self.lifecycle.observe_termination(termination)
            self.lifecycle.return_code = process.poll()
            return self._result_from_evidence(
                lambda: self._build_evidence(
                    preflight=preflight,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    status=ProviderExecutionStatus.FAILED,
                    failure_kind=failure,
                    failure_stage=(
                        CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION
                    ),
                    exit_code=process.poll(),
                    parser=parser,
                    stderr_bytes=0,
                    stderr_truncated=False,
                    termination=termination,
                )
            )

        self.lifecycle.failure_stage = CodexFailureStage.PROVIDER_PROCESS_COLLECTION
        prompt_offset = 0
        stderr_bytes = 0
        stderr_truncated = False
        failure_kind: LiveFailureKind | None = None
        timed_out = False
        termination = termination_without_signal()
        return_code: int | None = None
        protocol_error: CodexProtocolError | None = None

        def close_stream(name: str) -> None:
            nonlocal failure_kind
            stream = streams[name]
            try:
                selector.unregister(stream)
            except (KeyError, ValueError):
                pass
            except Exception:
                if failure_kind is None:
                    failure_kind = LiveFailureKind.EVIDENCE_ERROR
            try:
                stream.close()
            except Exception:
                if failure_kind is None:
                    failure_kind = LiveFailureKind.EVIDENCE_ERROR

        def drain_once(timeout: float) -> None:
            nonlocal prompt_offset, stderr_bytes, stderr_truncated
            nonlocal failure_kind, protocol_error
            if not selector.get_map():
                return
            try:
                ready = selector.select(max(timeout, 0.0))
            except OSError:
                if failure_kind is None:
                    failure_kind = LiveFailureKind.EVIDENCE_ERROR
                return
            for key, _events in ready:
                name = cast(str, key.data)
                stream = cast(IO[bytes], key.fileobj)
                if name == "stdin":
                    try:
                        written = os.write(stream.fileno(), prompt[prompt_offset:])
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        failure_kind = LiveFailureKind.PROVIDER_INPUT_ERROR
                        close_stream("stdin")
                        continue
                    prompt_offset += written
                    if prompt_offset == len(prompt):
                        close_stream("stdin")
                    continue

                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                except OSError:
                    if failure_kind is None:
                        failure_kind = LiveFailureKind.EVIDENCE_ERROR
                    close_stream(name)
                    continue
                if not chunk:
                    close_stream(name)
                    continue
                if name == "stdout":
                    try:
                        parser.feed(chunk)
                    except CodexProtocolError as error:
                        protocol_error = error
                        failure_kind = error.failure_kind
                else:
                    stderr_bytes += len(chunk)
                    if stderr_bytes > max_provider_output_bytes:
                        stderr_truncated = True
                    self.lifecycle.stderr_bytes = stderr_bytes
                    self.lifecycle.stderr_truncated = stderr_truncated

        try:
            deadline = started_monotonic + live.provider_timeout_ms / 1000
            while True:
                drain_once(0.05)
                return_code = process.poll()
                if failure_kind is not None:
                    termination = terminate_process_group(
                        process,
                        reason=TerminationReason.EMERGENCY_CLEANUP,
                        grace_seconds=self._runner.termination_grace_ms / 1000,
                        drain=drain_once,
                    )
                    return_code = process.poll()
                    break
                if return_code is not None:
                    exists, inspection_error = process_group_exists(process.pid)
                    if inspection_error is not None:
                        termination = TerminationEvidence(
                            reason=TerminationReason.RESIDUAL_PROCESS,
                            sigterm_sent=False,
                            sigkill_sent=False,
                            process_group_cleared=False,
                            error=inspection_error,
                        )
                    elif exists:
                        termination = terminate_process_group(
                            process,
                            reason=TerminationReason.RESIDUAL_PROCESS,
                            grace_seconds=self._runner.termination_grace_ms / 1000,
                            drain=drain_once,
                        )
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    termination = terminate_process_group(
                        process,
                        reason=TerminationReason.TIMEOUT,
                        grace_seconds=self._runner.termination_grace_ms / 1000,
                        drain=drain_once,
                    )
                    return_code = process.poll()
                    break

            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=max(self._runner.termination_grace_ms / 1000, 0.5))
                except subprocess.TimeoutExpired:
                    termination = merge_termination_error(
                        termination,
                        "Provider parent remained after direct kill",
                    )
                else:
                    termination = merge_termination_error(
                        termination,
                        "Provider parent escaped its process group",
                    )
                return_code = process.poll()

            output_deadline = time.monotonic() + max(
                self._runner.termination_grace_ms / 1000,
                0.5,
            )
            while (
                any(name in {"stdout", "stderr"} for name in self._registered_names(selector))
                and time.monotonic() < output_deadline
            ):
                drain_once(0.05)
            if any(
                name in {"stdout", "stderr"} for name in self._registered_names(selector)
            ) and failure_kind is None:
                failure_kind = LiveFailureKind.EVIDENCE_ERROR
        except Exception:
            termination = terminate_process_group(
                process,
                reason=TerminationReason.EMERGENCY_CLEANUP,
                grace_seconds=self._runner.termination_grace_ms / 1000,
                drain=lambda _timeout: None,
            )
            return_code = process.poll()
            failure_kind = (
                LiveFailureKind.EVIDENCE_ERROR
                if termination.process_group_cleared
                else LiveFailureKind.PROCESS_CLEANUP_ERROR
            )
        finally:
            for name in tuple(streams):
                close_stream(name)
            try:
                selector.close()
            except Exception:
                if failure_kind is None:
                    failure_kind = LiveFailureKind.EVIDENCE_ERROR

        self.lifecycle.observe_termination(termination)
        self.lifecycle.return_code = return_code
        self.lifecycle.stderr_bytes = stderr_bytes
        self.lifecycle.stderr_truncated = stderr_truncated
        summary = parser.summary()
        if protocol_error is None:
            try:
                summary = parser.finish()
            except CodexProtocolError as error:
                protocol_error = error

        if not termination.process_group_cleared:
            final_failure = LiveFailureKind.PROCESS_CLEANUP_ERROR
        elif timed_out:
            final_failure = LiveFailureKind.PROVIDER_TIMEOUT
        elif failure_kind is not None:
            final_failure = failure_kind
        elif return_code is not None and return_code < 0:
            final_failure = LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
        elif summary.turn_failed:
            final_failure = LiveFailureKind.PROVIDER_TURN_FAILED
        elif return_code != 0:
            final_failure = LiveFailureKind.PROVIDER_CLI_NONZERO
        elif protocol_error is not None:
            final_failure = protocol_error.failure_kind
        elif prompt_offset != len(prompt):
            final_failure = LiveFailureKind.PROVIDER_INPUT_ERROR
        else:
            final_failure = LiveFailureKind.NONE
        status = (
            ProviderExecutionStatus.SUCCEEDED
            if final_failure is LiveFailureKind.NONE
            else ProviderExecutionStatus.FAILED
        )
        self.lifecycle.observe_termination(termination)
        self.lifecycle.return_code = return_code
        self.lifecycle.stderr_bytes = stderr_bytes
        self.lifecycle.stderr_truncated = stderr_truncated
        self.lifecycle.stdout_limit_exceeded = (
            failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
            or (
                protocol_error is not None
                and protocol_error.failure_kind
                is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
            )
        )
        return self._result_from_evidence(
            lambda: self._build_evidence(
                preflight=preflight,
                started_at=started_at,
                started_monotonic=started_monotonic,
                status=status,
                failure_kind=final_failure,
                failure_stage=(
                    None
                    if final_failure is LiveFailureKind.NONE
                    else CodexFailureStage.PROVIDER_PROCESS_COLLECTION
                ),
                exit_code=return_code,
                parser=parser,
                stderr_bytes=stderr_bytes,
                stderr_truncated=stderr_truncated,
                stdout_limit_exceeded=self.lifecycle.stdout_limit_exceeded,
                termination=termination,
                summary=summary,
            )
        )

    def _result_from_evidence(
        self,
        build: Callable[[], CodexExecutionEvidence],
    ) -> CodexRunResult:
        self.lifecycle.failure_stage = CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION
        evidence = build()
        self.lifecycle.failure_stage = (
            CodexFailureStage.PROVIDER_RUNNER_RESULT_CONSTRUCTION
        )
        return CodexRunResult(evidence=evidence)

    def _emergency_cleanup(self) -> None:
        lifecycle = self.lifecycle
        process = lifecycle.process
        if (
            process is None
            or lifecycle.invocation_state is not CodexInvocationState.PROCESS_STARTED
            or lifecycle.cleanup_state is not None
        ):
            return
        try:
            termination = terminate_process_group(
                process,
                reason=TerminationReason.EMERGENCY_CLEANUP,
                grace_seconds=self._runner.termination_grace_ms / 1000,
                drain=lambda _timeout: None,
            )
        except Exception:
            termination = TerminationEvidence(
                reason=TerminationReason.EMERGENCY_CLEANUP,
                sigterm_sent=False,
                sigkill_sent=False,
                process_group_cleared=False,
                error="Provider emergency cleanup could not be completed",
            )
        try:
            lifecycle.return_code = process.poll()
        except Exception:
            lifecycle.return_code = None
        lifecycle.observe_termination(termination)

    @staticmethod
    def _registered_names(selector: selectors.BaseSelector) -> set[str]:
        return {cast(str, key.data) for key in selector.get_map().values()}

    def _build_evidence(
        self,
        *,
        preflight: CodexPreflight,
        started_at: datetime,
        started_monotonic: float,
        status: ProviderExecutionStatus,
        failure_kind: LiveFailureKind,
        failure_stage: CodexFailureStage | None,
        exit_code: int | None,
        parser: CodexJsonlParser,
        stderr_bytes: int,
        stderr_truncated: bool,
        termination: TerminationEvidence,
        process_started: bool = True,
        stdout_limit_exceeded: bool = False,
        summary: CodexParseSummary | None = None,
    ) -> CodexExecutionEvidence:
        assert self._live.model is not None
        assert self._live.reasoning_effort is not None
        parsed = parser.summary() if summary is None else summary
        return CodexExecutionEvidence(
            schema_version=CODEX_EVIDENCE_SCHEMA_VERSION,
            provider=Provider.CODEX,
            cli_version=preflight.cli_version,
            cli_profile=preflight.cli_profile,
            execution_stage=(
                CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
                if self.lifecycle.invocation_state
                is not CodexInvocationState.NOT_ATTEMPTED
                else CodexExecutionStage.PREFLIGHT_COMPLETED
            ),
            failure_stage=failure_stage,
            runner_state=self.lifecycle.runner_state,
            invocation_state=self.lifecycle.invocation_state,
            cleanup_state=self.lifecycle.persisted_cleanup_state(),
            preflight_checked_at=preflight.checked_at,
            verified_flags=sorted(preflight.verified_flags),
            requested_model=self._live.model,
            requested_reasoning_effort=self._live.reasoning_effort,
            sandbox_mode="workspace-write",
            approval_policy="never",
            approval_basis=CodexApprovalBasis.EXPLICIT_CONFIG_NEVER,
            web_search_disabled=True,
            command_network_disabled=True,
            raw_stream_persisted=False,
            process_started=process_started,
            status=status,
            failure_kind=failure_kind,
            exit_code=exit_code,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=(
                max(0, int((time.monotonic() - started_monotonic) * 1000))
                if process_started
                else 0
            ),
            event_count=parsed.event_count,
            unknown_event_count=parsed.unknown_event_count,
            thread_started_count=parsed.thread_started_count,
            turn_started_count=parsed.turn_started_count,
            terminal_event=parsed.terminal_event,
            turn_completed_count=parsed.turn_completed_count,
            turn_failed_count=parsed.turn_failed_count,
            error_event_count=parsed.error_event_count,
            item_type_counts=parsed.item_type_counts,
            usage_metrics=parsed.usage_metrics,
            stdout_bytes=parser.total_bytes,
            stderr_bytes=stderr_bytes,
            stdout_limit_exceeded=stdout_limit_exceeded,
            stderr_truncated=stderr_truncated,
            termination=termination,
        )


def unsupported_platform_evidence(
    error: UnsupportedRunnerPlatformError,
    *,
    preflight: CodexPreflight,
    live: LiveSettings,
) -> CodexExecutionEvidence:
    del error
    assert live.model is not None
    assert live.reasoning_effort is not None
    now = datetime.now(UTC)
    return CodexExecutionEvidence(
        schema_version=CODEX_EVIDENCE_SCHEMA_VERSION,
        provider=Provider.CODEX,
        cli_version=preflight.cli_version,
        cli_profile=preflight.cli_profile,
        execution_stage=CodexExecutionStage.PREFLIGHT_COMPLETED,
        failure_stage=CodexFailureStage.PROVIDER_RUNTIME_PRECHECK,
        runner_state=CodexRunnerState.STARTED,
        invocation_state=CodexInvocationState.NOT_ATTEMPTED,
        cleanup_state=CodexCleanupState.NOT_APPLICABLE,
        preflight_checked_at=preflight.checked_at,
        verified_flags=sorted(preflight.verified_flags),
        requested_model=live.model,
        requested_reasoning_effort=live.reasoning_effort,
        sandbox_mode="workspace-write",
        approval_policy=None,
        approval_basis=None,
        web_search_disabled=True,
        command_network_disabled=True,
        raw_stream_persisted=False,
        process_started=False,
        status=ProviderExecutionStatus.FAILED,
        failure_kind=LiveFailureKind.UNSUPPORTED_PLATFORM,
        exit_code=None,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        event_count=0,
        unknown_event_count=0,
        thread_started_count=0,
        turn_started_count=0,
        terminal_event=CodexTerminalEvent.NONE,
        turn_completed_count=0,
        turn_failed_count=0,
        error_event_count=0,
        item_type_counts={},
        usage_metrics=_not_available_usage(),
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_limit_exceeded=False,
        stderr_truncated=False,
        termination=termination_without_signal(),
    )
