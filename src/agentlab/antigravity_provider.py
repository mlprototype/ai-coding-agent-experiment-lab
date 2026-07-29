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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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


def _is_process_group_alive(pgid: int) -> bool:
    """Check if any process in the process group is still alive."""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False
    except PermissionError:
        return True


def _run_preflight_subprocess(
    cmd: str,
    args: list[str],
    timeout_seconds: float,
    clean_env: dict[str, str],
) -> tuple[
    int | None,
    str,
    str,
    AntigravityFailureStage | None,
    LiveFailureKind,
    int,
    int,
    bool,
]:
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
        return (
            None,
            "",
            "",
            AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            0,
            0,
            False,
        )
    except Exception:
        return (
            None,
            "",
            "",
            AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            0,
            0,
            False,
        )

    stdout_buf = bytearray()
    stderr_buf = bytearray()

    pgid = proc.pid
    start_time = time.monotonic()
    timed_out = False
    collection_error = False
    output_limit_exceeded = False
    cleanup_failed = False
    cleanup_exception = False

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
                    max_read = min(4096, (PREFLIGHT_MAX_BYTES + 1) - len(buf))
                    if max_read <= 0:
                        output_limit_exceeded = True
                        pipe.close()
                        continue

                    try:
                        data = pipe.read(max_read)
                    except Exception:
                        collection_error = True
                        break

                    if not data:
                        pipe.close()
                        continue

                    buf.extend(data)
                    if len(buf) > PREFLIGHT_MAX_BYTES:
                        output_limit_exceeded = True
                        del buf[PREFLIGHT_MAX_BYTES:]
                        pipe.close()

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
                                output_limit_exceeded = True
                                del buf[PREFLIGHT_MAX_BYTES:]
                            pipe.close()
                    break

    except Exception:
        collection_error = True

    # Single mandatory cleanup block for process group and pipes
    finally:
        try:
            # Check if termination is required (timeout, alive pgid, or un-reaped child)
            pg_alive = False
            try:
                pg_alive = _is_process_group_alive(pgid)
            except Exception:
                cleanup_failed = True

            if timed_out or pg_alive or (proc.poll() is None):
                if pg_alive:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGTERM)

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
                        break
                    time.sleep(0.01)

                try:
                    pg_alive_after_term = _is_process_group_alive(pgid)
                except Exception:
                    cleanup_failed = True
                    pg_alive_after_term = True

                if pg_alive_after_term:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)

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
                            break
                        time.sleep(0.01)

                    try:
                        if _is_process_group_alive(pgid):
                            cleanup_failed = True
                    except Exception:
                        cleanup_failed = True

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
                cleanup_failed = not (proc_reaped and pg_extinct)
            except Exception:
                cleanup_failed = True

        except Exception:
            cleanup_exception = True

        # Ensure pipes are closed
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()

    stdout_bytes = len(stdout_buf)
    stderr_bytes = len(stderr_buf)
    stdout_str = stdout_buf.decode("utf-8", errors="replace")
    stderr_str = stderr_buf.decode("utf-8", errors="replace")

    # Priority ranking: Cleanup failure > Timeout > Output limit > Collection error
    if cleanup_failed or cleanup_exception:
        return (
            proc.returncode,
            stdout_str,
            stderr_str,
            AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP,
            LiveFailureKind.PROCESS_CLEANUP_ERROR,
            stdout_bytes,
            stderr_bytes,
            output_limit_exceeded,
        )

    if timed_out:
        return (
            None,
            stdout_str,
            stderr_str,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.PROVIDER_TIMEOUT,
            stdout_bytes,
            stderr_bytes,
            output_limit_exceeded,
        )

    if output_limit_exceeded:
        return (
            proc.returncode,
            stdout_str,
            stderr_str,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
            stdout_bytes,
            stderr_bytes,
            True,
        )

    if collection_error:
        return (
            proc.returncode,
            stdout_str,
            stderr_str,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.EVIDENCE_ERROR,
            stdout_bytes,
            stderr_bytes,
            output_limit_exceeded,
        )

    return (
        proc.returncode,
        stdout_str,
        stderr_str,
        None,
        LiveFailureKind.NONE,
        stdout_bytes,
        stderr_bytes,
        False,
    )


def _matches_cli_flag(flag_val: str, text: str) -> bool:
    """Check if flag_val matches exact CLI token boundaries in text."""
    pattern = r"(?:^|[\s=,'\"])" + re.escape(flag_val) + r"(?:$|[\s=,'\"])"
    return bool(re.search(pattern, text))


def probe_antigravity_preflight(
    executable_path: str | None = None,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    allowlist: frozenset[str] = ANTIGRAVITY_ALLOWLISTED_CLI_VERSIONS,
) -> tuple[
    AntigravityCliProfile,
    str | None,
    list[AntigravityHelpMarker],
    datetime,
    AntigravityFailureStage | None,
    LiveFailureKind,
]:
    """Execute bounded agy --version and agy --help preflight."""
    checked_at = datetime.now(UTC)
    cmd = executable_path or shutil.which("agy")

    if cmd is None:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            None,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
        )

    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME", "HOME", "LANG", "LC_ALL"}
    }

    # Version probe
    ret_v, stdout_v, stderr_v, stage_v, kind_v, _b_v_out, _b_v_err, trunc_v = (
        _run_preflight_subprocess(cmd, ["--version"], timeout_seconds, clean_env)
    )
    if kind_v is not LiveFailureKind.NONE or trunc_v:
        final_kind = (
            kind_v
            if kind_v is not LiveFailureKind.NONE
            else LiveFailureKind.PROVIDER_OUTPUT_LIMIT
        )
        final_stage = (
            stage_v
            if stage_v is not None
            else AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
        )
        return (
            AntigravityCliProfile.NOT_SELECTED,
            None,
            [],
            checked_at,
            final_stage,
            final_kind,
        )

    version_str: str | None = None
    if ret_v == 0:
        stdout_lines = [line.strip() for line in stdout_v.splitlines() if line.strip()]
        stderr_lines = [line.strip() for line in stderr_v.splitlines() if line.strip()]
        all_non_empty_lines = stdout_lines + stderr_lines

        # Reject if total non-empty lines across stdout + stderr is not exactly 1
        if len(all_non_empty_lines) == 1:
            single_line = all_non_empty_lines[0]
            if match := re.fullmatch(r"agy \d+\.\d+\.\d+", single_line):
                version_str = match.group(0)

    # Help probe
    ret_h, stdout_h, stderr_h, stage_h, kind_h, _b_h_out, _b_h_err, trunc_h = (
        _run_preflight_subprocess(cmd, ["--help"], timeout_seconds, clean_env)
    )
    if kind_h is not LiveFailureKind.NONE or trunc_h:
        final_kind = (
            kind_h
            if kind_h is not LiveFailureKind.NONE
            else LiveFailureKind.PROVIDER_OUTPUT_LIMIT
        )
        final_stage = (
            stage_h
            if stage_h is not None
            else AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
        )
        return (
            AntigravityCliProfile.NOT_SELECTED,
            version_str,
            [],
            checked_at,
            final_stage,
            final_kind,
        )

    verified_flags: list[AntigravityHelpMarker] = []
    if ret_h == 0:
        help_output = f"{stdout_h}\n{stderr_h}".lower()
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

    return (
        profile,
        version_str,
        sorted(verified_flags, key=lambda f: f.value),
        checked_at,
        failure_stage,
        failure_kind,
    )



def build_antigravity_evidence(
    *,
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
        cli_version=cli_version,
        profile=profile,
        preflight_checked_at=preflight_checked_at,
        preflight_verified_flags=preflight_verified_flags or [],
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
