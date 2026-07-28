from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from pydantic import ValidationError

import agentlab.codex_provider as codex_provider_module
import agentlab.live as live_module
import agentlab.runner as runner_module
from agentlab.codex_provider import REQUIRED_CODEX_EXEC_FLAGS
from agentlab.live import (
    LiveArtifactLoadError,
    LiveCodexError,
    LiveDiagnosticCreatedError,
    LiveDiagnosticPublicationError,
    load_failure_diagnostic,
    load_live_artifact,
    run_live_codex,
    write_failure_diagnostic,
    write_live_outputs,
)
from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    CodexCleanupState,
    CodexCliProfile,
    CodexExecutionStage,
    CodexFailureStage,
    CodexInvocationState,
    CodexItemType,
    CodexProviderFailureHint,
    CodexRunnerState,
    CodexStdinWriteState,
    CodexTerminalEvent,
    DiagnosticCleanupState,
    DiagnosticFailureStage,
    DiagnosticInvocationState,
    DiagnosticRunnerState,
    LiveDiagnosticCode,
    LiveFailureDiagnostic,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
    LiveSettings,
    ProviderActivityDetermination,
    TerminationEvidence,
    TerminationReason,
    WorkspaceLifecycle,
)
from agentlab.recording import (
    LiveRunCompletedEvent,
    LiveRunFailedEvent,
    RecordingLoadError,
    load_replay_recording,
)
from agentlab.replay import ReplayError, run_replay
from agentlab.specs import SpecLoadError
from agentlab.workspace import WorkspaceError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Live Codex is POSIX-only")

_SUPPORTED_CODEX_VERSION = next(iter(CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS))
_CODEX_15_FIELDS = (
    "stdin_write_state",
    "stdin_bytes_written",
    "stdin_bytes_total",
    "provider_failure_hint",
)


def _remove_codex_15_fields(codex_payload: dict[str, Any]) -> None:
    for field in _CODEX_15_FIELDS:
        codex_payload.pop(field)


def _fake_codex(
    tmp_path: Path,
    *,
    live_code: str,
    version_code: str = f"print({_SUPPORTED_CODEX_VERSION!r})",
) -> tuple[dict[str, str], Path]:
    fake_directory = tmp_path / "fake-bin"
    fake_directory.mkdir(exist_ok=True)
    inspection = tmp_path / "provider-inspection.json"
    script = (
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        f"inspection=pathlib.Path({str(inspection)!r})\n"
        "if sys.argv[1:] == ['--version']:\n"
        f"{textwrap.indent(version_code, '    ')}\n"
        "elif sys.argv[1:] == ['exec','--help']:\n"
        f"    print({' '.join(REQUIRED_CODEX_EXEC_FLAGS)!r})\n"
        "else:\n"
        f"{textwrap.indent(live_code, '    ')}\n"
    )
    executable = fake_directory / "codex"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir(exist_ok=True)
    environment = {
        "PATH": f"{fake_directory}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "parent-home"),
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "synthetic-openai-api-key",
        "CODEX_API_KEY": "synthetic-codex-api-key",
        "AGENTLAB_PARENT_SECRET": "synthetic-parent-environment-secret",
    }
    return environment, inspection


def _base_spec() -> dict[str, Any]:
    loaded = yaml.safe_load(
        Path("experiments/examples/codex-live-smoke.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def _write_case(
    tmp_path: Path,
    *,
    prompt: str = "Implement the requested deterministic change. prompt-secret-marker",
    quality_gate: dict[str, list[list[str]]] | None = None,
) -> tuple[Path, Path, Path, Path]:
    case = tmp_path / "case"
    fixture = case / "fixtures" / "task"
    prompt_directory = case / "prompts"
    fixture.mkdir(parents=True)
    prompt_directory.mkdir()
    (fixture / "task.txt").write_text("status=TODO\n", encoding="utf-8")
    (fixture / "check.py").write_text("# unchanged gate fixture\n", encoding="utf-8")
    prompt_path = prompt_directory / "task.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    spec = deepcopy(_base_spec())
    spec["task_ids"] = ["task-1"]
    spec["live"]["record_to"] = "recordings/live.jsonl"
    spec["live"]["prompt_path"] = "prompts/task.md"
    spec["runner"]["fixture_path"] = "fixtures/task"
    spec["runner"]["command_timeout_ms"] = 1000
    spec["runner"]["termination_grace_ms"] = 100
    spec["quality_gate"] = quality_gate or {
        "acceptance": [
            [
                sys.executable,
                "-c",
                "import pathlib,sys;"
                "sys.exit(0 if pathlib.Path('task.txt').read_text()"
                "=='status=COMPLETE\\n' else 1)",
            ]
        ],
        "regression": [
            [
                sys.executable,
                "-c",
                "import pathlib,sys;sys.exit(0 if pathlib.Path('check.py').is_file() else 1)",
            ]
        ],
        "lint": [
            [
                sys.executable,
                "-c",
                "import pathlib,sys;"
                "sys.exit(0 if pathlib.Path('task.txt').read_bytes().endswith(b'\\n') else 1)",
            ]
        ],
        "typecheck": [
            [
                sys.executable,
                "-c",
                "import os,pathlib,sys;"
                "home=pathlib.Path(os.environ['HOME']);"
                "sys.exit(1 if 'CODEX_HOME' in os.environ "
                "or (home/'provider-only-marker').exists() else 0)",
            ]
        ],
    }
    spec_path = case / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    output_path = tmp_path / "evidence" / "live.json"
    return spec_path, fixture, prompt_path, output_path


def _success_code(*, binary: bool = False) -> str:
    change = (
        "pathlib.Path('binary.dat').write_bytes(b'\\x00binary')"
        if binary
        else "pathlib.Path('task.txt').write_text('status=COMPLETE\\n',encoding='utf-8')"
    )
    return (
        "prompt=sys.stdin.read()\n"
        "inspection.write_text(json.dumps({"
        "'argv':sys.argv,'prompt':prompt,"
        "'openai_key':os.environ.get('OPENAI_API_KEY'),"
        "'codex_key':os.environ.get('CODEX_API_KEY'),"
        "'parent_secret':os.environ.get('AGENTLAB_PARENT_SECRET'),"
        "'codex_home_present':'CODEX_HOME' in os.environ"
        "}),encoding='utf-8')\n"
        f"{change}\n"
        "print(json.dumps({'type':'thread.started','thread_id':'raw-thread-secret'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'message','text':'raw-event-secret'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'cached_input_tokens':2,'output_tokens':5,'reasoning_output_tokens':1}}),flush=True)\n"
        "pathlib.Path(os.environ['HOME'],'provider-only-marker').write_text('private',encoding='utf-8')\n"
        "sys.stderr.write('raw-stderr-secret')"
    )


def _run(
    spec_path: Path,
    output_path: Path,
    environment: dict[str, str],
    *,
    force: bool = False,
):
    return run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id="live-test-run",
        output_path=output_path,
        confirm_live_codex=True,
        force=force,
        parent_environment=environment,
    )


def _diagnostic_path(output_path: Path) -> Path:
    suffix = output_path.suffix or ".json"
    return output_path.with_name(f"{output_path.stem}.diagnostic{suffix}")


def _sample_diagnostic() -> LiveFailureDiagnostic:
    return LiveFailureDiagnostic(
        schema_version="1.0",
        run_id="diagnostic-run",
        experiment_id="diagnostic-experiment",
        task_id="diagnostic-task",
        failure_kind=LiveFailureKind.EVIDENCE_ERROR,
        diagnostic_code=LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED,
        failure_stage=DiagnosticFailureStage.PREFLIGHT,
        runner_state=DiagnosticRunnerState.NOT_STARTED,
        invocation_state=DiagnosticInvocationState.NOT_ATTEMPTED,
        cleanup_state=DiagnosticCleanupState.NOT_APPLICABLE,
        workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
        paired_artifacts_published=False,
        gate_executed=False,
        provider_activity_determined=ProviderActivityDetermination.DETERMINED,
        created_at=datetime.now(UTC),
    )


def _assert_safe_failed_live_outputs(
    outcome: Any,
    output_path: Path,
    prompt_path: Path,
    environment: dict[str, str],
    *,
    expected_stage: CodexFailureStage,
    expected_execution_stage: CodexExecutionStage,
    expected_failure_kind: LiveFailureKind,
    expected_process_started: bool = False,
) -> None:
    artifact = load_live_artifact(output_path)
    recording = load_replay_recording(outcome.recording_path)
    recording_bytes = outcome.recording_path.read_bytes()

    assert artifact == outcome.artifact
    assert artifact.failure_kind is expected_failure_kind
    assert artifact.codex.failure_kind is expected_failure_kind
    assert artifact.codex.failure_stage is expected_stage
    assert artifact.codex.execution_stage is expected_execution_stage
    assert artifact.codex.process_started is expected_process_started
    assert artifact.gate_commands == []
    assert artifact.metrics is None
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert artifact.recording_sha256 == hashlib.sha256(recording_bytes).hexdigest()
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert recording.completed is None
    assert recording.failed.failure_kind is expected_failure_kind
    assert recording.failed.codex.failure_stage is expected_stage
    assert len(recording_bytes.splitlines()) == 2

    persisted = output_path.read_bytes() + recording_bytes
    forbidden_values = [
        prompt_path.read_bytes(),
        environment["CODEX_HOME"].encode(),
        environment["OPENAI_API_KEY"].encode(),
        environment["CODEX_API_KEY"].encode(),
        environment["AGENTLAB_PARENT_SECRET"].encode(),
    ]
    assert all(value not in persisted for value in forbidden_values)

    forbidden_keys = {
        "prompt",
        "raw_jsonl",
        "raw_stderr",
        "agent_message",
        "reasoning",
        "command_output",
        "thread_id",
        "session_id",
        "executable_path",
        "home",
        "codex_home",
        "authentication",
        "api_key",
    }
    payloads = [json.loads(output_path.read_text(encoding="utf-8"))]
    payloads.extend(
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    )
    observed_keys: set[str] = set()
    stack: list[object] = list(payloads)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            observed_keys.update(str(key).casefold() for key in value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    assert observed_keys.isdisjoint(forbidden_keys)


def test_live_success_runs_provider_and_gates_in_same_workspace_and_replays(
    tmp_path: Path,
) -> None:
    prompt_secret = "prompt-secret-marker"
    spec_path, fixture, _prompt_path, output = _write_case(
        tmp_path,
        prompt=f"Make the deterministic edit. {prompt_secret}",
    )
    environment, inspection = _fake_codex(
        tmp_path,
        live_code=_success_code(),
    )

    outcome = _run(spec_path, output, environment)
    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)
    persisted = output.read_text(encoding="utf-8") + outcome.recording_path.read_text(
        encoding="utf-8"
    )
    observed = json.loads(inspection.read_text(encoding="utf-8"))

    assert artifact == outcome.artifact
    assert artifact.overall_status is LiveOverallStatus.PASSED
    assert artifact.failure_kind is LiveFailureKind.NONE
    assert artifact.codex.schema_version == "1.5"
    assert artifact.codex.stdin_write_state is CodexStdinWriteState.COMPLETE
    assert artifact.codex.stdin_bytes_written == artifact.prompt_bytes
    assert artifact.codex.stdin_bytes_total == artifact.prompt_bytes
    assert (
        artifact.codex.provider_failure_hint
        is CodexProviderFailureHint.NOT_APPLICABLE
    )
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert artifact.metrics is not None
    assert artifact.metrics.quality_gate_pass is True
    assert artifact.metrics.agent_call_count == 1
    assert artifact.metrics.acceptance_tests_passed == 1
    assert artifact.metrics.usage_metrics is not None
    assert artifact.metrics.usage_metrics.input_tokens == 11
    assert len(artifact.gate_commands) == 4
    assert artifact.diff.changed_files == ["task.txt"]
    assert artifact.recording_sha256 == hashlib.sha256(
        outcome.recording_path.read_bytes()
    ).hexdigest()
    assert isinstance(recording.completed, LiveRunCompletedEvent)
    assert recording.failed is None
    assert recording.completed.schema_version == "1.1"
    assert recording.completed.codex.stdin_write_state is CodexStdinWriteState.COMPLETE
    assert (
        recording.completed.codex.provider_failure_hint
        is CodexProviderFailureHint.NOT_APPLICABLE
    )
    assert artifact.schema_version == "1.1"
    assert artifact.evaluation_duration_ms == artifact.metrics.evaluation_duration_ms
    assert observed["prompt"].endswith(prompt_secret)
    assert observed["codex_home_present"] is True
    assert observed["openai_key"] is None
    assert observed["codex_key"] is None
    assert observed["parent_secret"] is None
    assert (fixture / "task.txt").read_text(encoding="utf-8") == "status=TODO\n"
    assert not _diagnostic_path(output).exists()
    for secret in (
        prompt_secret,
        "raw-thread-secret",
        "raw-event-secret",
        "raw-stderr-secret",
        "synthetic-parent-environment-secret",
    ):
        assert secret not in persisted
    assert str(tmp_path) not in persisted

    replay_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    replay_spec["execution_mode"] = "replay"
    replay_spec["replay"] = {"recording_path": "recordings/live.jsonl"}
    replay_spec["live"] = None
    replay_spec_path = spec_path.parent / "replay.yaml"
    replay_spec_path.write_text(
        yaml.safe_dump(replay_spec, sort_keys=False),
        encoding="utf-8",
    )
    replay_output = tmp_path / "replay-result.json"
    result = run_replay(replay_spec_path, replay_output)

    assert result.execution_mode.value == "replay"
    assert result.metrics == artifact.metrics


def test_live_artifact_1_0_remains_strict_loadable(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code=_success_code(),
    )
    artifact = _run(spec_path, output, environment).artifact
    legacy_payload = artifact.model_dump(mode="json")
    legacy_payload["schema_version"] = "1.0"
    del legacy_payload["evaluation_duration_ms"]
    legacy_path = tmp_path / "legacy-live-evidence.json"
    legacy_path.write_text(
        json.dumps(legacy_payload, sort_keys=True),
        encoding="utf-8",
    )

    legacy = load_live_artifact(legacy_path)
    assert legacy.schema_version == "1.0"
    assert legacy.evaluation_duration_ms is None
    assert "evaluation_duration_ms" not in legacy.model_dump(mode="json")

    legacy_payload["evaluation_duration_ms"] = 0
    legacy_path.write_text(
        json.dumps(legacy_payload, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(LiveArtifactLoadError, match=r"1\.0"):
        load_live_artifact(legacy_path)

    current_payload = artifact.model_dump(mode="json")
    del current_payload["evaluation_duration_ms"]
    legacy_path.write_text(
        json.dumps(current_payload, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(LiveArtifactLoadError, match=r"1\.1"):
        load_live_artifact(legacy_path)


def test_live_pre_turn_warning_is_nonfatal_and_runs_gates(tmp_path: Path) -> None:
    raw_warning = "raw-pre-turn-warning-secret"
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    live_code = (
        "sys.stdin.read()\n"
        "pathlib.Path('task.txt').write_text('status=COMPLETE\\n',encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'type':'error','message':{raw_warning!r}}}}}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'raw-agent-secret'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)

    assert artifact.overall_status is LiveOverallStatus.PASSED
    assert artifact.failure_kind is LiveFailureKind.NONE
    assert artifact.codex.schema_version == "1.5"
    assert artifact.codex.item_type_counts == {
        CodexItemType.AGENT_MESSAGE: 1,
        CodexItemType.ERROR: 1,
    }
    assert artifact.codex.error_event_count == 0
    assert len(artifact.gate_commands) == 4
    assert artifact.metrics is not None
    assert artifact.metrics.quality_gate_pass is True
    assert isinstance(recording.completed, LiveRunCompletedEvent)
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert not _diagnostic_path(output).exists()
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert raw_warning.encode() not in persisted
    assert prompt_path.read_bytes() not in persisted


def test_normal_gate_failure_has_metrics_and_completed_recording(tmp_path: Path) -> None:
    quality_gate = {
        "acceptance": [[sys.executable, "-c", "raise SystemExit(1)"]],
        "regression": [[sys.executable, "-c", "raise SystemExit(0)"]],
        "lint": [],
        "typecheck": [],
    }
    spec_path, _fixture, _prompt, output = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.FAILED
    assert outcome.artifact.failure_kind is LiveFailureKind.QUALITY_GATE_FAILURE
    assert outcome.artifact.metrics is not None
    assert outcome.artifact.metrics.quality_gate_pass is False
    assert isinstance(recording.completed, LiveRunCompletedEvent)


def test_provider_failure_skips_gates_and_writes_failed_recording(tmp_path: Path) -> None:
    gate_marker = tmp_path / "gate-must-not-run"
    quality_gate = {
        "acceptance": [
            [
                sys.executable,
                "-c",
                f"import pathlib;pathlib.Path({str(gate_marker)!r}).write_text('ran')",
            ]
        ],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec_path, _fixture, _prompt, output = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    live_code = (
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'error','message':'provider-raw-secret'}),flush=True)\n"
        "print(json.dumps({'type':'turn.failed','error':{'message':'provider-raw-secret'}}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.PROVIDER_TURN_FAILED
    assert (
        outcome.artifact.codex.provider_failure_hint
        is CodexProviderFailureHint.UNKNOWN
    )
    assert outcome.artifact.gate_commands == []
    assert outcome.artifact.metrics is None
    assert outcome.artifact.codex.schema_version == "1.5"
    assert outcome.artifact.codex.error_event_count == 1
    assert outcome.artifact.codex.turn_failed_count == 1
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert not gate_marker.exists()
    assert not _diagnostic_path(output).exists()
    with pytest.raises(ReplayError, match="Metrics are absent"):
        replay_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        replay_spec["execution_mode"] = "replay"
        replay_spec["replay"] = {"recording_path": "recordings/live.jsonl"}
        replay_spec["live"] = None
        replay_path = spec_path.parent / "replay-failed.yaml"
        replay_path.write_text(yaml.safe_dump(replay_spec), encoding="utf-8")
        run_replay(replay_path, tmp_path / "must-not-exist.json")


def test_live_protocol_error_publishes_strict_paired_failure_without_diagnostic(
    tmp_path: Path,
) -> None:
    gate_marker = tmp_path / "gate-must-not-run"
    quality_gate = {
        "acceptance": [
            [
                sys.executable,
                "-c",
                f"import pathlib;pathlib.Path({str(gate_marker)!r}).write_text('ran')",
            ]
        ],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec_path, _fixture, prompt_path, output = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    raw_secret = "raw-protocol-event-secret"
    live_code = (
        "import signal\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type':'thread.started','value':{raw_secret!r}}}),flush=True)\n"
        f"print(json.dumps({{'type':'thread.started','value':{raw_secret!r}}}),flush=True)\n"
        "signal.pause()"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)

    assert artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert artifact.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert artifact.codex.schema_version == "1.5"
    assert artifact.codex.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert artifact.codex.process_started is True
    assert artifact.codex.cleanup_state is CodexCleanupState.CLEARED
    assert artifact.codex.event_count == 1
    assert artifact.codex.thread_started_count == 1
    assert artifact.gate_commands == []
    assert artifact.metrics is None
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert recording.failed.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert not gate_marker.exists()
    assert not _diagnostic_path(output).exists()
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert raw_secret.encode() not in persisted
    assert prompt_path.read_bytes() not in persisted


def test_buffered_protocol_error_skips_gates_and_redacts_rejected_event(
    tmp_path: Path,
) -> None:
    gate_marker = tmp_path / "gate-must-not-run"
    quality_gate = {
        "acceptance": [
            [
                sys.executable,
                "-c",
                f"import pathlib;pathlib.Path({str(gate_marker)!r}).write_text('ran')",
            ]
        ],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec_path, _fixture, prompt_path, output = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    raw_secret = "quota raw-buffered-live-secret"
    live_code = (
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        f"sys.stdout.write(json.dumps({{'type':'turn.failed','error':{{'message':[{raw_secret!r}]}}}}))\n"
        "raise SystemExit(1)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)

    assert artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert artifact.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert artifact.codex.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert artifact.codex.event_count == 2
    assert artifact.codex.turn_failed_count == 0
    assert artifact.codex.terminal_event is CodexTerminalEvent.NONE
    assert (
        artifact.codex.provider_failure_hint
        is CodexProviderFailureHint.NOT_APPLICABLE
    )
    assert artifact.gate_commands == []
    assert artifact.metrics is None
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert recording.failed.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert not gate_marker.exists()
    assert not _diagnostic_path(output).exists()
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert raw_secret.encode() not in persisted
    assert prompt_path.read_bytes() not in persisted


def test_provider_timeout_skips_gates_and_removes_workspace(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["live"]["provider_timeout_ms"] = 100
    spec["runner"]["termination_grace_ms"] = 50
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    live_code = (
        "import signal\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "signal.pause()"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
    assert outcome.artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert outcome.artifact.gate_commands == []
    assert outcome.artifact.metrics is None
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert isinstance(recording.failed, LiveRunFailedEvent)


def test_provider_process_cleanup_failure_survives_workspace_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["live"]["provider_timeout_ms"] = 100
    spec["runner"]["termination_grace_ms"] = 50
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="import signal\nsignal.pause()",
    )
    terminate_process_group = codex_provider_module.terminate_process_group
    remove_disposable_workspace = live_module.remove_disposable_workspace

    def report_process_cleanup_failure(*args: object, **kwargs: object):
        termination = terminate_process_group(*args, **kwargs)
        assert termination.process_group_cleared
        return termination.model_copy(
            update={
                "process_group_cleared": False,
                "error": "synthetic Provider process cleanup failure",
            }
        )

    def report_workspace_cleanup_failure(workspace: object) -> tuple[bool, str]:
        cleanup_succeeded, _cleanup_error = remove_disposable_workspace(workspace)
        assert cleanup_succeeded
        return False, "synthetic Workspace cleanup failure"

    monkeypatch.setattr(
        "agentlab.codex_provider.terminate_process_group",
        report_process_cleanup_failure,
    )
    monkeypatch.setattr(
        "agentlab.live.remove_disposable_workspace",
        report_workspace_cleanup_failure,
    )

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert (
        outcome.artifact.codex.failure_kind
        is LiveFailureKind.PROCESS_CLEANUP_ERROR
    )
    assert (
        outcome.artifact.workspace_lifecycle
        is WorkspaceLifecycle.CLEANUP_FAILED
    )
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert (
        recording.failed.failure_kind
        is LiveFailureKind.PROCESS_CLEANUP_ERROR
    )


def test_provider_signal_termination_skips_gates_and_metrics(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code=(
            "import signal\n"
            "sys.stdin.read()\n"
            "os.kill(os.getpid(),signal.SIGTERM)"
        ),
    )

    outcome = _run(spec_path, output, environment)

    assert (
        outcome.artifact.failure_kind
        is LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
    )
    assert outcome.artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert outcome.artifact.codex.exit_code is not None
    assert outcome.artifact.codex.exit_code < 0
    assert outcome.artifact.gate_commands == []
    assert outcome.artifact.metrics is None
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED


def test_internal_orchestration_exception_removes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_gate_orchestration(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic internal failure")

    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        fail_gate_orchestration,
    )
    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.metrics is None
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED


def test_workspace_preparation_failure_is_persisted_with_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_prepare(*_args: object, **_kwargs: object) -> NoReturn:
        raise WorkspaceError(
            "synthetic preparation failure",
            lifecycle=WorkspaceLifecycle.REMOVED,
        )

    monkeypatch.setattr("agentlab.live.prepare_disposable_workspace", fail_prepare)
    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert outcome.artifact.codex.process_started is False
    assert (
        outcome.artifact.codex.cli_profile
        is CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2
    )
    assert (
        outcome.artifact.codex.execution_stage
        is CodexExecutionStage.PREFLIGHT_COMPLETED
    )
    assert (
        outcome.artifact.codex.failure_stage
        is CodexFailureStage.WORKSPACE_PREPARATION
    )
    assert outcome.artifact.codex.verified_flags == sorted(
        REQUIRED_CODEX_EXEC_FLAGS
    )
    assert outcome.artifact.codex.approval_policy is None
    assert outcome.artifact.codex.approval_basis is None
    assert outcome.artifact.metrics is None
    assert isinstance(recording.failed, LiveRunFailedEvent)


@pytest.mark.parametrize(
    ("cleanup_succeeds", "expected_lifecycle"),
    [
        (True, WorkspaceLifecycle.REMOVED),
        (False, WorkspaceLifecycle.CLEANUP_FAILED),
    ],
)
def test_workspace_root_resolve_failure_persists_cleanup_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_succeeds: bool,
    expected_lifecycle: WorkspaceLifecycle,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    temporary_root = tmp_path / "unresolved-live-run-root"
    resolve = Path.resolve

    monkeypatch.setattr(
        "agentlab.workspace.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root.mkdir() or temporary_root),
    )

    def fail_created_root_resolve(
        path: Path,
        strict: bool = False,
    ) -> Path:
        if path == temporary_root:
            raise OSError("synthetic Live workspace root resolve failure")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_created_root_resolve)
    if not cleanup_succeeds:
        monkeypatch.setattr(
            "agentlab.workspace._remove_temporary_root",
            lambda _path: (False, "synthetic cleanup failure"),
        )

    outcome = run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id="workspace-resolve-failure",
        output_path=output,
        confirm_live_codex=True,
        parent_environment=environment,
        preflight=lambda **_kwargs: preflight_result,
    )
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.workspace_lifecycle is expected_lifecycle
    assert outcome.artifact.codex.process_started is False
    assert (
        outcome.artifact.codex.execution_stage
        is CodexExecutionStage.PREFLIGHT_COMPLETED
    )
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert recording.failed.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert temporary_root.exists() is (not cleanup_succeeds)
    if temporary_root.exists():
        temporary_root.rmdir()


def test_provider_environment_preparation_failure_preserves_selected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_provider_environment(_parent: Path, name: str) -> Path:
        if name == "provider":
            raise OSError("synthetic Provider environment failure")
        raise AssertionError("Gate environment must not be prepared")

    monkeypatch.setattr(
        "agentlab.live._prepare_live_environment_root",
        fail_provider_environment,
    )

    def reject_gate_execution(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("quality Gates must not run after Provider failure")

    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        reject_gate_execution,
    )
    outcome = _run(spec_path, output, environment)

    _assert_safe_failed_live_outputs(
        outcome,
        output,
        prompt_path,
        environment,
        expected_stage=(
            CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION
        ),
        expected_execution_stage=CodexExecutionStage.PREFLIGHT_COMPLETED,
        expected_failure_kind=LiveFailureKind.EVIDENCE_ERROR,
    )
    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.codex.process_started is False
    assert (
        outcome.artifact.codex.cli_profile
        is CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2
    )
    assert (
        outcome.artifact.codex.execution_stage
        is CodexExecutionStage.PREFLIGHT_COMPLETED
    )
    assert (
        outcome.artifact.codex.failure_stage
        is CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION
    )
    assert outcome.artifact.codex.verified_flags == sorted(
        REQUIRED_CODEX_EXEC_FLAGS
    )
    assert outcome.artifact.codex.approval_policy is None
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED


@pytest.mark.parametrize(
    ("fault_mode", "expected_stage", "expected_runner_state"),
    [
        (
            "runner_construction",
            CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION,
            CodexRunnerState.NOT_STARTED,
        ),
        (
            "runner_entry",
            CodexFailureStage.PROVIDER_RUNNER_ENTRY,
            CodexRunnerState.STARTED,
        ),
        (
            "runtime_precheck",
            CodexFailureStage.PROVIDER_RUNTIME_PRECHECK,
            CodexRunnerState.STARTED,
        ),
    ],
)
def test_runner_boundary_faults_preserve_pre_spawn_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_mode: str,
    expected_stage: CodexFailureStage,
    expected_runner_state: CodexRunnerState,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def fail_boundary(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic runner boundary secret")

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    if fault_mode == "runner_construction":
        monkeypatch.setattr("agentlab.live.CodexProcessRunner", fail_boundary)
    elif fault_mode == "runner_entry":
        monkeypatch.setattr(
            "agentlab.codex_provider.CodexProcessRunner.run",
            fail_boundary,
        )
    else:
        monkeypatch.setattr(
            "agentlab.codex_provider.ensure_runner_platform_supported",
            fail_boundary,
        )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    outcome = run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id=f"offline-{fault_mode}",
        output_path=output,
        confirm_live_codex=True,
        parent_environment=environment,
        preflight=lambda **_kwargs: preflight_result,
    )

    _assert_safe_failed_live_outputs(
        outcome,
        output,
        prompt_path,
        environment,
        expected_stage=expected_stage,
        expected_execution_stage=CodexExecutionStage.PREFLIGHT_COMPLETED,
        expected_failure_kind=LiveFailureKind.EVIDENCE_ERROR,
    )
    assert outcome.artifact.codex.schema_version == "1.5"
    assert outcome.artifact.codex.runner_state is expected_runner_state
    assert (
        outcome.artifact.codex.invocation_state
        is CodexInvocationState.NOT_ATTEMPTED
    )
    assert (
        outcome.artifact.codex.cleanup_state
        is CodexCleanupState.NOT_APPLICABLE
    )
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert b"synthetic runner boundary secret" not in persisted


@pytest.mark.parametrize(
    (
        "fault_target",
        "expected_stage",
        "expected_execution_stage",
        "expected_failure_kind",
    ),
    [
        (
            "agentlab.codex_provider.CodexJsonlParser",
            CodexFailureStage.JSONL_PARSER_INITIALIZATION,
            CodexExecutionStage.PREFLIGHT_COMPLETED,
            LiveFailureKind.EVIDENCE_ERROR,
        ),
        (
            "agentlab.codex_provider.build_codex_argv",
            CodexFailureStage.PROVIDER_ARGV_CONSTRUCTION,
            CodexExecutionStage.PREFLIGHT_COMPLETED,
            LiveFailureKind.EVIDENCE_ERROR,
        ),
        (
            "agentlab.codex_provider.build_codex_environment",
            CodexFailureStage.PROVIDER_ENVIRONMENT_CONSTRUCTION,
            CodexExecutionStage.PREFLIGHT_COMPLETED,
            LiveFailureKind.EVIDENCE_ERROR,
        ),
        (
            "agentlab.codex_provider.subprocess.Popen",
            CodexFailureStage.PROVIDER_PROCESS_SPAWN,
            CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED,
            LiveFailureKind.PROVIDER_SPAWN_ERROR,
        ),
    ],
)
def test_pre_provider_faults_persist_safe_stage_without_running_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
    expected_stage: CodexFailureStage,
    expected_execution_stage: CodexExecutionStage,
    expected_failure_kind: LiveFailureKind,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def fail_boundary(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic failure text must not be persisted")

    def reject_gate_execution(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("quality Gates must not run after Provider failure")

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    monkeypatch.setattr(fault_target, fail_boundary)
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        reject_gate_execution,
    )

    outcome = run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id=f"offline-{expected_stage.value}",
        output_path=output,
        confirm_live_codex=True,
        parent_environment=environment,
        preflight=lambda **_kwargs: preflight_result,
    )

    _assert_safe_failed_live_outputs(
        outcome,
        output,
        prompt_path,
        environment,
        expected_stage=expected_stage,
        expected_execution_stage=expected_execution_stage,
        expected_failure_kind=expected_failure_kind,
    )
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert b"synthetic failure text must not be persisted" not in persisted
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()
    assert outcome.artifact.codex.schema_version == "1.5"
    assert outcome.artifact.codex.runner_state is CodexRunnerState.STARTED
    if expected_execution_stage is CodexExecutionStage.PREFLIGHT_COMPLETED:
        assert (
            outcome.artifact.codex.invocation_state
            is CodexInvocationState.NOT_ATTEMPTED
        )
        assert (
            outcome.artifact.codex.cleanup_state
            is CodexCleanupState.NOT_APPLICABLE
        )
        assert outcome.artifact.codex.approval_policy is None
        assert outcome.artifact.codex.approval_basis is None
    else:
        assert (
            outcome.artifact.codex.invocation_state
            is CodexInvocationState.SPAWN_ATTEMPTED
        )
        assert (
            outcome.artifact.codex.cleanup_state
            is CodexCleanupState.NOT_APPLICABLE
        )
        assert outcome.artifact.codex.approval_policy == "never"
        assert outcome.artifact.codex.approval_basis == "explicit_config_never"


@pytest.mark.parametrize(
    ("fault_mode", "expected_stage"),
    [
        (
            "result_construction",
            CodexFailureStage.PROVIDER_RUNNER_RESULT_CONSTRUCTION,
        ),
        (
            "result_extraction",
            CodexFailureStage.PROVIDER_RUNNER_RESULT_EXTRACTION,
        ),
    ],
)
def test_post_spawn_handoff_faults_preserve_process_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_mode: str,
    expected_stage: CodexFailureStage,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    popen = codex_provider_module.subprocess.Popen
    spawned_pids: list[int] = []

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_boundary(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic handoff secret")

    monkeypatch.setattr(
        "agentlab.codex_provider.subprocess.Popen",
        track_spawn,
    )
    if fault_mode == "result_construction":
        monkeypatch.setattr(
            "agentlab.codex_provider.CodexRunResult",
            fail_boundary,
        )
    else:
        original_run = codex_provider_module.CodexProcessRunner.run

        class ExtractionFailure:
            @property
            def evidence(self) -> NoReturn:
                raise RuntimeError("synthetic handoff secret")

        def fail_result_extraction(
            runner: Any,
            *args: object,
            **kwargs: object,
        ) -> ExtractionFailure:
            original_run(runner, *args, **kwargs)
            return ExtractionFailure()

        monkeypatch.setattr(
            "agentlab.codex_provider.CodexProcessRunner.run",
            fail_result_extraction,
        )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    outcome = run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id=f"offline-{fault_mode}",
        output_path=output,
        confirm_live_codex=True,
        parent_environment=environment,
        preflight=lambda **_kwargs: preflight_result,
    )

    _assert_safe_failed_live_outputs(
        outcome,
        output,
        prompt_path,
        environment,
        expected_stage=expected_stage,
        expected_execution_stage=(
            CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
        ),
        expected_failure_kind=LiveFailureKind.EVIDENCE_ERROR,
        expected_process_started=True,
    )
    codex = outcome.artifact.codex
    assert codex.schema_version == "1.5"
    assert codex.runner_state is CodexRunnerState.STARTED
    assert codex.invocation_state is CodexInvocationState.PROCESS_STARTED
    assert codex.cleanup_state is CodexCleanupState.CLEARED
    assert codex.termination.process_group_cleared is True
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)
    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    assert b"synthetic handoff secret" not in persisted


def test_codex_evidence_construction_failure_publishes_only_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    popen = codex_provider_module.subprocess.Popen
    spawned_pids: list[int] = []

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_evidence(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic Evidence exception path secret")

    monkeypatch.setattr("agentlab.codex_provider.subprocess.Popen", track_spawn)
    monkeypatch.setattr(
        "agentlab.codex_provider.CodexProcessRunner._build_evidence",
        fail_evidence,
    )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    with pytest.raises(LiveDiagnosticCreatedError) as raised:
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="offline-evidence-construction",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )

    recording_path = spec_path.parent / "recordings" / "live.jsonl"
    diagnostic_path = _diagnostic_path(output)
    diagnostic = load_failure_diagnostic(diagnostic_path)
    assert (
        raised.value.diagnostic_code
        is LiveDiagnosticCode.CODEX_EVIDENCE_VALIDATION_FAILED
    )
    assert diagnostic.schema_version == "1.0"
    assert (
        diagnostic.diagnostic_code
        is LiveDiagnosticCode.CODEX_EVIDENCE_VALIDATION_FAILED
    )
    assert (
        diagnostic.failure_stage
        is DiagnosticFailureStage.CODEX_EVIDENCE_CONSTRUCTION
    )
    assert diagnostic.runner_state is DiagnosticRunnerState.STARTED
    assert diagnostic.invocation_state is DiagnosticInvocationState.PROCESS_STARTED
    assert diagnostic.cleanup_state is DiagnosticCleanupState.CLEARED
    assert (
        diagnostic.provider_activity_determined
        is ProviderActivityDetermination.DETERMINED
    )
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert diagnostic.paired_artifacts_published is False
    assert diagnostic.gate_executed is False
    assert not output.exists()
    assert not recording_path.exists()
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)

    persisted = diagnostic_path.read_bytes()
    assert set(json.loads(persisted)) == {
        "schema_version",
        "run_id",
        "experiment_id",
        "task_id",
        "failure_kind",
        "diagnostic_code",
        "failure_stage",
        "runner_state",
        "invocation_state",
        "cleanup_state",
        "workspace_lifecycle",
        "paired_artifacts_published",
        "gate_executed",
        "provider_activity_determined",
        "created_at",
    }
    forbidden = [
        prompt_path.read_bytes(),
        b"synthetic Evidence exception path secret",
        environment["CODEX_HOME"].encode(),
        environment["OPENAI_API_KEY"].encode(),
        environment["CODEX_API_KEY"].encode(),
        environment["AGENTLAB_PARENT_SECRET"].encode(),
        b"raw-event-secret",
        b"raw-stderr-secret",
        str(spawned_pids[0]).encode(),
        str(prompt_path).encode(),
    ]
    assert all(value not in persisted for value in forbidden)


@pytest.mark.parametrize(
    "interruption",
    [
        KeyboardInterrupt("synthetic interrupt"),
        SystemExit(73),
    ],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_post_spawn_base_exception_reaps_process_tree_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    live_code = (
        "subprocess=__import__('subprocess');signal=__import__('signal')\n"
        "child=subprocess.Popen("
        "[sys.executable,'-c','import signal; signal.pause()'])\n"
        "inspection.write_text(json.dumps({"
        "'parent':os.getpid(),'child':child.pid"
        "}),encoding='utf-8')\n"
        "signal.pause()"
    )
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    selector_factory = codex_provider_module.selectors.DefaultSelector
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    class InterruptingSelector:
        def __init__(self) -> None:
            self._selector = selector_factory()

        def register(self, *args: object, **kwargs: object):
            return self._selector.register(*args, **kwargs)

        def unregister(self, *args: object, **kwargs: object):
            return self._selector.unregister(*args, **kwargs)

        def get_map(self):
            return self._selector.get_map()

        def select(self, _timeout: float):
            deadline = time.monotonic() + 2
            while not inspection.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not inspection.exists():
                raise AssertionError("fake Provider did not start")
            raise interruption

        def close(self) -> None:
            self._selector.close()

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    monkeypatch.setattr(
        "agentlab.codex_provider.selectors.DefaultSelector",
        InterruptingSelector,
    )
    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    with pytest.raises(type(interruption)) as raised:
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="offline-interrupted-provider",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )

    assert raised.value is interruption
    process_ids = json.loads(inspection.read_text(encoding="utf-8"))
    for process_id in process_ids.values():
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)
    recording_path = spec_path.parent / "recordings" / "live.jsonl"
    assert not output.exists()
    assert not recording_path.exists()
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()


def test_failed_emergency_cleanup_is_not_recorded_as_cleared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_code = (
        "child=__import__('subprocess').Popen("
        "[sys.executable,'-c','import signal; signal.pause()'])\n"
        "inspection.write_text(str(child.pid),encoding='utf-8')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}),flush=True)"
    )
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    terminate = codex_provider_module.terminate_process_group
    popen = codex_provider_module.subprocess.Popen
    provider_pids: list[int] = []

    def track_provider_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        provider_pids.append(process.pid)
        return process

    def report_cleanup_failure(*args: object, **kwargs: object):
        termination = terminate(*args, **kwargs)
        assert termination.process_group_cleared
        return termination.model_copy(
            update={
                "process_group_cleared": False,
                "error": "synthetic fixed cleanup failure",
            }
        )

    def fail_evidence(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic Evidence construction secret")

    monkeypatch.setattr(
        "agentlab.codex_provider.terminate_process_group",
        report_cleanup_failure,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.subprocess.Popen",
        track_provider_spawn,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.CodexProcessRunner._build_evidence",
        fail_evidence,
    )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    with pytest.raises(LiveDiagnosticCreatedError):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="offline-cleanup-failure",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )
    child_pid = int(inspection.read_text(encoding="utf-8"))
    diagnostic = load_failure_diagnostic(_diagnostic_path(output))

    assert diagnostic.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert (
        diagnostic.failure_stage
        is DiagnosticFailureStage.CODEX_EVIDENCE_CONSTRUCTION
    )
    assert diagnostic.invocation_state is DiagnosticInvocationState.PROCESS_STARTED
    assert diagnostic.cleanup_state is DiagnosticCleanupState.FAILED
    assert diagnostic.gate_executed is False
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert not output.exists()
    assert not (spec_path.parent / "recordings" / "live.jsonl").exists()
    assert len(provider_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(provider_pids[0], 0)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_unbuildable_lifecycle_evidence_publishes_no_paired_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def fail_runner_entry(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic runner failure")

    def fail_strict_evidence(*_args: object, **_kwargs: object) -> NoReturn:
        raise ValidationError.from_exception_data("synthetic", [])

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.CodexProcessRunner.run",
        fail_runner_entry,
    )
    monkeypatch.setattr(
        "agentlab.live.lifecycle_failure_evidence",
        fail_strict_evidence,
    )

    with pytest.raises(
        LiveDiagnosticCreatedError,
        match="lifecycle_fallback_evidence_validation_failed",
    ) as raised:
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="offline-unbuildable-evidence",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )

    recording_path = spec_path.parent / "recordings" / "live.jsonl"
    diagnostic = load_failure_diagnostic(_diagnostic_path(output))
    assert (
        raised.value.diagnostic_code
        is LiveDiagnosticCode.LIFECYCLE_FALLBACK_EVIDENCE_VALIDATION_FAILED
    )
    assert (
        diagnostic.diagnostic_code
        is LiveDiagnosticCode.LIFECYCLE_FALLBACK_EVIDENCE_VALIDATION_FAILED
    )
    assert diagnostic.runner_state is DiagnosticRunnerState.STARTED
    assert diagnostic.invocation_state is DiagnosticInvocationState.NOT_ATTEMPTED
    assert diagnostic.cleanup_state is DiagnosticCleanupState.NOT_APPLICABLE
    assert diagnostic.gate_executed is False
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert not output.exists()
    assert not recording_path.exists()
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()


@pytest.mark.parametrize(
    (
        "runner_state",
        "invocation_state",
        "cleanup_state",
        "failure_stage",
        "strict_evidence_possible",
    ),
    [
        (
            CodexRunnerState.NOT_STARTED,
            CodexInvocationState.NOT_ATTEMPTED,
            CodexCleanupState.NOT_APPLICABLE,
            CodexFailureStage.PROVIDER_RUNNER_CONSTRUCTION,
            True,
        ),
        (
            CodexRunnerState.STARTED,
            CodexInvocationState.NOT_ATTEMPTED,
            CodexCleanupState.NOT_APPLICABLE,
            CodexFailureStage.JSONL_PARSER_INITIALIZATION,
            True,
        ),
        (
            CodexRunnerState.STARTED,
            CodexInvocationState.SPAWN_ATTEMPTED,
            CodexCleanupState.NOT_APPLICABLE,
            CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION,
            True,
        ),
        (
            CodexRunnerState.STARTED,
            CodexInvocationState.PROCESS_STARTED,
            CodexCleanupState.CLEARED,
            CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION,
            True,
        ),
        (
            CodexRunnerState.STARTED,
            CodexInvocationState.PROCESS_STARTED,
            None,
            CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION,
            False,
        ),
        (
            CodexRunnerState.STARTED,
            CodexInvocationState.PROCESS_STARTED,
            CodexCleanupState.FAILED,
            CodexFailureStage.CODEX_EVIDENCE_CONSTRUCTION,
            True,
        ),
    ],
)
def test_reachable_lifecycle_states_build_strict_evidence_or_truthful_diagnostic(
    runner_state: CodexRunnerState,
    invocation_state: CodexInvocationState,
    cleanup_state: CodexCleanupState | None,
    failure_stage: CodexFailureStage,
    strict_evidence_possible: bool,
) -> None:
    preflight_result = codex_provider_module.CodexPreflight(
        executable="not-persisted",
        cli_version=_SUPPORTED_CODEX_VERSION,
        cli_profile=CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2,
        checked_at=datetime.now(UTC),
        verified_flags=tuple(REQUIRED_CODEX_EXEC_FLAGS),
    )
    live = LiveSettings.model_validate(_base_spec()["live"])
    lifecycle = codex_provider_module.CodexLifecycleTracker(
        runner_state=runner_state,
        invocation_state=invocation_state,
        cleanup_state=cleanup_state,
        failure_stage=failure_stage,
    )
    if cleanup_state is CodexCleanupState.FAILED:
        lifecycle.termination = TerminationEvidence(
            reason=TerminationReason.EMERGENCY_CLEANUP,
            sigterm_sent=True,
            sigkill_sent=True,
            process_group_cleared=False,
            error="fixed cleanup failure",
        )

    if strict_evidence_possible:
        evidence = codex_provider_module.lifecycle_failure_evidence(
            preflight_result,
            live=live,
            lifecycle=lifecycle,
        )
        assert evidence.schema_version == "1.5"
        assert evidence.runner_state is runner_state
        assert evidence.invocation_state is invocation_state
        assert evidence.cleanup_state is cleanup_state
    else:
        with pytest.raises(RuntimeError, match="cleanup state was not observed"):
            codex_provider_module.lifecycle_failure_evidence(
                preflight_result,
                live=live,
                lifecycle=lifecycle,
            )
        diagnostic = live_module._diagnostic_from_lifecycle(
            run_id="lifecycle-table-run",
            experiment_id="lifecycle-table-experiment",
            task_id="lifecycle-table-task",
            diagnostic_code=(
                LiveDiagnosticCode.LIFECYCLE_FALLBACK_EVIDENCE_VALIDATION_FAILED
            ),
            lifecycle=lifecycle,
            workspace_lifecycle=WorkspaceLifecycle.REMOVED,
            gate_executed=False,
        )
        assert diagnostic.invocation_state is DiagnosticInvocationState.PROCESS_STARTED
        assert diagnostic.cleanup_state is DiagnosticCleanupState.UNKNOWN
        assert (
            diagnostic.provider_activity_determined
            is ProviderActivityDetermination.UNKNOWN
        )
        assert diagnostic.failure_kind is LiveFailureKind.EVIDENCE_ERROR


def test_missing_lifecycle_observation_is_unknown_not_false() -> None:
    diagnostic = live_module._diagnostic_from_lifecycle(
        run_id="unknown-lifecycle-run",
        experiment_id="unknown-lifecycle-experiment",
        task_id="unknown-lifecycle-task",
        diagnostic_code=LiveDiagnosticCode.CODEX_EVIDENCE_VALIDATION_FAILED,
        lifecycle=None,
        workspace_lifecycle=WorkspaceLifecycle.NOT_CREATED,
        gate_executed=False,
    )

    assert diagnostic.runner_state is DiagnosticRunnerState.UNKNOWN
    assert diagnostic.invocation_state is DiagnosticInvocationState.UNKNOWN
    assert diagnostic.cleanup_state is DiagnosticCleanupState.UNKNOWN
    assert diagnostic.failure_stage is DiagnosticFailureStage.UNKNOWN
    assert (
        diagnostic.provider_activity_determined
        is ProviderActivityDetermination.UNKNOWN
    )


@pytest.mark.parametrize(
    ("fault_target", "expected_code"),
    [
        (
            "recording",
            LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED,
        ),
        (
            "artifact",
            LiveDiagnosticCode.LIVE_ARTIFACT_CONSTRUCTION_FAILED,
        ),
    ],
)
def test_strict_paired_construction_faults_have_distinct_diagnostic_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
    expected_code: LiveDiagnosticCode,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_home),
        "AGENTLAB_PARENT_SECRET": "diagnostic-parent-secret",
    }

    def fail_construction(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic strict construction exception secret")

    if fault_target == "recording":
        monkeypatch.setattr(
            "agentlab.live.live_recording_jsonl_bytes",
            fail_construction,
        )
    else:
        monkeypatch.setattr("agentlab.live.LiveRunArtifact", fail_construction)
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        lambda *_args, **_kwargs: pytest.fail("quality Gates must not run"),
    )

    with pytest.raises(LiveDiagnosticCreatedError) as raised:
        _run(spec_path, output, environment)

    diagnostic_path = _diagnostic_path(output)
    diagnostic = load_failure_diagnostic(diagnostic_path)
    recording_path = spec_path.parent / "recordings" / "live.jsonl"
    assert raised.value.diagnostic_code is expected_code
    assert diagnostic.diagnostic_code is expected_code
    assert diagnostic.failure_stage is DiagnosticFailureStage.PREFLIGHT
    assert diagnostic.runner_state is DiagnosticRunnerState.NOT_STARTED
    assert diagnostic.invocation_state is DiagnosticInvocationState.NOT_ATTEMPTED
    assert diagnostic.cleanup_state is DiagnosticCleanupState.NOT_APPLICABLE
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
    assert diagnostic.gate_executed is False
    assert not output.exists()
    assert not recording_path.exists()
    persisted = diagnostic_path.read_bytes()
    assert b"synthetic strict construction exception secret" not in persisted
    assert b"diagnostic-parent-secret" not in persisted
    assert str(tmp_path).encode() not in persisted


@pytest.mark.parametrize(
    ("fault_target", "expected_code"),
    [
        (
            "recording",
            LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED,
        ),
        (
            "artifact",
            LiveDiagnosticCode.LIVE_ARTIFACT_CONSTRUCTION_FAILED,
        ),
    ],
)
def test_post_gate_paired_construction_failure_records_gate_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
    expected_code: LiveDiagnosticCode,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def fail_construction(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic post-Gate construction exception secret")

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    if fault_target == "recording":
        monkeypatch.setattr(
            "agentlab.live.live_recording_jsonl_bytes",
            fail_construction,
        )
    else:
        monkeypatch.setattr("agentlab.live.LiveRunArtifact", fail_construction)

    with pytest.raises(LiveDiagnosticCreatedError):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id=f"offline-post-gate-{fault_target}",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )

    diagnostic_path = _diagnostic_path(output)
    diagnostic = load_failure_diagnostic(diagnostic_path)
    assert diagnostic.diagnostic_code is expected_code
    assert diagnostic.gate_executed is True
    assert diagnostic.paired_artifacts_published is False
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert not output.exists()
    assert not (spec_path.parent / "recordings" / "live.jsonl").exists()
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()
    persisted = diagnostic_path.read_bytes()
    forbidden = [
        prompt_path.read_bytes(),
        b"synthetic post-Gate construction exception secret",
        environment["CODEX_HOME"].encode(),
        environment["OPENAI_API_KEY"].encode(),
        environment["CODEX_API_KEY"].encode(),
        environment["AGENTLAB_PARENT_SECRET"].encode(),
        b"raw-event-secret",
        b"raw-stderr-secret",
        str(tmp_path).encode(),
    ]
    assert all(value not in persisted for value in forbidden)


def test_gate_invocation_exception_does_not_reset_diagnostic_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    temporary_roots: list[Path] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def fail_gate_invocation(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic Gate invocation exception secret")

    def fail_recording(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic paired construction exception secret")

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    monkeypatch.setattr(
        "agentlab.gates.LocalCommandRunner.run",
        fail_gate_invocation,
    )
    monkeypatch.setattr(
        "agentlab.live.live_recording_jsonl_bytes",
        fail_recording,
    )

    with pytest.raises(LiveDiagnosticCreatedError):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="offline-gate-invocation-exception",
            output_path=output,
            confirm_live_codex=True,
            parent_environment=environment,
            preflight=lambda **_kwargs: preflight_result,
        )

    diagnostic_path = _diagnostic_path(output)
    diagnostic = load_failure_diagnostic(diagnostic_path)
    assert (
        diagnostic.diagnostic_code
        is LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED
    )
    assert diagnostic.gate_executed is True
    assert diagnostic.paired_artifacts_published is False
    assert diagnostic.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert not output.exists()
    assert not (spec_path.parent / "recordings" / "live.jsonl").exists()
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()
    persisted = diagnostic_path.read_bytes()
    forbidden = [
        prompt_path.read_bytes(),
        b"synthetic Gate invocation exception secret",
        b"synthetic paired construction exception secret",
        environment["CODEX_HOME"].encode(),
        environment["OPENAI_API_KEY"].encode(),
        environment["CODEX_API_KEY"].encode(),
        environment["AGENTLAB_PARENT_SECRET"].encode(),
        str(tmp_path).encode(),
    ]
    assert all(value not in persisted for value in forbidden)


def test_paired_publication_failure_creates_standalone_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_home),
    }

    def fail_publication(**_kwargs: object) -> NoReturn:
        raise LiveCodexError("synthetic paired publication exception secret")

    monkeypatch.setattr("agentlab.live.write_live_outputs", fail_publication)

    with pytest.raises(LiveDiagnosticCreatedError) as raised:
        _run(spec_path, output, environment)

    diagnostic = load_failure_diagnostic(_diagnostic_path(output))
    assert (
        raised.value.diagnostic_code
        is LiveDiagnosticCode.PAIRED_OUTPUT_PUBLICATION_FAILED
    )
    assert (
        diagnostic.diagnostic_code
        is LiveDiagnosticCode.PAIRED_OUTPUT_PUBLICATION_FAILED
    )
    assert diagnostic.paired_artifacts_published is False
    assert not output.exists()
    assert not (spec_path.parent / "recordings" / "live.jsonl").exists()
    assert (
        b"synthetic paired publication exception secret"
        not in _diagnostic_path(output).read_bytes()
    )


def test_spec_can_select_future_diagnostic_output_without_changing_paired_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["live"]["diagnostic_to"] = "diagnostics/selected.json"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_home),
    }

    def fail_recording(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic selected diagnostic failure")

    monkeypatch.setattr(
        "agentlab.live.live_recording_jsonl_bytes",
        fail_recording,
    )

    with pytest.raises(LiveDiagnosticCreatedError):
        _run(spec_path, output, environment)

    selected = spec_path.parent / "diagnostics" / "selected.json"
    assert load_failure_diagnostic(selected).diagnostic_code is (
        LiveDiagnosticCode.RECORDING_CONSTRUCTION_FAILED
    )
    assert not _diagnostic_path(output).exists()


@pytest.mark.parametrize(
    ("fault_mode", "expected_stage"),
    [
        (
            "selector_initialization",
            CodexFailureStage.PROVIDER_PIPE_SELECTOR_INITIALIZATION,
        ),
        ("process_collection", CodexFailureStage.PROVIDER_PROCESS_COLLECTION),
    ],
)
def test_post_spawn_faults_persist_safe_stage_and_reap_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_mode: str,
    expected_stage: CodexFailureStage,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="import signal; signal.pause()",
    )
    preflight_result = codex_provider_module.preflight_codex(
        parent_environment=environment
    )
    prepare_workspace = live_module.prepare_disposable_workspace
    popen = codex_provider_module.subprocess.Popen
    selector_factory = codex_provider_module.selectors.DefaultSelector
    temporary_roots: list[Path] = []
    spawned_pids: list[int] = []

    def track_workspace(*args: object, **kwargs: object):
        workspace = prepare_workspace(*args, **kwargs)
        temporary_roots.append(workspace.temporary_root)
        return workspace

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_selector_initialization() -> NoReturn:
        raise RuntimeError("synthetic selector failure must not be persisted")

    class FailingCollectionSelector:
        def __init__(self) -> None:
            self._selector = selector_factory()

        def register(self, *args: object, **kwargs: object):
            return self._selector.register(*args, **kwargs)

        def unregister(self, *args: object, **kwargs: object):
            return self._selector.unregister(*args, **kwargs)

        def get_map(self):
            return self._selector.get_map()

        def select(self, _timeout: float):
            raise RuntimeError("synthetic collection failure must not be persisted")

        def close(self) -> None:
            self._selector.close()

    def reject_gate_execution(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("quality Gates must not run after Provider failure")

    monkeypatch.setattr(
        "agentlab.live.prepare_disposable_workspace",
        track_workspace,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.subprocess.Popen",
        track_spawn,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.selectors.DefaultSelector",
        (
            fail_selector_initialization
            if fault_mode == "selector_initialization"
            else FailingCollectionSelector
        ),
    )
    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        reject_gate_execution,
    )

    outcome = run_live_codex(
        spec_path,
        task_id="task-1",
        repetition_index=0,
        run_id=f"offline-{expected_stage.value}",
        output_path=output,
        confirm_live_codex=True,
        parent_environment=environment,
        preflight=lambda **_kwargs: preflight_result,
    )

    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)
    assert artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert artifact.codex.failure_stage is expected_stage
    assert (
        artifact.codex.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )
    assert artifact.codex.process_started is True
    assert artifact.codex.schema_version == "1.5"
    assert artifact.codex.runner_state is CodexRunnerState.STARTED
    assert (
        artifact.codex.invocation_state is CodexInvocationState.PROCESS_STARTED
    )
    assert artifact.codex.cleanup_state is CodexCleanupState.CLEARED
    assert artifact.codex.approval_policy == "never"
    assert artifact.codex.approval_basis == "explicit_config_never"
    assert artifact.codex.termination.process_group_cleared is True
    assert artifact.gate_commands == []
    assert artifact.metrics is None
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED
    assert artifact.recording_sha256 == hashlib.sha256(
        outcome.recording_path.read_bytes()
    ).hexdigest()
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert len(outcome.recording_path.read_bytes().splitlines()) == 2
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()

    persisted = output.read_bytes() + outcome.recording_path.read_bytes()
    forbidden_values = [
        prompt_path.read_bytes(),
        environment["CODEX_HOME"].encode(),
        environment["OPENAI_API_KEY"].encode(),
        environment["CODEX_API_KEY"].encode(),
        environment["AGENTLAB_PARENT_SECRET"].encode(),
    ]
    assert all(value not in persisted for value in forbidden_values)
    assert b"synthetic selector failure must not be persisted" not in persisted
    assert b"synthetic collection failure must not be persisted" not in persisted


def test_workspace_error_after_provider_does_not_replace_codex_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_gates(*_args: object, **_kwargs: object) -> NoReturn:
        raise WorkspaceError("synthetic post-Provider workspace failure")

    monkeypatch.setattr(
        "agentlab.live.execute_quality_gates_in_workspace",
        fail_gates,
    )
    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.codex.status.value == "succeeded"
    assert outcome.artifact.codex.process_started is True
    assert (
        outcome.artifact.codex.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )
    assert outcome.artifact.codex.approval_policy == "never"
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.REMOVED


@pytest.mark.parametrize(
    "configured_codex_home",
    [None, "relative/codex-home", "/definitely/missing/agentlab-codex-home"],
)
def test_live_requires_explicit_existing_absolute_codex_home(
    tmp_path: Path,
    configured_codex_home: str | None,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    if configured_codex_home is None:
        environment.pop("CODEX_HOME")
    else:
        environment["CODEX_HOME"] = configured_codex_home

    with pytest.raises(LiveCodexError, match="CODEX_HOME"):
        _run(spec_path, output, environment)

    assert not output.exists()
    assert not (spec_path.parent / "recordings/live.jsonl").exists()


def test_binary_provider_change_has_null_metrics(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code=_success_code(binary=True),
    )

    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.metrics is None
    assert outcome.artifact.diff.binary_files == ["binary.dat"]


def test_invalid_utf8_provider_change_has_null_metrics(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    code = _success_code().replace(
        "pathlib.Path('task.txt').write_text('status=COMPLETE\\n',encoding='utf-8')",
        "pathlib.Path('invalid.bin').write_bytes(b'\\xff');"
        "pathlib.Path('task.txt').write_text('status=COMPLETE\\n',encoding='utf-8')",
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=code)

    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert outcome.artifact.metrics is None
    assert outcome.artifact.diff.binary_files == ["invalid.bin"]


def test_live_diff_paths_are_stably_sorted(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    code = (
        "pathlib.Path('z-last.txt').write_text('z\\n',encoding='utf-8')\n"
        "pathlib.Path('a-first.txt').write_text('a\\n',encoding='utf-8')\n"
        f"{_success_code()}"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=code)

    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.diff.changed_files == [
        "a-first.txt",
        "task.txt",
        "z-last.txt",
    ]


def test_gate_harness_failure_is_separate_and_has_null_metrics(tmp_path: Path) -> None:
    quality_gate = {
        "acceptance": [["phase3-gate-that-does-not-exist"]],
        "regression": [],
        "lint": [],
        "typecheck": [],
    }
    spec_path, _fixture, _prompt, output = _write_case(
        tmp_path,
        quality_gate=quality_gate,
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.GATE_HARNESS_ERROR
    assert outcome.artifact.metrics is None
    assert len(outcome.artifact.gate_commands) == 1


def test_usage_absence_is_not_coerced_to_zero(tmp_path: Path) -> None:
    code = _success_code().replace(
        "'turn.completed','usage':{'input_tokens':11,'cached_input_tokens':2,"
        "'output_tokens':5,'reasoning_output_tokens':1}",
        "'turn.completed'",
    )
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=code)

    outcome = _run(spec_path, output, environment)

    assert outcome.artifact.metrics is not None
    usage = outcome.artifact.metrics.usage_metrics
    assert usage is not None
    assert usage.source is not None
    assert usage.source.value == "not_available"
    assert usage.input_tokens is None
    assert usage.output_tokens is None


def test_preflight_failure_is_saved_without_creating_workspace(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_home),
    }

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.PROVIDER_UNAVAILABLE
    assert outcome.artifact.workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
    assert outcome.artifact.codex.cli_profile is CodexCliProfile.NOT_SELECTED
    assert (
        outcome.artifact.codex.execution_stage
        is CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
    )
    assert outcome.artifact.codex.approval_policy is None
    assert outcome.artifact.codex.approval_basis is None
    assert isinstance(recording.failed, LiveRunFailedEvent)


def test_preflight_process_cleanup_failure_is_saved_with_termination_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="raise SystemExit(99)",
        version_code="import signal;signal.pause()",
    )
    spawned_pids: list[int] = []
    popen = runner_module.subprocess.Popen
    terminate_process_group = runner_module._terminate_process_group

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_selector_creation() -> NoReturn:
        raise RuntimeError("synthetic unexpected preflight collection failure")

    def report_cleanup_failure(*args: object, **kwargs: object):
        termination = terminate_process_group(*args, **kwargs)
        assert termination.process_group_cleared
        return termination.model_copy(
            update={
                "process_group_cleared": False,
                "error": "synthetic preflight process cleanup failure",
            }
        )

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", track_spawn)
    monkeypatch.setattr(
        "agentlab.runner.selectors.DefaultSelector",
        fail_selector_creation,
    )
    monkeypatch.setattr(
        "agentlab.runner._terminate_process_group",
        report_cleanup_failure,
    )

    outcome = _run(spec_path, output, environment)
    artifact = load_live_artifact(output)
    recording = load_replay_recording(outcome.recording_path)

    assert output.is_file()
    assert outcome.recording_path.is_file()
    assert artifact == outcome.artifact
    assert artifact.overall_status is LiveOverallStatus.HARNESS_ERROR
    assert artifact.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert artifact.workspace_lifecycle is WorkspaceLifecycle.NOT_CREATED
    assert (
        artifact.codex.execution_stage
        is CodexExecutionStage.PREFLIGHT_NOT_COMPLETED
    )
    assert artifact.codex.process_started is False
    assert artifact.codex.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert artifact.codex.termination.process_group_cleared is False
    assert (
        artifact.codex.termination.error
        == "synthetic preflight process cleanup failure"
    )
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert recording.failed.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert recording.failed.codex.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert recording.failed.codex.termination.process_group_cleared is False
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_live_outputs_require_force_before_preflight_and_replace_explicitly(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    first = _run(spec_path, output, environment)

    with pytest.raises(LiveCodexError, match="already exists"):
        _run(
            spec_path,
            output,
            {"PATH": str(tmp_path / "must-not-be-probed")},
        )

    replaced = _run(spec_path, output, environment, force=True)

    assert load_live_artifact(output) == replaced.artifact
    assert load_replay_recording(replaced.recording_path).completed is not None
    assert first.recording_path == replaced.recording_path


def test_confirmation_is_checked_before_preflight_or_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)

    def blocked_preflight(**_kwargs: object) -> NoReturn:
        raise AssertionError("preflight must not run without confirmation")

    monkeypatch.setattr("agentlab.live.preflight_codex", blocked_preflight)
    with pytest.raises(LiveCodexError, match="confirm-live-codex"):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="not-run",
            output_path=output,
            confirm_live_codex=False,
        )

    assert not output.exists()
    assert not (spec_path.parent / "recordings").exists()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda spec: spec.update(execution_mode="replay"), "execution_mode"),
        (lambda spec: spec.update(provider="antigravity"), "provider"),
        (
            lambda spec: spec.update(
                workflow="staged",
                control="staged",
                treatments=["one_shot"],
            ),
            "workflow",
        ),
        (lambda spec: spec.pop("runner"), "Runner"),
    ],
)
def test_live_request_rejects_wrong_phase3_conditions(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    mutate(spec)
    if spec.get("execution_mode") == "replay":
        spec["replay"] = {"recording_path": "recordings/unused.jsonl"}
        spec["live"] = None
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")

    with pytest.raises((LiveCodexError, SpecLoadError), match=message):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="invalid",
            output_path=output,
            confirm_live_codex=True,
            parent_environment={"PATH": ""},
        )


@pytest.mark.parametrize("prompt_content", [b"", b" \n", b"\x00secret", b"\xff"])
def test_invalid_prompt_is_rejected_before_preflight(
    tmp_path: Path,
    prompt_content: bytes,
) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    prompt_path.write_bytes(prompt_content)

    with pytest.raises(LiveCodexError, match="Prompt"):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="invalid-prompt",
            output_path=output,
            confirm_live_codex=True,
            parent_environment={"PATH": ""},
        )


def test_prompt_symlink_is_rejected_before_preflight(tmp_path: Path) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    target = prompt_path.with_name("target.md")
    prompt_path.rename(target)
    prompt_path.symlink_to(target)

    with pytest.raises(LiveCodexError, match="symlink"):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="symlink-prompt",
            output_path=output,
            confirm_live_codex=True,
            parent_environment={"PATH": ""},
        )


def test_prompt_byte_limit_is_rejected_before_preflight(tmp_path: Path) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["live"]["max_prompt_bytes"] = 3
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    prompt_path.write_bytes(b"four")

    with pytest.raises(LiveCodexError, match="max_prompt_bytes"):
        run_live_codex(
            spec_path,
            task_id="task-1",
            repetition_index=0,
            run_id="oversized-prompt",
            output_path=output,
            confirm_live_codex=True,
            parent_environment={"PATH": ""},
        )


@pytest.mark.parametrize(
    ("task_id", "repetition_index", "message"),
    [
        ("unknown-task", 0, "task_id"),
        ("task-1", -1, "repetition_index"),
        ("task-1", 1, "repetition_index"),
    ],
)
def test_live_request_rejects_unknown_task_or_repetition(
    tmp_path: Path,
    task_id: str,
    repetition_index: int,
    message: str,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)

    with pytest.raises(LiveCodexError, match=message):
        run_live_codex(
            spec_path,
            task_id=task_id,
            repetition_index=repetition_index,
            run_id="invalid-condition",
            output_path=output,
            confirm_live_codex=True,
            parent_environment={"PATH": ""},
        )


@pytest.mark.parametrize("protected", ["spec", "prompt", "fixture"])
def test_force_cannot_overwrite_live_inputs(
    tmp_path: Path,
    protected: str,
) -> None:
    spec_path, fixture, prompt_path, _output = _write_case(tmp_path)
    target = {
        "spec": spec_path,
        "prompt": prompt_path,
        "fixture": fixture / "task.txt",
    }[protected]
    original = target.read_bytes()
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    with pytest.raises(
        LiveCodexError,
        match=r"must not overwrite|alias|inside the Fixture",
    ):
        _run(spec_path, target, environment, force=True)

    assert target.read_bytes() == original


def test_force_cannot_overwrite_prompt_through_hardlink(tmp_path: Path) -> None:
    spec_path, _fixture, prompt_path, _output = _write_case(tmp_path)
    alias = tmp_path / "prompt-hardlink.json"
    alias.hardlink_to(prompt_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    with pytest.raises(LiveCodexError, match="Prompt"):
        _run(spec_path, alias, environment, force=True)


def test_force_cannot_overwrite_prompt_with_recording_hardlink(tmp_path: Path) -> None:
    spec_path, _fixture, prompt_path, output = _write_case(tmp_path)
    recording_path = spec_path.parent / "recordings" / "live.jsonl"
    recording_path.parent.mkdir()
    recording_path.hardlink_to(prompt_path)
    original = prompt_path.read_bytes()
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    with pytest.raises(LiveCodexError, match="Prompt"):
        _run(spec_path, output, environment, force=True)

    assert prompt_path.read_bytes() == original


def test_live_artifact_loader_rejects_unknown_field_and_type_coercion(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    _run(spec_path, output, environment)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["unknown"] = True
    output.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LiveArtifactLoadError):
        load_live_artifact(output)

    del payload["unknown"]
    payload["repetition_index"] = "0"
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveArtifactLoadError):
        load_live_artifact(output)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("started", "prompt_redacted", "true"),
        ("started", "prompt_redacted", 1),
        ("started", "repetition_index", "0"),
        ("started", "occurred_at", "2026-07-26T12:00:00"),
        ("started", "occurred_at", "2026-07-26T12:00:00+09:00"),
        ("terminal", "future_field", True),
    ],
)
def test_live_recording_loader_is_strict(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    target = events[0] if section == "started" else events[1]
    target[field] = value
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError):
        load_replay_recording(outcome.recording_path)


def test_live_recording_rejects_cross_event_provider_mismatch(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    events[1]["codex"]["requested_model"] = "different-fixed-model"
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="model mismatch"):
        load_replay_recording(outcome.recording_path)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("evaluation", "acceptance", "passed_count"), 0),
        (("evaluation", "changed_files"), []),
        (("evaluation", "evaluation_duration_ms"), 999_999),
    ],
)
def test_live_recording_rejects_metrics_evaluation_summary_mismatch(
    tmp_path: Path,
    field_path: tuple[str, ...],
    value: object,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    target: Any = events[1]
    for component in field_path[:-1]:
        target = target[component]
    target[field_path[-1]] = value
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="evaluation"):
        load_replay_recording(outcome.recording_path)


def test_live_recording_rejects_success_without_acceptance_gate(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    terminal = events[1]
    terminal["evaluation"]["acceptance"] = {
        "command_count": 0,
        "passed_count": 0,
        "failed_count": 0,
    }
    terminal["metrics"]["acceptance_tests_passed"] = 0
    terminal["metrics"]["acceptance_tests_total"] = 0
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="acceptance Gate"):
        load_replay_recording(outcome.recording_path)


def test_failed_live_recording_rejects_non_failure_kind(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    live_code = (
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.failed','error':"
        "{'message':'synthetic unknown failure'}}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    events[1]["failure_kind"] = "none"
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="non-quality failure"):
        load_replay_recording(outcome.recording_path)


def test_paired_writer_rolls_back_recording_when_evidence_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    recording_bytes = outcome.recording_path.read_bytes()
    artifact = outcome.artifact
    outcome.recording_path.unlink()
    output.unlink()
    original_link = os.link
    calls = 0

    def fail_second_link(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic Evidence publish failure")
        original_link(source, destination)

    monkeypatch.setattr("agentlab.live.os.link", fail_second_link)
    with pytest.raises(LiveCodexError, match="publish"):
        write_live_outputs(
            recording_bytes=recording_bytes,
            artifact=artifact,
            recording_path=outcome.recording_path,
            output_path=output,
            force=False,
        )

    assert not outcome.recording_path.exists()
    assert not output.exists()
    assert list(outcome.recording_path.parent.iterdir()) == []


def test_failure_diagnostic_writer_is_atomic_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "diagnostics" / "failure.json"
    diagnostic = _sample_diagnostic()

    write_failure_diagnostic(diagnostic, path)
    original = path.read_bytes()

    assert load_failure_diagnostic(path) == diagnostic
    assert list(path.parent.iterdir()) == [path]
    with pytest.raises(LiveDiagnosticPublicationError) as raised:
        write_failure_diagnostic(diagnostic, path)
    assert (
        raised.value.diagnostic_code
        is LiveDiagnosticCode.DIAGNOSTIC_PUBLICATION_FAILED
    )
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_existing_failure_diagnostic_blocks_live_even_with_force(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, inspection = _fake_codex(tmp_path, live_code=_success_code())
    path = _diagnostic_path(output)
    write_failure_diagnostic(_sample_diagnostic(), path)
    original = path.read_bytes()

    with pytest.raises(LiveCodexError, match="Diagnostic output already exists"):
        _run(spec_path, output, environment, force=True)

    assert path.read_bytes() == original
    assert not output.exists()
    assert not (spec_path.parent / "recordings" / "live.jsonl").exists()
    assert not inspection.exists()


@pytest.mark.parametrize("fault", ["link", "fsync"])
def test_failure_diagnostic_publication_failure_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    path = tmp_path / "diagnostics" / "failure.json"
    diagnostic = _sample_diagnostic()

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("synthetic diagnostic publication exception secret")

    monkeypatch.setattr(
        "agentlab.live.os.link" if fault == "link" else "agentlab.live.os.fsync",
        fail,
    )

    with pytest.raises(LiveDiagnosticPublicationError) as raised:
        write_failure_diagnostic(diagnostic, path)

    assert (
        raised.value.diagnostic_code
        is LiveDiagnosticCode.DIAGNOSTIC_PUBLICATION_FAILED
    )
    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_force_paired_writer_restores_original_recording_on_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    replacement_recording = outcome.recording_path.read_bytes()
    artifact = outcome.artifact
    original_recording = b"original recording\n"
    original_evidence = b"original evidence\n"
    outcome.recording_path.write_bytes(original_recording)
    output.write_bytes(original_evidence)
    original_replace = os.replace
    calls = 0

    def fail_evidence_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic Evidence replace failure")
        original_replace(source, destination)

    monkeypatch.setattr("agentlab.live.os.replace", fail_evidence_replace)
    with pytest.raises(LiveCodexError, match="publish"):
        write_live_outputs(
            recording_bytes=replacement_recording,
            artifact=artifact,
            recording_path=outcome.recording_path,
            output_path=output,
            force=True,
        )

    assert outcome.recording_path.read_bytes() == original_recording
    assert output.read_bytes() == original_evidence
    assert list(outcome.recording_path.parent.iterdir()) == [outcome.recording_path]
    assert list(output.parent.iterdir()) == [output]


def test_paired_writer_fsync_failure_leaves_no_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    recording_bytes = outcome.recording_path.read_bytes()
    artifact = outcome.artifact
    outcome.recording_path.unlink()
    output.unlink()

    def fail_fsync(_file_descriptor: int) -> NoReturn:
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr("agentlab.live.os.fsync", fail_fsync)
    with pytest.raises(LiveCodexError, match="stage"):
        write_live_outputs(
            recording_bytes=recording_bytes,
            artifact=artifact,
            recording_path=outcome.recording_path,
            output_path=output,
            force=False,
        )

    assert list(outcome.recording_path.parent.iterdir()) == []
    assert list(output.parent.iterdir()) == []


def test_force_backup_failure_preserves_existing_outputs_without_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    replacement_recording = outcome.recording_path.read_bytes()
    artifact = outcome.artifact
    original_recording = b"original recording\n"
    original_evidence = b"original evidence\n"
    outcome.recording_path.write_bytes(original_recording)
    output.write_bytes(original_evidence)

    def fail_backup_link(_source: Path, _destination: Path) -> NoReturn:
        raise OSError("synthetic backup link failure")

    monkeypatch.setattr("agentlab.live.os.link", fail_backup_link)
    with pytest.raises(LiveCodexError, match="publish"):
        write_live_outputs(
            recording_bytes=replacement_recording,
            artifact=artifact,
            recording_path=outcome.recording_path,
            output_path=output,
            force=True,
        )

    assert outcome.recording_path.read_bytes() == original_recording
    assert output.read_bytes() == original_evidence
    assert list(outcome.recording_path.parent.iterdir()) == [outcome.recording_path]
    assert list(output.parent.iterdir()) == [output]


def test_live_artifact_model_rejects_metrics_status_contradiction(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    artifact = _run(spec_path, output, environment).artifact
    assert artifact.metrics is not None
    invalid_metrics = artifact.metrics.model_copy(update={"quality_gate_pass": False})

    with pytest.raises(ValidationError):
        LiveRunArtifact.model_validate(
            artifact.model_copy(update={"metrics": invalid_metrics}).model_dump()
        )


def test_live_artifact_rejects_gate_harness_without_abnormal_gate(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    artifact = _run(spec_path, output, environment).artifact

    with pytest.raises(ValidationError, match="abnormal Gate"):
        LiveRunArtifact.model_validate(
            artifact.model_copy(
                update={
                    "overall_status": LiveOverallStatus.HARNESS_ERROR,
                    "failure_kind": LiveFailureKind.GATE_HARNESS_ERROR,
                    "metrics": None,
                }
            ).model_dump()
        )


def test_live_artifact_rejects_non_provider_codex_usage_source(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["usage_metrics"]["source"] = "estimated"

    with pytest.raises(ValidationError, match="Usage source"):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_prompt_stdin_byte_total_mismatch(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["stdin_bytes_total"] = payload["prompt_bytes"] + 1
    payload["codex"]["stdin_bytes_written"] = payload["prompt_bytes"] + 1

    with pytest.raises(ValidationError, match="Prompt byte count"):
        LiveRunArtifact.model_validate(payload)


def test_live_recording_rejects_prompt_stdin_byte_total_mismatch(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    outcome = _run(spec_path, output, environment)
    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    terminal = events[1]
    terminal["codex"]["stdin_bytes_total"] += 1
    terminal["codex"]["stdin_bytes_written"] += 1
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="Prompt byte count"):
        load_replay_recording(outcome.recording_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_started_count", 0),
        ("turn_started_count", 0),
        ("terminal_event", "turn_failed"),
        ("turn_completed_count", 0),
    ],
)
def test_live_artifact_rejects_forged_codex_lifecycle(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"][field] = value

    with pytest.raises(ValidationError):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_unknown_item_type_key(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["item_type_counts"] = {"secret_vendor_item": 1}

    with pytest.raises(ValidationError, match="item_type_counts"):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_negative_item_type_count(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["item_type_counts"] = {"message": -1, "command": 2}

    with pytest.raises(ValidationError, match="greater than or equal"):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_profile_with_unallowlisted_cli_version(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["cli_version"] = "codex-cli 999.0.0"

    with pytest.raises(ValidationError, match="allowlisted CLI version"):
        LiveRunArtifact.model_validate(payload)


def test_legacy_codex_evidence_11_failure_without_stage_remains_loadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_provider_environment(_parent: Path, name: str) -> Path:
        assert name == "provider"
        raise OSError("synthetic Provider environment failure")

    monkeypatch.setattr(
        "agentlab.live._prepare_live_environment_root",
        fail_provider_environment,
    )
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["schema_version"] = "1.1"
    _remove_codex_15_fields(payload["codex"])
    payload["codex"].pop("failure_stage")
    payload["codex"].pop("runner_state")
    payload["codex"].pop("invocation_state")
    payload["codex"].pop("cleanup_state")

    legacy = LiveRunArtifact.model_validate(payload)

    assert legacy.codex.schema_version == "1.1"
    assert legacy.codex.failure_stage is None
    assert legacy.codex.failure_kind is LiveFailureKind.EVIDENCE_ERROR


def test_legacy_codex_evidence_12_failure_remains_loadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_provider_environment(_parent: Path, name: str) -> Path:
        assert name == "provider"
        raise OSError("synthetic Provider environment failure")

    monkeypatch.setattr(
        "agentlab.live._prepare_live_environment_root",
        fail_provider_environment,
    )
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["schema_version"] = "1.2"
    _remove_codex_15_fields(payload["codex"])
    payload["codex"].pop("runner_state")
    payload["codex"].pop("invocation_state")
    payload["codex"].pop("cleanup_state")

    legacy = LiveRunArtifact.model_validate(payload)

    assert legacy.codex.schema_version == "1.2"
    assert (
        legacy.codex.failure_stage
        is CodexFailureStage.PROVIDER_ENVIRONMENT_DIRECTORY_PREPARATION
    )
    assert legacy.codex.runner_state is None
    assert legacy.codex.invocation_state is None
    assert legacy.codex.cleanup_state is None


def test_legacy_codex_evidence_13_remains_strictly_loadable(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["schema_version"] = "1.3"
    _remove_codex_15_fields(payload["codex"])

    legacy = LiveRunArtifact.model_validate(payload)

    assert legacy.codex.schema_version == "1.3"
    assert legacy.codex.runner_state is CodexRunnerState.STARTED
    assert legacy.codex.invocation_state is CodexInvocationState.PROCESS_STARTED
    assert legacy.codex.cleanup_state is CodexCleanupState.CLEARED


def test_legacy_codex_evidence_14_remains_strictly_loadable(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["schema_version"] = "1.4"
    _remove_codex_15_fields(payload["codex"])

    legacy = LiveRunArtifact.model_validate(payload)

    assert legacy.codex.schema_version == "1.4"
    assert legacy.codex.stdin_write_state is None
    assert legacy.codex.provider_failure_hint is None


def test_codex_evidence_13_does_not_adopt_14_error_count_semantics(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    live_code = (
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'error','message':'synthetic unknown failure'}),flush=True)\n"
        "print(json.dumps({'type':'turn.failed','error':"
        "{'message':'synthetic unknown failure'}}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    assert payload["codex"]["schema_version"] == "1.5"
    payload["codex"]["schema_version"] = "1.3"
    _remove_codex_15_fields(payload["codex"])

    with pytest.raises(ValidationError, match="terminal_event"):
        LiveRunArtifact.model_validate(payload)


def test_codex_evidence_12_failure_requires_safe_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_provider_environment(_parent: Path, name: str) -> Path:
        assert name == "provider"
        raise OSError("synthetic Provider environment failure")

    monkeypatch.setattr(
        "agentlab.live._prepare_live_environment_root",
        fail_provider_environment,
    )
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"].pop("failure_stage")

    with pytest.raises(ValidationError, match="requires failure_stage"):
        LiveRunArtifact.model_validate(payload)


def test_codex_evidence_rejects_failure_stage_execution_contradiction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_provider_environment(_parent: Path, name: str) -> Path:
        assert name == "provider"
        raise OSError("synthetic Provider environment failure")

    monkeypatch.setattr(
        "agentlab.live._prepare_live_environment_root",
        fail_provider_environment,
    )
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["failure_stage"] = "provider_process_collection"
    payload["codex"]["runner_state"] = "started"
    payload["codex"]["invocation_state"] = "process_started"
    payload["codex"]["cleanup_state"] = "cleared"
    payload["codex"]["process_started"] = True

    with pytest.raises(ValidationError, match="execution_stage"):
        LiveRunArtifact.model_validate(payload)


@pytest.mark.parametrize(
    "verified_flags",
    [
        [],
        [*sorted(REQUIRED_CODEX_EXEC_FLAGS), "--synthetic-extra-flag"],
    ],
)
def test_live_artifact_rejects_selected_profile_without_exact_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_flags: list[str],
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_prepare(*_args: object, **_kwargs: object) -> NoReturn:
        raise WorkspaceError("synthetic preparation failure")

    monkeypatch.setattr("agentlab.live.prepare_disposable_workspace", fail_prepare)
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["verified_flags"] = verified_flags

    with pytest.raises(ValidationError, match="exactly its preflight flags"):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_approval_before_provider_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_prepare(*_args: object, **_kwargs: object) -> NoReturn:
        raise WorkspaceError("synthetic preparation failure")

    monkeypatch.setattr("agentlab.live.prepare_disposable_workspace", fail_prepare)
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["codex"]["approval_policy"] = "never"
    payload["codex"]["approval_basis"] = "explicit_config_never"

    with pytest.raises(ValidationError, match="absent before Provider invocation"):
        LiveRunArtifact.model_validate(payload)


def test_live_artifact_rejects_impossible_workspace_lifecycle(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["workspace_lifecycle"] = "not_created"
    payload["metrics"] = None

    with pytest.raises(ValidationError, match="requires a created Workspace"):
        LiveRunArtifact.model_validate(payload)


def test_artifact_and_recording_reject_invocation_without_created_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())

    def fail_prepare(*_args: object, **_kwargs: object) -> NoReturn:
        raise WorkspaceError("synthetic preparation failure")

    monkeypatch.setattr("agentlab.live.prepare_disposable_workspace", fail_prepare)
    outcome = _run(spec_path, output, environment)
    artifact_payload = outcome.artifact.model_dump(mode="json")
    artifact_payload["overall_status"] = "provider_error"
    artifact_payload["failure_kind"] = "provider_spawn_error"
    artifact_payload["codex"]["execution_stage"] = "provider_invocation_attempted"
    artifact_payload["codex"]["approval_policy"] = "never"
    artifact_payload["codex"]["approval_basis"] = "explicit_config_never"
    artifact_payload["codex"]["failure_kind"] = "provider_spawn_error"
    artifact_payload["codex"]["failure_stage"] = "provider_process_spawn"
    artifact_payload["codex"]["runner_state"] = "started"
    artifact_payload["codex"]["invocation_state"] = "spawn_attempted"
    artifact_payload["codex"]["cleanup_state"] = "not_applicable"

    with pytest.raises(ValidationError, match="requires a created Workspace"):
        LiveRunArtifact.model_validate(artifact_payload)

    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    terminal = events[1]
    terminal["failure_kind"] = "provider_spawn_error"
    terminal["codex"]["execution_stage"] = "provider_invocation_attempted"
    terminal["codex"]["approval_policy"] = "never"
    terminal["codex"]["approval_basis"] = "explicit_config_never"
    terminal["codex"]["failure_kind"] = "provider_spawn_error"
    terminal["codex"]["failure_stage"] = "provider_process_spawn"
    terminal["codex"]["runner_state"] = "started"
    terminal["codex"]["invocation_state"] = "spawn_attempted"
    terminal["codex"]["cleanup_state"] = "not_applicable"
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="requires a created Workspace"):
        load_replay_recording(outcome.recording_path)


def test_artifact_and_recording_reject_removed_workspace_before_preflight(
    tmp_path: Path,
) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    codex_home = tmp_path / "managed-auth"
    codex_home.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_home),
    }
    outcome = _run(spec_path, output, environment)
    artifact_payload = outcome.artifact.model_dump(mode="json")
    artifact_payload["workspace_lifecycle"] = "removed"

    with pytest.raises(ValidationError, match="requires a not_created Workspace"):
        LiveRunArtifact.model_validate(artifact_payload)

    events = [
        json.loads(line)
        for line in outcome.recording_path.read_text(encoding="utf-8").splitlines()
    ]
    events[1]["evaluation"]["workspace_lifecycle"] = "removed"
    outcome.recording_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RecordingLoadError, match="requires a not_created Workspace"):
        load_replay_recording(outcome.recording_path)
