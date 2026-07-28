"""Command-line interface for Replay, Live runs, and Workflow experiments."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentlab.campaign import CampaignError, CampaignStopReason, run_workflow_campaign
from agentlab.capabilities import doctor_report
from agentlab.gates import RunGatesError, run_gates
from agentlab.live import LiveCodexError, run_live_codex
from agentlab.models import EvidenceOverallStatus, LiveOverallStatus
from agentlab.recording import RecordingLoadError
from agentlab.replay import ReplayError, run_replay
from agentlab.specs import SpecLoadError, load_experiment_spec
from agentlab.workflow import (
    WorkflowPlanError,
    WorkflowSpecError,
    create_workflow_plan,
    load_workflow_spec,
    plan_publication_path,
)
from agentlab.workflow_report import WorkflowReportError, create_workflow_report

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


@app.command("validate-workflow")
def validate_workflow_spec(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Validate a strict Phase 4 Workflow A/B Spec without executing anything."""
    try:
        loaded = load_workflow_spec(path)
    except WorkflowSpecError as error:
        typer.echo(f"invalid Workflow Spec: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"valid Workflow Spec: {loaded.spec.experiment_id} "
        f"(axis=workflow, runs={len(loaded.spec.task_ids) * loaded.spec.repetitions * 2})"
    )
    typer.echo("external AI executed: no")


@app.command("plan-workflow")
def plan_workflow_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Create-only destination for canonical Plan JSON."),
    ],
) -> None:
    """Preregister a deterministic Workflow A/B run order without external AI."""
    try:
        plan = create_workflow_plan(spec_path, output_path)
    except (WorkflowSpecError, WorkflowPlanError) as error:
        typer.echo(f"plan-workflow failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"experiment: {plan.experiment_id}")
    typer.echo(f"planned runs: {plan.planned_run_count}")
    typer.echo(f"planned Provider calls: {plan.planned_provider_call_count}")
    typer.echo(f"canonical Plan: {output_path}")
    typer.echo(f"publication metadata: {plan_publication_path(output_path)}")
    typer.echo("external AI executed: no")


@app.command("run-workflow-campaign")
def run_workflow_campaign_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    plan_path: Annotated[
        Path,
        typer.Option("--plan", exists=True, dir_okay=False, help="Preregistered Plan JSON."),
    ],
    campaign_path: Annotated[
        Path,
        typer.Option("--campaign", help="Create-only append-only Campaign JSONL."),
    ],
    confirm_live_codex: Annotated[
        bool,
        typer.Option(
            "--confirm-live-codex",
            help="Explicitly allow external AI transmission and quota consumption.",
        ),
    ] = False,
    confirm_provider_calls: Annotated[
        int | None,
        typer.Option(
            "--confirm-provider-calls",
            help="Confirm the exact total Provider-call budget shown in the Plan.",
        ),
    ] = None,
) -> None:
    """Run one sequential, preregistered Workflow Campaign; never used by CI."""
    if confirm_live_codex and confirm_provider_calls is not None:
        typer.echo(
            "WARNING: external AI execution, data transmission, and quota consumption "
            f"may occur for up to {confirm_provider_calls} Provider calls."
        )
    try:
        outcome = run_workflow_campaign(
            spec_path,
            plan_path,
            campaign_path,
            confirm_live_codex=confirm_live_codex,
            confirm_provider_calls=confirm_provider_calls,
        )
    except (CampaignError, WorkflowSpecError, WorkflowPlanError) as error:
        typer.echo(f"run-workflow-campaign failed: {error}", err=True)
        typer.echo("automatic retry/fallback: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"campaign: {outcome.campaign_path}")
    typer.echo(f"attempted runs: {outcome.attempted_run_count}")
    typer.echo(f"Provider calls: {outcome.provider_call_count}")
    typer.echo(
        "Provider call count unknown runs: "
        f"{outcome.provider_call_count_unknown_runs}"
    )
    typer.echo(f"stop reason: {outcome.stop_reason.value}")
    typer.echo("automatic retry/fallback: 0")
    if outcome.stop_reason is not CampaignStopReason.NONE:
        raise typer.Exit(code=1)


@app.command("report-workflow")
def report_workflow_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    plan_path: Annotated[
        Path,
        typer.Option("--plan", exists=True, dir_okay=False),
    ],
    campaign_path: Annotated[
        Path,
        typer.Option("--campaign", exists=True, dir_okay=False),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Create-only strict aggregate JSON."),
    ],
    markdown_path: Annotated[
        Path,
        typer.Option("--markdown", help="Create-only Markdown report."),
    ],
) -> None:
    """Aggregate only saved Plan, Campaign, Recording, and Evidence Artifacts."""
    try:
        report = create_workflow_report(
            spec_path,
            plan_path,
            campaign_path,
            output_path,
            markdown_path,
        )
    except WorkflowReportError as error:
        typer.echo(f"report-workflow failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"experiment: {report.experiment_id}")
    typer.echo(f"pairing: {report.pairing.status.value}")
    typer.echo(f"JSON report: {output_path}")
    typer.echo(f"Markdown report: {markdown_path}")
    typer.echo("Provider, subprocess, network, and quality Gate executed: no")


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


@app.command("live-codex")
def live_codex_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    task_id: Annotated[
        str,
        typer.Option("--task-id", help="Task ID from ExperimentSpec.task_ids."),
    ],
    repetition_index: Annotated[
        int,
        typer.Option("--repetition-index", help="Zero-based repetition index."),
    ],
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="Identifier for Recording and Evidence."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Required destination for redacted Live Evidence."),
    ],
    confirm_live_codex: Annotated[
        bool,
        typer.Option(
            "--confirm-live-codex",
            help="Explicitly allow external AI data transmission and quota consumption.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Explicitly replace existing Recording and Evidence outputs.",
        ),
    ] = False,
) -> None:
    """Run one manually confirmed Codex CLI vertical slice."""
    if confirm_live_codex:
        typer.echo(
            "WARNING: external AI execution, data transmission, and quota consumption will occur."
        )
    try:
        outcome = run_live_codex(
            spec_path,
            task_id=task_id,
            repetition_index=repetition_index,
            run_id=run_id,
            output_path=output_path,
            confirm_live_codex=confirm_live_codex,
            force=force,
        )
    except (SpecLoadError, LiveCodexError) as error:
        typer.echo(f"live-codex failed: {error}", err=True)
        typer.echo(f"run: {run_id}")
        typer.echo(f"task: {task_id}")
        typer.echo(f"evidence output: {output_path}")
        if isinstance(error, LiveCodexError) and error.workspace_lifecycle is not None:
            typer.echo(f"workspace lifecycle: {error.workspace_lifecycle.value}")
        else:
            typer.echo("workspace removed: not_created")
        typer.echo("raw Prompt persisted: no")
        typer.echo("raw Codex JSONL persisted: no")
        raise typer.Exit(code=2) from error

    artifact = outcome.artifact
    typer.echo(f"run: {artifact.run_id}")
    typer.echo(f"experiment: {artifact.experiment_id}")
    typer.echo(f"task: {artifact.task_id}")
    typer.echo(f"status: {artifact.overall_status.value}")
    typer.echo(f"failure kind: {artifact.failure_kind.value}")
    if artifact.codex.provider_failure_hint is not None:
        typer.echo(f"provider failure hint: {artifact.codex.provider_failure_hint.value}")
    typer.echo(f"recording output: {outcome.recording_path}")
    typer.echo(f"evidence output: {outcome.output_path}")
    typer.echo(f"workspace lifecycle: {artifact.workspace_lifecycle.value}")
    typer.echo("raw Prompt persisted: no")
    typer.echo("raw Codex JSONL persisted: no")
    if artifact.overall_status is LiveOverallStatus.FAILED:
        raise typer.Exit(code=1)
    if artifact.overall_status in {
        LiveOverallStatus.PROVIDER_ERROR,
        LiveOverallStatus.HARNESS_ERROR,
    }:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
