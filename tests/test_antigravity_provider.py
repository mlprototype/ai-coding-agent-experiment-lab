"""Offline acceptance and reproduction tests for Antigravity CLI Provider Phase 5 Slice 5A."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agentlab.antigravity_provider import (
    AntigravityEvidenceDiagnostic,
    StrictAntigravityStreamParser,
    build_antigravity_evidence,
    probe_antigravity_preflight,
    safe_build_antigravity_evidence,
    select_antigravity_profile,
)
from agentlab.models import (
    AntigravityCliProfile,
    AntigravityEventType,
    AntigravityExecutionEvidence,
    AntigravityExecutionStage,
    AntigravityHelpMarker,
    AntigravityPermissionMode,
    AntigravityStepType,
    AntigravityTerminalStatus,
    CodexCleanupState,
    CodexInvocationState,
    LiveFailureKind,
    ProviderExecutionStatus,
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


def test_parser_arbitrary_chunk_and_split_utf8() -> None:
    # Test parsing 1 byte at a time including multi-byte UTF-8
    stream = (
        b'{"event": "init", "permission_mode": "confirm"}\n'
        b'{"event": "step_update", "step_type": "user_input"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1, "duration_ms": 100}\n'
    )
    parser = StrictAntigravityStreamParser()
    for byte in stream:
        parser.parse_chunk(bytes([byte]))
    parser.finalize()

    assert parser.protocol_error is None
    assert parser.event_count == 3
    assert parser.observed_permission_mode is AntigravityPermissionMode.CONFIRM


def test_parser_all_terminal_statuses() -> None:
    statuses = [
        ("SUCCESS", AntigravityTerminalStatus.SUCCESS),
        ("ERROR", AntigravityTerminalStatus.ERROR),
        ("CANCELED", AntigravityTerminalStatus.CANCELED),
        ("INTERRUPTED", AntigravityTerminalStatus.INTERRUPTED),
    ]
    for status_str, expected_enum in statuses:
        parser = StrictAntigravityStreamParser()
        stream = f'{{"event": "init"}}\n{{"event": "result", "status": "{status_str}"}}\n'.encode()
        parser.parse_chunk(stream)
        parser.finalize()

        assert parser.normalized_terminal_status is expected_enum


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
        preflight_checked_at=pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC),
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


def test_preflight_residual_grandchild_process_cleanup(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    pid_file = tmp_path / "child.pid"
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
    profile, version_str, _flags, _checked_at, _failure_stage, _failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version_str == "agy 1.2.3"

    # Verify child pid was spawned and subsequently killed by process group cleanup
    child_pid = int(pid_file.read_text().strip())
    # Assert child process PID is dead
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_preflight_max_bytes_strict_limit(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_markers = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"

    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "--help" ]; then\n'
        f'  python3 -c "print(\'{help_markers}\'); print(\'X\' * 1000)"\n'
        "  exit 0\n"
        "fi\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, _version_str, _flags, _checked_at, _failure_stage, _failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1


def test_evidence_validator_rejects_lifecycle_mismatch() -> None:
    now = pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC)
    base = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
    )

    # 1. Unselected profile cannot have invocation attempted / process started
    with pytest.raises(
        ValueError,
        match=r"Invocation attempted requires HEADLESS_STREAM_JSON_V1 profile|Unselected profile",
    ):
        dict_data = base.model_dump()
        dict_data["profile"] = AntigravityCliProfile.NOT_SELECTED
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 2. PREFLIGHT_NOT_COMPLETED cannot have PROCESS_STARTED
    with pytest.raises(
        ValueError,
        match=r"Process invocation attempted requires PROVIDER_INVOCATION_ATTEMPTED stage"
        r"|Uncompleted preflight",
    ):
        dict_data = base.model_dump()
        dict_data["execution_stage"] = AntigravityExecutionStage.PREFLIGHT_NOT_COMPLETED
        AntigravityExecutionEvidence.model_validate(dict_data)

    # 3. TIMEOUT failure_kind requires TIMEOUT termination reason
    with pytest.raises(ValueError, match="PROVIDER_TIMEOUT requires TIMEOUT termination reason"):
        dict_data = base.model_dump()
        dict_data["failure_kind"] = LiveFailureKind.PROVIDER_TIMEOUT
        AntigravityExecutionEvidence.model_validate(dict_data)


def test_evidence_strict_roundtrip_and_forbid_extra() -> None:
    now = pytest.importorskip("datetime").datetime.now(pytest.importorskip("datetime").UTC)
    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        preflight_checked_at=now,
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
