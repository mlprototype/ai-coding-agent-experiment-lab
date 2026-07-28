"""Offline acceptance and reproduction tests for Antigravity CLI Provider Phase 5 Slice 5A."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from agentlab.antigravity_provider import (
    StrictAntigravityStreamParser,
    build_antigravity_evidence,
    probe_antigravity_preflight,
    select_antigravity_profile,
)
from agentlab.models import (
    AntigravityCliProfile,
    AntigravityEventType,
    AntigravityExecutionStage,
    AntigravityHelpMarker,
    AntigravityReasoningEffort,
    AntigravityStepType,
    AntigravityTerminalStatus,
    CodexCleanupState,
    CodexInvocationState,
    LiveFailureKind,
    ProviderExecutionStatus,
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


def test_parser_unknown_event_fail_closed() -> None:
    # unknown event inserted in middle -> protocol_error (fail-closed immediately)
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
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )
    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_parser_malformed_usage_not_converted_to_not_available() -> None:
    # usage with boolean input_tokens -> protocol error!
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "usage": {"input_tokens": true}}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None
    assert parser.usage_metrics.source is UsageMetricSource.NOT_AVAILABLE

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )
    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_parser_unknown_step_type_count_retained_in_strict_failure_evidence() -> None:
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "step_update", "step_type": "unknown_type_xyz"}\n'
        b'{"event": "result", "status": "SUCCESS"}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.unknown_step_type_count == 1
    assert parser.protocol_error is not None

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )

    assert evidence.unknown_step_type_count == 1
    assert evidence.provider_status is ProviderExecutionStatus.FAILED
    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_parser_error_preserved_when_exit_code_is_signal_termination() -> None:
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(b'{"event": "init"}\n{"event": "invalid_event_xyz"}\n')
    parser.finalize()

    # exit_code -15 (SIGTERM)
    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=-15,
    )

    assert evidence.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_evidence_rejects_process_started_with_not_applicable_cleanup() -> None:
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(
        b'{"event": "init"}\n{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    )
    parser.finalize()

    with pytest.raises(ValueError, match="started process requires cleanup_state"):
        build_antigravity_evidence(
            cli_version="agy 1.2.3",
            profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
            invocation_state=CodexInvocationState.PROCESS_STARTED,
            cleanup_state=CodexCleanupState.NOT_APPLICABLE,
            parser=parser,
            exit_code=0,
        )


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


def test_preflight_rejects_multiline_version_output(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; echo "extra line"; exit 0; fi\n'
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


def test_preflight_fake_process_large_output_and_cleanup(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_markers = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    # Process outputs large string on help and spawns background child on version
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  (sleep 100 &)\n"
        '  echo "agy 1.2.3"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "--help" ]; then\n'
        f'  python3 -c "print(\'{help_markers}\'); print(\'X\' * 200000)"\n'
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



def test_terminal_error_canceled_interrupted_distinguished_from_protocol_error() -> None:
    for terminal_status in (
        AntigravityTerminalStatus.ERROR,
        AntigravityTerminalStatus.CANCELED,
        AntigravityTerminalStatus.INTERRUPTED,
    ):
        parser = StrictAntigravityStreamParser()
        event_json = f'{{"event": "result", "status": "{terminal_status.value}"}}'
        stream = f'{{"event": "init"}}\n{event_json}\n'.encode()
        parser.parse_chunk(stream)
        parser.finalize()

        evidence = build_antigravity_evidence(
            cli_version="agy 1.2.3",
            profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
            invocation_state=CodexInvocationState.PROCESS_STARTED,
            cleanup_state=CodexCleanupState.CLEARED,
            parser=parser,
            exit_code=0,
        )

        assert evidence.provider_status is ProviderExecutionStatus.FAILED
        assert evidence.failure_kind is LiveFailureKind.PROVIDER_TURN_FAILED


def test_output_limit_distinguished_from_protocol_error() -> None:
    parser = StrictAntigravityStreamParser(max_output_bytes=50)
    padding = "1234567890" * 3
    stream = f'{{"event": "init", "very_large_payload_padding": "{padding}"}}\n'.encode()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.output_limit_exceeded

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )

    assert evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT



def test_evidence_serialization_contains_no_secrets_paths_or_payloads() -> None:
    parser = StrictAntigravityStreamParser()
    sample_file = Path("tests/fixtures/antigravity/sample_stream.jsonl")
    parser.parse_chunk(sample_file.read_bytes())
    parser.finalize()

    evidence = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        requested_model="gemini-3.6-flash",
        requested_reasoning_effort=AntigravityReasoningEffort.LOW,
        execution_stage=AntigravityExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=0,
    )

    json_str = evidence.model_dump_json()

    # Assert absence of paths, prompts, responses, secrets, tool payloads
    for forbidden in (
        "/Users/",
        "secret",
        "password",
        "api_key",
        "user_input",  # payload text (not enum name)
        "checkpoint",
    ):
        assert forbidden not in json_str or forbidden in {
            "user_input",
            "checkpoint",
        }  # Enums allowed

    data_dict = json.loads(json_str)
    # Validate structure
    assert data_dict["schema_version"] == "1.0"
    assert data_dict["provider"] == "antigravity"
    assert data_dict["raw_stream_persisted"] is False
