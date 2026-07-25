"""Command-line interface for experiment contracts, Replay, and Phase 2 gates."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentlab.capabilities import doctor_report
from agentlab.gates import RunGatesError, run_gates
from agentlab.models import EvidenceOverallStatus
from agentlab.recording import RecordingLoadError
from agentlab.replay import ReplayError, run_replay
from agentlab.specs import SpecLoadError, load_experiment_spec

app = typer.Typer(
    name="agentlab",
    help="Reproducible AI coding-agent experiment foundation.",
    no_args_is_help=True,
)


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable JSON report."),
    ] = False,
) -> None:
    """Inspect local CLI capabilities without running an AI task."""
    report = doctor_report()
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
        return

    typer.echo(f"Capability check: {report.checked_at.isoformat()}")
    for capability in report.capabilities:
        availability = "available" if capability.command_available else "not_available"
        typer.echo(f"\n{capability.provider.value}: {availability}")
        typer.echo(f"  executable: {capability.executable_path or 'not_verified'}")
        typer.echo(f"  version: {capability.cli_version or 'not_verified'}")
        typer.echo(f"  non-interactive: {capability.non_interactive_supported}")
        typer.echo(f"  structured output: {capability.structured_output_supported}")
        typer.echo(f"  usage metrics: {capability.usage_metrics_supported}")
        for note in capability.notes:
            typer.echo(f"  note: {note}")


@app.command("validate")
def validate_spec(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Validate an ExperimentSpec YAML file."""
    try:
        spec = load_experiment_spec(path)
    except SpecLoadError as error:
        typer.echo(f"invalid ExperimentSpec: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(
        f"valid ExperimentSpec: {spec.experiment_id} "
        f"(axis={spec.comparison_axis.value}, mode={spec.execution_mode.value})"
    )


@app.command()
def replay(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Required destination for deterministic RunResult JSON."),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Explicitly replace an existing output file."),
    ] = False,
) -> None:
    """Create one RunResult from one saved recording without external AI."""
    try:
        result = run_replay(spec_path, output_path, force=force)
    except (SpecLoadError, RecordingLoadError, ReplayError) as error:
        typer.echo(f"replay failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(f"replayed run: {result.run_id}")
    typer.echo(f"experiment: {result.experiment_id}")
    typer.echo(f"output: {output_path}")
    typer.echo("external AI executed: no (Replay only)")


@app.command("run-gates")
def run_gates_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    task_id: Annotated[
        str,
        typer.Option("--task-id", help="Task ID from ExperimentSpec.task_ids."),
    ],
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="Identifier to persist in the Evidence Artifact."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Required destination for Evidence JSON."),
    ],
    confirm_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-execution",
            help="Explicitly allow the configured local quality-gate subprocesses.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Explicitly replace an existing Evidence file."),
    ] = False,
) -> None:
    """Run only the quality-gate argv listed in a trusted ExperimentSpec."""
    try:
        outcome = run_gates(
            spec_path,
            task_id=task_id,
            run_id=run_id,
            output_path=output_path,
            confirm_execution=confirm_execution,
            force=force,
        )
    except (SpecLoadError, RunGatesError) as error:
        typer.echo(f"run-gates failed: {error}", err=True)
        typer.echo(f"run: {run_id}")
        typer.echo(f"task: {task_id}")
        typer.echo(f"output: {output_path}")
        if isinstance(error, RunGatesError) and error.workspace_removed is not None:
            removed = "yes" if error.workspace_removed else "no"
            typer.echo(f"workspace removed: {removed}")
        else:
            typer.echo("workspace removed: not_created")
        typer.echo("external AI executed: no")
        raise typer.Exit(code=2) from error

    artifact = outcome.artifact
    typer.echo(f"run: {artifact.run_id}")
    typer.echo(f"experiment: {artifact.experiment_id}")
    typer.echo(f"task: {artifact.task_id}")
    if artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR:
        typer.echo(f"Harness failure: {artifact.failure_kind.value}")
    else:
        typer.echo(f"quality gates: {artifact.overall_status.value}")
    typer.echo(f"output: {outcome.output_path}")
    typer.echo(f"workspace removed: {'yes' if artifact.workspace_removed else 'no'}")
    typer.echo("external AI executed: no")

    if artifact.overall_status is EvidenceOverallStatus.FAILED:
        raise typer.Exit(code=1)
    if artifact.overall_status is EvidenceOverallStatus.HARNESS_ERROR:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
