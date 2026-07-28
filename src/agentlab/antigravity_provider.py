"""Antigravity CLI Provider preflight, strict parser, and evidence construction for Phase 5.

Offline Slice 5A implementation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
        self.result_received = False

    def parse_chunk(self, chunk: bytes) -> None:
        if self.protocol_error is not None:
            return

        self.total_bytes_read += len(chunk)
        if self.total_bytes_read > self.max_output_bytes:
            self.protocol_error = "Total output exceeded maximum bytes limit"
            return

        self.buffer.extend(chunk)

        while True:
            newline_index = self.buffer.find(b"\n")
            if newline_index == -1:
                if len(self.buffer) > self.max_line_bytes:
                    self.protocol_error = "Line byte count exceeded limit"
                break

            line_bytes = bytes(self.buffer[:newline_index]).rstrip(b"\r")
            del self.buffer[: newline_index + 1]

            if not line_bytes:
                self.protocol_error = "Empty line in NDJSON stream"
                return

            if len(line_bytes) > self.max_line_bytes:
                self.protocol_error = "Line byte count exceeded limit"
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
                return

            if not isinstance(obj, dict):
                self.protocol_error = "Stream line is not a JSON object"
                return

            self._process_event(obj)
            if self.protocol_error is not None:
                return

    def finalize(self) -> None:
        if self.protocol_error is not None:
            return

        if self.buffer:
            line_bytes = bytes(self.buffer).rstrip(b"\r")
            if line_bytes:
                if len(line_bytes) > self.max_line_bytes:
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

        if (
            not self.result_received
            and self.event_count > 0
            and self.protocol_error is None
        ):
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
                self.protocol_error = "Missing or non-string step_type"
                return

            try:
                step_type = AntigravityStepType(step_type_str)
                self.step_counts[step_type] += 1
            except ValueError:
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
            if isinstance(num_turns, int) and not isinstance(num_turns, bool) and num_turns >= 0:
                self.terminal_num_turns = num_turns

            duration_ms = obj.get("duration_ms")
            if (
                isinstance(duration_ms, (int, float))
                and not isinstance(duration_ms, bool)
                and duration_ms >= 0
            ):
                self.provider_duration_ms = round(float(duration_ms))

            usage_obj = obj.get("usage")
            if isinstance(usage_obj, dict):
                in_tok = usage_obj.get("input_tokens")
                cache_tok = usage_obj.get("cache_read_tokens")
                out_tok = usage_obj.get("output_tokens")
                think_tok = usage_obj.get("thinking_tokens")
                tot_tok = usage_obj.get("total_tokens")

                def _valid_int(val: Any) -> int | None:
                    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                        return val
                    return None

                in_val = _valid_int(in_tok)
                cache_val = _valid_int(cache_tok)
                out_val = _valid_int(out_tok)
                think_val = _valid_int(think_tok)
                tot_val = _valid_int(tot_tok)

                if (
                    tot_val is not None
                    and in_val is not None
                    and out_val is not None
                    and tot_val != in_val + out_val
                ):
                    self.protocol_error = (
                        "total_tokens does not equal input_tokens + output_tokens"
                    )
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

    version_str: str | None = None
    try:
        proc_v = subprocess.run(
            [cmd, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=clean_env,
        )
        if proc_v.returncode == 0:
            lines = proc_v.stdout.strip().splitlines()
            stdout_first_line = lines[0] if lines else ""
            if match := re.fullmatch(r"^agy \d+\.\d+\.\d+$", stdout_first_line):
                version_str = match.group(0)
    except FileNotFoundError:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            None,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
        )
    except subprocess.TimeoutExpired:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            None,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.PROVIDER_TIMEOUT,
        )
    except Exception:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            None,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.EVIDENCE_ERROR,
        )

    verified_flags: list[AntigravityHelpMarker] = []
    try:
        proc_h = subprocess.run(
            [cmd, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=clean_env,
        )
        if proc_h.returncode == 0:
            help_output = f"{proc_h.stdout}\n{proc_h.stderr}".lower()
            for marker in AntigravityHelpMarker:
                if marker.value in help_output:
                    verified_flags.append(marker)
    except subprocess.TimeoutExpired:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            version_str,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.PROVIDER_TIMEOUT,
        )
    except Exception:
        return (
            AntigravityCliProfile.NOT_SELECTED,
            version_str,
            [],
            checked_at,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            LiveFailureKind.EVIDENCE_ERROR,
        )

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
        final_failure_stage = failure_stage or AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
    elif term.reason is TerminationReason.TIMEOUT:
        final_failure_kind = LiveFailureKind.PROVIDER_TIMEOUT
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif exit_code is not None and exit_code < 0:
        final_failure_kind = LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
        final_failure_stage = failure_stage or AntigravityFailureStage.STREAM_PARSING
    elif (
        initial_failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
        or p.protocol_error is not None
    ):
        final_failure_kind = LiveFailureKind.PROVIDER_PROTOCOL_ERROR
        final_failure_stage = (
            failure_stage or AntigravityFailureStage.STREAM_PARSING
        )
    elif exit_code is not None and exit_code > 0:
        final_failure_kind = LiveFailureKind.PROVIDER_CLI_NONZERO
        final_failure_stage = (
            failure_stage or AntigravityFailureStage.STREAM_PARSING
        )
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
        stdout_truncated=False,
        stderr_truncated=False,
        termination=term,
        failure_kind=final_failure_kind,
    )
