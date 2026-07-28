"""Offline acceptance and reproduction tests for Antigravity CLI Provider Phase 5 Slice 5A."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from agentlab.antigravity_provider import (
    AntigravityEvidenceDiagnostic,
    StrictAntigravityStreamParser,
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
    # Real multi-byte UTF-8 characters (Japanese + Emoji) split 1 byte at a time
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


def test_parser_output_limit_64kb_and_64kb_plus_1byte() -> None:
    # 1. 64KB exactly should pass within max_output_bytes limit
    parser_64k = StrictAntigravityStreamParser(max_output_bytes=65536)
    # create line that fits within 64KB
    padding = "X" * 60000
    line_ok = f'{{"event": "init", "pad": "{padding}"}}\n'.encode()
    parser_64k.parse_chunk(line_ok)
    assert not parser_64k.output_limit_exceeded

    # 2. Exceeding limit triggers output_limit_exceeded and protocol_error
    parser_overflow = StrictAntigravityStreamParser(max_output_bytes=100)
    parser_overflow.parse_chunk(b'{"event": "init", "pad": "' + b"X" * 150 + b'"}\n')
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
        exit_code=0,
    )
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert evidence.stdout_truncated is True


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
        stream = f'{{"event": "init"}}\n{{"event": "result", "status": "{status_str}"}}\n'.encode()
        parser.parse_chunk(stream)
        parser.finalize()

        assert parser.normalized_terminal_status is expected_enum

    # Unknown status string fails protocol parsing
    parser_unknown = StrictAntigravityStreamParser()
    parser_unknown.parse_chunk(b'{"event": "init"}\n{"event": "result", "status": "UNKNOWN_XYZ"}\n')
    parser_unknown.finalize()
    assert parser_unknown.protocol_error is not None
    assert parser_unknown.normalized_terminal_status is None


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
    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_parser_malformed_usage_all_boundaries() -> None:
    malformed_payloads = [
        b'{"input_tokens": "100"}',
        b'{"input_tokens": true}',
        b'{"output_tokens": -5}',
        b'{"input_tokens": 10, "output_tokens": 10, "total_tokens": 999}',
        b'{"input_tokens": 999999999}',
    ]

    for payload in malformed_payloads:
        parser = StrictAntigravityStreamParser()
        prefix = b'{"event": "init"}\n{"event": "result", "status": "SUCCESS", "usage": '
        stream = prefix + payload + b"}\n"
        parser.parse_chunk(stream)
        parser.finalize()

        assert parser.protocol_error is not None
        assert parser.usage_metrics.source is UsageMetricSource.NOT_AVAILABLE


def test_usage_metrics_rejects_cache_only_or_thinking_only() -> None:
    now = pytest.importorskip("datetime").datetime.now(
        pytest.importorskip("datetime").UTC
    )
    base = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        preflight_verified_flags=list(AntigravityHelpMarker),
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
    )

    with pytest.raises(ValueError, match="cached_input_tokens requires input_tokens"):
        dict_data = base.model_dump()
        dict_data["usage_metrics"] = UsageMetrics(
            cached_input_tokens=10, source=UsageMetricSource.PROVIDER_REPORTED
        )
        AntigravityExecutionEvidence.model_validate(dict_data)

    with pytest.raises(ValueError, match="reasoning_output_tokens requires output_tokens"):
        dict_data = base.model_dump()
        dict_data["usage_metrics"] = UsageMetrics(
            reasoning_output_tokens=5, source=UsageMetricSource.PROVIDER_REPORTED
        )
        AntigravityExecutionEvidence.model_validate(dict_data)


def test_preflight_timeout_normal_recovery_vs_cleanup_failure(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    # Script hangs indefinitely on --version
    script = "#!/bin/sh\nsleep 100\n"
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    # 1. Normal timeout recovery -> LiveFailureKind.PROVIDER_TIMEOUT
    _ret, _out, _err, stage, kind = _run_preflight_subprocess(
        str(fake_agy), ["--version"], timeout_seconds=0.1, clean_env={}
    )
    assert kind is LiveFailureKind.PROVIDER_TIMEOUT
    assert stage is AntigravityFailureStage.PREFLIGHT_PROCESS_COLLECTION

    # 2. Cleanup failure during alive check -> LiveFailureKind.PROCESS_CLEANUP_ERROR
    with patch(
        "agentlab.antigravity_provider._is_process_group_alive",
        side_effect=OSError("Pgid error"),
    ):
        _ret_c, _out_c, _err_c, stage_c, kind_c = _run_preflight_subprocess(
            str(fake_agy), ["--version"], timeout_seconds=0.1, clean_env={}
        )
        assert kind_c is LiveFailureKind.PROCESS_CLEANUP_ERROR
        assert stage_c is AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP


def test_preflight_sigterm_direct_child_reaped_and_pgid_extinct(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    pid_file = tmp_path / "child.pid"
    script = (
        "#!/bin/sh\n"
        f"echo $$ > {pid_file}\n"
        "trap 'exit 0' TERM\n"
        "sleep 100\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    # Allow tiny delay for echo $$ > pid_file to execute before timeout triggers
    _ret, _out, _err, _stage, kind = _run_preflight_subprocess(
        str(fake_agy), ["--version"], timeout_seconds=0.4, clean_env={}
    )

    child_pid = int(pid_file.read_text().strip())
    assert kind is LiveFailureKind.PROVIDER_TIMEOUT
    # Verify process was reaped and no longer exists
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_preflight_grandchild_process_extinct_after_parent_exit(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    pid_file = tmp_path / "grandchild.pid"
    help_markers = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"

    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  (sleep 100) & echo $! > {pid_file}\n"
        '  echo "agy 1.2.3"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "--help" ]; then\n'
        f'  echo "{help_markers}"\n'
        "  exit 0\n"
        "fi\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, version_str, _flags, _checked_at, _failure_stage, failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version_str == "agy 1.2.3"
    assert failure_kind is LiveFailureKind.NONE

    grandchild_pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_preflight_only_version_and_help_invoked_with_clean_env(tmp_path: Path) -> None:
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

    # Inject secret environment into parent process
    os.environ["SECRET_KEY"] = "sk-super-secret-key-12345"

    try:
        allowlist = frozenset({"agy 1.2.3"})
        profile, _version, _flags, _checked_at, _stage, _kind = probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
        assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1

        # 1. Assert ONLY --version and --help were invoked
        invoked_args = args_log.read_text().splitlines()
        assert set(invoked_args) == {"--version", "--help"}

        # 2. Assert SECRET_KEY was NOT inherited by preflight processes
        env_content = env_log.read_text()
        assert "sk-super-secret-key-12345" not in env_content
        assert "SECRET_KEY=" in env_content
    finally:
        os.environ.pop("SECRET_KEY", None)


def test_preflight_version_in_stderr(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3" >&2; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 1\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, version_str, _flags, _checked_at, _failure_stage, failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version_str == "agy 1.2.3"
    assert failure_kind is LiveFailureKind.NONE


def test_preflight_rejects_version_in_both_stdout_and_stderr(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; echo "agy 1.2.3" >&2; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 1\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, version_str, _flags, _checked_at, _failure_stage, _failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.NOT_SELECTED
    assert version_str is None


def test_evidence_validator_bidirectional_state_transitions_all_cases() -> None:
    now = pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC)
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

    # 1. Reject SUCCEEDED status if process was not started
    with pytest.raises(
        ValueError,
        match=r"SUCCEEDED status requires|PROVIDER_INVOCATION_ATTEMPTED stage requires",
    ):
        dict_data = base.model_dump()
        dict_data["invocation_state"] = CodexInvocationState.NOT_ATTEMPTED
        dict_data["cleanup_state"] = CodexCleanupState.NOT_APPLICABLE
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 2. Reject SUCCEEDED status if exit_code != 0
    with pytest.raises(ValueError, match="SUCCEEDED status requires exit_code == 0"):
        dict_data = base.model_dump()
        dict_data["exit_code"] = 1
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 3. Reject SUCCEEDED status if help_markers incomplete
    with pytest.raises(
        ValueError,
        match=r"Selected profile requires all mandatory help markers"
        r"|SUCCEEDED status requires all mandatory help markers",
    ):
        dict_data = base.model_dump()
        dict_data["preflight_verified_flags"] = [AntigravityHelpMarker.PROMPT]
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 4. Bidirectional mismatch: PROVIDER_INVOCATION_ATTEMPTED <=> PROCESS_STARTED
    with pytest.raises(ValueError, match="PROVIDER_INVOCATION_ATTEMPTED stage requires"):
        dict_data = base.model_dump()
        dict_data["invocation_state"] = CodexInvocationState.NOT_ATTEMPTED
        dict_data["cleanup_state"] = CodexCleanupState.NOT_APPLICABLE
        dict_data["provider_status"] = ProviderExecutionStatus.FAILED
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_UNAVAILABLE
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 5. Bidirectional mismatch: Selected Profile => exact cli_version
    with pytest.raises(
        ValueError,
        match=r"cli_version must strictly match|Selected profile requires valid cli_version",
    ):
        dict_data = base.model_dump()
        dict_data["cli_version"] = "invalid_version_str"
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 6. PREFLIGHT_NOT_COMPLETED requires NOT_SELECTED profile
    with pytest.raises(
        ValueError,
        match=r"Selected profile requires PREFLIGHT_COMPLETED"
        r"|PREFLIGHT_NOT_COMPLETED stage requires",
    ):
        dict_data = base.model_dump()
        dict_data["execution_stage"] = AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
        dict_data["invocation_state"] = CodexInvocationState.NOT_ATTEMPTED
        dict_data["cleanup_state"] = CodexCleanupState.NOT_APPLICABLE
        dict_data["provider_status"] = ProviderExecutionStatus.FAILED
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_UNAVAILABLE
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 7. Bidirectional mismatch: cleanup_state == FAILED <=> PROCESS_CLEANUP_ERROR
    with pytest.raises(
        ValueError,
        match=r"failed cleanup requires|FAILED cleanup_state requires",
    ):
        dict_data = base.model_dump()
        dict_data["cleanup_state"] = CodexCleanupState.FAILED
        dict_data["provider_status"] = ProviderExecutionStatus.FAILED
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_TIMEOUT
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 8. Bidirectional mismatch: termination.reason == TIMEOUT <=> PROVIDER_TIMEOUT
    with pytest.raises(ValueError, match="TIMEOUT termination reason requires PROVIDER_TIMEOUT"):
        dict_data = base.model_dump()
        dict_data["termination"] = AntigravityTerminationEvidence(
            reason=TerminationReason.TIMEOUT,
            sigterm_sent=True,
            sigkill_sent=False,
            process_group_cleared=True,
            error_code=AntigravityCleanupErrorCode.NONE,
        )
        dict_data["provider_status"] = ProviderExecutionStatus.FAILED
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
        dict_data["exit_code"] = -15
        AntigravityExecutionEvidence.model_validate(dict_data)


def test_evidence_strict_roundtrip_and_forbid_extra() -> None:
    now = pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC)
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
    reloaded = AntigravityExecutionEvidence.model_validate_json(json_data)
    assert reloaded == evidence

    # Forbid extra fields
    invalid_dict = json.loads(json_data)
    invalid_dict["extra_unknown_field"] = "malicious_payload"
    with pytest.raises(ValueError):
        AntigravityExecutionEvidence.model_validate(invalid_dict)


def test_evidence_redaction_with_sensitive_stream_payloads() -> None:
    stream = (
        b'{"event": "init", "user_prompt": "SECRET PROMPT", "path": "/Users/apple/secret"}\n'
        b'{"event": "step_update", "step_type": "tool", "tool_input": "API_KEY=sk-12345"}\n'
        b'{"event": "result", "status": "SUCCESS", "output": "SENSITIVE AGENT RESPONSE"}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    now = pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC)
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

    for secret in ("SECRET PROMPT", "/Users/apple/secret", "sk-12345", "SENSITIVE AGENT RESPONSE"):
        assert secret not in json_str


def test_safe_build_evidence_returns_fixed_diagnostic_on_failure() -> None:
    # Passing invalid invalid combination to trigger construction failure
    diagnostic = safe_build_antigravity_evidence(
        profile=AntigravityCliProfile.NOT_SELECTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
    )

    assert isinstance(diagnostic, AntigravityEvidenceDiagnostic)
    assert diagnostic.error_code == "EVIDENCE_CONSTRUCTION_FAILED"
    assert diagnostic.failure_kind is LiveFailureKind.EVIDENCE_ERROR
