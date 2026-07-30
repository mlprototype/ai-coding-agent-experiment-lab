"""Offline acceptance and reproduction tests for Antigravity CLI Provider Phase 5 Slice 5A."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentlab.antigravity_provider import (
    AntigravityEvidenceDiagnostic,
    AntigravityPreflightProcessResult,
    AntigravityPreflightResult,
    StrictAntigravityStreamParser,
    _is_process_group_alive,
    _run_preflight_subprocess,
    build_antigravity_evidence,
    probe_antigravity_preflight,
    safe_build_antigravity_evidence,
    select_antigravity_profile,
)
from agentlab.models import (
    AntigravityCleanupErrorCode,
    AntigravityCliProfile,
    AntigravityEventType,
    AntigravityExecutionEvidence,
    AntigravityExecutionStage,
    AntigravityFailureStage,
    AntigravityHelpMarker,
    AntigravityPermissionMode,
    AntigravityPreflightOperation,
    AntigravityStepType,
    AntigravityTerminalStatus,
    AntigravityTerminationEvidence,
    CodexCleanupState,
    CodexInvocationState,
    LiveFailureKind,
    ProviderExecutionStatus,
    TerminationReason,
    UsageMetricSource,
)


def _build_successful_v11_evidence(
    tmp_path: Path,
) -> AntigravityExecutionEvidence:
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then echo "--prompt --output-format '
        'stream-json --model --effort --print-timeout --sandbox"; exit 0; fi\n'
        "exit 2\n"
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)
    preflight = probe_antigravity_preflight(
        executable_path=str(fake_agy),
        allowlist=frozenset({"agy 1.2.3"}),
    )
    assert isinstance(preflight, AntigravityPreflightResult)

    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    parser.finalize()

    return build_antigravity_evidence(
        preflight_result=preflight,
        execution_stage=(
            AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
        ),
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )


def test_select_antigravity_profile_default_empty_allowlist() -> None:
    profile = select_antigravity_profile("agy 1.0.0", list(AntigravityHelpMarker))
    assert profile is AntigravityCliProfile.NOT_SELECTED


def test_select_antigravity_profile_injected_allowlist() -> None:
    allowlist = frozenset({"agy 1.0.0"})
    profile = select_antigravity_profile(
        "agy 1.0.0", list(AntigravityHelpMarker), allowlist=allowlist
    )
    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1

    partial_flags = [AntigravityHelpMarker.PROMPT, AntigravityHelpMarker.SANDBOX]
    profile_partial = select_antigravity_profile(
        "agy 1.0.0", partial_flags, allowlist=allowlist
    )
    assert profile_partial is AntigravityCliProfile.NOT_SELECTED


def test_parser_valid_stream() -> None:
    sample_file = Path("tests/fixtures/antigravity/sample_stream.jsonl")
    data = sample_file.read_bytes()

    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(data)
    parser.finalize()

    assert parser.protocol_error is None
    assert parser.event_count == 6
    assert parser.init_event_index == 0
    assert parser.result_event_index == 5
    assert parser.normalized_terminal_status is AntigravityTerminalStatus.SUCCESS
    assert parser.terminal_num_turns == 1
    assert parser.provider_duration_ms == 1250

    assert parser.event_counts[AntigravityEventType.INIT] == 1
    assert parser.event_counts[AntigravityEventType.STEP_UPDATE] == 4
    assert parser.event_counts[AntigravityEventType.RESULT] == 1

    assert parser.step_counts[AntigravityStepType.USER_INPUT] == 1
    assert parser.step_counts[AntigravityStepType.AGENT_RESPONSE] == 1
    assert parser.step_counts[AntigravityStepType.TOOL] == 1
    assert parser.step_counts[AntigravityStepType.CHECKPOINT] == 1

    assert parser.usage_metrics.input_tokens == 100
    assert parser.usage_metrics.cached_input_tokens == 20
    assert parser.usage_metrics.output_tokens == 50
    assert parser.usage_metrics.reasoning_output_tokens == 10
    assert parser.usage_metrics.source is UsageMetricSource.PROVIDER_REPORTED


def test_parser_multibyte_utf8_1byte_chunk_boundary() -> None:
    text = "こんにちは🚀 Antigravity CLI テスト"
    line1 = f'{{"event": "init", "permission_mode": "confirm", "msg": "{text}"}}\n'.encode()
    line2 = b'{"event": "step_update", "step_type": "user_input"}\n'
    line3 = b'{"event": "result", "status": "SUCCESS", "num_turns": 1, "duration_ms": 100}\n'
    stream = line1 + line2 + line3

    parser = StrictAntigravityStreamParser()
    for byte in stream:
        parser.parse_chunk(bytes([byte]))
    parser.finalize()

    assert parser.protocol_error is None
    assert parser.event_count == 3
    assert parser.observed_permission_mode is AntigravityPermissionMode.CONFIRM


def test_preflight_exact_65536_bytes_passes_and_65537_bytes_fails(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    markers_str = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"

    # 1. Exact 65,536 bytes help output -> passes preflight
    padding_65536 = " " * (65536 - len(markers_str) - 1)
    script_pass = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then printf "%s%s\\n" "{markers_str}"'
        f' "{padding_65536}"; exit 0; fi\n'
    )
    fake_agy.write_text(script_pass)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, version_str, _flags, _checked, _stage, failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )
    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version_str == "agy 1.2.3"
    assert failure_kind is LiveFailureKind.NONE

    # 2. Exceeding by 1 byte (65,537 bytes) -> fails preflight with PROVIDER_OUTPUT_LIMIT
    padding_65537 = " " * (65537 - len(markers_str) - 1)
    script_fail = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then printf "%s%s\\n" "{markers_str}"'
        f' "{padding_65537}"; exit 0; fi\n'
    )
    fake_agy.write_text(script_fail)

    profile_fail, _v, _f, _c, _stage_fail, kind_fail = probe_antigravity_preflight(
        executable_path=str(fake_agy),
        allowlist=allowlist,
    )
    assert profile_fail is AntigravityCliProfile.NOT_SELECTED
    assert kind_fail is LiveFailureKind.PROVIDER_OUTPUT_LIMIT


def test_preflight_missing_executable_fails_closed_at_spawn(tmp_path: Path) -> None:
    profile, version, flags, _checked, stage, kind = probe_antigravity_preflight(
        executable_path=str(tmp_path / "missing-agy"),
        allowlist=frozenset({"agy 1.2.3"}),
    )

    assert profile is AntigravityCliProfile.NOT_SELECTED
    assert version is None
    assert flags == []
    assert stage is AntigravityFailureStage.PREFLIGHT_PROCESS_SPAWN
    assert kind is LiveFailureKind.PROVIDER_UNAVAILABLE


def test_preflight_accepts_version_and_help_from_stderr(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3" >&2; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}" >&2; exit 0; fi\n'
        "exit 2\n"
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    profile, version, flags, _checked, stage, kind = probe_antigravity_preflight(
        executable_path=str(fake_agy),
        allowlist=frozenset({"agy 1.2.3"}),
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version == "agy 1.2.3"
    assert set(flags) == set(AntigravityHelpMarker)
    assert stage is None
    assert kind is LiveFailureKind.NONE


def test_preflight_nonzero_and_unregistered_versions_fail_closed(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 7; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 2\n"
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    profile_nonzero, version_nonzero, _flags, _checked, stage, kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=frozenset({"agy 1.2.3"}),
        )
    )
    assert profile_nonzero is AntigravityCliProfile.NOT_SELECTED
    assert version_nonzero is None
    assert stage is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    assert kind is LiveFailureKind.PROVIDER_UNAVAILABLE

    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.4"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 2\n"
    )
    profile_unregistered, version_unregistered, _flags, _checked, stage, kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=frozenset({"agy 1.2.3"}),
        )
    )
    assert profile_unregistered is AntigravityCliProfile.NOT_SELECTED
    assert version_unregistered == "agy 1.2.4"
    assert stage is AntigravityFailureStage.PREFLIGHT
    assert kind is LiveFailureKind.PROVIDER_UNAVAILABLE


def test_process_group_liveness_distinguishes_permission_and_unknown_errors() -> None:
    with patch("os.killpg", side_effect=PermissionError("denied")):
        assert _is_process_group_alive(12345) is True

    with (
        patch("os.killpg", side_effect=OSError("unknown liveness error")),
        pytest.raises(OSError, match="unknown liveness error"),
    ):
        _is_process_group_alive(12345)


@pytest.mark.parametrize(
    "liveness_error",
    [
        PermissionError("permission denied"),
        OSError("unknown process-group state"),
    ],
)
def test_preflight_liveness_errors_become_cleanup_error(
    liveness_error: OSError,
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\nexit 0\n")
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    with patch(
        "agentlab.antigravity_provider._is_process_group_alive",
        side_effect=liveness_error,
    ):
        preflight = probe_antigravity_preflight(
            executable_path=str(fake_agy),
            timeout_seconds=0.5,
        )

    result = preflight.version_probe
    assert isinstance(result, AntigravityPreflightProcessResult)
    assert result.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert (
        result.failure_stage
        is AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
    )
    assert result.termination.process_group_cleared is False
    assert (
        result.termination.error_code
        is AntigravityCleanupErrorCode.CLEANUP_PROCESS_ERROR
    )

    evidence = build_antigravity_evidence(
        preflight_result=preflight
    )
    assert evidence.schema_version == "1.1"
    assert evidence.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert evidence.cleanup_state is CodexCleanupState.NOT_APPLICABLE
    assert (
        evidence.preflight_commands[0].failure_kind
        is LiveFailureKind.PROCESS_CLEANUP_ERROR
    )


def test_preflight_stderr_limit_flows_into_evidence_v11(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    markers = (
        "--prompt --output-format stream-json --model --effort "
        "--print-timeout --sandbox"
    )
    padding = " " * (65537 - len(markers) - 1)
    fake_agy.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then printf "%s%s\\n" "{markers}"'
        f' "{padding}" >&2; exit 0; fi\n'
        "exit 2\n"
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    preflight = probe_antigravity_preflight(
        executable_path=str(fake_agy),
        allowlist=frozenset({"agy 1.2.3"}),
    )
    assert preflight.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert preflight.stdout_truncated is False
    assert preflight.stderr_truncated is True
    assert preflight.help_probe is not None
    assert preflight.help_probe.stderr_bytes == 65536
    assert preflight.help_probe.stderr_truncated is True

    evidence = build_antigravity_evidence(preflight_result=preflight)
    assert evidence.schema_version == "1.1"
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert evidence.stdout_truncated is False
    assert evidence.stderr_truncated is True
    assert evidence.stderr_bytes == 65536
    assert [
        command.operation for command in evidence.preflight_commands
    ] == [
        AntigravityPreflightOperation.VERSION,
        AntigravityPreflightOperation.HELP,
    ]
    assert evidence.preflight_commands[1].stderr_truncated is True
    assert (
        AntigravityExecutionEvidence.model_validate_json(
            evidence.model_dump_json()
        )
        == evidence
    )


@pytest.mark.parametrize(
    "patch_target",
    [
        "agentlab.antigravity_provider.os.set_blocking",
    ],
)
def test_preflight_set_blocking_failure_is_collection_error(
    patch_target: str,
) -> None:
    with patch(patch_target, side_effect=OSError("set_blocking failed")):
        result = _run_preflight_subprocess(
            "/bin/sh",
            ["-c", "exit 0"],
            timeout_seconds=0.5,
            clean_env={},
        )

    assert result.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert (
        result.failure_stage
        is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )
    assert result.collection_error_observed is True
    assert result.termination.process_group_cleared is True


def test_preflight_pipe_read_failure_is_collection_error() -> None:
    real_popen = subprocess.Popen

    class FailingReadPipe:
        def __init__(self, pipe: object) -> None:
            self._pipe = pipe

        @property
        def closed(self) -> bool:
            return bool(self._pipe.closed)  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return int(self._pipe.fileno())  # type: ignore[attr-defined]

        def read(self, _size: int) -> bytes:
            raise OSError("pipe read failed")

        def close(self) -> None:
            self._pipe.close()  # type: ignore[attr-defined]

    def _spawn_with_failing_stdout(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        assert proc.stdout is not None
        proc.stdout = FailingReadPipe(proc.stdout)  # type: ignore[assignment]
        return proc

    with patch(
        "agentlab.antigravity_provider.subprocess.Popen",
        side_effect=_spawn_with_failing_stdout,
    ):
        result = _run_preflight_subprocess(
            "/bin/sh",
            ["-c", "echo output; sleep 100"],
            timeout_seconds=0.5,
            clean_env={},
        )

    assert result.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert (
        result.failure_stage
        is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )
    assert result.collection_error_observed is True
    assert result.termination.process_group_cleared is True


def test_parser_output_limit_64kb_and_64kb_plus_1byte() -> None:
    init_prefix = b'{"event": "init", "pad": "'
    init_suffix_and_result = (
        b'"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    padding = b"X" * (
        65536 - len(init_prefix) - len(init_suffix_and_result)
    )
    exact_stream = init_prefix + padding + init_suffix_and_result
    assert len(exact_stream) == 65536

    # 1. An actual 65,536-byte complete stream passes.
    parser_ok = StrictAntigravityStreamParser(max_output_bytes=65536)
    parser_ok.parse_chunk(exact_stream)
    parser_ok.finalize()
    assert not parser_ok.output_limit_exceeded
    assert parser_ok.protocol_error is None

    # 2. An actual 65,537-byte input fails at the exact +1 boundary.
    parser_overflow = StrictAntigravityStreamParser(
        max_output_bytes=65536
    )
    parser_overflow.parse_chunk(exact_stream + b"X")
    assert parser_overflow.output_limit_exceeded
    assert parser_overflow.protocol_error is not None

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").UTC
        ),
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser_overflow,
        stdout_bytes=65536,
        exit_code=0,
    )
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert evidence.stdout_truncated is True


def test_parser_discards_trailing_raw_line_after_success() -> None:
    secret = "SECRET_FINAL_RESPONSE"
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        (
            '{"event": "init"}\n'
            '{"event": "result", "status": "SUCCESS", "num_turns": 1, '
            f'"output": "{secret}"}}'
        ).encode()
    )
    parser.finalize()

    assert parser.protocol_error is None
    assert parser.result_received is True
    assert parser.buffer == bytearray()
    assert secret.encode() not in parser.buffer


def test_parser_zero_steps_and_sensitive_step_payloads_are_not_retained() -> None:
    zero_step = StrictAntigravityStreamParser()
    zero_step.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    zero_step.finalize()
    assert zero_step.protocol_error is None
    assert zero_step.event_counts[AntigravityEventType.STEP_UPDATE] == 0

    secrets = (
        "SECRET_AGENT_DELTA_ONE",
        "SECRET_AGENT_DELTA_TWO",
        "SECRET_TOOL_INPUT",
        "SECRET_SUBAGENT_URI",
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        (
            '{"event": "init"}\n'
            '{"event": "step_update", "step_type": "agent_response", '
            + '"text_delta": "' + secrets[0] + '"}\n'
            '{"event": "step_update", "step_type": "agent_response", '
            + '"text_delta": "' + secrets[1] + '"}\n'
            '{"event": "step_update", "step_type": "tool", '
            + '"tool_input": "'
            + secrets[2]
            + '", "subagent_log_uri": "'
            + secrets[3]
            + '"}\n'
            '{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
        ).encode()
    )
    parser.finalize()

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").UTC
        ),
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )
    serialized = evidence.model_dump_json()
    assert parser.buffer == bytearray()
    for secret in secrets:
        assert secret not in serialized


def test_parser_oversized_line_empty_stream_and_unknown_step_fail_closed() -> None:
    oversized = StrictAntigravityStreamParser(max_line_bytes=32)
    oversized.parse_chunk(b'{"event": "init", "padding": "' + (b"X" * 64) + b'"}')
    assert oversized.output_limit_exceeded is True
    assert oversized.buffer == bytearray()

    empty = StrictAntigravityStreamParser()
    empty.finalize()
    assert empty.protocol_error == "Stream ended without terminal result event"

    unknown_step = StrictAntigravityStreamParser()
    unknown_step.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "step_update", "step_type": "future_step"}\n'
    )
    assert unknown_step.protocol_error == "Unknown step_type: future_step"
    assert unknown_step.unknown_step_type_count == 1
    assert unknown_step.buffer == bytearray()


def test_parser_missing_and_duplicate_results_fail_closed() -> None:
    missing = StrictAntigravityStreamParser()
    missing.parse_chunk(b'{"event": "init"}\n')
    missing.finalize()
    assert missing.protocol_error == "Stream ended without terminal result event"

    duplicate = StrictAntigravityStreamParser()
    duplicate.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    assert duplicate.protocol_error == "Received event after terminal result event"
    assert duplicate.buffer == bytearray()


def test_parser_all_terminal_statuses_including_invalid_waiting_running() -> None:
    statuses = [
        ("SUCCESS", AntigravityTerminalStatus.SUCCESS),
        ("ERROR", AntigravityTerminalStatus.ERROR),
        ("CANCELED", AntigravityTerminalStatus.CANCELED),
        ("INTERRUPTED", AntigravityTerminalStatus.INTERRUPTED),
        ("INVALID", AntigravityTerminalStatus.INVALID),
        ("WAITING", AntigravityTerminalStatus.WAITING),
        ("RUNNING", AntigravityTerminalStatus.RUNNING),
    ]
    for status_str, expected_enum in statuses:
        parser = StrictAntigravityStreamParser()
        stream = (
            f'{{"event": "init"}}\n{{"event": "result", "status": "{status_str}"}}\n'.encode()
        )
        parser.parse_chunk(stream)
        parser.finalize()

        assert parser.normalized_terminal_status is expected_enum

    # Unknown status string fails protocol parsing
    parser_unknown = StrictAntigravityStreamParser()
    parser_unknown.parse_chunk(
        b'{"event": "init"}\n{"event": "result", "status": "UNKNOWN_XYZ"}\n'
    )
    parser_unknown.finalize()
    assert parser_unknown.protocol_error is not None
    assert parser_unknown.normalized_terminal_status is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("num_turns", True),
        ("num_turns", -1),
        ("num_turns", 10001),
        ("duration_ms", True),
        ("duration_ms", -1),
        ("duration_ms", 86400001),
    ],
)
def test_parser_rejects_terminal_numeric_bool_negative_and_overflow(
    field: str,
    value: object,
) -> None:
    parser = StrictAntigravityStreamParser()
    result = {
        "event": "result",
        "status": "SUCCESS",
        field: value,
    }
    parser.parse_chunk(
        b'{"event": "init"}\n'
        + json.dumps(result, separators=(",", ":")).encode()
        + b"\n"
    )
    parser.finalize()

    assert parser.protocol_error is not None
    assert parser.buffer == bytearray()


@pytest.mark.parametrize(
    ("status", "expected_failure"),
    [
        ("SUCCESS", LiveFailureKind.NONE),
        ("ERROR", LiveFailureKind.PROVIDER_TURN_FAILED),
        ("CANCELED", LiveFailureKind.PROVIDER_TURN_FAILED),
        ("INTERRUPTED", LiveFailureKind.PROVIDER_TURN_FAILED),
        ("INVALID", LiveFailureKind.PROVIDER_PROTOCOL_ERROR),
        ("WAITING", LiveFailureKind.PROVIDER_PROTOCOL_ERROR),
        ("RUNNING", LiveFailureKind.PROVIDER_PROTOCOL_ERROR),
    ],
)
def test_all_terminal_statuses_map_to_final_failure_kind(
    status: str,
    expected_failure: LiveFailureKind,
) -> None:
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        (
            '{"event": "init"}\n'
            f'{{"event": "result", "status": "{status}", '
            '"num_turns": 1}\n'
        ).encode()
    )
    parser.finalize()

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").UTC
        ),
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )

    assert evidence.failure_kind is expected_failure
    assert evidence.provider_status is (
        ProviderExecutionStatus.SUCCEEDED
        if expected_failure is LiveFailureKind.NONE
        else ProviderExecutionStatus.FAILED
    )


def test_parser_stream_event_order_abnormalities() -> None:
    # 1. Event after result event
    parser1 = StrictAntigravityStreamParser()
    parser1.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS"}\n'
        b'{"event": "step_update", "step_type": "user_input"}\n'
    )
    parser1.finalize()
    assert parser1.protocol_error == "Received event after terminal result event"

    # 2. Init event not first
    parser2 = StrictAntigravityStreamParser()
    parser2.parse_chunk(
        b'{"event": "step_update", "step_type": "user_input"}\n'
        b'{"event": "init"}\n'
    )
    parser2.finalize()
    assert parser2.protocol_error == "step_update event before init event"

    # 3. Duplicate init event
    parser3 = StrictAntigravityStreamParser()
    parser3.parse_chunk(b'{"event": "init"}\n{"event": "init"}\n')
    parser3.finalize()
    assert parser3.protocol_error == "Multiple init events in stream"

    # 4. Result event appears first without init
    parser4 = StrictAntigravityStreamParser()
    parser4.parse_chunk(b'{"event": "result", "status": "SUCCESS"}\n')
    parser4.finalize()
    assert parser4.protocol_error == "result event before init event"


def test_parser_invalid_json_duplicate_keys_non_object_blank_lines() -> None:
    # 1. Duplicate JSON keys -> fail closed
    parser1 = StrictAntigravityStreamParser()
    parser1.parse_chunk(b'{"event": "init", "event": "init"}\n')
    parser1.finalize()
    assert parser1.protocol_error is not None

    # 2. Invalid UTF-8 -> fail closed
    parser2 = StrictAntigravityStreamParser()
    parser2.parse_chunk(b'{"event": "\xff\xff"}\n')
    parser2.finalize()
    assert parser2.protocol_error is not None

    # 3. Empty / blank line -> fail closed
    parser3 = StrictAntigravityStreamParser()
    parser3.parse_chunk(b'{"event": "init"}\n\n')
    parser3.finalize()
    assert parser3.protocol_error is not None

    # 4. Non-object JSON line (array / number) -> fail closed
    parser4 = StrictAntigravityStreamParser()
    parser4.parse_chunk(b'["event", "init"]\n')
    parser4.finalize()
    assert parser4.protocol_error is not None


def test_parser_unknown_event_fail_closed() -> None:
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "unknown_future_event"}\n'
        b'{"event": "result", "status": "SUCCESS"}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None
    assert parser.unknown_event_count == 1
    assert parser.event_count == 2


def test_parser_malformed_usage_cache_read_exceeds_input_and_thinking_exceeds_output() -> (
    None
):
    # 1. cached_input_tokens > input_tokens -> rejected in parser
    parser1 = StrictAntigravityStreamParser()
    stream1 = (
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", '
        b'"usage": {"input_tokens": 10, "cache_read_tokens": 20}}\n'
    )
    parser1.parse_chunk(stream1)
    parser1.finalize()
    assert parser1.protocol_error == "cached_input_tokens exceeds input_tokens"

    # 2. reasoning_output_tokens > output_tokens -> rejected in parser
    parser2 = StrictAntigravityStreamParser()
    stream2 = (
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", '
        b'"usage": {"output_tokens": 10, "thinking_tokens": 20}}\n'
    )
    parser2.parse_chunk(stream2)
    parser2.finalize()
    assert parser2.protocol_error == "reasoning_output_tokens exceeds output_tokens"

    # Verify building evidence with parser2 results in LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").UTC
        ),
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser2,
        exit_code=0,
    )
    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


@pytest.mark.parametrize(
    "usage_json",
    [
        '{"input_tokens": -1}',
        '{"input_tokens": true}',
        '{"input_tokens": "10"}',
        '{"input_tokens": 10000001}',
        '{"input_tokens": NaN}',
        '{"input_tokens": 10, "output_tokens": 5, "total_tokens": 99}',
        '{"cache_read_tokens": 1}',
        '{"thinking_tokens": 1}',
    ],
)
def test_parser_rejects_all_malformed_usage_classes(usage_json: str) -> None:
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        (
            '{"event": "init"}\n'
            '{"event": "result", "status": "SUCCESS", "num_turns": 1, '
            + '"usage": '
            + usage_json
            + "}\n"
        ).encode()
    )
    parser.finalize()

    assert parser.protocol_error is not None
    assert parser.buffer == bytearray()


def test_parser_missing_usage_remains_not_available() -> None:
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    parser.finalize()

    assert parser.protocol_error is None
    assert parser.usage_metrics.source is UsageMetricSource.NOT_AVAILABLE
    assert parser.usage_metrics.input_tokens is None
    assert parser.usage_metrics.cached_input_tokens is None
    assert parser.usage_metrics.output_tokens is None
    assert parser.usage_metrics.reasoning_output_tokens is None


def test_success_requires_zero_exit_and_exactly_one_turn() -> None:
    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    flags = list(AntigravityHelpMarker)

    nonzero_parser = StrictAntigravityStreamParser()
    nonzero_parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    nonzero_parser.finalize()
    nonzero_evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=nonzero_parser,
        exit_code=9,
    )
    assert nonzero_evidence.failure_kind is LiveFailureKind.PROVIDER_CLI_NONZERO

    multi_turn_parser = StrictAntigravityStreamParser()
    multi_turn_parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 2}\n'
    )
    multi_turn_parser.finalize()
    multi_turn_evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=multi_turn_parser,
        exit_code=0,
    )
    assert multi_turn_evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_preflight_missing_each_required_help_marker_individually(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    all_markers = [
        "--prompt",
        "--output-format",
        "stream-json",
        "--model",
        "--effort",
        "--print-timeout",
        "--sandbox",
    ]

    for i in range(len(all_markers)):
        # Omit 1 marker at a time
        subset = all_markers[:i] + all_markers[i + 1 :]
        help_text = " ".join(subset)

        script = (
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
            f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        )
        fake_agy.write_text(script)
        fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

        allowlist = frozenset({"agy 1.2.3"})
        profile, _version, _flags, _checked, _stage, failure_kind = (
            probe_antigravity_preflight(
                executable_path=str(fake_agy),
                allowlist=allowlist,
            )
        )
        assert profile is AntigravityCliProfile.NOT_SELECTED
        assert failure_kind is LiveFailureKind.PROVIDER_UNAVAILABLE


def test_preflight_false_positive_help_flags_rejected(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    # Similar prefixes but incorrect CLI tokens
    false_flags = (
        "--prompting --output-formats stream-json-v2 "
        "--model-catalog --efforts --print-timeouts --sandboxed"
    )

    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{false_flags}"; exit 0; fi\n'
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, _version, _flags, _checked, _stage, failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )
    assert profile is AntigravityCliProfile.NOT_SELECTED
    assert failure_kind is LiveFailureKind.PROVIDER_UNAVAILABLE


def test_preflight_collection_exceptions_and_process_group_extinction(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    script = (
        "#!/bin/sh\n"
        f"echo $$ > {pid_file}\n"
        "sleep 100 &\n"
        f"echo $! > {grandchild_pid_file}\n"
        "trap 'exit 0' TERM\n"
        "wait\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    def _failing_select(
        rlist: list[object],
        wlist: list[object],
        xlist: list[object],
        timeout: float = 0,
    ) -> tuple[list[object], list[object], list[object]]:
        # Wait until both the shell and its spawned grandchild are observable.
        deadline = time.monotonic() + 1.0
        while (
            not pid_file.exists() or not grandchild_pid_file.exists()
        ) and time.monotonic() < deadline:
            time.sleep(0.005)
        raise OSError("select collection failure")

    with patch("select.select", side_effect=_failing_select):
        _ret, _out, _err, stage, kind, _b1, _b2, _tr = _run_preflight_subprocess(
            str(fake_agy), ["--version"], timeout_seconds=0.5, clean_env={}
        )
        assert kind is LiveFailureKind.EVIDENCE_ERROR
        assert stage is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION

    child_pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)

    grandchild_pid = int(grandchild_pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_preflight_timeout_normal_recovery_vs_cleanup_failure(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    script = "#!/bin/sh\nsleep 100\n"
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    # 1. Normal timeout recovery -> LiveFailureKind.PROVIDER_TIMEOUT
    _ret, _out, _err, stage, kind, _b1, _b2, _tr = _run_preflight_subprocess(
        str(fake_agy), ["--version"], timeout_seconds=0.1, clean_env={}
    )
    assert kind is LiveFailureKind.PROVIDER_TIMEOUT
    assert stage is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION

    # 2. Cleanup failure during alive check -> LiveFailureKind.PROCESS_CLEANUP_ERROR
    with patch(
        "agentlab.antigravity_provider._is_process_group_alive",
        side_effect=OSError("Pgid error"),
    ):
        _ret_c, _out_c, _err_c, stage_c, kind_c, _b1_c, _b2_c, _tr_c = (
            _run_preflight_subprocess(
                str(fake_agy), ["--version"], timeout_seconds=0.1, clean_env={}
            )
        )
        assert kind_c is LiveFailureKind.PROCESS_CLEANUP_ERROR
        assert stage_c is AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP


def test_preflight_only_version_and_help_invoked_with_clean_env(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "agy"
    args_log = tmp_path / "args.log"
    env_log = tmp_path / "env.log"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"

    script = (
        "#!/bin/sh\n"
        f'echo "$1" >> {args_log}\n'
        f'echo "SECRET_KEY=$SECRET_KEY" >> {env_log}\n'
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 1\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    os.environ["SECRET_KEY"] = "sk-super-secret-key-12345"

    try:
        allowlist = frozenset({"agy 1.2.3"})
        profile, _version, _flags, _checked, _stage, _kind = (
            probe_antigravity_preflight(
                executable_path=str(fake_agy),
                allowlist=allowlist,
            )
        )
        assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1

        invoked_args = args_log.read_text().splitlines()
        assert set(invoked_args) == {"--version", "--help"}

        env_content = env_log.read_text()
        assert "sk-super-secret-key-12345" not in env_content
        assert "SECRET_KEY=" in env_content
    finally:
        os.environ.pop("SECRET_KEY", None)


def test_evidence_validator_bidirectional_state_transitions_all_cases() -> None:
    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    valid_parser = StrictAntigravityStreamParser()
    valid_parser.parse_chunk(
        b'{"event": "init"}\n{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    valid_parser.finalize()

    base = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=valid_parser,
        exit_code=0,
    )
    assert base.provider_status is ProviderExecutionStatus.SUCCEEDED

    # 1. Reject NOT_SELECTED profile if invocation was attempted
    with pytest.raises(
        ValueError,
        match=r"PROVIDER_INVOCATION_ATTEMPTED stage requires HEADLESS_STREAM_JSON_V1 profile"
        r"|NOT_SELECTED profile forbids",
    ):
        dict_data = base.model_dump()
        dict_data["profile"] = AntigravityCliProfile.NOT_SELECTED
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 2. Reject PROVIDER_OUTPUT_LIMIT if stdout_bytes == 0 and stderr_bytes == 0
    with pytest.raises(
        ValueError,
        match=r"stdout_truncated requires stdout_bytes > 0",
    ):
        dict_data = base.model_dump()
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_OUTPUT_LIMIT
        dict_data["stdout_truncated"] = True
        dict_data["stdout_bytes"] = 0
        dict_data["stderr_bytes"] = 1
        AntigravityExecutionEvidence.model_validate(dict_data)

    with pytest.raises(
        ValueError,
        match=r"PROVIDER_OUTPUT_LIMIT requires stdout_truncated or stderr_truncated",
    ):
        dict_data = base.model_dump()
        dict_data["provider_status"] = ProviderExecutionStatus.FAILED
        dict_data["failure_stage"] = AntigravityFailureStage.STREAM_PARSING
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_OUTPUT_LIMIT
        dict_data["stdout_truncated"] = False
        dict_data["stderr_truncated"] = False
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 3. Reject PROVIDER_INVOCATION_ATTEMPTED if profile is NOT_SELECTED
    with pytest.raises(
        ValueError,
        match=r"PROVIDER_INVOCATION_ATTEMPTED stage requires HEADLESS_STREAM_JSON_V1 profile",
    ):
        dict_data = base.model_dump()
        dict_data["profile"] = AntigravityCliProfile.NOT_SELECTED
        dict_data["execution_stage"] = (
            AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
        )
        AntigravityExecutionEvidence.model_validate(dict_data)


def test_evidence_v11_rejects_failed_version_with_provider_success(
    tmp_path: Path,
) -> None:
    evidence = _build_successful_v11_evidence(tmp_path)
    payload = json.loads(evidence.model_dump_json())
    version_command = payload["preflight_commands"][0]
    version_command["returncode"] = 1
    version_command["failure_kind"] = LiveFailureKind.PROVIDER_UNAVAILABLE
    version_command["failure_stage"] = (
        AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )

    with pytest.raises(
        ValueError,
        match=r"requires both preflight commands to succeed",
    ):
        AntigravityExecutionEvidence.model_validate_json(json.dumps(payload))


def test_evidence_v11_rejects_missing_help_with_provider_success(
    tmp_path: Path,
) -> None:
    evidence = _build_successful_v11_evidence(tmp_path)
    payload = json.loads(evidence.model_dump_json())
    payload["preflight_commands"] = payload["preflight_commands"][:1]

    with pytest.raises(
        ValueError,
        match=r"requires successful version and help preflight commands",
    ):
        AntigravityExecutionEvidence.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    (
        "command_updates",
        "wrong_failure_kind",
        "wrong_failure_stage",
    ),
    [
        pytest.param(
            {
                "termination": {
                    "reason": TerminationReason.TIMEOUT,
                    "sigterm_sent": True,
                    "sigkill_sent": True,
                    "process_group_cleared": False,
                    "error_code": (
                        AntigravityCleanupErrorCode.CLEANUP_TIMEOUT
                    ),
                },
            },
            LiveFailureKind.PROVIDER_TIMEOUT,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            id="cleanup-over-timeout",
        ),
        pytest.param(
            {
                "stdout_truncated": True,
                "termination": {
                    "reason": TerminationReason.TIMEOUT,
                    "sigterm_sent": True,
                    "sigkill_sent": False,
                    "process_group_cleared": True,
                    "error_code": AntigravityCleanupErrorCode.NONE,
                },
            },
            LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            id="timeout-over-output-limit",
        ),
        pytest.param(
            {
                "stdout_truncated": True,
                "collection_error_observed": True,
            },
            LiveFailureKind.EVIDENCE_ERROR,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            id="output-limit-over-collection-error",
        ),
        pytest.param(
            {
                "returncode": None,
                "collection_error_observed": True,
            },
            LiveFailureKind.PROVIDER_UNAVAILABLE,
            AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
            id="collection-error-over-unavailable",
        ),
    ],
)
def test_evidence_v11_rejects_failure_kind_against_observed_priority(
    tmp_path: Path,
    command_updates: dict[str, object],
    wrong_failure_kind: LiveFailureKind,
    wrong_failure_stage: AntigravityFailureStage,
) -> None:
    evidence = _build_successful_v11_evidence(tmp_path)
    payload = json.loads(evidence.model_dump_json())
    payload.update(
        {
            "profile": AntigravityCliProfile.NOT_SELECTED,
            "preflight_verified_flags": [],
            "execution_stage": (
                AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
            ),
            "invocation_state": CodexInvocationState.NOT_ATTEMPTED,
            "cleanup_state": CodexCleanupState.NOT_APPLICABLE,
            "provider_status": ProviderExecutionStatus.FAILED,
            "failure_kind": wrong_failure_kind,
            "failure_stage": wrong_failure_stage,
        }
    )
    version_command = payload["preflight_commands"][0]
    version_command.update(command_updates)
    version_command["failure_kind"] = wrong_failure_kind
    version_command["failure_stage"] = wrong_failure_stage
    if version_command["stdout_truncated"]:
        payload["stdout_truncated"] = True

    with pytest.raises(
        ValueError,
        match=r"preflight failure_kind does not match observed",
    ):
        AntigravityExecutionEvidence.model_validate_json(json.dumps(payload))


def test_evidence_v11_rejects_unavailable_collection_without_observation(
    tmp_path: Path,
) -> None:
    evidence = _build_successful_v11_evidence(tmp_path)
    payload = json.loads(evidence.model_dump_json())
    payload.update(
        {
            "profile": AntigravityCliProfile.NOT_SELECTED,
            "preflight_verified_flags": [],
            "execution_stage": (
                AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
            ),
            "invocation_state": CodexInvocationState.NOT_ATTEMPTED,
            "cleanup_state": CodexCleanupState.NOT_APPLICABLE,
            "provider_status": ProviderExecutionStatus.FAILED,
            "failure_kind": LiveFailureKind.PROVIDER_UNAVAILABLE,
            "failure_stage": (
                AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
            ),
        }
    )
    version_command = payload["preflight_commands"][0]
    version_command["returncode"] = None
    version_command["failure_kind"] = LiveFailureKind.PROVIDER_UNAVAILABLE
    version_command["failure_stage"] = (
        AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )

    with pytest.raises(
        ValueError,
        match=r"preflight failure_stage does not match observed failure",
    ):
        AntigravityExecutionEvidence.model_validate_json(json.dumps(payload))


def test_evidence_v11_load_enforces_nested_preflight_failure_priority(
    tmp_path: Path,
) -> None:
    evidence = _build_successful_v11_evidence(tmp_path)
    payload = json.loads(evidence.model_dump_json())
    payload.update(
        {
            "profile": AntigravityCliProfile.NOT_SELECTED,
            "preflight_verified_flags": [],
            "execution_stage": (
                AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
            ),
            "invocation_state": CodexInvocationState.NOT_ATTEMPTED,
            "cleanup_state": CodexCleanupState.NOT_APPLICABLE,
            "provider_status": ProviderExecutionStatus.FAILED,
            "failure_kind": LiveFailureKind.PROCESS_CLEANUP_ERROR,
            "failure_stage": (
                AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
            ),
        }
    )

    version_command = payload["preflight_commands"][0]
    version_command["failure_kind"] = LiveFailureKind.EVIDENCE_ERROR
    version_command["failure_stage"] = (
        AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )
    version_command["collection_error_observed"] = True

    help_command = payload["preflight_commands"][1]
    help_command["failure_kind"] = LiveFailureKind.PROCESS_CLEANUP_ERROR
    help_command["failure_stage"] = (
        AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
    )
    help_command["stdout_truncated"] = True
    help_command["termination"] = {
        "reason": TerminationReason.TIMEOUT,
        "sigterm_sent": True,
        "sigkill_sent": True,
        "process_group_cleared": False,
        "error_code": AntigravityCleanupErrorCode.CLEANUP_TIMEOUT,
    }

    loaded = AntigravityExecutionEvidence.model_validate_json(
        json.dumps(payload)
    )
    assert loaded.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR

    payload["failure_kind"] = LiveFailureKind.EVIDENCE_ERROR
    payload["failure_stage"] = (
        AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION
    )
    with pytest.raises(
        ValueError,
        match=r"must match the highest-priority failed preflight command",
    ):
        AntigravityExecutionEvidence.model_validate_json(json.dumps(payload))


def test_evidence_strict_roundtrip_and_forbid_extra() -> None:
    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        exit_code=0,
    )

    json_data = evidence.model_dump_json()
    assert evidence.schema_version == "1.0"
    assert '"preflight_commands"' not in json_data
    reloaded = AntigravityExecutionEvidence.model_validate_json(json_data)
    assert reloaded == evidence

    invalid_dict = json.loads(json_data)
    invalid_dict["extra_unknown_field"] = "malicious_payload"
    with pytest.raises(ValueError):
        AntigravityExecutionEvidence.model_validate(invalid_dict)


def test_preflight_stderr_output_limit_builds_strict_evidence() -> None:
    evidence = build_antigravity_evidence(
        execution_stage=AntigravityExecutionStage.PREFLIGHT_COMPLETED,
        failure_stage=AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION,
        initial_failure_kind=LiveFailureKind.PROVIDER_OUTPUT_LIMIT,
        stderr_bytes=65536,
        stderr_truncated=True,
    )

    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert evidence.stdout_truncated is False
    assert evidence.stderr_truncated is True
    assert (
        AntigravityExecutionEvidence.model_validate_json(evidence.model_dump_json())
        == evidence
    )


def test_failure_kinds_remain_distinct_in_evidence() -> None:
    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    flags = list(AntigravityHelpMarker)

    error_parser = StrictAntigravityStreamParser()
    error_parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "ERROR", "num_turns": 1}\n'
    )
    error_parser.finalize()
    turn_failed = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=error_parser,
        exit_code=0,
    )

    signal_parser = StrictAntigravityStreamParser()
    signal_parser.parse_chunk(
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    signal_parser.finalize()
    signal_failed = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=signal_parser,
        exit_code=-15,
    )

    timeout_failed = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        termination=AntigravityTerminationEvidence(
            reason=TerminationReason.TIMEOUT,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=True,
            error_code=AntigravityCleanupErrorCode.NONE,
        ),
    )

    cleanup_failed = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=flags,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.FAILED,
        termination=AntigravityTerminationEvidence(
            reason=TerminationReason.RESIDUAL_PROCESS,
            sigterm_sent=True,
            sigkill_sent=True,
            process_group_cleared=False,
            error_code=AntigravityCleanupErrorCode.CLEANUP_TIMEOUT,
        ),
    )

    assert {
        turn_failed.failure_kind,
        signal_failed.failure_kind,
        timeout_failed.failure_kind,
        cleanup_failed.failure_kind,
    } == {
        LiveFailureKind.PROVIDER_TURN_FAILED,
        LiveFailureKind.PROVIDER_SIGNAL_TERMINATION,
        LiveFailureKind.PROVIDER_TIMEOUT,
        LiveFailureKind.PROCESS_CLEANUP_ERROR,
    }


def test_evidence_redaction_with_sensitive_stream_payloads() -> None:
    stream = (
        b'{"event": "init", "user_prompt": "SECRET PROMPT", "path": "/Users/apple/secret"}\n'
        b'{"event": "step_update", "step_type": "tool", "tool_input": "API_KEY=sk-12345"}\n'
        b'{"event": "result", "status": "SUCCESS", "output": "SENSITIVE AGENT RESPONSE"}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )

    json_str = evidence.model_dump_json()

    for secret in (
        "SECRET PROMPT",
        "/Users/apple/secret",
        "sk-12345",
        "SENSITIVE AGENT RESPONSE",
    ):
        assert secret not in json_str


def test_safe_build_evidence_returns_fixed_diagnostic_on_failure() -> None:
    diagnostic = safe_build_antigravity_evidence(
        profile=AntigravityCliProfile.NOT_SELECTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
    )

    assert isinstance(diagnostic, AntigravityEvidenceDiagnostic)
    assert diagnostic.error_code == "EVIDENCE_CONSTRUCTION_FAILED"
    assert diagnostic.failure_kind is LiveFailureKind.EVIDENCE_ERROR
