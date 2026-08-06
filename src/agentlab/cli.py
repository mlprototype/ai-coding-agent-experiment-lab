"""Command-line interface for Replay, Live runs, and Workflow experiments."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from agentlab.campaign import CampaignError, CampaignStopReason, run_workflow_campaign
from agentlab.capabilities import doctor_report
from agentlab.gates import RunGatesError, run_gates
from agentlab.live import LiveCodexError, run_live_codex
from agentlab.models import EvidenceOverallStatus, LiveOverallStatus
from agentlab.phase6 import Language, Phase6ContractError
from agentlab.phase6_approval import (
    SupplementalApprovalError,
    prepare_supplemental_live_campaign_approval,
)
from agentlab.phase6_campaign import (
    Phase6CampaignError,
    prepare_phase6_campaign,
    run_phase6_campaign,
)
from agentlab.phase6_fixtures import (
    FixtureAcceptanceError,
    accept_phase6_fixtures,
)
from agentlab.phase6_public import (
    Phase6PublicError,
    publish_public_suite,
    verify_phase6_historical,
)
from agentlab.phase7_inventory import (
    MAX_REQUEST_BYTES,
    InventoryContractError,
    InventoryPublicationError,
    InventorySafetyError,
    VerificationStatus,
    create_inventory_publication,
    publish_inventory_request_bytes,
)
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


@app.command("prepare-phase6-supplemental-approval")
def prepare_phase6_supplemental_approval_command(
    spec_path: Annotated[Path, typer.Argument()],
    approval_id: Annotated[str, typer.Option("--approval-id")],
    plan_path: Annotated[Path, typer.Option("--plan")],
    campaign_path: Annotated[Path, typer.Option("--campaign")],
    accepted_manifest_path: Annotated[
        Path,
        typer.Option("--accepted-manifest"),
    ],
    prior_provider_call_minimum: Annotated[
        int,
        typer.Option("--prior-provider-call-min"),
    ],
    prior_provider_call_maximum: Annotated[
        int,
        typer.Option("--prior-provider-call-max"),
    ],
    output_path: Annotated[Path, typer.Option("--output")],
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    confirm_local_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-local-execution",
            help=(
                "Create one pending, create-only offline packet. This is not Live "
                "approval, performs no Provider call, and requires separate human approval."
            ),
        ),
    ] = False,
) -> None:
    """Prepare a pending Java Live control-plane packet without executing Live work."""
    if not confirm_local_execution:
        typer.echo(
            "prepare-phase6-supplemental-approval stopped: "
            "--confirm-local-execution is required",
            err=True,
        )
        typer.echo("files created: 0")
        typer.echo("Provider calls, Prompt transmissions, Gates, and Campaigns: 0")
        raise typer.Exit(code=2)
    try:
        publication = prepare_supplemental_live_campaign_approval(
            repository_root=repository_root,
            approval_id=approval_id,
            spec_path=spec_path,
            plan_path=plan_path,
            campaign_path=campaign_path,
            accepted_manifest_path=accepted_manifest_path,
            prior_provider_call_minimum=prior_provider_call_minimum,
            prior_provider_call_maximum=prior_provider_call_maximum,
            output_path=output_path,
            confirm_local_execution=True,
        )
    except SupplementalApprovalError as error:
        typer.echo(f"prepare-phase6-supplemental-approval stopped: {error}", err=True)
        typer.echo("Provider calls, Prompt transmissions, Gates, and Campaigns: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"pending Supplemental Approval: {publication.output_path}")
    typer.echo(f"bytes: {publication.byte_count}")
    typer.echo(f"SHA-256: {publication.sha256}")
    typer.echo("Live approval granted: no; separate human approval is required")
    typer.echo("Provider calls, Prompt transmissions, Gates, and Campaigns: 0")


@app.command("verify-phase6-historical")
def verify_phase6_historical_command(
    repository_root: Annotated[Path, typer.Option("--repository-root")],
    historical_root: Annotated[Path, typer.Option("--historical-root")],
    reviewed_spec_path: Annotated[str, typer.Option("--reviewed-spec")],
    plan_path: Annotated[str, typer.Option("--plan")],
    campaign_path: Annotated[str, typer.Option("--campaign")],
    report_json_path: Annotated[str, typer.Option("--report-json")],
    report_markdown_path: Annotated[str, typer.Option("--report-markdown")],
    output_path: Annotated[Path, typer.Option("--output")],
    language: Annotated[Language, typer.Option("--language")],
    confirm_local_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-local-execution",
            help="Allow only bounded read-only Git verification; never rerun a Campaign.",
        ),
    ] = False,
) -> None:
    """Create one Historical Verification Record from saved Artifacts only."""
    if not confirm_local_execution:
        typer.echo(
            "verify-phase6-historical stopped: "
            "--confirm-local-execution is required",
            err=True,
        )
        typer.echo("subprocesses executed: 0")
        typer.echo("files or directories created: 0")
        typer.echo("Provider calls, Prompt transmissions, and Gate executions: 0")
        raise typer.Exit(code=2)
    try:
        result = verify_phase6_historical(
            repository=repository_root,
            historical_root=historical_root,
            reviewed_spec_path=reviewed_spec_path,
            plan_path=plan_path,
            campaign_path=campaign_path,
            report_json_path=report_json_path,
            report_markdown_path=report_markdown_path,
            output_path=output_path,
            language=language,
            confirm_local_execution=True,
        )
    except Phase6PublicError as error:
        typer.echo(f"verify-phase6-historical stopped: {error}", err=True)
        typer.echo("Provider calls, Prompt transmissions, and Gate executions: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"Historical Verification Record: {result.output_path}")
    typer.echo(f"source reviewed commit: {result.record.source_reviewed_commit}")
    typer.echo(f"verification commit: {result.record.verification_agentlab_commit}")
    typer.echo("Artifact regeneration and Campaign reexecution: 0")
    typer.echo("Provider calls, Prompt transmissions, and Gate executions: 0")


@app.command("publish-phase6-public-suite")
def publish_phase6_public_suite_command(
    manifest_path: Annotated[Path, typer.Argument()],
    root: Annotated[Path, typer.Option("--root")],
    destination: Annotated[Path, typer.Option("--destination")],
    external_anchor_path: Annotated[
        Path,
        typer.Option("--external-anchor"),
    ],
    confirm_publication: Annotated[
        bool,
        typer.Option(
            "--confirm-publication",
            help="Create one immutable offline bundle and checksum anchor.",
        ),
    ] = False,
) -> None:
    """Publish a deterministic Phase 6 public bundle from listed inputs only."""
    if not confirm_publication:
        typer.echo(
            "publish-phase6-public-suite stopped: --confirm-publication is required",
            err=True,
        )
        typer.echo("files or directories created: 0")
        typer.echo("subprocesses, Provider calls, Prompt transmissions, and Gates: 0")
        raise typer.Exit(code=2)
    try:
        result = publish_public_suite(
            manifest_path=manifest_path,
            root=root,
            destination=destination,
            external_anchor_path=external_anchor_path,
            confirm_publication=True,
        )
    except (Phase6PublicError, Phase6ContractError) as error:
        typer.echo(f"publish-phase6-public-suite stopped: {error}", err=True)
        typer.echo("subprocesses, Provider calls, Prompt transmissions, and Gates: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"Public Suite bundle: {result.destination}")
    typer.echo(f"External checksum anchor: {result.external_anchor_path}")
    typer.echo(f"checksums.json SHA-256: {result.checksum_manifest_sha256}")
    typer.echo(f"published files: {result.published_file_count}")
    typer.echo("subprocesses, Provider calls, Prompt transmissions, and Gates: 0")


@app.command("prepare-phase6-campaign")
def prepare_phase6_campaign_command(
    language: Annotated[Language, typer.Option("--language")],
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    acceptance_root: Annotated[Path, typer.Option("--acceptance-root")] = Path(
        ".artifacts/phase6/fixture-acceptance"
    ),
    output_root: Annotated[Path, typer.Option("--output-root")] = Path(
        ".artifacts/phase6/campaign-preparation"
    ),
    confirm_local_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-local-execution",
            help="Allow fixed local Git/toolchain checks; never calls a Provider.",
        ),
    ] = False,
) -> None:
    """Create one strict Phase 6 Spec 2.1 and Plan 1.2 offline."""
    if not confirm_local_execution:
        typer.echo(
            "prepare-phase6-campaign stopped: --confirm-local-execution is required", err=True
        )
        typer.echo("local subprocesses executed: 0")
        typer.echo("Provider calls and Prompt transmissions: 0")
        raise typer.Exit(code=2)
    try:
        outcome = prepare_phase6_campaign(
            repository_root,
            acceptance_root,
            output_root,
            language=language,
            confirm_local_execution=True,
        )
    except (Phase6CampaignError, FixtureAcceptanceError) as error:
        typer.echo(f"prepare-phase6-campaign stopped: {error}", err=True)
        typer.echo("Provider calls and Prompt transmissions: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"reviewed commit: {outcome.reviewed_commit}")
    typer.echo(f"Spec 2.1: {outcome.spec_path}")
    typer.echo(f"Plan 1.2: {outcome.plan_path}")
    typer.echo(f"planned Provider calls: {outcome.plan.planned_provider_call_count}")
    typer.echo("Provider calls and Prompt transmissions: 0")


@app.command("run-phase6-campaign")
def run_phase6_campaign_command(
    spec_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    plan_path: Annotated[Path, typer.Option("--plan", exists=True, dir_okay=False)],
    campaign_path: Annotated[Path, typer.Option("--campaign")],
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    confirm_live_codex: Annotated[bool, typer.Option("--confirm-live-codex")] = False,
    confirm_provider_calls: Annotated[int | None, typer.Option("--confirm-provider-calls")] = None,
) -> None:
    """Run a preregistered Phase 6 Campaign; never retry or resume."""
    if not confirm_live_codex:
        typer.echo("run-phase6-campaign stopped: --confirm-live-codex is required", err=True)
        typer.echo("subprocesses and Provider calls executed: 0")
        raise typer.Exit(code=2)
    try:
        outcome = run_phase6_campaign(
            repository_root,
            spec_path,
            plan_path,
            campaign_path,
            confirm_live_codex=True,
            confirm_provider_calls=confirm_provider_calls,
        )
    except Phase6CampaignError as error:
        typer.echo(f"run-phase6-campaign stopped: {error}", err=True)
        typer.echo("automatic retry/fallback/resume: 0")
        raise typer.Exit(code=2) from error
    typer.echo(f"campaign: {outcome.campaign_path}")
    typer.echo(f"Provider calls: {outcome.provider_call_count}")
    typer.echo(f"stop reason: {outcome.stop_reason.value}")
    typer.echo("automatic retry/fallback/resume: 0")


@app.command("accept-phase6-fixtures")
def accept_phase6_fixtures_command(
    language: Annotated[
        list[Language] | None,
        typer.Option(
            "--language",
            metavar="LANGUAGE",
            help=(
                "Accept exactly one selected language. Omit for the existing "
                "python, typescript, java sequence; duplicate selection is rejected."
            ),
        ),
    ] = None,
    repository_root: Annotated[
        Path,
        typer.Option(
            "--repository-root",
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Clean repository containing the committed Phase 6 Fixtures.",
        ),
    ] = Path("."),
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help=(
                "Create-only root below .artifacts/phase6/fixture-acceptance; "
                "language subdirectories are preserved. Default: "
                ".artifacts/phase6/fixture-acceptance."
            ),
        ),
    ] = Path(".artifacts/phase6/fixture-acceptance"),
    confirm_local_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-local-execution",
            help=(
                "Explicitly allow bounded local toolchain version commands and "
                "trusted Fixture Gate subprocesses; no Provider is called."
            ),
        ),
    ] = False,
) -> None:
    """Audit local toolchains and accept independent Phase 6 Fixtures offline."""
    if not confirm_local_execution:
        typer.echo(
            "accept-phase6-fixtures stopped: --confirm-local-execution is required",
            err=True,
        )
        typer.echo("local subprocesses executed: 0")
        typer.echo("Provider calls, Prompt transmissions, network, and quota: 0")
        raise typer.Exit(code=2)
    if language is not None and len(language) != 1:
        typer.echo(
            "accept-phase6-fixtures stopped: --language must be supplied at most once",
            err=True,
        )
        typer.echo("local subprocesses executed: 0")
        typer.echo("Provider calls, Prompt transmissions, network, and quota: 0")
        raise typer.Exit(code=2)
    try:
        outcome = accept_phase6_fixtures(
            repository_root,
            output_root,
            confirm_local_execution=confirm_local_execution,
            language=language[0] if language else None,
        )
    except FixtureAcceptanceError as error:
        typer.echo(f"accept-phase6-fixtures stopped: {error}", err=True)
        typer.echo("Provider calls, Prompt transmissions, network, and quota: 0")
        raise typer.Exit(code=2) from error

    typer.echo(f"reviewed commit: {outcome.commit_sha}")
    for result in outcome.results:
        typer.echo(f"{result.language.value}: {result.status}")
        if result.status == "accepted":
            typer.echo(f"  manifest: {result.manifest_path}")
            typer.echo(f"  acceptance: {result.acceptance_path}")
        else:
            typer.echo(f"  blocker: {result.blocker}")
            typer.echo(f"  detail: {result.detail}")
    typer.echo(f"engineering minimum met: {outcome.engineering_minimum_met}")
    typer.echo(f"full target met: {outcome.full_target_met}")
    typer.echo("Provider calls, Prompt transmissions, network, and quota: 0")
    succeeded = (
        all(result.status == "accepted" for result in outcome.results)
        if language
        else outcome.engineering_minimum_met
    )
    if not succeeded:
        raise typer.Exit(code=1)


@app.command()
def inventory_phase6_evidence(
    request_path: Annotated[Path, typer.Argument()],
    output_path: Annotated[Path, typer.Option("--output")],
    markdown_path: Annotated[Path, typer.Option("--markdown")],
    metadata_path: Annotated[Path, typer.Option("--metadata")],
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    confirm_local_execution: Annotated[
        bool,
        typer.Option(
            "--confirm-local-execution",
            help="Allow bounded read-only Artifact checks and one Git checkout HEAD observation.",
        ),
    ] = False,
) -> None:
    """Create one Phase 6 Evidence Inventory from a reviewed Request."""
    if not confirm_local_execution:
        typer.echo(
            "inventory-phase6-evidence stopped: --confirm-local-execution is required",
            err=True,
        )
        typer.echo("new complete publication: 0; existing or partial outputs may remain unchanged")
        typer.echo("Provider, Prompt, Gate, Campaign, Report, Public Suite, and network: 0")
        raise typer.Exit(code=1)
    try:
        publication = create_inventory_publication(
            request_path=request_path,
            repository_root=repository_root,
            output_path=output_path,
            markdown_path=markdown_path,
            metadata_path=metadata_path,
            confirm_local_execution=True,
        )
    except (InventoryContractError, InventoryPublicationError, InventorySafetyError) as error:
        typer.echo(f"inventory-phase6-evidence stopped: {error}", err=True)
        typer.echo("new complete publication: 0; existing or partial outputs may remain unchanged")
        typer.echo("Provider, Prompt, Gate, Campaign, Report, Public Suite, and network: 0")
        raise typer.Exit(code=1) from error
    typer.echo(f"Inventory: {publication.output_path}")
    typer.echo(f"Markdown: {publication.markdown_path}")
    typer.echo(f"metadata: {publication.metadata_path}")
    typer.echo(f"request correlation ID: {publication.inventory.request_correlation_id}")
    typer.echo(
        f"observed execution repository HEAD: "
        f"{publication.metadata.observed_execution_repository_head}"
    )
    typer.echo(f"verification status: {publication.inventory.verification_status.value}")
    typer.echo("Provider, Prompt, Gate, Campaign, Report, Public Suite, and network: 0")
    if publication.inventory.verification_status is VerificationStatus.FAILED:
        raise typer.Exit(code=2)


@app.command("publish-phase6-evidence-inventory-request")
def publish_phase6_evidence_inventory_request(
    expected_request_sha256: Annotated[
        str,
        typer.Option("--expected-request-sha256"),
    ],
    repository_root: Annotated[Path, typer.Option("--repository-root")] = Path("."),
    confirm_local_write: Annotated[
        bool,
        typer.Option(
            "--confirm-local-write",
            help="Create one SHA-bound reviewed Request below the fixed Phase 7 evidence root.",
        ),
    ] = False,
) -> None:
    """Read canonical Request bytes from stdin and publish them create-only."""
    request_bytes = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        publication = publish_inventory_request_bytes(
            request_bytes,
            repository_root,
            expected_request_sha256=expected_request_sha256,
            confirm_local_write=confirm_local_write,
        )
    except (InventoryContractError, InventoryPublicationError, InventorySafetyError) as error:
        typer.echo(f"publish-phase6-evidence-inventory-request stopped: {error}", err=True)
        typer.echo("new Request publication: 0; existing leaves were not reused")
        typer.echo("Provider, Prompt, Gate, Campaign, Report, Public Suite, and network: 0")
        raise typer.Exit(code=1) from error
    typer.echo(f"Request: {publication.request_path}")
    typer.echo(f"request SHA-256: {publication.request_sha256}")
    typer.echo("Provider, Prompt, Gate, Campaign, Report, Public Suite, and network: 0")


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
    typer.echo(f"Provider call count unknown runs: {outcome.provider_call_count_unknown_runs}")
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
