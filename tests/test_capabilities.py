from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

from agentlab.capabilities import PROBES, ProbeDefinition, doctor_report, probe_capability
from agentlab.cli import app
from agentlab.models import Provider

runner = CliRunner()


def _probe(provider: Provider) -> ProbeDefinition:
    return next(probe for probe in PROBES if probe.provider is provider)


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake-command"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _mock_available_command(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[subprocess.CompletedProcess[str] | BaseException],
) -> list[tuple[list[str], dict[str, Any]]]:
    response_iterator: Iterator[subprocess.CompletedProcess[str] | BaseException] = iter(responses)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "agentlab.capabilities.shutil.which",
        lambda command: f"/fake/bin/{command}",
    )

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        response = next(response_iterator)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("agentlab.capabilities.subprocess.run", fake_run)
    return calls


def test_doctor_succeeds_when_commands_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentlab.capabilities.shutil.which", lambda _command: None)

    report = doctor_report()

    assert len(report.capabilities) == 2
    assert all(not item.command_available for item in report.capabilities)


def test_doctor_json_is_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentlab.capabilities.shutil.which", lambda _command: None)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert {item["provider"] for item in payload["capabilities"]} == {
        "codex",
        "antigravity",
    }


def test_codex_available_probe_reads_stdout_and_capability_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_available_command(
        monkeypatch,
        [
            _completed(stdout="codex-cli 1.2.3\n"),
            _completed(
                stdout=(
                    "Run Codex non-interactively\n"
                    "--json Print events as JSON\n"
                    "--usage Show usage metrics\n"
                )
            ),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.command_available is True
    assert report.cli_version == "codex-cli 1.2.3"
    assert report.non_interactive_supported is True
    assert report.structured_output_supported is True
    assert report.usage_metrics_supported is True
    assert calls[0][0] == ["/fake/bin/codex", "--version"]
    assert calls[1][0] == ["/fake/bin/codex", "exec", "--help"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True
    assert "shell" not in calls[0][1]


def test_codex_probe_reads_version_and_help_from_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_available_command(
        monkeypatch,
        [
            _completed(stderr="codex-cli 2.0\n"),
            _completed(stderr="Run Codex non-interactively\n--json\n"),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.cli_version == "codex-cli 2.0"
    assert report.non_interactive_supported is True
    assert report.structured_output_supported is True


def test_codex_successful_help_without_markers_does_not_claim_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_available_command(
        monkeypatch,
        [_completed(stdout="codex 1.0"), _completed(stdout="General command help")],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.non_interactive_supported is False
    assert report.structured_output_supported is False
    assert report.usage_metrics_supported is False


def test_antigravity_uses_its_non_interactive_and_output_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_available_command(
        monkeypatch,
        [
            _completed(stdout="agy 1.0"),
            _completed(stderr="--prompt TEXT\n--output-format json\nToken usage\n"),
        ],
    )

    report = probe_capability(_probe(Provider.ANTIGRAVITY))

    assert report.non_interactive_supported is True
    assert report.structured_output_supported is True
    assert report.usage_metrics_supported is True


def test_version_nonzero_exit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_available_command(
        monkeypatch,
        [
            _completed(returncode=2, stderr="version failed"),
            _completed(stdout="Run Codex non-interactively"),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.cli_version is None
    assert "version command exited with status 2" in report.notes


def test_help_nonzero_exit_does_not_claim_marked_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_available_command(
        monkeypatch,
        [
            _completed(stdout="codex 1.0"),
            _completed(
                returncode=2,
                stdout="Run Codex non-interactively\n--json\nusage metrics",
            ),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.non_interactive_supported is False
    assert report.structured_output_supported is False
    assert report.usage_metrics_supported is False
    assert "help command exited with status 2" in report.notes


def test_version_oserror_is_recorded_and_help_probe_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_available_command(
        monkeypatch,
        [
            OSError("version executable failed"),
            _completed(stdout="Run Codex non-interactively"),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.cli_version is None
    assert report.non_interactive_supported is True
    assert "version probe failed: OSError" in report.notes


def test_help_subprocess_error_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_available_command(
        monkeypatch,
        [
            _completed(stdout="codex 1.0"),
            subprocess.SubprocessError("help failed"),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.non_interactive_supported is False
    assert "help probe failed: SubprocessError" in report.notes


def test_timeouts_do_not_escape_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_available_command(
        monkeypatch,
        [
            subprocess.TimeoutExpired(["codex", "--version"], 5),
            subprocess.TimeoutExpired(["codex", "exec", "--help"], 5),
        ],
    )

    report = probe_capability(_probe(Provider.CODEX))

    assert report.command_available is True
    assert "version probe failed: TimeoutExpired" in report.notes
    assert "help probe failed: TimeoutExpired" in report.notes


def test_doctor_returns_other_provider_when_one_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentlab.capabilities.shutil.which",
        lambda command: f"/fake/bin/{command}",
    )

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0].endswith("/codex"):
            raise OSError("codex probe failed")
        if "--version" in args:
            return _completed(stdout="agy 3.0")
        return _completed(stdout="--prompt TEXT")

    monkeypatch.setattr("agentlab.capabilities.subprocess.run", fake_run)

    report = doctor_report()
    by_provider = {item.provider: item for item in report.capabilities}

    assert len(report.capabilities) == 2
    assert "version probe failed: OSError" in by_provider[Provider.CODEX].notes
    assert by_provider[Provider.ANTIGRAVITY].cli_version == "agy 3.0"
    assert by_provider[Provider.ANTIGRAVITY].non_interactive_supported is True
