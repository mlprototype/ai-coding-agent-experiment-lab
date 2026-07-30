"""Antigravity CLI Provider preflight, strict parser, and evidence construction for Phase 5.

Offline Slice 5A implementation.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from agentlab.models import (
    ANTIGRAVITY_ALLOWLISTED_CLI_VERSIONS,
    AntigravityCleanupErrorCode,
    AntigravityCliProfile,
    AntigravityEventType,
    AntigravityExecutionEvidence,
    AntigravityExecutionStage,
    AntigravityFailureStage,
    AntigravityHelpMarker,
    AntigravityPermissionMode,
    AntigravityPreflightCommandEvidence,
    AntigravityPreflightOperation,
    AntigravityPromptTransport,
    AntigravityReasoningEffort,
    AntigravityStepType,
    AntigravityTerminalStatus,
    AntigravityTerminationEvidence,
    CodexCleanupState,
    CodexInvocationState,
    LiveFailureKind,
    ProviderExecutionStatus,
    TerminationReason,
    UsageMetrics,
    UsageMetricSource,
)

DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
PREFLIGHT_MAX_BYTES = 64 * 1024


def select_antigravity_profile(
    cli_version: str | None,
    verified_flags: list[AntigravityHelpMarker],
    allowlist: frozenset[str] = ANTIGRAVITY_ALLOWLISTED_CLI_VERSIONS,
) -> AntigravityCliProfile:
    """Select the versioned Antigravity CLI profile or NOT_SELECTED."""
    if cli_version is None or cli_version not in allowlist:
        return AntigravityCliProfile.NOT_SELECTED

    required = set(AntigravityHelpMarker)
    if not required.issubset(set(verified_flags)):
        return AntigravityCliProfile.NOT_SELECTED

    return AntigravityCliProfile.HEADLESS_STREAM_JSON_V1


def _no_non_finite_or_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    dict_obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in dict_obj:
            raise ValueError(f"Duplicate JSON key: {key}")
        dict_obj[key] = value
    return dict_obj


def _reject_non_finite_constants(raw: str) -> None:
    raise ValueError(f"Non-finite JSON constant not allowed: {raw}")


class StrictAntigravityStreamParser:
    """Incremental UTF-8 NDJSON parser for Antigravity stream-json output."""

    def __init__(
        self,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.max_line_bytes = max_line_bytes
        self.max_output_bytes = max_output_bytes

        self.buffer = bytearray()
        self.total_bytes_read = 0

        self.event_count = 0
        self.unknown_event_count = 0
        self.unknown_step_type_count = 0

        self.init_event_index: int | None = None
        self.result_event_index: int | None = None

        self.event_counts: dict[AntigravityEventType, int] = {
            t: 0 for t in AntigravityEventType
        }
        self.step_counts: dict[AntigravityStepType, int] = {
            s: 0 for s in AntigravityStepType
        }

        self.init_requested_model_present = False
        self.init_requested_agent_present = False
        self.observed_permission_mode: AntigravityPermissionMode | None = None

        self.normalized_terminal_status: AntigravityTerminalStatus | None = None
        self.terminal_num_turns: int | None = None
        self.provider_duration_ms: int | None = None

        self.usage_metrics: UsageMetrics = UsageMetrics(
            source=UsageMetricSource.NOT_AVAILABLE
        )

        self.protocol_error: str | None = None
        self.output_limit_exceeded = False
        self.result_received = False

    def parse_chunk(self, chunk: bytes) -> None:
        if self.protocol_error is not None or self.output_limit_exceeded:
            self.buffer.clear()
            return

        self.total_bytes_read += len(chunk)
        if self.total_bytes_read > self.max_output_bytes:
            self.output_limit_exceeded = True
            self.protocol_error = "Total output exceeded maximum bytes limit"
            self.buffer.clear()
            return

        self.buffer.extend(chunk)

        while True:
            newline_index = self.buffer.find(b"\n")
            if newline_index == -1:
                if len(self.buffer) > self.max_line_bytes:
                    self.output_limit_exceeded = True
                    self.protocol_error = "Line byte count exceeded limit"
                break

            line_bytes = bytes(self.buffer[:newline_index]).rstrip(b"\r")
            del self.buffer[: newline_index + 1]

            if not line_bytes:
                self.protocol_error = "Empty line in NDJSON stream"
                self.buffer.clear()
                return

            if len(line_bytes) > self.max_line_bytes:
                self.output_limit_exceeded = True
                self.protocol_error = "Line byte count exceeded limit"
                self.buffer.clear()
                return

            try:
                line_str = line_bytes.decode("utf-8")
                obj = json.loads(
                    line_str,
                    object_pairs_hook=_no_non_finite_or_duplicates,
                    parse_constant=_reject_non_finite_constants,
                )
            except Exception as exc:
                self.protocol_error = f"JSON parse error: {exc}"
                self.buffer.clear()
                return

            if not isinstance(obj, dict):
                self.protocol_error = "Stream line is not a JSON object"
                self.buffer.clear()
                return

            self._process_event(obj)
            if self.protocol_error is not None or self.output_limit_exceeded:
                self.buffer.clear()
                return

        if self.protocol_error is not None or self.output_limit_exceeded:
            self.buffer.clear()

    def finalize(self) -> None:
        if self.protocol_error is not None or self.output_limit_exceeded:
            self.buffer.clear()
            return

        if self.buffer:
            line_bytes = bytes(self.buffer).rstrip(b"\r")
            self.buffer.clear()
            if line_bytes:
                if len(line_bytes) > self.max_line_bytes:
                    self.output_limit_exceeded = True
                    self.protocol_error = "Line byte count exceeded limit"
                    return
                try:
                    line_str = line_bytes.decode("utf-8")
                    obj = json.loads(
                        line_str,
                        object_pairs_hook=_no_non_finite_or_duplicates,
                        parse_constant=_reject_non_finite_constants,
                    )
                    if not isinstance(obj, dict):
                        self.protocol_error = "Stream line is not a JSON object"
                        return
                    self._process_event(obj)
                except Exception as exc:
                    self.protocol_error = f"Trailing JSON line error: {exc}"
                    return

        if not self.result_received and self.protocol_error is None:
            self.protocol_error = "Stream ended without terminal result event"

    def _process_event(self, obj: dict[str, Any]) -> None:
        if self.result_received:
            self.protocol_error = "Received event after terminal result event"
            return

        event_type_str = obj.get("event")
        if not isinstance(event_type_str, str):
            self.protocol_error = "Missing or invalid 'event' field"
            return

        try:
            event_type = AntigravityEventType(event_type_str)
        except ValueError:
            self.unknown_event_count += 1
            self.event_count += 1
            self.protocol_error = f"Unknown top-level event: {event_type_str}"
            return

        current_index = self.event_count
        self.event_count += 1
        self.event_counts[event_type] += 1

        if event_type is AntigravityEventType.INIT:
            if self.init_event_index is not None:
                self.protocol_error = "Multiple init events in stream"
                return
            if current_index != 0:
                self.protocol_error = "init event is not first event"
                return
            self.init_event_index = current_index

            self.init_requested_model_present = "requested_model" in obj or "model" in obj
            self.init_requested_agent_present = "requested_agent" in obj or "agent" in obj

            mode_str = obj.get("permission_mode")
            if isinstance(mode_str, str):
                try:
                    self.observed_permission_mode = AntigravityPermissionMode(mode_str)
                except ValueError:
                    self.observed_permission_mode = None
                    self.protocol_error = f"Unknown permission mode: {mode_str}"
                    return

        elif event_type is AntigravityEventType.STEP_UPDATE:
            if self.init_event_index is None:
                self.protocol_error = "step_update event before init event"
                return

            step_type_str = obj.get("step_type")
            if not isinstance(step_type_str, str):
                self.unknown_step_type_count += 1
                self.protocol_error = "Missing or non-string step_type"
                return

            try:
                step_type = AntigravityStepType(step_type_str)
                self.step_counts[step_type] += 1
            except ValueError:
                self.unknown_step_type_count += 1
                self.protocol_error = f"Unknown step_type: {step_type_str}"
                return

        elif event_type is AntigravityEventType.RESULT:
            if self.init_event_index is None:
                self.protocol_error = "result event before init event"
                return
            if self.result_event_index is not None:
                self.protocol_error = "Multiple result events in stream"
                return

            self.result_event_index = current_index
            self.result_received = True

            status_str = obj.get("status")
            if not isinstance(status_str, str):
                self.protocol_error = "Missing or invalid status in result event"
                return

            try:
                self.normalized_terminal_status = AntigravityTerminalStatus(status_str)
            except ValueError:
                self.protocol_error = f"Invalid terminal status: {status_str}"
                return

            num_turns = obj.get("num_turns")
            if num_turns is not None:
                if (
                    isinstance(num_turns, bool)
                    or not isinstance(num_turns, int)
                    or num_turns < 0
                    or num_turns > 10_000
                ):
                    self.protocol_error = f"Invalid num_turns: {num_turns}"
                    return
                self.terminal_num_turns = num_turns

            duration_ms = obj.get("duration_ms")
            if duration_ms is not None:
                if (
                    isinstance(duration_ms, bool)
                    or not isinstance(duration_ms, (int, float))
                    or duration_ms < 0
                    or duration_ms > 86_400_000
                ):
                    self.protocol_error = f"Invalid duration_ms: {duration_ms}"
                    return
                self.provider_duration_ms = round(float(duration_ms))

            usage_raw = obj.get("usage")
            if usage_raw is not None:
                if not isinstance(usage_raw, dict):
                    self.protocol_error = "usage field must be a JSON object"
                    return

                in_tok = usage_raw.get("input_tokens")
                cache_tok = usage_raw.get("cache_read_tokens")
                out_tok = usage_raw.get("output_tokens")
                think_tok = usage_raw.get("thinking_tokens")
                tot_tok = usage_raw.get("total_tokens")

                def _strict_valid_int(val: Any, name: str) -> int | None:
                    if val is None:
                        return None
                    if (
                        isinstance(val, bool)
                        or not isinstance(val, int)
                        or val < 0
                        or val > 10_000_000
                    ):
                        raise ValueError(f"Invalid {name}: {val}")
                    return int(val)

                try:
                    in_val = _strict_valid_int(in_tok, "input_tokens")
                    cache_val = _strict_valid_int(cache_tok, "cache_read_tokens")
                    out_val = _strict_valid_int(out_tok, "output_tokens")
                    think_val = _strict_valid_int(think_tok, "thinking_tokens")
                    tot_val = _strict_valid_int(tot_tok, "total_tokens")
                except ValueError as err:
                    self.protocol_error = str(err)
                    return

                if (
                    tot_val is not None
                    and (in_val is None or out_val is None or tot_val != in_val + out_val)
                ):
                    self.protocol_error = (
                        "total_tokens does not equal input_tokens + output_tokens"
                    )
                    return

                if cache_val is not None:
                    if in_val is None:
                        self.protocol_error = "cached_input_tokens requires input_tokens"
                        return
                    if cache_val > in_val:
                        self.protocol_error = "cached_input_tokens exceeds input_tokens"
                        return

                if think_val is not None:
                    if out_val is None:
                        self.protocol_error = "reasoning_output_tokens requires output_tokens"
                        return
                    if think_val > out_val:
                        self.protocol_error = "reasoning_output_tokens exceeds output_tokens"
                        return

                if any(v is not None for v in (in_val, cache_val, out_val, think_val)):
                    self.usage_metrics = UsageMetrics(
                        input_tokens=in_val,
                        cached_input_tokens=cache_val,
                        output_tokens=out_val,
                        reasoning_output_tokens=think_val,
                        source=UsageMetricSource.PROVIDER_REPORTED,
                    )
                else:
                    self.usage_metrics = UsageMetrics(source=UsageMetricSource.NOT_AVAILABLE)


@dataclass(frozen=True)
class AntigravityEvidenceDiagnostic:
    """Fixed diagnostic returned when AntigravityExecutionEvidence construction fails."""

    error_code: str = "EVIDENCE_CONSTRUCTION_FAILED"
    failure_kind: LiveFailureKind = LiveFailureKind.EVIDENCE_ERROR


@dataclass(frozen=True)
class AntigravityPreflightProcessResult:
    """Bounded in-memory result of one version/help subprocess."""

    returncode: int | None
    stdout: str
    stderr: str
    failure_stage: AntigravityFailureStage | None
    failure_kind: LiveFailureKind
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    collection_error_observed: bool
    termination: AntigravityTerminationEvidence

    @property
    def output_limit_exceeded(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated

    def __iter__(self) -> Iterator[object]:
        """Keep the former private tuple shape available during Slice 5A."""
        yield self.returncode
        yield self.stdout
        yield self.stderr
        yield self.failure_stage
        yield self.failure_kind
        yield self.stdout_bytes
        yield self.stderr_bytes
        yield self.output_limit_exceeded

    def to_evidence(
        self,
        operation: AntigravityPreflightOperation,
    ) -> AntigravityPreflightCommandEvidence:
        return AntigravityPreflightCommandEvidence(
            operation=operation,
            returncode=self.returncode,
            stdout_bytes=self.stdout_bytes,
            stderr_bytes=self.stderr_bytes,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            collection_error_observed=self.collection_error_observed,
            failure_stage=self.failure_stage,
            failure_kind=self.failure_kind,
            termination=self.termination,
        )


@dataclass(frozen=True)
class AntigravityPreflightResult:
    """Structured, redaction-ready result of the complete preflight."""

    profile: AntigravityCliProfile
    cli_version: str | None
    verified_flags: tuple[AntigravityHelpMarker, ...]
    checked_at: datetime
    failure_stage: AntigravityFailureStage | None
    failure_kind: LiveFailureKind
    version_probe: AntigravityPreflightProcessResult | None
    help_probe: AntigravityPreflightProcessResult | None

    def __iter__(self) -> Iterator[object]:
        """Keep the former public tuple shape available during Slice 5A."""
        yield self.profile
        yield self.cli_version
        yield list(self.verified_flags)
        yield self.checked_at
        yield self.failure_stage
        yield self.failure_kind

    @property
    def process_results(self) -> tuple[AntigravityPreflightProcessResult, ...]:
        return tuple(
            result
            for result in (self.version_probe, self.help_probe)
            if result is not None
        )

    @property
    def stdout_bytes(self) -> int:
        return sum(result.stdout_bytes for result in self.process_results)

    @property
    def stderr_bytes(self) -> int:
        return sum(result.stderr_bytes for result in self.process_results)

    @property
    def stdout_truncated(self) -> bool:
        return any(result.stdout_truncated for result in self.process_results)

    @property
    def stderr_truncated(self) -> bool:
        return any(result.stderr_truncated for result in self.process_results)

    def command_evidence(self) -> list[AntigravityPreflightCommandEvidence]:
        evidence: list[AntigravityPreflightCommandEvidence] = []
        if self.version_probe is not None:
            evidence.append(
                self.version_probe.to_evidence(
                    AntigravityPreflightOperation.VERSION
                )
            )
        if self.help_probe is not None:
            evidence.append(
                self.help_probe.to_evidence(
                    AntigravityPreflightOperation.HELP
                )
            )
        return evidence


def _is_process_group_alive(pgid: int) -> bool:
    """Check if any process in the process group is still alive."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleared_preflight_termination() -> AntigravityTerminationEvidence:
    return AntigravityTerminationEvidence(
        reason=TerminationReason.NONE,
        sigterm_sent=False,
        sigkill_sent=False,
        process_group_cleared=True,
        error_code=AntigravityCleanupErrorCode.NONE,
    )


def _run_preflight_subprocess(
    cmd: str,
    args: list[str],
    timeout_seconds: float,
    clean_env: dict[str, str],
) -> AntigravityPreflightProcessResult:
    """Execute preflight subprocess with strict process group isolation and bounded collection."""
    try:
        proc = subprocess.Popen(
            [cmd, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env=clean_env,
        )
    except FileNotFoundError:
        return AntigravityPreflightProcessResult(
            returncode=None,
            stdout="",
            stderr="",
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            failure_kind=LiveFailureKind.PROVIDER_UNAVAILABLE,
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            collection_error_observed=False,
            termination=_cleared_preflight_termination(),
        )
    except Exception:
        return AntigravityPreflightProcessResult(
            returncode=None,
            stdout="",
            stderr="",
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            failure_kind=LiveFailureKind.PROVIDER_UNAVAILABLE,
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            collection_error_observed=False,
            termination=_cleared_preflight_termination(),
        )

    stdout_buf = bytearray()
    stderr_buf = bytearray()

    pgid = proc.pid
    start_time = time.monotonic()
    timed_out = False
    collection_error = False
    stdout_truncated = False
    stderr_truncated = False
    cleanup_failed = False
    cleanup_exception = False
    cleanup_observation_error = False
    sigterm_sent = False
    sigkill_sent = False
    termination_reason = TerminationReason.NONE

    pipes = [proc.stdout, proc.stderr]
    for pipe in pipes:
        if pipe is not None:
            try:
                os.set_blocking(pipe.fileno(), False)
            except Exception:
                collection_error = True

    try:
        if not collection_error:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout_seconds:
                    timed_out = True
                    break

                active_pipes = [p for p in pipes if p is not None and not p.closed]
                if not active_pipes:
                    break

                remaining_timeout = max(0.001, timeout_seconds - elapsed)
                select_wait = min(0.1, remaining_timeout)
                try:
                    rlist, _, _ = select.select(active_pipes, [], [], select_wait)
                except Exception:
                    collection_error = True
                    break

                for pipe in rlist:
                    buf = stdout_buf if pipe is proc.stdout else stderr_buf
                    stream_already_truncated = (
                        stdout_truncated
                        if pipe is proc.stdout
                        else stderr_truncated
                    )
                    max_read = (
                        4096
                        if stream_already_truncated
                        else min(
                            4096,
                            (PREFLIGHT_MAX_BYTES + 1) - len(buf),
                        )
                    )

                    try:
                        data = pipe.read(max_read)
                    except Exception:
                        collection_error = True
                        break

                    if not data:
                        pipe.close()
                        continue

                    if stream_already_truncated:
                        continue

                    buf.extend(data)
                    if len(buf) > PREFLIGHT_MAX_BYTES:
                        if pipe is proc.stdout:
                            stdout_truncated = True
                        else:
                            stderr_truncated = True
                        del buf[PREFLIGHT_MAX_BYTES:]

                if collection_error:
                    break

                if proc.poll() is not None:
                    # Read any remaining buffered data up to limit + 1 byte
                    for pipe in [proc.stdout, proc.stderr]:
                        if pipe is not None and not pipe.closed:
                            buf = stdout_buf if pipe is proc.stdout else stderr_buf
                            max_read = (PREFLIGHT_MAX_BYTES + 1) - len(buf)
                            if max_read > 0:
                                try:
                                    data = pipe.read(max_read)
                                    if data:
                                        buf.extend(data)
                                except Exception:
                                    collection_error = True
                                    break
                            if len(buf) > PREFLIGHT_MAX_BYTES:
                                if pipe is proc.stdout:
                                    stdout_truncated = True
                                else:
                                    stderr_truncated = True
                                del buf[PREFLIGHT_MAX_BYTES:]
                            pipe.close()
                    break

    except Exception:
        collection_error = True

    # Single mandatory cleanup block for process group and pipes
    finally:
        try:
            # Check if termination is required (timeout, alive pgid, or un-reaped child)
            if (
                not timed_out
                and not collection_error
                and proc.poll() is None
            ):
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    cleanup_failed = True
            # Poll first so a normally exited direct child is reaped before the
            # process-group liveness check. On macOS an unreaped child can
            # otherwise make killpg(..., 0) report an ambiguous permission
            # state even though no residual process remains.
            proc.poll()
            pg_alive = False
            try:
                pg_alive = _is_process_group_alive(pgid)
            except Exception:
                cleanup_failed = True
                cleanup_observation_error = True
                pg_alive = True

            if timed_out or pg_alive or (proc.poll() is None):
                termination_reason = (
                    TerminationReason.TIMEOUT
                    if timed_out
                    else TerminationReason.EMERGENCY_CLEANUP
                )
                if pg_alive:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        sigterm_sent = True
                    except ProcessLookupError:
                        pass

                term_deadline = time.monotonic() + 0.5
                while time.monotonic() < term_deadline:
                    if proc.poll() is None:
                        try:
                            proc.wait(timeout=0.02)
                        except subprocess.TimeoutExpired:
                            pass
                        except Exception:
                            cleanup_failed = True

                    try:
                        if not _is_process_group_alive(pgid):
                            break
                    except Exception:
                        cleanup_failed = True
                        cleanup_observation_error = True
                        break
                    time.sleep(0.01)

                try:
                    pg_alive_after_term = _is_process_group_alive(pgid)
                except Exception:
                    cleanup_failed = True
                    cleanup_observation_error = True
                    pg_alive_after_term = True

                if pg_alive_after_term:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                        sigkill_sent = True
                    except ProcessLookupError:
                        pass

                    kill_deadline = time.monotonic() + 0.5
                    while time.monotonic() < kill_deadline:
                        if proc.poll() is None:
                            try:
                                proc.wait(timeout=0.02)
                            except subprocess.TimeoutExpired:
                                pass
                            except Exception:
                                cleanup_failed = True

                        try:
                            if not _is_process_group_alive(pgid):
                                break
                        except Exception:
                            cleanup_failed = True
                            cleanup_observation_error = True
                            break
                        time.sleep(0.01)

                    try:
                        if _is_process_group_alive(pgid):
                            cleanup_failed = True
                    except Exception:
                        cleanup_failed = True
                        cleanup_observation_error = True

            # Final reap attempt for direct child
            if proc.poll() is None:
                try:
                    proc.wait(timeout=0.2)
                except Exception:
                    cleanup_failed = True

            # Final re-verification: if child is reaped and pg is extinct, cleanup succeeded
            try:
                proc_reaped = proc.poll() is not None
                pg_extinct = not _is_process_group_alive(pgid)
                if not proc_reaped or not pg_extinct:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
                cleanup_observation_error = True

        except Exception:
            cleanup_exception = True
            cleanup_failed = True

        # Ensure pipes are closed
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()

    stdout_bytes = len(stdout_buf)
    stderr_bytes = len(stderr_buf)
    stdout_str = stdout_buf.decode("utf-8", errors="replace")
    stderr_str = stderr_buf.decode("utf-8", errors="replace")
    cleanup_error_code = AntigravityCleanupErrorCode.NONE
    if cleanup_failed or cleanup_exception:
        cleanup_error_code = (
            AntigravityCleanupErrorCode.CLEANUP_PROCESS_ERROR
            if cleanup_exception or cleanup_observation_error
            else AntigravityCleanupErrorCode.CLEANUP_TIMEOUT
        )
    termination = AntigravityTerminationEvidence(
        reason=termination_reason,
        sigterm_sent=sigterm_sent,
        sigkill_sent=sigkill_sent,
        process_group_cleared=not (cleanup_failed or cleanup_exception),
        error_code=cleanup_error_code,
    )

    # Priority ranking: Cleanup failure > Timeout > Output limit > Collection error
    if cleanup_failed or cleanup_exception:
        return AntigravityPreflightProcessResult(
            returncode=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP,
            failure_kind=LiveFailureKind.PROCESS_CLEANUP_ERROR,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            collection_error_observed=collection_error,
            termination=termination,
        )

    if timed_out:
        return AntigravityPreflightProcessResult(
            returncode=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            failure_kind=LiveFailureKind.PROVIDER_TIMEOUT,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            collection_error_observed=collection_error,
            termination=termination,
        )

    if stdout_truncated or stderr_truncated:
        return AntigravityPreflightProcessResult(
            returncode=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            failure_kind=LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            collection_error_observed=collection_error,
            termination=termination,
        )

    if collection_error:
        return AntigravityPreflightProcessResult(
            returncode=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            failure_kind=LiveFailureKind.EVIDENCE_ERROR,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            collection_error_observed=True,
            termination=termination,
        )

    if proc.returncode != 0:
        return AntigravityPreflightProcessResult(
            returncode=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            failure_kind=LiveFailureKind.PROVIDER_UNAVAILABLE,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=False,
            stderr_truncated=False,
            collection_error_observed=False,
            termination=termination,
        )

    return AntigravityPreflightProcessResult(
        returncode=proc.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
        failure_stage=None,
        failure_kind=LiveFailureKind.NONE,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stdout_truncated=False,
        stderr_truncated=False,
        collection_error_observed=False,
        termination=termination,
    )


def _matches_cli_flag(flag_val: str, text: str) -> bool:
    """Check if flag_val matches exact CLI token boundaries in text."""
    pattern = r"(?:^|[\s=,'\"])" + re.escape(flag_val) + r"(?:$|[\s=,'\"])"
    return bool(re.search(pattern, text))


def probe_antigravity_preflight(
    executable_path: str | None = None,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    allowlist: frozenset[str] = ANTIGRAVITY_ALLOWLISTED_CLI_VERSIONS,
) -> AntigravityPreflightResult:
    """Execute bounded agy --version and agy --help preflight."""
    checked_at = datetime.now(UTC)
    cmd = executable_path or shutil.which("agy")

    if cmd is None:
        missing_probe = AntigravityPreflightProcessResult(
            returncode=None,
            stdout="",
            stderr="",
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            failure_kind=LiveFailureKind.PROVIDER_UNAVAILABLE,
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            collection_error_observed=False,
            termination=_cleared_preflight_termination(),
        )
        return AntigravityPreflightResult(
            profile=AntigravityCliProfile.NOT_SELECTED,
            cli_version=None,
            verified_flags=(),
            checked_at=checked_at,
            failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            failure_kind=LiveFailureKind.PROVIDER_UNAVAILABLE,
            version_probe=missing_probe,
            help_probe=None,
        )

    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME", "HOME", "LANG", "LC_ALL"}
    }

    # Version probe
    version_probe = _run_preflight_subprocess(
        cmd,
        ["--version"],
        timeout_seconds,
        clean_env,
    )
    if version_probe.failure_kind is not LiveFailureKind.NONE:
        final_stage = (
            version_probe.failure_stage
            if version_probe.failure_stage is not None
            else AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
        )
        return AntigravityPreflightResult(
            profile=AntigravityCliProfile.NOT_SELECTED,
            cli_version=None,
            verified_flags=(),
            checked_at=checked_at,
            failure_stage=final_stage,
            failure_kind=version_probe.failure_kind,
            version_probe=version_probe,
            help_probe=None,
        )

    version_str: str | None = None
    if version_probe.returncode == 0:
        stdout_lines = [
            line.strip()
            for line in version_probe.stdout.splitlines()
            if line.strip()
        ]
        stderr_lines = [
            line.strip()
            for line in version_probe.stderr.splitlines()
            if line.strip()
        ]
        all_non_empty_lines = stdout_lines + stderr_lines

        # Reject if total non-empty lines across stdout + stderr is not exactly 1
        if len(all_non_empty_lines) == 1:
            single_line = all_non_empty_lines[0]
            if match := re.fullmatch(r"agy \d+\.\d+\.\d+", single_line):
                version_str = match.group(0)

    # Help probe
    help_probe = _run_preflight_subprocess(
        cmd,
        ["--help"],
        timeout_seconds,
        clean_env,
    )
    if help_probe.failure_kind is not LiveFailureKind.NONE:
        final_stage = (
            help_probe.failure_stage
            if help_probe.failure_stage is not None
            else AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
        )
        return AntigravityPreflightResult(
            profile=AntigravityCliProfile.NOT_SELECTED,
            cli_version=version_str,
            verified_flags=(),
            checked_at=checked_at,
            failure_stage=final_stage,
            failure_kind=help_probe.failure_kind,
            version_probe=version_probe,
            help_probe=help_probe,
        )

    verified_flags: list[AntigravityHelpMarker] = []
    if help_probe.returncode == 0:
        help_output = f"{help_probe.stdout}\n{help_probe.stderr}".lower()
        for marker in AntigravityHelpMarker:
            if _matches_cli_flag(marker.value, help_output):
                verified_flags.append(marker)

    profile = select_antigravity_profile(version_str, verified_flags, allowlist=allowlist)
    if profile is not AntigravityCliProfile.NOT_SELECTED:
        failure_stage = None
        failure_kind = LiveFailureKind.NONE
    else:
        failure_stage = AntigravityFailureStage.PREFLIGHT
        failure_kind = LiveFailureKind.PROVIDER_UNAVAILABLE

    return AntigravityPreflightResult(
        profile=profile,
        cli_version=version_str,
        verified_flags=tuple(
            sorted(verified_flags, key=lambda flag: flag.value)
        ),
        checked_at=checked_at,
        failure_stage=failure_stage,
        failure_kind=failure_kind,
        version_probe=version_probe,
        help_probe=help_probe,
    )



def build_antigravity_evidence(
    *,
    preflight_result: AntigravityPreflightResult | None = None,
    cli_version: str | None = None,
    profile: AntigravityCliProfile = AntigravityCliProfile.NOT_SELECTED,
    preflight_checked_at: datetime | None = None,
    preflight_verified_flags: list[AntigravityHelpMarker] | None = None,
    requested_model: str | None = None,
    requested_reasoning_effort: AntigravityReasoningEffort | None = None,
    execution_stage: AntigravityExecutionStage = AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED,
    failure_stage: AntigravityFailureStage | None = None,
    invocation_state: CodexInvocationState = CodexInvocationState.NOT_ATTEMPTED,
    cleanup_state: CodexCleanupState = CodexCleanupState.NOT_APPLICABLE,
    observed_permission_mode: AntigravityPermissionMode | None = None,
    parser: StrictAntigravityStreamParser | None = None,
    exit_code: int | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    termination: AntigravityTerminationEvidence | None = None,
    initial_failure_kind: LiveFailureKind = LiveFailureKind.NONE,
) -> AntigravityExecutionEvidence:
    """Construct redacted AntigravityExecutionEvidence with exclusive failure taxonomy."""
    schema_version: Literal["1.0", "1.1"] = "1.0"
    preflight_commands: list[AntigravityPreflightCommandEvidence] | None = None
    if preflight_result is not None:
        schema_version = "1.1"
        preflight_commands = preflight_result.command_evidence()
        cli_version = preflight_result.cli_version
        profile = preflight_result.profile
        preflight_checked_at = preflight_result.checked_at
        preflight_verified_flags = list(preflight_result.verified_flags)
        if failure_stage is None:
            failure_stage = preflight_result.failure_stage
        if initial_failure_kind is LiveFailureKind.NONE:
            initial_failure_kind = preflight_result.failure_kind
        if stdout_bytes == 0:
            stdout_bytes = preflight_result.stdout_bytes
        if stderr_bytes == 0:
            stderr_bytes = preflight_result.stderr_bytes
        stdout_truncated = (
            stdout_truncated or preflight_result.stdout_truncated
        )
        stderr_truncated = (
            stderr_truncated or preflight_result.stderr_truncated
        )
        if (
            execution_stage
            is AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
            and (
                preflight_result.failure_stage
                is AntigravityFailureStage.PREFLIGHT
                or preflight_result.failure_kind is LiveFailureKind.NONE
            )
        ):
            execution_stage = AntigravityExecutionStage.PREFLIGHT_COMPLETED

    p = parser or StrictAntigravityStreamParser()
    term = termination or AntigravityTerminationEvidence(
        reason=TerminationReason.NONE,
        sigterm_sent=False,
        sigkill_sent=False,
        process_group_cleared=True,
        error_code=AntigravityCleanupErrorCode.NONE,
    )

    final_failure_kind = LiveFailureKind.NONE
    final_failure_stage: AntigravityFailureStage | None = None
    provider_status = ProviderExecutionStatus.SUCCEEDED

    if term.error_code is not AntigravityCleanupErrorCode.NONE or not term.process_group_cleared:
        final_failure_kind = LiveFailureKind.PROCESS_CLEANUP_ERROR
        final_failure_stage = (
            failure_stage or AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
        )
    elif term.reason is TerminationReason.TIMEOUT:
        final_failure_kind = LiveFailureKind.PROVIDER_TIMEOUT
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif (
        p.output_limit_exceeded
        or stdout_truncated
        or stderr_truncated
        or initial_failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    ):
        final_failure_kind = LiveFailureKind.PROVIDER_OUTPUT_LIMIT
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif (
        initial_failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
        or p.protocol_error is not None
    ):
        final_failure_kind = LiveFailureKind.PROVIDER_PROTOCOL_ERROR
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif exit_code is not None and exit_code < 0:
        final_failure_kind = LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif p.normalized_terminal_status in {
        AntigravityTerminalStatus.ERROR,
        AntigravityTerminalStatus.CANCELED,
        AntigravityTerminalStatus.INTERRUPTED,
    }:
        final_failure_kind = LiveFailureKind.PROVIDER_TURN_FAILED
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif exit_code is not None and exit_code > 0:
        final_failure_kind = LiveFailureKind.PROVIDER_CLI_NONZERO
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif initial_failure_kind is not LiveFailureKind.NONE:
        final_failure_kind = initial_failure_kind
        final_failure_stage = failure_stage or AntigravityFailureStage.PREFLIGHT
    elif (
        p.normalized_terminal_status is not AntigravityTerminalStatus.SUCCESS
        or p.terminal_num_turns != 1
    ):
        final_failure_kind = LiveFailureKind.PROVIDER_PROTOCOL_ERROR
        final_failure_stage = AntigravityFailureStage.STREAM_PARSING


    if final_failure_kind is not LiveFailureKind.NONE:
        provider_status = ProviderExecutionStatus.FAILED
    else:
        final_failure_stage = None

    sorted_event_counts = {t: p.event_counts[t] for t in AntigravityEventType}
    sorted_step_counts = {s: p.step_counts[s] for s in AntigravityStepType}

    return AntigravityExecutionEvidence(
        schema_version=schema_version,
        cli_version=cli_version,
        profile=profile,
        preflight_checked_at=preflight_checked_at,
        preflight_verified_flags=preflight_verified_flags or [],
        preflight_commands=preflight_commands,
        requested_model=requested_model,
        requested_reasoning_effort=requested_reasoning_effort,
        requested_output_format="stream-json",
        prompt_transport=AntigravityPromptTransport.UNSUPPORTED,
        prompt_argv_exposure=True,
        execution_stage=execution_stage,
        failure_stage=final_failure_stage,
        invocation_state=invocation_state,
        cleanup_state=cleanup_state,
        requested_sandbox=True,
        observed_permission_mode=observed_permission_mode or p.observed_permission_mode,
        raw_stream_persisted=False,
        provider_status=provider_status,
        normalized_terminal_status=p.normalized_terminal_status,
        exit_code=exit_code,
        terminal_num_turns=p.terminal_num_turns,
        provider_duration_ms=p.provider_duration_ms,
        init_requested_model_present=p.init_requested_model_present,
        init_requested_agent_present=p.init_requested_agent_present,
        event_count=p.event_count,
        unknown_event_count=p.unknown_event_count,
        unknown_step_type_count=p.unknown_step_type_count,
        init_event_index=p.init_event_index,
        result_event_index=p.result_event_index,
        event_counts=sorted_event_counts,
        step_counts=sorted_step_counts,
        usage_metrics=p.usage_metrics,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stdout_truncated=p.output_limit_exceeded or stdout_truncated,
        stderr_truncated=stderr_truncated,
        termination=term,
        failure_kind=final_failure_kind,
    )


def safe_build_antigravity_evidence(
    **kwargs: Any,
) -> AntigravityExecutionEvidence | AntigravityEvidenceDiagnostic:
    """Safely construct AntigravityExecutionEvidence or return typed Diagnostic."""
    try:
        return build_antigravity_evidence(**kwargs)
    except Exception:
        return AntigravityEvidenceDiagnostic(
            error_code="EVIDENCE_CONSTRUCTION_FAILED",
            failure_kind=LiveFailureKind.EVIDENCE_ERROR,
        )
