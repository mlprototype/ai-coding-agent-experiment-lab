from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentlab.campaign import (
    AdapterCleanupState,
    CampaignError,
    CampaignOutcome,
    CampaignRunExecution,
    CampaignRunStatus,
    CampaignStopReason,
    load_campaign,
    run_workflow_campaign,
)
from agentlab.models import (
    CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS,
    LiveFailureKind,
    Workflow,
)
from agentlab.workflow import (
    WorkflowPlanError,
    build_workflow_plan,
    build_workflow_prompt,
    create_workflow_plan,
    load_plan_publication,
    load_workflow_plan,
    workflow_plan_bytes,
)
from agentlab.workflow_report import (
    Estimability,
    WorkflowReportError,
    aggregate_workflow_campaign,
    create_workflow_report,
    load_workflow_report,
)


def _case(tmp_path: Path, **updates: Any) -> Path:
    case = tmp_path / "case"
    (case / "fixtures").mkdir(parents=True)
    shutil.copytree(
        Path("experiments/examples/fixtures/codex-live-smoke"),
        case / "fixtures" / "task",
    )
    (case / "prompts").mkdir()
    shutil.copy(
        Path("experiments/examples/prompts/codex-live-smoke.md"),
        case / "prompts" / "task.md",
    )
    raw = yaml.safe_load(Path("experiments/examples/workflow-ab.yaml").read_text(encoding="utf-8"))
    raw["task_ids"] = ["task-1"]
    raw["repetitions"] = 2
    raw["task_prompt_path"] = "prompts/task.md"
    raw["runner"]["fixture_path"] = "fixtures/task"
    raw["artifacts"]["root"] = "artifacts"
    for key, value in updates.items():
        if key.startswith("stop_"):
            raw["stop_conditions"][key.removeprefix("stop_")] = value
        else:
            raw[key] = value
    spec_path = case / "experiment.yaml"
    spec_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return spec_path


def _plan_file(spec_path: Path) -> Path:
    plan_path = spec_path.parent / "plan.json"
    create_workflow_plan(spec_path, plan_path)
    return plan_path


def test_plan_is_deterministic_blocked_and_unique(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    first = build_workflow_plan(spec_path)
    second = build_workflow_plan(spec_path)

    assert workflow_plan_bytes(first) == workflow_plan_bytes(second)
    assert first.schema_version == "1.1"
    assert first.planned_run_count == 4
    assert first.one_shot_prompt_sha256 != first.staged_prompt_sha256
    assert first.one_shot_prompt_bytes > 0
    assert first.staged_prompt_bytes > 0
    assert len({run.run_id for run in first.runs}) == 4
    paths = {
        path
        for run in first.runs
        for path in (run.recording_path, run.evidence_path, run.diagnostic_path)
    }
    assert len(paths) == 12
    for index in range(0, len(first.runs), 2):
        block = first.runs[index : index + 2]
        assert {(run.task_id, run.repetition_index) for run in block} == {
            (block[0].task_id, block[0].repetition_index)
        }
        assert {run.workflow for run in block} == {
            Workflow.ONE_SHOT,
            Workflow.STAGED,
        }


def test_prompt_changes_only_workflow_suffix(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    spec = build_workflow_plan(spec_path)
    loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    from agentlab.workflow import WorkflowExperimentSpec

    model = WorkflowExperimentSpec.model_validate(loaded)
    one_shot = build_workflow_prompt(spec_path, model, Workflow.ONE_SHOT)
    staged = build_workflow_prompt(spec_path, model, Workflow.STAGED)
    task = (spec_path.parent / "prompts/task.md").read_bytes()

    assert one_shot.task_sha256 == staged.task_sha256 == spec.task_prompt_sha256
    assert one_shot.content.startswith(task)
    assert staged.content.startswith(task)
    assert one_shot.content != staged.content
    assert b"1. Investigate" not in one_shot.content
    assert b"1. Investigate" in staged.content
    assert b"single Provider turn" in staged.content


def test_plan_is_create_only_and_strict(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    canonical = plan_path.read_bytes()
    publication = load_plan_publication(plan_path)
    assert publication.plan_sha256 == hashlib.sha256(canonical).hexdigest()

    with pytest.raises(WorkflowPlanError):
        create_workflow_plan(spec_path, plan_path)
    assert plan_path.read_bytes() == canonical

    raw = json.loads(canonical)
    raw["unknown"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(WorkflowPlanError):
        load_workflow_plan(invalid)


def test_confirmation_is_required_before_executor(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    calls = 0

    def executor(*_args: object) -> CampaignRunExecution:
        nonlocal calls
        calls += 1
        return CampaignRunExecution(CampaignOutcome.SUCCESS, 1, LiveFailureKind.NONE)

    with pytest.raises(CampaignError, match="no subprocess"):
        run_workflow_campaign(
            spec_path,
            plan_path,
            spec_path.parent / "campaign.jsonl",
            confirm_live_codex=False,
            confirm_provider_calls=None,
            run_executor=executor,
        )
    assert calls == 0


def test_scheduler_follows_plan_sequentially_without_retry(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)
    observed: list[str] = []

    def executor(
        _spec_path: object,
        _loaded: object,
        run: object,
        _prompt: object,
        _environment: object,
    ) -> CampaignRunExecution:
        observed.append(run.run_id)
        return CampaignRunExecution(CampaignOutcome.SUCCESS, 1, LiveFailureKind.NONE)

    campaign_path = spec_path.parent / "campaign.jsonl"
    result = run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
    )
    events = load_campaign(campaign_path)

    assert observed == [run.run_id for run in plan.runs]
    assert result.provider_call_count == len(plan.runs)
    assert all(getattr(event, "retry_count", 0) == 0 for event in events)


@pytest.mark.parametrize("changed_input", ["prompt", "fixture"])
def test_campaign_stops_before_next_call_when_fixed_input_changes(
    tmp_path: Path,
    changed_input: str,
) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)
    calls = 0

    def executor(
        _spec_path: object,
        _loaded: object,
        _run: object,
        fixed: object,
        _environment: object,
    ) -> CampaignRunExecution:
        nonlocal calls
        calls += 1
        assert fixed.fixture.sha256 == plan.fixture_sha256
        if changed_input == "prompt":
            (spec_path.parent / "prompts/task.md").write_text(
                "changed after Campaign start\n",
                encoding="utf-8",
            )
        else:
            (spec_path.parent / "fixtures/task/task.txt").write_text(
                "status=CHANGED\n",
                encoding="utf-8",
            )
        return CampaignRunExecution(
            CampaignOutcome.SUCCESS,
            1,
            LiveFailureKind.NONE,
        )

    campaign_path = spec_path.parent / "campaign.jsonl"
    result = run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
    )
    terminal = [
        event
        for event in load_campaign(campaign_path)
        if hasattr(event, "status") and event.status is not CampaignRunStatus.STARTED
    ]

    assert calls == 1
    assert result.stop_reason is CampaignStopReason.INPUT_CHANGED
    assert [event.status for event in terminal] == [
        CampaignRunStatus.COMPLETED,
        CampaignRunStatus.NOT_RUN,
        CampaignRunStatus.NOT_RUN,
        CampaignRunStatus.NOT_RUN,
    ]
    assert all(
        event.stop_reason is CampaignStopReason.INPUT_CHANGED
        for event in terminal[1:]
    )


def test_campaign_strict_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)
    campaign_path = spec_path.parent / "campaign.jsonl"

    def executor(*_args: object) -> CampaignRunExecution:
        return CampaignRunExecution(CampaignOutcome.SUCCESS, 1, LiveFailureKind.NONE)

    run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
    )
    lines = campaign_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["unknown"] = True
    lines[0] = json.dumps(first)
    campaign_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="invalid Campaign event"):
        load_campaign(campaign_path)


@pytest.mark.skipif(os.name != "posix", reason="symlink test is POSIX-only")
def test_campaign_rejects_symlinked_artifact_root_before_executor(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)
    target = spec_path.parent / "artifact-target"
    target.mkdir()
    (spec_path.parent / "artifacts").symlink_to(target, target_is_directory=True)
    calls = 0

    def executor(*_args: object) -> CampaignRunExecution:
        nonlocal calls
        calls += 1
        return CampaignRunExecution(CampaignOutcome.SUCCESS, 1, LiveFailureKind.NONE)

    with pytest.raises(CampaignError, match="symlinks"):
        run_workflow_campaign(
            spec_path,
            plan_path,
            spec_path.parent / "campaign.jsonl",
            confirm_live_codex=True,
            confirm_provider_calls=plan.planned_provider_call_count,
            run_executor=executor,
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("outcome", "expected_stop"),
    [
        (CampaignOutcome.QUALITY_GATE_FAILURE, CampaignStopReason.FAIL_FAST),
        (CampaignOutcome.PROVIDER_FAILURE, CampaignStopReason.FAIL_FAST),
        (CampaignOutcome.HARNESS_FAILURE, CampaignStopReason.HARNESS_FAILURE),
        (CampaignOutcome.CLEANUP_FAILURE, CampaignStopReason.CLEANUP_FAILURE),
    ],
)
def test_scheduler_stops_and_retains_not_run(
    tmp_path: Path,
    outcome: CampaignOutcome,
    expected_stop: CampaignStopReason,
) -> None:
    spec_path = _case(tmp_path, stop_fail_fast=True)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)

    def executor(*_args: object) -> CampaignRunExecution:
        return CampaignRunExecution(outcome, 1, LiveFailureKind.EVIDENCE_ERROR)

    campaign_path = spec_path.parent / "campaign.jsonl"
    result = run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
    )
    terminal = [
        event
        for event in load_campaign(campaign_path)
        if getattr(event, "status", None) is not CampaignRunStatus.STARTED
        and hasattr(event, "status")
    ]

    assert result.stop_reason is expected_stop
    assert sum(event.status is CampaignRunStatus.NOT_RUN for event in terminal) == 3
    assert all(
        event.stop_reason is expected_stop
        for event in terminal
        if event.status is CampaignRunStatus.NOT_RUN
    )


def test_max_failures_and_duration_stop_before_next_run(tmp_path: Path) -> None:
    spec_path = _case(
        tmp_path,
        stop_fail_fast=False,
        stop_max_failures=2,
        stop_max_total_duration_ms=1,
    )
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)
    clock = iter([0.0, 0.0, 0.002])

    def executor(*_args: object) -> CampaignRunExecution:
        return CampaignRunExecution(
            CampaignOutcome.QUALITY_GATE_FAILURE,
            1,
            LiveFailureKind.QUALITY_GATE_FAILURE,
        )

    result = run_workflow_campaign(
        spec_path,
        plan_path,
        spec_path.parent / "campaign.jsonl",
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
        monotonic=lambda: next(clock),
    )
    assert result.stop_reason is CampaignStopReason.MAX_TOTAL_DURATION
    assert result.attempted_run_count == 1


def test_max_failures_counts_only_quality_and_provider_failures(tmp_path: Path) -> None:
    spec_path = _case(
        tmp_path,
        stop_fail_fast=False,
        stop_max_failures=2,
        stop_max_total_duration_ms=100000,
    )
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)

    def executor(*_args: object) -> CampaignRunExecution:
        return CampaignRunExecution(
            CampaignOutcome.PROVIDER_TIMEOUT,
            1,
            LiveFailureKind.PROVIDER_TIMEOUT,
        )

    result = run_workflow_campaign(
        spec_path,
        plan_path,
        spec_path.parent / "campaign.jsonl",
        confirm_live_codex=True,
        confirm_provider_calls=plan.planned_provider_call_count,
        run_executor=executor,
    )
    assert result.stop_reason is CampaignStopReason.MAX_FAILURES
    assert result.attempted_run_count == 2


def _fake_codex_environment(
    tmp_path: Path,
    *,
    usage: bool = True,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.jsonl"
    supported = next(iter(CODEX_EXPLICIT_NEVER_V2_CLI_VERSIONS))
    script = fake_bin / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib,json,os,pathlib,sys\n"
        f"log=pathlib.Path({str(log)!r})\n"
        f"supported={supported!r}\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print(supported)\n"
        "elif sys.argv[1:] == ['exec','--help']:\n"
        "    print('--config --ephemeral --ignore-rules --ignore-user-config --json "
        "--model --sandbox --skip-git-repo-check --strict-config')\n"
        "else:\n"
        "    prompt=sys.stdin.buffer.read()\n"
        "    with log.open('a',encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps({'prompt_sha256':hashlib.sha256(prompt).hexdigest(),"
        "'cwd':os.getcwd()})+'\\n')\n"
        "    pathlib.Path('task.txt').write_text('status=COMPLETE\\n',encoding='utf-8')\n"
        "    print(json.dumps({'type':'thread.started','thread_id':'discarded'}),flush=True)\n"
        "    print(json.dumps({'type':'turn.started'}),flush=True)\n"
        + (
            "    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,"
            "'cached_input_tokens':1,'output_tokens':3,'reasoning_output_tokens':1}}),"
            "flush=True)\n"
            if usage
            else "    print(json.dumps({'type':'turn.completed'}),flush=True)\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "parent-home"),
        "CODEX_HOME": str(codex_home),
    }
    return environment, log


def _run_fake_campaign(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    spec_path = _case(tmp_path, repetitions=1)
    plan_path = _plan_file(spec_path)
    environment, call_log = _fake_codex_environment(tmp_path)
    campaign_path = spec_path.parent / "campaign.jsonl"
    run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        parent_environment=environment,
    )
    return spec_path, plan_path, campaign_path, call_log


@pytest.mark.skipif(os.name != "posix", reason="Codex runner is POSIX-only")
def test_fake_campaign_has_one_call_per_run_independent_workspaces_and_offline_report(
    tmp_path: Path,
) -> None:
    spec_path, plan_path, campaign_path, call_log = _run_fake_campaign(tmp_path)
    events = load_campaign(campaign_path)
    outcome = events[-1]
    calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]

    assert outcome.stop_reason is CampaignStopReason.NONE
    assert outcome.provider_call_count == 2
    assert len(calls) == 2
    assert len({call["cwd"] for call in calls}) == 2
    assert len({call["prompt_sha256"] for call in calls}) == 2
    assert all(not Path(call["cwd"]).exists() for call in calls)

    report = aggregate_workflow_campaign(spec_path, plan_path, campaign_path)
    assert report.pairing.status is Estimability.ESTIMABLE
    assert [item.agent_call_count.total for item in report.workflows] == [1, 1]
    assert [item.retry_count.total for item in report.workflows] == [0, 0]
    assert [item.usage.usage_missing_runs for item in report.workflows] == [0, 0]

    report_path = spec_path.parent / "artifacts/report.json"
    markdown_path = spec_path.parent / "artifacts/report.md"
    created = create_workflow_report(
        spec_path,
        plan_path,
        campaign_path,
        report_path,
        markdown_path,
    )
    report_json = json.loads(report_path.read_text(encoding="utf-8"))
    assert load_workflow_report(report_path) == created
    markdown = markdown_path.read_text(encoding="utf-8")
    assert report_json["pairing"]["complete_pair_count"] == created.pairing.complete_pair_count
    for item in created.workflows:
        assert f"| {item.workflow.value} | {item.scheduled_runs} |" in markdown
    persisted = report_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in persisted
    assert "Edit only `task.txt`" not in persisted
    persisted_artifacts = [
        plan_path,
        campaign_path,
        *[
            spec_path.parent / Path(path)
            for run in load_workflow_plan(plan_path).runs
            for path in (run.recording_path, run.evidence_path)
        ],
    ]
    for path in persisted_artifacts:
        content = path.read_text(encoding="utf-8")
        assert str(tmp_path) not in content
        assert "Edit only `task.txt`" not in content


@pytest.mark.skipif(os.name != "posix", reason="Codex runner is POSIX-only")
def test_adapter_cleanup_failure_is_explicit_and_redacts_prompt(
    tmp_path: Path,
) -> None:
    spec_path = _case(tmp_path, repetitions=1)
    plan_path = _plan_file(spec_path)
    environment, call_log = _fake_codex_environment(tmp_path)
    campaign_path = spec_path.parent / "campaign.jsonl"
    adapter_roots: list[Path] = []

    def fail_cleanup(path: Path) -> tuple[bool, str | None]:
        adapter_roots.append(path)
        raise OSError("injected cleanup failure")

    try:
        outcome = run_workflow_campaign(
            spec_path,
            plan_path,
            campaign_path,
            confirm_live_codex=True,
            confirm_provider_calls=2,
            parent_environment=environment,
            adapter_cleanup=fail_cleanup,
        )
        events = load_campaign(campaign_path)
        attempted_terminal = next(
            event
            for event in events
            if getattr(event, "status", None) is CampaignRunStatus.FAILED
        )
        calls = call_log.read_text(encoding="utf-8").splitlines()

        assert outcome.stop_reason is CampaignStopReason.CLEANUP_FAILURE
        assert len(calls) == 1
        assert attempted_terminal.outcome is CampaignOutcome.CLEANUP_FAILURE
        assert (
            attempted_terminal.adapter_cleanup_state
            is AdapterCleanupState.FAILED
        )
        assert len(adapter_roots) == 1
        assert not adapter_roots[0].is_relative_to(
            spec_path.parent / "artifacts"
        )
        assert not (adapter_roots[0] / "prompt.md").exists()
        report = aggregate_workflow_campaign(spec_path, plan_path, campaign_path)
        assert report.pairing.status is Estimability.NOT_ESTIMABLE
        assert sum(item.cleanup_failed_runs for item in report.workflows) == 1
    finally:
        for adapter_root in adapter_roots:
            shutil.rmtree(adapter_root, ignore_errors=True)


def _rewrite_campaign(
    campaign_path: Path,
    mutate: Any,
) -> None:
    events = [
        json.loads(line)
        for line in campaign_path.read_text(encoding="utf-8").splitlines()
    ]
    mutate(events)
    campaign_path.write_text(
        "".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(os.name != "posix", reason="Codex runner is POSIX-only")
@pytest.mark.parametrize(
    "contradiction",
    ["outcome", "provider_calls", "run_identity"],
)
def test_report_rejects_campaign_plan_evidence_contradictions(
    tmp_path: Path,
    contradiction: str,
) -> None:
    spec_path, plan_path, campaign_path, _call_log = _run_fake_campaign(tmp_path)

    def mutate(events: list[dict[str, Any]]) -> None:
        started = next(
            event
            for event in events
            if event["event_type"] == "run_state"
            and event["status"] == "started"
        )
        terminal = next(
            event
            for event in events
            if event["event_type"] == "run_state"
            and event["run_id"] == started["run_id"]
            and event["status"] != "started"
        )
        if contradiction == "outcome":
            terminal["outcome"] = "quality_gate_failure"
            terminal["live_failure_kind"] = "quality_gate_failure"
        elif contradiction == "provider_calls":
            terminal["provider_call_count"] = 0
            finished = events[-1]
            finished["provider_call_count"] -= 1
        else:
            started["task_id"] = "different-task"
            terminal["task_id"] = "different-task"

    _rewrite_campaign(campaign_path, mutate)
    with pytest.raises(WorkflowReportError):
        aggregate_workflow_campaign(spec_path, plan_path, campaign_path)


@pytest.mark.skipif(os.name != "posix", reason="Codex runner is POSIX-only")
@pytest.mark.parametrize(
    "contradiction",
    ["evidence_model", "plan_model", "fixture", "prompt"],
)
def test_report_rejects_evidence_recording_or_plan_condition_mismatch(
    tmp_path: Path,
    contradiction: str,
) -> None:
    spec_path, plan_path, campaign_path, _call_log = _run_fake_campaign(tmp_path)
    run = load_workflow_plan(plan_path).runs[0]
    evidence_path = spec_path.parent / run.evidence_path
    recording_path = spec_path.parent / run.recording_path
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if contradiction in {"evidence_model", "plan_model"}:
        evidence["codex"]["requested_model"] = "different-exact-model"
    elif contradiction == "fixture":
        evidence["fixture_sha256"] = "0" * 64
    else:
        evidence["prompt_sha256"] = "0" * 64

    if contradiction in {"plan_model", "prompt"}:
        recording = [
            json.loads(line)
            for line in recording_path.read_text(encoding="utf-8").splitlines()
        ]
        if contradiction == "plan_model":
            recording[0]["requested_model"] = "different-exact-model"
            recording[1]["codex"]["requested_model"] = "different-exact-model"
        else:
            recording[0]["prompt_sha256"] = "0" * 64
        recording_bytes = "".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in recording
        ).encode()
        recording_path.write_bytes(recording_bytes)
        evidence["recording_sha256"] = hashlib.sha256(recording_bytes).hexdigest()

    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowReportError):
        aggregate_workflow_campaign(spec_path, plan_path, campaign_path)


@pytest.mark.skipif(os.name != "posix", reason="Codex runner is POSIX-only")
def test_missing_usage_remains_missing_in_report(tmp_path: Path) -> None:
    spec_path = _case(tmp_path, repetitions=1)
    plan_path = _plan_file(spec_path)
    environment, _call_log = _fake_codex_environment(tmp_path, usage=False)
    campaign_path = spec_path.parent / "campaign.jsonl"
    run_workflow_campaign(
        spec_path,
        plan_path,
        campaign_path,
        confirm_live_codex=True,
        confirm_provider_calls=2,
        parent_environment=environment,
    )

    report = aggregate_workflow_campaign(spec_path, plan_path, campaign_path)
    assert [item.usage.usage_missing_runs for item in report.workflows] == [1, 1]
    assert [
        item.usage.provider_reported.input_tokens.total for item in report.workflows
    ] == [None, None]


def test_interruption_records_remaining_runs_and_reraises(tmp_path: Path) -> None:
    spec_path = _case(tmp_path)
    plan_path = _plan_file(spec_path)
    plan = load_workflow_plan(plan_path)

    def executor(*_args: object) -> CampaignRunExecution:
        raise KeyboardInterrupt

    campaign_path = spec_path.parent / "campaign.jsonl"
    with pytest.raises(KeyboardInterrupt):
        run_workflow_campaign(
            spec_path,
            plan_path,
            campaign_path,
            confirm_live_codex=True,
            confirm_provider_calls=plan.planned_provider_call_count,
            run_executor=executor,
        )
    events = load_campaign(campaign_path)
    statuses = [
        event.status
        for event in events
        if hasattr(event, "status") and event.status is not CampaignRunStatus.STARTED
    ]
    assert statuses == [
        CampaignRunStatus.INTERRUPTED,
        CampaignRunStatus.NOT_RUN,
        CampaignRunStatus.NOT_RUN,
        CampaignRunStatus.NOT_RUN,
    ]
