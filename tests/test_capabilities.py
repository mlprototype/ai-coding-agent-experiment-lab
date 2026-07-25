from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentlab.capabilities import doctor_report
from agentlab.cli import app

runner = CliRunner()


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
