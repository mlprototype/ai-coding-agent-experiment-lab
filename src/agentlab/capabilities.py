"""Read-only capability probes for supported coding-agent CLIs."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from agentlab.models import CapabilityReport, DoctorReport, Provider

PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ProbeDefinition:
    provider: Provider
    command: str
    version_args: tuple[str, ...]
    help_args: tuple[str, ...]
    non_interactive_markers: tuple[str, ...]


PROBES = (
    ProbeDefinition(
        Provider.CODEX,
        "codex",
        ("--version",),
        ("exec", "--help"),
        ("run codex non-interactively",),
    ),
    ProbeDefinition(
        Provider.ANTIGRAVITY,
        "agy",
        ("--version",),
        ("--help",),
        ("non-interactive", "--prompt", "headless"),
    ),
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run(executable: str, args: tuple[str, ...]) -> CommandResult:
    completed = subprocess.run(
        [executable, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _first_line(result: CommandResult) -> str | None:
    for output in (result.stdout, result.stderr):
        if line := next((line.strip() for line in output.splitlines() if line.strip()), None):
            return line
    return None


def probe_capability(definition: ProbeDefinition) -> CapabilityReport:
    checked_at = datetime.now(UTC)
    executable = shutil.which(definition.command)
    if executable is None:
        return CapabilityReport(
            provider=definition.provider,
            command_available=False,
            executable_path=None,
            cli_version=None,
            non_interactive_supported=False,
            structured_output_supported=False,
            usage_metrics_supported=False,
            checked_at=checked_at,
            notes=[
                f"{definition.command} was not found on PATH",
                "support flags are false because capabilities are not_verified",
            ],
        )

    notes: list[str] = []
    version: str | None = None
    help_text = ""
    help_succeeded = False

    try:
        version_result = _run(executable, definition.version_args)
        if version_result.returncode == 0:
            version = _first_line(version_result)
        else:
            notes.append(f"version command exited with status {version_result.returncode}")
    except (OSError, subprocess.SubprocessError) as error:
        notes.append(f"version probe failed: {type(error).__name__}")

    try:
        help_result = _run(executable, definition.help_args)
        help_text = f"{help_result.stdout}\n{help_result.stderr}".lower()
        help_succeeded = help_result.returncode == 0
        if not help_succeeded:
            notes.append(f"help command exited with status {help_result.returncode}")
    except (OSError, subprocess.SubprocessError) as error:
        notes.append(f"help probe failed: {type(error).__name__}")

    non_interactive = help_succeeded and any(
        marker in help_text for marker in definition.non_interactive_markers
    )

    structured_output = help_succeeded and any(
        marker in help_text for marker in ("--json", "json output", "output-format")
    )
    usage_metrics = help_succeeded and any(
        marker in help_text for marker in ("usage metrics", "token usage", "--usage")
    )
    notes.append("capabilities were inferred only from the requested local help output")

    return CapabilityReport(
        provider=definition.provider,
        command_available=True,
        executable_path=executable,
        cli_version=version,
        non_interactive_supported=non_interactive,
        structured_output_supported=structured_output,
        usage_metrics_supported=usage_metrics,
        checked_at=checked_at,
        notes=notes,
    )


def doctor_report() -> DoctorReport:
    return DoctorReport(capabilities=[probe_capability(probe) for probe in PROBES])
