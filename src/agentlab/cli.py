"""Command-line interface for Phase 0 foundation tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentlab.capabilities import doctor_report
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


if __name__ == "__main__":
    app()

