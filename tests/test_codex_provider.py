from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import NoReturn

import pytest

import agentlab.codex_provider as codex_provider_module
import agentlab.runner as runner_module
from agentlab.codex_provider import (
    REQUIRED_CODEX_EXEC_FLAGS,
    CodexJsonlParser,
    CodexPreflightError,
    CodexProcessRunner,
    CodexProtocolError,
    build_codex_argv,
    preflight_codex,
    resolve_codex_home,
)
from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    CodexCliProfile,
    CodexExecutionStage,
    CodexItemType,
    CodexTerminalEvent,
    LiveFailureKind,
    LiveSettings,
    ProviderExecutionStatus,
    RunnerSettings,
    UsageMetricSource,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Codex Provider runner is POSIX-only")

_SUPPORTED_CODEX_VERSION = next(iter(CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS))


def _live_settings(**updates: object) -> LiveSettings:
    values: dict[str, object] = {
        "record_to": "recordings/live.jsonl",
        "prompt_path": "prompts/task.md",
        "model": "gpt-test-fixed",
        "reasoning_effort": "high",
        "provider_timeout_ms": 2000,
        "max_prompt_bytes": 65536,
        "max_event_line_bytes": 65536,
        "max_provider_output_bytes": 1024 * 1024,
        "require_explicit_confirmation": True,
    }
    values.update(updates)
    return LiveSettings.model_validate(values)


def _runner_settings(**updates: object) -> RunnerSettings:
    values: dict[str, object] = {
        "fixture_path": "fixtures/task",
        "command_timeout_ms": 2000,
        "termination_grace_ms": 100,
        "max_output_bytes": 4096,
        "max_diff_bytes": 65536,
    }
    values.update(updates)
    return RunnerSettings.model_validate(values)


def _fake_codex(
    tmp_path: Path,
    *,
    live_code: str = "raise SystemExit(99)",
    version_code: str = f"print({_SUPPORTED_CODEX_VERSION!r})",
    help_code: str | None = None,
) -> tuple[dict[str, str], Path]:
    fake_directory = tmp_path / "fake-bin"
    fake_directory.mkdir(exist_ok=True)
    inspection = tmp_path / "inspection.json"
    rendered_help_code = help_code or (
        f"print({' '.join(REQUIRED_CODEX_EXEC_FLAGS)!r})"
    )
    script = (
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,signal,subprocess,sys,time\n"
        f"inspection=pathlib.Path({str(inspection)!r})\n"
        "if sys.argv[1:] == ['--version']:\n"
        f"{textwrap.indent(version_code, '    ')}\n"
        "elif sys.argv[1:] == ['exec','--help']:\n"
        f"{textwrap.indent(rendered_help_code, '    ')}\n"
        "else:\n"
        f"{textwrap.indent(live_code, '    ')}\n"
    )
    executable = fake_directory / "codex"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    managed_auth = tmp_path / "managed-auth"
    managed_auth.mkdir(exist_ok=True)
    environment = {
        "PATH": f"{fake_directory}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "parent-home"),
        "CODEX_HOME": str(managed_auth),
        "OPENAI_API_KEY": "synthetic-openai-key",
        "CODEX_API_KEY": "synthetic-codex-key",
        "AGENTLAB_PARENT_SECRET": "synthetic-parent-secret",
    }
    return environment, inspection


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    environment_root = tmp_path / "environment"
    workspace.mkdir()
    for name in ("home", "tmp", "cache"):
        (environment_root / name).mkdir(parents=True, exist_ok=True)
    return workspace, environment_root


def _json_line(event: object) -> bytes:
    return f"{json.dumps(event)}\n".encode()


def test_preflight_uses_only_version_and_help(tmp_path: Path) -> None:
    environment, inspection = _fake_codex(tmp_path)

    result = preflight_codex(parent_environment=environment)

    assert result.cli_version == _SUPPORTED_CODEX_VERSION
    assert result.cli_profile is CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2
    assert result.verified_flags == tuple(sorted(REQUIRED_CODEX_EXEC_FLAGS))
    assert "--ask-for-approval" not in result.verified_flags
    assert not inspection.exists()


def test_preflight_rejects_missing_command(tmp_path: Path) -> None:
    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment={"PATH": str(tmp_path)})

    assert error.value.failure_kind is LiveFailureKind.PROVIDER_UNAVAILABLE


def test_preflight_cleanup_failure_is_a_harness_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(tmp_path)
    remove_temporary_root = codex_provider_module.remove_temporary_root

    def report_cleanup_failure(path: Path) -> tuple[bool, str]:
        cleanup_succeeded, _cleanup_error = remove_temporary_root(path)
        assert cleanup_succeeded
        return False, "synthetic preflight cleanup failure"

    monkeypatch.setattr(
        "agentlab.codex_provider.remove_temporary_root",
        report_cleanup_failure,
    )

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.EVIDENCE_ERROR


def test_preflight_temporary_root_creation_failure_is_a_harness_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(tmp_path)

    def fail_temporary_root_creation(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("synthetic preflight temporary root creation failure")

    monkeypatch.setattr(
        "agentlab.codex_provider.tempfile.mkdtemp",
        fail_temporary_root_creation,
    )

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert error.value.termination.process_group_cleared is True


def test_preflight_temporary_root_resolve_failure_removes_created_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(tmp_path)
    temporary_root = tmp_path / "unresolved-preflight-root"
    resolve = Path.resolve

    monkeypatch.setattr(
        "agentlab.codex_provider.tempfile.mkdtemp",
        lambda **_kwargs: str(temporary_root.mkdir() or temporary_root),
    )

    def fail_created_root_resolve(
        path: Path,
        strict: bool = False,
    ) -> Path:
        if path == temporary_root:
            raise OSError("synthetic preflight root resolve failure")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_created_root_resolve)

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert not temporary_root.exists()


def test_preflight_workspace_creation_failure_is_a_harness_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(tmp_path)
    mkdir = Path.mkdir
    temporary_roots: list[Path] = []
    mkdtemp = codex_provider_module.tempfile.mkdtemp

    def track_temporary_root(*args: object, **kwargs: object) -> str:
        created = Path(mkdtemp(*args, **kwargs))
        temporary_roots.append(created)
        return str(created)

    def fail_workspace_creation(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if (
            path.name == "workspace"
            and path.parent.name.startswith("agentlab-codex-preflight-")
        ):
            raise PermissionError("synthetic preflight workspace creation failure")
        mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(
        "agentlab.codex_provider.tempfile.mkdtemp",
        track_temporary_root,
    )
    monkeypatch.setattr(Path, "mkdir", fail_workspace_creation)

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert error.value.termination.process_group_cleared is True
    assert len(temporary_roots) == 1
    assert not temporary_roots[0].exists()


def test_preflight_unexpected_collection_error_cleans_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        version_code="signal.pause()",
    )
    spawned_pids: list[int] = []
    popen = runner_module.subprocess.Popen

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_selector_creation() -> NoReturn:
        raise RuntimeError("synthetic unexpected preflight collection failure")

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", track_spawn)
    monkeypatch.setattr(
        "agentlab.runner.selectors.DefaultSelector",
        fail_selector_creation,
    )

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_preflight_preserves_process_cleanup_failure_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        version_code="signal.pause()",
    )
    spawned_pids: list[int] = []
    popen = runner_module.subprocess.Popen
    terminate_process_group = runner_module._terminate_process_group
    remove_temporary_root = codex_provider_module.remove_temporary_root

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

    def report_temporary_cleanup_failure(path: Path) -> tuple[bool, str]:
        cleanup_succeeded, _cleanup_error = remove_temporary_root(path)
        assert cleanup_succeeded
        return False, "synthetic preflight temporary cleanup failure"

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", track_spawn)
    monkeypatch.setattr(
        "agentlab.runner.selectors.DefaultSelector",
        fail_selector_creation,
    )
    monkeypatch.setattr(
        "agentlab.runner._terminate_process_group",
        report_cleanup_failure,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.remove_temporary_root",
        report_temporary_cleanup_failure,
    )

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert error.value.termination.process_group_cleared is False
    assert (
        error.value.termination.error
        == "synthetic preflight process cleanup failure"
    )
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_preflight_rejects_unallowlisted_version_before_help(tmp_path: Path) -> None:
    environment, inspection = _fake_codex(
        tmp_path,
        version_code="print('codex-cli 999.0.0')",
        help_code="inspection.write_text('help-called',encoding='utf-8')",
    )

    with pytest.raises(CodexPreflightError, match="not allowlisted") as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is LiveFailureKind.PROVIDER_PROTOCOL_ERROR
    assert error.value.cli_version == "codex-cli 999.0.0"
    assert not inspection.exists()


def test_codex_home_must_be_an_existing_absolute_directory(tmp_path: Path) -> None:
    configured_file = tmp_path / "not-a-directory"
    configured_file.write_text("not auth", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        resolve_codex_home({"CODEX_HOME": str(configured_file)})


def test_preflight_does_not_accept_required_flag_as_a_longer_name(
    tmp_path: Path,
) -> None:
    misleading_help = " ".join(REQUIRED_CODEX_EXEC_FLAGS).replace(
        "--json",
        "--json-output",
    )
    environment, _inspection = _fake_codex(
        tmp_path,
        help_code=f"print({misleading_help!r})",
    )

    with pytest.raises(CodexPreflightError, match="--json"):
        preflight_codex(parent_environment=environment)


@pytest.mark.parametrize(
    ("version_code", "help_code", "expected_kind"),
    [
        (
            "raise SystemExit(3)",
            None,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
        ),
        (
            f"print({_SUPPORTED_CODEX_VERSION!r})",
            "raise SystemExit(4)",
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
        ),
        (
            f"print({_SUPPORTED_CODEX_VERSION!r})",
            "print('--json --ephemeral')",
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
        ),
        (
            "os.write(1,b'\\xff')",
            None,
            LiveFailureKind.PROVIDER_UNAVAILABLE,
        ),
        (
            f"print({_SUPPORTED_CODEX_VERSION!r})",
            "os.write(1,b'\\xff')",
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
        ),
    ],
)
def test_preflight_fails_closed(
    tmp_path: Path,
    version_code: str,
    help_code: str | None,
    expected_kind: LiveFailureKind,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        version_code=version_code,
        help_code=help_code,
    )

    with pytest.raises(CodexPreflightError) as error:
        preflight_codex(parent_environment=environment)

    assert error.value.failure_kind is expected_kind


@pytest.mark.parametrize(
    ("version_code", "help_code"),
    [
        ("os.write(1,b'x'*70000)", None),
        (f"os.write(2,b'x'*70000); print({_SUPPORTED_CODEX_VERSION!r})", None),
        (f"print({_SUPPORTED_CODEX_VERSION!r})", "os.write(1,b'x'*70000)"),
        (f"print({_SUPPORTED_CODEX_VERSION!r})", "os.write(2,b'x'*70000)"),
    ],
)
def test_preflight_rejects_bounded_output_overflow(
    tmp_path: Path,
    version_code: str,
    help_code: str | None,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        version_code=version_code,
        help_code=help_code,
    )

    with pytest.raises(CodexPreflightError):
        preflight_codex(parent_environment=environment)


def test_preflight_timeout_removes_background_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_code = (
        "child=subprocess.Popen([sys.executable,'-c','import signal;signal.pause()'])\n"
        "inspection.write_text(str(child.pid),encoding='utf-8')\n"
        "signal.pause()"
    )
    environment, inspection = _fake_codex(tmp_path, version_code=version_code)
    monkeypatch.setattr("agentlab.codex_provider.PREFLIGHT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("agentlab.codex_provider.PREFLIGHT_TERMINATION_GRACE_MS", 50)

    with pytest.raises(CodexPreflightError):
        preflight_codex(parent_environment=environment)

    child_pid = int(inspection.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_preflight_timeout_escalates_past_ignored_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_code = (
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "inspection.write_text(str(os.getpid()),encoding='utf-8')\n"
        "signal.pause()"
    )
    environment, inspection = _fake_codex(tmp_path, version_code=version_code)
    monkeypatch.setattr("agentlab.codex_provider.PREFLIGHT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("agentlab.codex_provider.PREFLIGHT_TERMINATION_GRACE_MS", 50)

    with pytest.raises(CodexPreflightError):
        preflight_codex(parent_environment=environment)

    process_pid = int(inspection.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_pid, 0)


def test_jsonl_parser_normalizes_counts_and_provider_usage() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    secret = "synthetic-raw-item-secret"
    events = [
        {"type": "thread.started", "thread_id": "never-persist-this"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "message", "text": secret}},
        {"type": "future.vendor.event", "payload": secret},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 3,
                "output_tokens": 7,
                "reasoning_output_tokens": 2,
            },
        },
    ]
    for event in events:
        parser.feed(_json_line(event))

    summary = parser.finish()

    assert summary.event_count == 5
    assert summary.unknown_event_count == 1
    assert summary.thread_started_count == 1
    assert summary.turn_started_count == 1
    assert summary.terminal_event is CodexTerminalEvent.TURN_COMPLETED
    assert summary.turn_completed_count == 1
    assert summary.turn_failed_count == 0
    assert summary.item_type_counts == {CodexItemType.MESSAGE: 1}
    assert summary.usage_metrics.input_tokens == 12
    assert summary.usage_metrics.source is UsageMetricSource.PROVIDER_REPORTED
    assert secret not in repr(summary)


def test_jsonl_parser_does_not_persist_unrecognized_item_type_text() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    secret = "synthetic-secret-in-item-type"
    for event in (
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": secret}},
        {"type": "turn.completed"},
    ):
        parser.feed(_json_line(event))

    summary = parser.finish()

    assert summary.item_type_counts == {"unknown": 1}
    assert secret not in repr(summary)


def test_jsonl_parser_preserves_missing_usage_as_not_available() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    for event in (
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "turn.completed"},
    ):
        parser.feed(_json_line(event))

    usage = parser.finish().usage_metrics

    assert usage.source is UsageMetricSource.NOT_AVAILABLE
    assert usage.input_tokens is None
    assert usage.output_tokens is None


@pytest.mark.parametrize(
    "payload",
    [
        b"{broken\n",
        b'{"type":"thread.started","type":"turn.started"}\n',
        b'{"type":NaN}\n',
        b'{"type":"thread.started","value":1e999}\n',
        b"[]\n",
        b"\n",
        b"\xff\n",
    ],
    ids=[
        "invalid-json",
        "duplicate-key",
        "non-finite",
        "overflowing-float",
        "non-object",
        "empty-line",
        "invalid-utf8",
    ],
)
def test_jsonl_parser_rejects_invalid_lines(payload: bytes) -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)

    with pytest.raises(CodexProtocolError):
        parser.feed(payload)


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "turn.started"}, {"type": "thread.started"}],
        [{"type": "thread.started"}, {"type": "turn.completed"}],
        [
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "turn.completed"},
            {"type": "turn.completed"},
        ],
    ],
    ids=["turn-before-thread", "terminal-before-turn", "duplicate-terminal"],
)
def test_jsonl_parser_rejects_invalid_lifecycle(events: list[dict[str, str]]) -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)

    with pytest.raises(CodexProtocolError):
        for event in events:
            parser.feed(_json_line(event))


def test_jsonl_parser_rejects_missing_terminal() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    parser.feed(_json_line({"type": "thread.started"}))
    parser.feed(_json_line({"type": "turn.started"}))

    with pytest.raises(CodexProtocolError, match="terminal"):
        parser.finish()


def test_jsonl_parser_classifies_turn_failed() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    for event in (
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": "raw-secret"},
    ):
        parser.feed(_json_line(event))

    assert parser.finish().turn_failed is True


def test_jsonl_parser_classifies_terminal_error() -> None:
    parser = CodexJsonlParser(max_line_bytes=4096, max_total_bytes=16384)
    parser.feed(_json_line({"type": "error", "message": "raw-secret"}))

    summary = parser.finish()

    assert summary.turn_failed is True
    assert summary.event_count == 1


def test_jsonl_parser_enforces_line_and_total_limits() -> None:
    line_limited = CodexJsonlParser(max_line_bytes=5, max_total_bytes=100)
    with pytest.raises(CodexProtocolError) as line_error:
        line_limited.feed(b'{"type":"thread.started"}\n')
    assert line_error.value.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT

    total_limited = CodexJsonlParser(max_line_bytes=100, max_total_bytes=5)
    with pytest.raises(CodexProtocolError) as total_error:
        total_limited.feed(b'{"type":"thread.started"}\n')
    assert total_error.value.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT


def test_process_runner_uses_safe_argv_stdin_and_separate_environment(
    tmp_path: Path,
) -> None:
    prompt_secret = "synthetic-prompt-secret"
    event_secret = "synthetic-event-secret"
    stderr_secret = "synthetic-stderr-secret"
    live_code = (
        "prompt=sys.stdin.read()\n"
        "inspection.write_text(json.dumps({"
        "'argv':sys.argv,'prompt':prompt,'cwd':str(pathlib.Path.cwd()),"
        "'codex_home':os.environ.get('CODEX_HOME'),"
        "'openai_key':os.environ.get('OPENAI_API_KEY'),"
        "'codex_key':os.environ.get('CODEX_API_KEY'),"
        "'parent_secret':os.environ.get('AGENTLAB_PARENT_SECRET')"
        "}),encoding='utf-8')\n"
        f"print(json.dumps({{'type':'thread.started','thread_id':{event_secret!r}}}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'type':'message','text':{event_secret!r}}}}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':2}}),flush=True)\n"
        f"sys.stderr.write({stderr_secret!r})"
    )
    environment, inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=prompt_secret.encode(),
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )
    observed = json.loads(inspection.read_text(encoding="utf-8"))
    argv = observed["argv"]

    assert result.evidence.status is ProviderExecutionStatus.SUCCEEDED
    assert observed["prompt"] == prompt_secret
    assert prompt_secret not in argv
    assert observed["cwd"] == str(workspace)
    assert observed["codex_home"] == environment["CODEX_HOME"]
    assert observed["openai_key"] is None
    assert observed["codex_key"] is None
    assert observed["parent_secret"] is None
    assert "--json" in argv
    assert "--ephemeral" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--ask-for-approval" not in argv
    assert result.evidence.approval_policy == "never"
    assert result.evidence.approval_basis == "explicit_config_never"
    config_values = [
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "--config"
    ]
    assert 'approval_policy="never"' in config_values
    assert "sandbox_workspace_write.network_access=false" in argv
    assert 'web_search="disabled"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--full-auto" not in argv
    persisted = result.evidence.model_dump_json()
    for secret in (prompt_secret, event_secret, stderr_secret):
        assert secret not in persisted


def test_explicit_never_argv_is_present_with_auto_review_like_config(
    tmp_path: Path,
) -> None:
    live_code = (
        "managed=(pathlib.Path(os.environ['CODEX_HOME'])/'config.toml').read_text()\n"
        "configs=[sys.argv[index+1] for index,value in enumerate(sys.argv) "
        "if value=='--config']\n"
        "if 'approvals_reviewer = \"auto_review\"' not in managed "
        "or 'approval_policy=\"never\"' not in configs:\n"
        "    raise SystemExit(97)\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    (Path(environment["CODEX_HOME"]) / "config.toml").write_text(
        'approvals_reviewer = "auto_review"\n',
        encoding="utf-8",
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.status is ProviderExecutionStatus.SUCCEEDED
    assert (
        result.evidence.cli_profile
        is CodexCliProfile.HEADLESS_EXEC_EXPLICIT_NEVER_V2
    )
    assert (
        result.evidence.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )
    assert result.evidence.approval_basis == "explicit_config_never"


def test_process_runner_timeout_stops_parent_and_child(tmp_path: Path) -> None:
    live_code = (
        "child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'])\n"
        "inspection.write_text(str(child.pid),encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "signal.pause()"
    )
    environment, inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(provider_timeout_ms=150),
        runner=_runner_settings(termination_grace_ms=50),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )
    child_pid = int(inspection.read_text(encoding="utf-8"))

    assert result.evidence.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
    assert result.evidence.termination.sigterm_sent is True
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.parametrize(
    ("live_code", "expected"),
    [
        ("sys.stdin.read(); raise SystemExit(7)", LiveFailureKind.PROVIDER_CLI_NONZERO),
        (
            "sys.stdin.read(); print('{broken',flush=True)",
            LiveFailureKind.PROVIDER_PROTOCOL_ERROR,
        ),
    ],
    ids=["nonzero", "protocol"],
)
def test_process_runner_classifies_cli_and_protocol_failures(
    tmp_path: Path,
    live_code: str,
    expected: LiveFailureKind,
) -> None:
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.status is ProviderExecutionStatus.FAILED
    assert result.evidence.failure_kind is expected


def test_process_runner_classifies_signal_termination(tmp_path: Path) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code=(
            "sys.stdin.read()\n"
            "os.kill(os.getpid(),signal.SIGTERM)"
        ),
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.status is ProviderExecutionStatus.FAILED
    assert (
        result.evidence.failure_kind
        is LiveFailureKind.PROVIDER_SIGNAL_TERMINATION
    )
    assert result.evidence.exit_code is not None
    assert result.evidence.exit_code < 0


def test_process_runner_classifies_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(tmp_path)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    def fail_spawn(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr("agentlab.codex_provider.subprocess.Popen", fail_spawn)
    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.failure_kind is LiveFailureKind.PROVIDER_SPAWN_ERROR
    assert result.evidence.exit_code is None


def test_process_runner_classifies_prompt_stdin_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_code = "import signal; signal.pause()"
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    def fail_prompt_write(_file_descriptor: int, _content: bytes) -> NoReturn:
        raise BrokenPipeError("synthetic stdin failure")

    monkeypatch.setattr("agentlab.codex_provider.os.write", fail_prompt_write)
    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.failure_kind is LiveFailureKind.PROVIDER_INPUT_ERROR
    assert result.evidence.termination.process_group_cleared is True


def test_process_runner_classifies_total_output_limit(tmp_path: Path) -> None:
    live_code = (
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','padding':'x'*500}),flush=True)"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(
            max_event_line_bytes=128,
            max_provider_output_bytes=128,
        ),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.failure_kind is LiveFailureKind.PROVIDER_OUTPUT_LIMIT
    assert result.evidence.stdout_limit_exceeded is True


def test_process_runner_cleans_up_when_pipe_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="import signal; signal.pause()",
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    def fail_set_blocking(_file_descriptor: int, _blocking: bool) -> NoReturn:
        raise OSError("synthetic pipe setup failure")

    monkeypatch.setattr("agentlab.codex_provider.os.set_blocking", fail_set_blocking)
    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.status is ProviderExecutionStatus.FAILED
    assert result.evidence.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert result.evidence.termination.process_group_cleared is True


def test_process_runner_cleans_up_when_selector_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="signal.pause()",
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)
    spawned_pids: list[int] = []
    popen = codex_provider_module.subprocess.Popen

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    def fail_selector_creation() -> NoReturn:
        raise OSError("synthetic selector creation failure")

    monkeypatch.setattr(
        "agentlab.codex_provider.subprocess.Popen",
        track_spawn,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.selectors.DefaultSelector",
        fail_selector_creation,
    )

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert len(spawned_pids) == 1
    assert result.evidence.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert result.evidence.process_started is True
    assert (
        result.evidence.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )
    assert result.evidence.approval_policy == "never"
    assert result.evidence.approval_basis == "explicit_config_never"
    assert result.evidence.termination.process_group_cleared is True
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_process_runner_classifies_emergency_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="signal.pause()",
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)
    terminate_process_group = codex_provider_module.terminate_process_group

    def fail_selector_creation() -> NoReturn:
        raise OSError("synthetic selector creation failure")

    def report_cleanup_failure(*args: object, **kwargs: object):
        termination = terminate_process_group(*args, **kwargs)
        assert termination.process_group_cleared
        return termination.model_copy(
            update={
                "process_group_cleared": False,
                "error": "synthetic cleanup reporting failure",
            }
        )

    monkeypatch.setattr(
        "agentlab.codex_provider.selectors.DefaultSelector",
        fail_selector_creation,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.terminate_process_group",
        report_cleanup_failure,
    )

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.failure_kind is LiveFailureKind.PROCESS_CLEANUP_ERROR
    assert result.evidence.process_started is True
    assert result.evidence.termination.process_group_cleared is False


def test_process_runner_cleans_up_on_unexpected_collection_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _inspection = _fake_codex(
        tmp_path,
        live_code="signal.pause()",
    )
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)
    spawned_pids: list[int] = []
    popen = codex_provider_module.subprocess.Popen
    selector_factory = codex_provider_module.selectors.DefaultSelector

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    class UnexpectedCollectionSelector:
        def __init__(self) -> None:
            self._selector = selector_factory()

        def register(self, *args: object, **kwargs: object):
            return self._selector.register(*args, **kwargs)

        def unregister(self, *args: object, **kwargs: object):
            return self._selector.unregister(*args, **kwargs)

        def get_map(self):
            return self._selector.get_map()

        def select(self, _timeout: float):
            raise RuntimeError("synthetic unexpected collection failure")

        def close(self) -> None:
            self._selector.close()

    monkeypatch.setattr(
        "agentlab.codex_provider.subprocess.Popen",
        track_spawn,
    )
    monkeypatch.setattr(
        "agentlab.codex_provider.selectors.DefaultSelector",
        UnexpectedCollectionSelector,
    )

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert len(spawned_pids) == 1
    assert result.evidence.failure_kind is LiveFailureKind.EVIDENCE_ERROR
    assert result.evidence.process_started is True
    assert (
        result.evidence.execution_stage
        is CodexExecutionStage.PROVIDER_INVOCATION_ATTEMPTED
    )
    assert result.evidence.approval_policy == "never"
    assert result.evidence.termination.process_group_cleared is True
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_process_runner_escalates_to_sigkill(tmp_path: Path) -> None:
    live_code = (
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "signal.pause()"
    )
    environment, _inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(provider_timeout_ms=150),
        runner=_runner_settings(termination_grace_ms=50),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )

    assert result.evidence.failure_kind is LiveFailureKind.PROVIDER_TIMEOUT
    assert result.evidence.termination.sigkill_sent is True


def test_process_runner_cleans_background_child_after_success(tmp_path: Path) -> None:
    live_code = (
        "child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'])\n"
        "inspection.write_text(str(child.pid),encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.started'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}),flush=True)"
    )
    environment, inspection = _fake_codex(tmp_path, live_code=live_code)
    preflight = preflight_codex(parent_environment=environment)
    workspace, environment_root = _workspace(tmp_path)

    result = CodexProcessRunner(
        live=_live_settings(),
        runner=_runner_settings(),
    ).run(
        preflight=preflight,
        prompt=b"safe prompt",
        workspace=workspace,
        environment_root=environment_root,
        parent_environment=environment,
    )
    child_pid = int(inspection.read_text(encoding="utf-8"))

    assert result.evidence.status is ProviderExecutionStatus.SUCCEEDED
    assert result.evidence.termination.sigterm_sent is True
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_build_argv_never_contains_prompt() -> None:
    secret = "never-put-this-prompt-in-argv"
    environment = {"PATH": os.environ.get("PATH", "")}
    del environment
    preflight = type(
        "Preflight",
        (),
        {
            "executable": "/synthetic/codex",
            "cli_version": "fake",
            "checked_at": None,
            "verified_flags": (),
        },
    )()

    argv = build_codex_argv(
        preflight,
        model="gpt-test-fixed",
        reasoning_effort=_live_settings().reasoning_effort,
    )

    assert secret not in argv
    assert argv[-1] == "-"
