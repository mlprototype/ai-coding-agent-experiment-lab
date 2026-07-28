from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

import agentlab.runner as runner_module
from agentlab.models import (
    CommandStatus,
    FailureKind,
    GateKind,
    RunnerSettings,
    TerminationReason,
)
from agentlab.runner import LocalCommandRunner, UnsupportedRunnerPlatformError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Phase 2 runner is POSIX-only")


def _settings(**updates: int | str) -> RunnerSettings:
    values: dict[str, int | str] = {
        "fixture_path": "fixtures/test",
        "command_timeout_ms": 2000,
        "termination_grace_ms": 100,
        "max_output_bytes": 4096,
        "max_diff_bytes": 65536,
    }
    values.update(updates)
    return RunnerSettings.model_validate(values)


def _run(
    tmp_path: Path,
    argv: list[str],
    *,
    settings: RunnerSettings | None = None,
):
    workspace = tmp_path / "workspace"
    environment_root = tmp_path / "environment"
    workspace.mkdir()
    for name in ("home", "tmp", "cache"):
        (environment_root / name).mkdir(parents=True, exist_ok=True)
    return LocalCommandRunner(settings or _settings()).run(
        gate=GateKind.ACCEPTANCE,
        command_index=0,
        argv=argv,
        workspace=workspace,
        environment_root=environment_root,
        temporary_root=tmp_path,
    )


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def test_command_uses_workspace_cwd_and_separates_stdout_stderr(tmp_path: Path) -> None:
    script = (
        "import pathlib,sys;"
        "print(pathlib.Path.cwd());"
        "print('stderr-value',file=sys.stderr);"
        "raise SystemExit(7)"
    )

    result = _run(tmp_path, [sys.executable, "-c", script])

    assert result.evidence.status is CommandStatus.FAILED
    assert result.evidence.return_code == 7
    assert result.evidence.stdout == "<WORKSPACE>\n"
    assert result.evidence.stderr == "stderr-value\n"
    assert result.harness_failure is None


def test_popen_receives_argv_shell_false_closed_stdin_and_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_popen = subprocess.Popen
    calls: list[tuple[object, dict[str, Any]]] = []

    def recording_popen(args: object, **kwargs: Any):
        calls.append((args, kwargs))
        return original_popen(args, **kwargs)

    monkeypatch.setattr("agentlab.runner.subprocess.Popen", recording_popen)
    command = [sys.executable, "-c", "print('ok')"]

    result = _run(tmp_path, command)

    assert result.evidence.status is CommandStatus.PASSED
    assert calls[0][0] == command
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.PIPE
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["cwd"] == tmp_path / "workspace"


def test_shell_metacharacters_are_literal_argv_and_do_not_create_file(tmp_path: Path) -> None:
    argument = "$(touch shell-was-used); *.txt"

    result = _run(
        tmp_path,
        [sys.executable, "-c", "import sys; print(sys.argv[1])", argument],
    )

    assert result.evidence.status is CommandStatus.PASSED
    assert result.evidence.stdout == f"{argument}\n"
    assert not (tmp_path / "workspace" / "shell-was-used").exists()


def test_stdin_is_closed_with_devnull(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; print(len(sys.stdin.buffer.read()))",
        ],
    )

    assert result.evidence.status is CommandStatus.PASSED
    assert result.evidence.stdout == "0\n"


def test_parent_secret_is_not_inherited_and_temp_paths_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "phase2-synthetic-secret-value"
    monkeypatch.setenv("AGENTLAB_SYNTHETIC_SECRET", secret)
    script = (
        "import os;"
        "print(os.environ.get('AGENTLAB_SYNTHETIC_SECRET','not_present'));"
        "print(os.environ['HOME']);"
        "print(os.environ['TMPDIR']);"
        "print(os.environ['XDG_CACHE_HOME'])"
    )

    result = _run(tmp_path, [sys.executable, "-c", script])

    assert "not_present" in result.evidence.stdout
    assert secret not in result.evidence.stdout
    assert str(tmp_path) not in result.evidence.stdout
    assert "<TEMP_HOME>" in result.evidence.stdout
    assert "<TEMP_DIR>" in result.evidence.stdout
    assert "<TEMP_CACHE>" in result.evidence.stdout


def test_invalid_utf8_output_uses_replacement_character(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [sys.executable, "-c", "import os; os.write(1, b'bad:\\xff\\n')"],
    )

    assert result.evidence.status is CommandStatus.PASSED
    assert result.evidence.stdout == "bad:�\n"
    assert result.evidence.stdout_decode_replaced is True
    assert result.evidence.stderr_decode_replaced is False


def test_literal_replacement_character_is_not_reported_as_decode_repair(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, [sys.executable, "-c", "print('�')"])

    assert result.evidence.stdout == "�\n"
    assert result.evidence.stdout_decode_replaced is False


def test_signal_termination_is_not_a_quality_gate_failure(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
    )

    assert result.evidence.status is CommandStatus.SIGNAL_TERMINATED
    assert result.evidence.return_code == -signal.SIGTERM
    assert result.harness_failure is FailureKind.SIGNAL_TERMINATION


def test_output_selector_failure_is_collection_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_select(_selector: object, _timeout: float | None = None) -> NoReturn:
        raise OSError("synthetic selector failure")

    monkeypatch.setattr("agentlab.runner.selectors.DefaultSelector.select", fail_select)
    result = _run(tmp_path, [sys.executable, "-c", "print('started')"])

    assert result.evidence.status is CommandStatus.COLLECTION_ERROR
    assert result.evidence.status is not CommandStatus.SPAWN_ERROR
    assert "output selector failed: OSError" in (result.evidence.error or "")
    assert result.harness_failure is FailureKind.EVIDENCE_ERROR


def test_unexpected_collection_exception_cleans_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector_factory = runner_module.selectors.DefaultSelector
    popen = runner_module.subprocess.Popen
    spawned_pids: list[int] = []

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

    def track_spawn(*args: object, **kwargs: object):
        process = popen(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    monkeypatch.setattr(
        "agentlab.runner.selectors.DefaultSelector",
        UnexpectedCollectionSelector,
    )
    monkeypatch.setattr("agentlab.runner.subprocess.Popen", track_spawn)

    result = _run(
        tmp_path,
        [sys.executable, "-c", "import signal; signal.pause()"],
    )

    assert len(spawned_pids) == 1
    assert result.evidence.status is CommandStatus.COLLECTION_ERROR
    assert result.harness_failure is FailureKind.EVIDENCE_ERROR
    assert result.evidence.termination.process_group_cleared is True
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


def test_large_stdout_and_stderr_are_drained_and_truncated(tmp_path: Path) -> None:
    script = "import os; os.write(1,b'o'*200000); os.write(2,b'e'*200000)"

    result = _run(
        tmp_path,
        [sys.executable, "-c", script],
        settings=_settings(max_output_bytes=1024),
    )

    assert result.evidence.status is CommandStatus.PASSED
    assert len(result.evidence.stdout.encode()) == 1024
    assert len(result.evidence.stderr.encode()) == 1024
    assert result.evidence.stdout_truncated is True
    assert result.evidence.stderr_truncated is True


def test_missing_command_is_classified_as_command_unavailable(tmp_path: Path) -> None:
    result = _run(tmp_path, ["agentlab-command-that-does-not-exist"])

    assert result.evidence.status is CommandStatus.SPAWN_ERROR
    assert result.evidence.return_code is None
    assert result.harness_failure is FailureKind.COMMAND_UNAVAILABLE
    assert "FileNotFoundError" in (result.evidence.error or "")


def test_timeout_stops_parent_and_child_process_group(tmp_path: Path) -> None:
    child_script = "import signal; signal.pause()"
    parent_script = (
        "import signal,subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        "print(child.pid,flush=True);"
        "signal.pause()"
    )

    result = _run(
        tmp_path,
        [sys.executable, "-c", parent_script],
        settings=_settings(command_timeout_ms=250, termination_grace_ms=100),
    )
    child_process_id = int(result.evidence.stdout.strip())

    assert result.evidence.status is CommandStatus.TIMED_OUT
    assert result.evidence.termination.reason is TerminationReason.TIMEOUT
    assert result.evidence.termination.sigterm_sent is True
    assert result.evidence.termination.process_group_cleared is True
    assert result.harness_failure is FailureKind.TIMEOUT
    assert not _process_exists(child_process_id)


def test_timeout_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path: Path) -> None:
    script = (
        "import signal;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "signal.pause()"
    )

    result = _run(
        tmp_path,
        [sys.executable, "-c", script],
        settings=_settings(command_timeout_ms=200, termination_grace_ms=50),
    )

    assert result.evidence.status is CommandStatus.TIMED_OUT
    assert result.evidence.termination.sigterm_sent is True
    assert result.evidence.termination.sigkill_sent is True
    assert result.evidence.termination.process_group_cleared is True


def test_normal_parent_exit_cleans_up_background_child(tmp_path: Path) -> None:
    child_script = "import signal; signal.pause()"
    parent_script = (
        "import subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        "print(child.pid,flush=True)"
    )

    result = _run(tmp_path, [sys.executable, "-c", parent_script])
    child_process_id = int(result.evidence.stdout.strip())

    assert result.evidence.status is CommandStatus.PASSED
    assert result.evidence.termination.reason is TerminationReason.RESIDUAL_PROCESS
    assert result.evidence.termination.sigterm_sent is True
    assert result.evidence.termination.process_group_cleared is True
    assert result.harness_failure is None
    assert not _process_exists(child_process_id)


def test_unsupported_platform_fails_closed_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentlab.runner.os.name", "nt")

    with pytest.raises(UnsupportedRunnerPlatformError, match="POSIX"):
        _run(tmp_path, [sys.executable, "-c", "print('must not run')"])


def test_unverified_posix_platform_also_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentlab.runner.sys.platform", "freebsd-test")

    with pytest.raises(UnsupportedRunnerPlatformError, match="POSIX"):
        _run(tmp_path, [sys.executable, "-c", "print('must not run')"])
