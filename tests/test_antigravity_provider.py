"""Offline tests for Antigravity CLI Provider preflight, parser, and Evidence 1.0."""

from __future__ import annotations

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
    AntigravityCleanupErrorCode,
    AntigravityCliProfile,
    AntigravityEventType,
    AntigravityExecutionStage,
    AntigravityFailureStage,
    AntigravityHelpMarker,
    AntigravityReasoningEffort,
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


def test_select_antigravity_profile_default_empty_allowlist() -> None:
    # Default production allowlist is empty -> returns NOT_SELECTED
    profile = select_antigravity_profile("agy 1.0.0", list(AntigravityHelpMarker))
    assert profile is AntigravityCliProfile.NOT_SELECTED


def test_select_antigravity_profile_injected_allowlist() -> None:
    allowlist = frozenset({"agy 1.0.0"})

    # Complete flags -> selected
    profile = select_antigravity_profile(
        "agy 1.0.0", list(AntigravityHelpMarker), allowlist=allowlist
    )
    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1

    # Incomplete flags -> NOT_SELECTED
    partial_flags = [AntigravityHelpMarker.PROMPT, AntigravityHelpMarker.SANDBOX]
    profile_partial = select_antigravity_profile(
        "agy 1.0.0", partial_flags, allowlist=allowlist
    )
    assert profile_partial is AntigravityCliProfile.NOT_SELECTED


def test_parser_valid_stream(tmp_path: Path) -> None:
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


def test_parser_total_tokens_mismatch() -> None:
    # total_tokens = 200, input=100, output=50 -> mismatch!
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", '
        b'"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 200}}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None
    assert "total_tokens" in parser.protocol_error


def test_parser_unknown_step_type_fail_closed() -> None:
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "step_update", "step_type": "unknown_invalid_type"}\n'
        b'{"event": "result", "status": "SUCCESS"}\n'
    )
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None
    assert "Unknown step_type" in parser.protocol_error


def test_parser_duplicate_key_rejection() -> None:
    stream = b'{"event": "init", "foo": 1, "foo": 2}\n'
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None
    assert "Duplicate JSON key" in parser.protocol_error


def test_parser_non_finite_number_rejection() -> None:
    stream = b'{"event": "init", "val": NaN}\n'
    parser = StrictAntigravityStreamParser()
    parser.parse_chunk(stream)
    parser.finalize()

    assert parser.protocol_error is not None


def test_probe_antigravity_preflight_fake_executable(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy"
    help_text = "--prompt --output-format stream-json --model --effort --print-timeout --sandbox"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "agy 1.2.3"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 1\n"
    )
    fake_agy.write_text(script)
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    allowlist = frozenset({"agy 1.2.3"})
    profile, version_str, flags, _checked_at, failure_stage, failure_kind = (
        probe_antigravity_preflight(
            executable_path=str(fake_agy),
            allowlist=allowlist,
        )
    )

    assert profile is AntigravityCliProfile.HEADLESS_STREAM_JSON_V1
    assert version_str == "agy 1.2.3"
    assert len(flags) == len(AntigravityHelpMarker)
    assert failure_stage is None
    assert failure_kind is LiveFailureKind.NONE


def test_evidence_grammar_and_rounding() -> None:
    parser = StrictAntigravityStreamParser()
    # Test .5ms half-to-even rounding (12.5 -> 12)
    stream = (
        b'{"event": "init"}\n'
        b'{"event": "result", "status": "SUCCESS", "num_turns": 1, "duration_ms": 12.5}\n'
    )
    parser.parse_chunk(stream)
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

    assert evidence.provider_duration_ms == 12
    assert evidence.provider_status is ProviderExecutionStatus.SUCCEEDED
    assert evidence.failure_kind is LiveFailureKind.NONE
    assert evidence.failure_stage is None


def test_evidence_exclusive_failure_taxonomy() -> None:
    parser = StrictAntigravityStreamParser()
    stream = b'{"event": "init"}\n{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    parser.parse_chunk(stream)
    parser.finalize()

    # Case 1: Cleanup failure takes priority over success
    term_cleanup_fail = AntigravityTerminationEvidence(
        reason=TerminationReason.EMERGENCY_CLEANUP,
        sigterm_sent=True,
        sigkill_sent=True,
        process_group_cleared=False,
        error_code=AntigravityCleanupErrorCode.CLEANUP_PROCESS_ERROR,
    )
    ev_cleanup = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.FAILED,
        parser=parser,
        exit_code=0,
        termination=term_cleanup_fail,
    )
    assert ev_cleanup.provider_status is ProviderExecutionStatus.FAILED
    assert ev_cleanup.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert ev_cleanup.failure_stage is AntigravityFailureStage.PREFLIGHT_PROCESS_CLEANUP
    assert ev_cleanup.normalized_terminal_status is AntigravityTerminalStatus.SUCCESS

    # Case 2: Timeout takes priority over exit code > 0
    term_timeout = AntigravityTerminationEvidence(
        reason=TerminationReason.TIMEOUT,
        sigterm_sent=True,
        sigkill_sent=False,
        process_group_cleared=True,
        error_code=AntigravityCleanupErrorCode.NONE,
    )
    ev_timeout = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=130,
        termination=term_timeout,
    )
    assert ev_timeout.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT

    # Case 3: Initial protocol error preserved even if exit code is non-zero
    ev_proto = build_antigravity_evidence(
        cli_version="agy 1.2.3",
        profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
        invocation_state=CodexInvocationState.PROCESS_STARTED,
        cleanup_state=CodexCleanupState.CLEARED,
        parser=parser,
        exit_code=1,
        initial_failure_kind=LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
    )
    assert ev_proto.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR


def test_evidence_invalid_grammar_rejection() -> None:
    parser = StrictAntigravityStreamParser()
    stream = b'{"event": "init"}\n{"event": "result", "status": "SUCCESS", "num_turns": 1}\n'
    parser.parse_chunk(stream)
    parser.finalize()

    # Invalid cli_version with extra tokens -> raises ValueError
    with pytest.raises(ValueError, match="cli_version"):
        build_antigravity_evidence(
            cli_version="agy 1.2.3-extra-token",
            profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
            invocation_state=CodexInvocationState.PROCESS_STARTED,
            cleanup_state=CodexCleanupState.CLEARED,
            parser=parser,
            exit_code=0,
        )

    # Invalid model slug starting with dash -> raises ValueError
    with pytest.raises(ValueError, match="requested_model"):
        build_antigravity_evidence(
            cli_version="agy 1.2.3",
            profile=AntigravityCliProfile.HEADLESS_STREAM_JSON_V1,
            requested_model="-invalid-slug",
            invocation_state=CodexInvocationState.PROCESS_STARTED,
            cleanup_state=CodexCleanupState.CLEARED,
            parser=parser,
            exit_code=0,
        )

