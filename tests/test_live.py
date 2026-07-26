from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from pydantic import ValidationError

from agentlab.codex_provider import REQUIRED_CODEX_EXEC_FLAGS
from agentlab.live import (
    LiveArtifactLoadError,
    LiveCodexError,
    load_live_artifact,
    run_live_codex,
    write_live_outputs,
)
from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    CodexCliProfile,
    LiveFailureKind,
    LiveOverallStatus,
    LiveRunArtifact,
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


def _fake_codex(
    tmp_path: Path,
    *,
    live_code: str,
) -> tuple[dict[str, str], Path]:
    fake_directory = tmp_path / "fake-bin"
    fake_directory.mkdir(exist_ok=True)
    inspection = tmp_path / "provider-inspection.json"
    script = (
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        f"inspection=pathlib.Path({str(inspection)!r})\n"
        "if sys.argv[1:] == ['--version']:\n"
        f"    print({_SUPPORTED_CODEX_VERSION!r})\n"
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
    assert observed["prompt"].endswith(prompt_secret)
    assert observed["codex_home_present"] is True
    assert observed["openai_key"] is None
    assert observed["codex_key"] is None
    assert observed["parent_secret"] is None
    assert (fixture / "task.txt").read_text(encoding="utf-8") == "status=TODO\n"
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
        "print(json.dumps({'type':'turn.failed','error':'provider-raw-secret'}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)

    outcome = _run(spec_path, output, environment)
    recording = load_replay_recording(outcome.recording_path)

    assert outcome.artifact.overall_status is LiveOverallStatus.PROVIDER_ERROR
    assert outcome.artifact.failure_kind is LiveFailureKind.PROVIDER_TURN_FAILED
    assert outcome.artifact.gate_commands == []
    assert outcome.artifact.metrics is None
    assert isinstance(recording.failed, LiveRunFailedEvent)
    assert not gate_marker.exists()
    with pytest.raises(ReplayError, match="Metrics are absent"):
        replay_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        replay_spec["execution_mode"] = "replay"
        replay_spec["replay"] = {"recording_path": "recordings/live.jsonl"}
        replay_spec["live"] = None
        replay_path = spec_path.parent / "replay-failed.yaml"
        replay_path.write_text(yaml.safe_dump(replay_spec), encoding="utf-8")
        run_replay(replay_path, tmp_path / "must-not-exist.json")


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
    assert outcome.artifact.metrics is None
    assert isinstance(recording.failed, LiveRunFailedEvent)


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
    assert outcome.artifact.codex.approval_policy is None
    assert outcome.artifact.codex.approval_basis is None
    assert isinstance(recording.failed, LiveRunFailedEvent)


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
        "print(json.dumps({'type':'turn.failed'}),flush=True)"
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


def test_live_artifact_rejects_impossible_workspace_lifecycle(tmp_path: Path) -> None:
    spec_path, _fixture, _prompt, output = _write_case(tmp_path)
    environment, _inspection = _fake_codex(tmp_path, live_code=_success_code())
    payload = _run(spec_path, output, environment).artifact.model_dump(mode="json")
    payload["workspace_lifecycle"] = "not_created"
    payload["metrics"] = None

    with pytest.raises(ValidationError, match="not_created"):
        LiveRunArtifact.model_validate(payload)
