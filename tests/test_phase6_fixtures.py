from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import agentlab.phase6_fixtures as fixtures
from agentlab.cli import app
from agentlab.models import (
    CommandEvidence,
    CommandStatus,
    GateKind,
    TerminationEvidence,
    TerminationReason,
)
from agentlab.phase6 import (
    DiffPolicy,
    EditablePathPolicy,
    Language,
    ProtectedPathPolicy,
    ToolchainComponent,
    ToolchainComponentRole,
    ToolchainIdentity,
    canonical_json_bytes,
    load_fixture_acceptance,
    load_fixture_manifest,
)
from agentlab.phase6_fixtures import (
    FixtureAcceptanceError,
    FixtureAcceptanceOutcome,
    FixtureDefinition,
    ToolchainCandidates,
    accept_fixture,
    audit_toolchain,
    secure_tree_snapshot,
)
from agentlab.runner import CommandRunResult

FULL_COMMIT = "a" * 40
CLI_RUNNER = CliRunner()


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _version_executable(
    path: Path,
    version: str,
    *,
    stderr: bool = False,
) -> Path:
    stream = "sys.stderr" if stderr else "sys.stdout"
    return _write_executable(
        path,
        f'import sys\n{stream}.write({version!r} + "\\n")',
    )


def _python_candidates(root: Path, version: str = "Python 3.12.9") -> ToolchainCandidates:
    return ToolchainCandidates(python=_version_executable(root / "bin" / "python", version))


def _typescript_candidates(
    root: Path,
    *,
    package_version: str = "5.7.3",
    command_version: str | None = None,
) -> ToolchainCandidates:
    package = root / "typescript"
    (package / "lib").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "typescript", "version": package_version}),
        encoding="utf-8",
    )
    (package / "lib" / "tsc.js").write_text("// compiler\n", encoding="utf-8")
    node = _write_executable(
        root / "node-bin" / "node",
        "import sys\n"
        f"print({'Version ' + package_version!r} "
        "if len(sys.argv) > 1 and sys.argv[1].endswith('tsc.js') "
        "else 'v22.12.0')",
    )
    tsc = _version_executable(
        package / "bin" / "tsc",
        command_version or f"Version {package_version}",
    )
    return ToolchainCandidates(node=node, tsc=tsc)


def _java_candidates(root: Path, *, split_roots: bool = False) -> ToolchainCandidates:
    runtime_root = root / "jdk-a"
    compiler_root = root / ("jdk-b" if split_roots else "jdk-a")
    java = _version_executable(
        runtime_root / "bin" / "java",
        'openjdk version "21.0.6"',
        stderr=True,
    )
    javac = _version_executable(
        compiler_root / "bin" / "javac",
        "javac 21.0.6",
        stderr=True,
    )
    return ToolchainCandidates(java=java, javac=javac)


def test_python_toolchain_capability_audit_uses_exact_measured_version(
    tmp_path: Path,
) -> None:
    identity = audit_toolchain(Language.PYTHON, _python_candidates(tmp_path))

    assert [component.role for component in identity.components] == [
        ToolchainComponentRole.PYTHON_RUNTIME
    ]
    assert identity.components[0].exact_version == "Python 3.12.9"
    assert Path(identity.components[0].version_argv[0]).is_absolute()
    assert identity.workspace_executable_lookup_allowed is False


def test_typescript_capability_audit_binds_node_package_and_compiler_tree(
    tmp_path: Path,
) -> None:
    identity = audit_toolchain(Language.TYPESCRIPT, _typescript_candidates(tmp_path))

    assert [component.role for component in identity.components] == [
        ToolchainComponentRole.NODE_RUNTIME,
        ToolchainComponentRole.TYPESCRIPT_COMPILER,
    ]
    compiler = identity.components[1]
    assert compiler.package_version == "5.7.3"
    assert compiler.package_fingerprint is not None
    assert compiler.exact_version == "Version 5.7.3"


def test_typescript_version_audit_explicitly_uses_recorded_node_and_compiler_js(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _typescript_candidates(tmp_path)
    observed_argv: list[tuple[str, ...]] = []
    original = fixtures._run_version_command

    def record_argv(argv: list[str], **kwargs: Any) -> Any:
        observed_argv.append(tuple(argv))
        return original(argv, **kwargs)

    monkeypatch.setattr(fixtures, "_run_version_command", record_argv)
    audit_toolchain(Language.TYPESCRIPT, candidates)

    assert candidates.node is not None
    assert candidates.tsc is not None
    assert (
        str(candidates.node.resolve()),
        str((tmp_path / "typescript" / "lib" / "tsc.js").resolve()),
        "--version",
    ) in observed_argv


def test_java_capability_audit_requires_and_records_one_jdk(tmp_path: Path) -> None:
    identity = audit_toolchain(Language.JAVA, _java_candidates(tmp_path))

    assert [component.role for component in identity.components] == [
        ToolchainComponentRole.JAVA_COMPILER,
        ToolchainComponentRole.JAVA_RUNTIME,
    ]
    assert identity.components[0].exact_version == "javac 21.0.6"
    assert identity.components[1].exact_version == 'openjdk version "21.0.6"'


def test_gate_contract_uses_audited_compilers_explicitly(tmp_path: Path) -> None:
    typescript = audit_toolchain(
        Language.TYPESCRIPT,
        _typescript_candidates(tmp_path / "typescript-toolchain"),
    )
    typescript_binding = fixtures._capture_toolchain_binding(
        Language.TYPESCRIPT,
        typescript,
    )
    typescript_commands = fixtures._commands_for(
        Language.TYPESCRIPT,
        typescript,
        typescript_binding,
    )
    node = next(
        component.resolved_executable_path
        for component in typescript.components
        if component.role is ToolchainComponentRole.NODE_RUNTIME
    )
    assert typescript_binding.typescript_compiler_js is not None
    for _gate, argv in typescript_commands:
        assert argv[0] == node
        assert argv[3:] == [node, str(typescript_binding.typescript_compiler_js)]

    java = audit_toolchain(
        Language.JAVA,
        _java_candidates(tmp_path / "java-toolchain"),
    )
    java_binding = fixtures._capture_toolchain_binding(Language.JAVA, java)
    java_commands = fixtures._commands_for(Language.JAVA, java, java_binding)
    javac = next(
        component.resolved_executable_path
        for component in java.components
        if component.role is ToolchainComponentRole.JAVA_COMPILER
    )
    assert all(argv[-1] == javac for _gate, argv in java_commands)


def test_workspace_executable_is_rejected_even_when_explicitly_selected(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    candidates = _python_candidates(workspace)

    with pytest.raises(FixtureAcceptanceError, match="outside Workspace"):
        audit_toolchain(
            Language.PYTHON,
            candidates,
            forbidden_roots=(workspace,),
        )


def test_version_command_does_not_inherit_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _write_executable(
        tmp_path / "trusted" / "python",
        'import os\nprint(os.environ["PATH"])',
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'poison'}{os.pathsep}/usr/bin")

    identity = audit_toolchain(
        Language.PYTHON,
        ToolchainCandidates(python=executable),
    )

    assert identity.components[0].exact_version == str(executable.parent)
    assert "poison" not in identity.components[0].exact_version


def test_executable_symlink_is_resolved_to_final_regular_file(tmp_path: Path) -> None:
    target = _version_executable(tmp_path / "real" / "python", "Python fake")
    link = tmp_path / "link-python"
    link.symlink_to(target)

    identity = audit_toolchain(
        Language.PYTHON,
        ToolchainCandidates(python=link),
    )

    assert identity.components[0].resolved_executable_path == str(target.resolve())


@pytest.mark.parametrize("invalid_kind", ["hardlink", "fifo", "not_executable"])
def test_invalid_toolchain_executable_is_rejected(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    executable = _version_executable(tmp_path / "python", "Python fake")
    candidate = executable
    if invalid_kind == "hardlink":
        candidate = tmp_path / "python-hardlink"
        os.link(executable, candidate)
    elif invalid_kind == "fifo":
        candidate = tmp_path / "python-fifo"
        os.mkfifo(candidate)
    else:
        executable.chmod(0o644)

    with pytest.raises(FixtureAcceptanceError):
        audit_toolchain(Language.PYTHON, ToolchainCandidates(python=candidate))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("import time\ntime.sleep(60)", "strict validation"),
        ("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)", "strict validation"),
        ("import sys\nsys.exit(7)", "strict validation"),
        ("import os\nos.write(1, b'\\xff')", "strict validation"),
        ("print('x' * 4096)", "strict validation"),
    ],
)
def test_abnormal_version_command_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    match: str,
) -> None:
    executable = _write_executable(tmp_path / "python", body)
    monkeypatch.setattr(fixtures, "_VERSION_TIMEOUT_MS", 50)
    monkeypatch.setattr(fixtures, "_MAX_OUTPUT_BYTES", 64)

    with pytest.raises(FixtureAcceptanceError, match=match):
        audit_toolchain(Language.PYTHON, ToolchainCandidates(python=executable))


def test_uncleared_version_process_group_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentlab.runner as runner_module

    executable = _version_executable(tmp_path / "python", "Python fake")
    monkeypatch.setattr(
        runner_module,
        "_group_exists",
        lambda _group_id: (True, "process group inspection denied"),
    )

    with pytest.raises(FixtureAcceptanceError, match="strict validation"):
        audit_toolchain(Language.PYTHON, ToolchainCandidates(python=executable))


def test_executable_replacement_during_audit_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _version_executable(tmp_path / "python", "Python fake")
    original = fixtures._run_version_command

    def replace_after_version(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        executable.write_text("replacement", encoding="utf-8")
        executable.chmod(0o755)
        return result

    monkeypatch.setattr(fixtures, "_run_version_command", replace_after_version)
    with pytest.raises(FixtureAcceptanceError, match="changed during"):
        audit_toolchain(Language.PYTHON, ToolchainCandidates(python=executable))


def test_typescript_package_and_command_version_must_match(tmp_path: Path) -> None:
    candidates = _typescript_candidates(
        tmp_path,
        package_version="5.7.3",
        command_version="Version 5.8.0",
    )
    with pytest.raises(FixtureAcceptanceError, match="versions differ"):
        audit_toolchain(Language.TYPESCRIPT, candidates)


@pytest.mark.parametrize("changed_file", ["package.json", "lib/tsc.js"])
def test_typescript_package_mutation_during_audit_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_file: str,
) -> None:
    candidates = _typescript_candidates(tmp_path)
    package = tmp_path / "typescript"
    original = fixtures._run_version_command
    calls = 0

    def mutate_after_tsc(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 3:
            (package / changed_file).write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(fixtures, "_run_version_command", mutate_after_tsc)
    with pytest.raises(FixtureAcceptanceError, match="changed during audit"):
        audit_toolchain(Language.TYPESCRIPT, candidates)


@pytest.mark.parametrize("language", [Language.PYTHON, Language.JAVA])
def test_gate_rejects_audited_executable_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    if language is Language.PYTHON:
        candidates = _python_candidates(tmp_path)
    else:
        candidates = _java_candidates(tmp_path)
    toolchain = audit_toolchain(language, candidates)
    binding = fixtures._capture_toolchain_binding(language, toolchain)
    target_role = (
        ToolchainComponentRole.PYTHON_RUNTIME
        if language is Language.PYTHON
        else ToolchainComponentRole.JAVA_COMPILER
    )
    target = next(
        component.resolved_path
        for component in binding.components
        if component.role is target_role
    )

    class MutatingRunner:
        def __init__(self, _settings: Any) -> None:
            pass

        def run(self, **_kwargs: Any) -> CommandRunResult:
            target.write_text("replacement", encoding="utf-8")
            target.chmod(0o755)
            return CommandRunResult(
                evidence=_command(GateKind.ACCEPTANCE, CommandStatus.PASSED),
                harness_failure=None,
            )

    monkeypatch.setattr(fixtures, "LocalCommandRunner", MutatingRunner)
    temporary = tmp_path / "gate"
    workspace = temporary / "workspace"
    environment = temporary / "environment"
    workspace.mkdir(parents=True)
    environment.mkdir()

    with pytest.raises(FixtureAcceptanceError, match="executable identity changed"):
        fixtures._run_gate_commands(
            [(GateKind.ACCEPTANCE, ["/fake/gate"])],
            workspace=workspace,
            environment_root=environment,
            temporary_root=temporary,
            toolchain=toolchain,
            toolchain_binding=binding,
        )


def test_gate_rejects_typescript_package_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolchain = audit_toolchain(
        Language.TYPESCRIPT,
        _typescript_candidates(tmp_path),
    )
    binding = fixtures._capture_toolchain_binding(Language.TYPESCRIPT, toolchain)
    assert binding.typescript_compiler_js is not None

    class MutatingRunner:
        def __init__(self, _settings: Any) -> None:
            pass

        def run(self, **_kwargs: Any) -> CommandRunResult:
            binding.typescript_compiler_js.write_text("changed", encoding="utf-8")
            return CommandRunResult(
                evidence=_command(GateKind.ACCEPTANCE, CommandStatus.PASSED),
                harness_failure=None,
            )

    monkeypatch.setattr(fixtures, "LocalCommandRunner", MutatingRunner)
    temporary = tmp_path / "gate"
    workspace = temporary / "workspace"
    environment = temporary / "environment"
    workspace.mkdir(parents=True)
    environment.mkdir()

    with pytest.raises(FixtureAcceptanceError, match="TypeScript package"):
        fixtures._run_gate_commands(
            [(GateKind.ACCEPTANCE, ["/fake/gate"])],
            workspace=workspace,
            environment_root=environment,
            temporary_root=temporary,
            toolchain=toolchain,
            toolchain_binding=binding,
        )


def test_java_and_javac_must_share_jdk_root(tmp_path: Path) -> None:
    with pytest.raises(FixtureAcceptanceError, match="same JDK root"):
        audit_toolchain(
            Language.JAVA,
            _java_candidates(tmp_path, split_roots=True),
        )


def test_toolchain_fingerprint_tampering_is_rejected(tmp_path: Path) -> None:
    identity = audit_toolchain(Language.PYTHON, _python_candidates(tmp_path))
    payload = identity.model_dump(mode="json")
    payload["fingerprint"] = "0" * 64

    with pytest.raises(ValidationError, match="fingerprint"):
        ToolchainIdentity.model_validate(payload)


def _test_toolchain(tmp_path: Path) -> ToolchainIdentity:
    executable = tmp_path / "outside" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake")
    executable.chmod(0o755)
    component = ToolchainComponent(
        role=ToolchainComponentRole.PYTHON_RUNTIME,
        resolved_executable_path=str(executable.absolute()),
        executable_sha256=hashlib.sha256(b"fake").hexdigest(),
        version_argv=[str(executable.absolute()), "--version"],
        exact_version="Python fake",
        version_output_sha256="1" * 64,
        package_version=None,
        package_fingerprint=None,
    )
    return fixtures._toolchain_identity([component])


def _definition(tmp_path: Path) -> tuple[Path, FixtureDefinition]:
    repository = tmp_path / "repository"
    fixture = repository / "experiments" / "phase6" / "fixtures" / "python" / "baseline"
    reference = repository / "experiments" / "phase6" / "fixtures" / "python" / "reference"
    fixture.mkdir(parents=True)
    reference.mkdir(parents=True)
    (fixture / "implementation.txt").write_text("baseline\n", encoding="utf-8")
    (fixture / "gate.txt").write_text("protected\n", encoding="utf-8")
    (reference / "implementation.txt").write_text("reference\n", encoding="utf-8")
    policy = DiffPolicy(
        schema_version="1.0",
        language=Language.PYTHON,
        fixture_revision="tag-normalizer-python-v1",
        editable_paths=[
            EditablePathPolicy(
                path="implementation.txt",
                allow_create=False,
                allow_delete=False,
            )
        ],
        protected_paths=[ProtectedPathPolicy(path="gate.txt", role="gate_helper")],
        reject_unclassified_paths=True,
        reject_symlinks=True,
        reject_hardlinks=True,
        reject_special_files=True,
    )
    policy_path = fixture.parent / "diff-policy.json"
    policy_path.write_bytes(canonical_json_bytes(policy))
    output = repository / ".artifacts" / "phase6" / "fixture-acceptance" / "test" / "python"
    return repository, FixtureDefinition(
        language=Language.PYTHON,
        fixture_revision="tag-normalizer-python-v1",
        fixture_root=fixture,
        reference_root=reference,
        policy_path=policy_path,
        output_root=output,
    )


def _command(
    gate: GateKind,
    status: CommandStatus,
    *,
    truncated: bool = False,
    decode_replaced: bool = False,
    cleared: bool = True,
) -> CommandEvidence:
    now = datetime.now(UTC)
    if status is CommandStatus.PASSED:
        return_code = 0
    elif status is CommandStatus.FAILED:
        return_code = 1
    elif status is CommandStatus.SIGNAL_TERMINATED:
        return_code = -signal.SIGTERM
    else:
        return_code = None
    if status is CommandStatus.TIMED_OUT:
        reason = TerminationReason.TIMEOUT
        sigterm_sent = True
    elif not cleared:
        reason = TerminationReason.RESIDUAL_PROCESS
        sigterm_sent = True
    else:
        reason = TerminationReason.NONE
        sigterm_sent = False
    error = "synthetic error" if status in {
        CommandStatus.SPAWN_ERROR,
        CommandStatus.COLLECTION_ERROR,
    } else None
    return CommandEvidence(
        gate=gate,
        command_index=0,
        argv=["/fake/tool"],
        status=status,
        return_code=return_code,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        stdout="",
        stderr="",
        stdout_truncated=truncated,
        stderr_truncated=False,
        stdout_decode_replaced=decode_replaced,
        stderr_decode_replaced=False,
        termination=TerminationEvidence(
            reason=reason,
            sigterm_sent=sigterm_sent,
            sigkill_sent=False,
            process_group_cleared=cleared,
            error=None if cleared else "synthetic cleanup failure",
        ),
        error=error,
    )


def _normal_gate_results(*, baseline: bool) -> tuple[CommandEvidence, ...]:
    return tuple(
        _command(
            gate,
            CommandStatus.FAILED
            if baseline and gate is GateKind.ACCEPTANCE
            else CommandStatus.PASSED,
        )
        for gate in GateKind
    )


def _install_acceptance_fakes(
    monkeypatch: pytest.MonkeyPatch,
    toolchain: ToolchainIdentity,
) -> None:
    monkeypatch.setattr(fixtures, "audit_toolchain", lambda *_args, **_kwargs: toolchain)
    calls = 0

    def run_gates(*_args: Any, **_kwargs: Any) -> tuple[CommandEvidence, ...]:
        nonlocal calls
        calls += 1
        return _normal_gate_results(baseline=calls == 1)

    monkeypatch.setattr(fixtures, "_run_gate_commands", run_gates)


def test_all_language_acceptance_gates_share_hyphen_separator_boundary_case() -> None:
    repository = Path(__file__).resolve().parents[1]
    helpers = (
        repository
        / "experiments/phase6/fixtures/python/baseline/gate_helper.py",
        repository
        / "experiments/phase6/fixtures/typescript/baseline/gate_helper.mjs",
        repository
        / "experiments/phase6/fixtures/java/baseline/GateHelper.java",
    )
    for helper in helpers:
        content = helper.read_text(encoding="utf-8")
        assert '"a- _b"' in content
        assert '"a--b"' in content
    java_reference = (
        repository
        / "experiments/phase6/fixtures/java/reference/TagNormalizer.java"
    ).read_text(encoding="utf-8")
    assert "if (pendingSeparator)" in java_reference
    assert "value.charAt(value.length() - 1) != '-'" not in java_reference


def test_fixture_acceptance_generates_strict_canonical_records_only_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, definition = _definition(tmp_path)
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))
    source_before = secure_tree_snapshot(definition.fixture_root)

    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )

    assert outcome.status == "accepted", outcome.detail
    assert [command.status for command in outcome.baseline_commands] == [
        CommandStatus.FAILED,
        CommandStatus.PASSED,
        CommandStatus.PASSED,
        CommandStatus.PASSED,
    ]
    assert all(
        command.status is CommandStatus.PASSED
        for command in outcome.reference_commands
    )
    assert secure_tree_snapshot(definition.fixture_root) == source_before
    assert outcome.manifest_path is not None
    assert outcome.acceptance_path is not None
    assert load_fixture_manifest(outcome.manifest_path) == outcome.manifest
    assert load_fixture_acceptance(outcome.acceptance_path) == outcome.acceptance
    assert outcome.manifest_path.read_bytes() == canonical_json_bytes(outcome.manifest)
    assert outcome.acceptance_path.read_bytes() == canonical_json_bytes(outcome.acceptance)


@pytest.mark.parametrize(
    ("gate", "status", "baseline"),
    [
        (GateKind.ACCEPTANCE, CommandStatus.PASSED, True),
        (GateKind.REGRESSION, CommandStatus.FAILED, True),
        (GateKind.LINT, CommandStatus.FAILED, True),
        (GateKind.TYPECHECK, CommandStatus.FAILED, True),
        (GateKind.ACCEPTANCE, CommandStatus.FAILED, False),
    ],
)
def test_unexpected_quality_gate_result_blocks_acceptance_without_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: GateKind,
    status: CommandStatus,
    baseline: bool,
) -> None:
    repository, definition = _definition(tmp_path)
    toolchain = _test_toolchain(tmp_path)
    monkeypatch.setattr(fixtures, "audit_toolchain", lambda *_args, **_kwargs: toolchain)
    calls = 0

    def run_gates(*_args: Any, **_kwargs: Any) -> tuple[CommandEvidence, ...]:
        nonlocal calls
        calls += 1
        commands = list(_normal_gate_results(baseline=calls == 1))
        if (calls == 1) is baseline:
            commands[list(GateKind).index(gate)] = _command(gate, status)
        return tuple(commands)

    monkeypatch.setattr(fixtures, "_run_gate_commands", run_gates)

    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )

    assert outcome.status == "blocked"
    assert not definition.output_root.exists()


def test_reference_hash_change_before_apply_blocks_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, definition = _definition(tmp_path)
    toolchain = _test_toolchain(tmp_path)
    monkeypatch.setattr(fixtures, "audit_toolchain", lambda *_args, **_kwargs: toolchain)

    def mutate_reference(*_args: Any, **_kwargs: Any) -> tuple[CommandEvidence, ...]:
        (definition.reference_root / "implementation.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        return _normal_gate_results(baseline=True)

    monkeypatch.setattr(fixtures, "_run_gate_commands", mutate_reference)
    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"
    assert "reference solution changed" in (outcome.detail or "")


@pytest.mark.parametrize("forbidden", ["fixture", "prompt", "artifact"])
def test_reference_root_must_be_isolated_from_sensitive_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden: str,
) -> None:
    repository, definition = _definition(tmp_path)
    if forbidden == "fixture":
        reference = definition.fixture_root / "reference"
    elif forbidden == "prompt":
        reference = repository / "experiments" / "phase6" / "prompts" / "reference"
    else:
        reference = definition.output_root / "reference"
    reference.mkdir(parents=True)
    (reference / "implementation.txt").write_text("reference\n", encoding="utf-8")
    changed = replace(definition, reference_root=reference)
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))

    outcome = accept_fixture(
        changed,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"


@pytest.mark.parametrize("mutation", ["protected", "unclassified", "deleted"])
def test_reference_overlay_policy_violation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    repository, definition = _definition(tmp_path)
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))
    original = fixtures._apply_reference

    def invalid_apply(
        policy: DiffPolicy,
        reference: fixtures.SecureTreeSnapshot,
        workspace: Path,
    ) -> None:
        original(policy, reference, workspace)
        if mutation == "protected":
            (workspace / "gate.txt").write_text("changed\n", encoding="utf-8")
        elif mutation == "unclassified":
            (workspace / "extra.txt").write_text("extra\n", encoding="utf-8")
        else:
            (workspace / "implementation.txt").unlink()

    monkeypatch.setattr(fixtures, "_apply_reference", invalid_apply)
    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "fifo"])
def test_fixture_tree_rejects_links_and_special_files(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "target"
    target.write_text("content", encoding="utf-8")
    invalid = root / "invalid"
    if entry_kind == "symlink":
        invalid.symlink_to(target)
    elif entry_kind == "hardlink":
        os.link(target, invalid)
    else:
        os.mkfifo(invalid)

    with pytest.raises(FixtureAcceptanceError):
        secure_tree_snapshot(root)


def test_source_fixture_mutation_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, definition = _definition(tmp_path)
    toolchain = _test_toolchain(tmp_path)
    monkeypatch.setattr(fixtures, "audit_toolchain", lambda *_args, **_kwargs: toolchain)
    calls = 0

    def mutate_source(*_args: Any, **_kwargs: Any) -> tuple[CommandEvidence, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            (definition.fixture_root / "implementation.txt").write_text(
                "changed source\n",
                encoding="utf-8",
            )
        return _normal_gate_results(baseline=calls == 1)

    monkeypatch.setattr(fixtures, "_run_gate_commands", mutate_source)
    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"
    assert "source Fixture changed" in (outcome.detail or "")


@pytest.mark.parametrize(
    "command",
    [
        _command(GateKind.ACCEPTANCE, CommandStatus.TIMED_OUT),
        _command(GateKind.ACCEPTANCE, CommandStatus.SIGNAL_TERMINATED),
        _command(GateKind.ACCEPTANCE, CommandStatus.SPAWN_ERROR),
        _command(GateKind.ACCEPTANCE, CommandStatus.PASSED, truncated=True),
        _command(GateKind.ACCEPTANCE, CommandStatus.PASSED, decode_replaced=True),
        _command(GateKind.ACCEPTANCE, CommandStatus.PASSED, cleared=False),
    ],
    ids=["timeout", "signal", "spawn", "output-limit", "decode", "cleanup"],
)
def test_gate_harness_abnormality_is_not_quality_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: CommandEvidence,
) -> None:
    class FakeRunner:
        def __init__(self, _settings: Any) -> None:
            pass

        def run(self, **_kwargs: Any) -> CommandRunResult:
            return CommandRunResult(evidence=command, harness_failure=None)

    monkeypatch.setattr(fixtures, "LocalCommandRunner", FakeRunner)
    toolchain = _test_toolchain(tmp_path)
    temporary = tmp_path / "temporary"
    workspace = temporary / "workspace"
    environment = temporary / "environment"
    workspace.mkdir(parents=True)
    environment.mkdir()

    with pytest.raises(FixtureAcceptanceError, match="normal quality result"):
        fixtures._run_gate_commands(
            [(GateKind.ACCEPTANCE, ["/fake/tool"])],
            workspace=workspace,
            environment_root=environment,
            temporary_root=temporary,
            toolchain=toolchain,
        )


@pytest.mark.parametrize("failed_cleanup_call", [1, 2], ids=["baseline", "reference"])
def test_workspace_cleanup_failure_blocks_success_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_cleanup_call: int,
) -> None:
    repository, definition = _definition(tmp_path)
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))
    original = fixtures.remove_disposable_workspace
    calls = 0

    def fail_cleanup(workspace: Any) -> tuple[bool, str | None]:
        nonlocal calls
        calls += 1
        original(workspace)
        if calls == failed_cleanup_call:
            return False, "synthetic cleanup failure"
        return True, None

    monkeypatch.setattr(fixtures, "remove_disposable_workspace", fail_cleanup)
    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"
    assert not definition.output_root.exists()


def test_confirmation_is_required_before_any_subprocess_or_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden() -> ToolchainCandidates:
        nonlocal called
        called = True
        raise AssertionError("must not discover or execute")

    monkeypatch.setattr(fixtures, "discover_toolchain_candidates", forbidden)
    with pytest.raises(FixtureAcceptanceError, match="confirm-local-execution"):
        fixtures.accept_phase6_fixtures(
            tmp_path,
            tmp_path / ".artifacts" / "phase6" / "fixture-acceptance",
            confirm_local_execution=False,
        )
    assert called is False


def test_cli_help_discloses_local_execution_and_missing_confirmation_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess_called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal subprocess_called
        subprocess_called = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(fixtures.subprocess, "run", forbidden)
    help_result = CLI_RUNNER.invoke(app, ["accept-phase6-fixtures", "--help"])
    stopped = CLI_RUNNER.invoke(
        app,
        [
            "accept-phase6-fixtures",
            "--repository-root",
            str(tmp_path),
            "--output-root",
            str(tmp_path / ".artifacts" / "phase6" / "fixture-acceptance"),
        ],
    )

    assert help_result.exit_code == 0
    assert "--confirm-local-execution" in help_result.output
    assert "toolchain" in help_result.output
    assert stopped.exit_code == 2
    assert "local subprocesses executed: 0" in stopped.output
    assert subprocess_called is False


def test_existing_output_is_never_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, definition = _definition(tmp_path)
    definition.output_root.mkdir(parents=True)
    sentinel = definition.output_root / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))

    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )
    assert outcome.status == "blocked"
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_atomic_publish_preserves_empty_directory_created_in_race_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, definition = _definition(tmp_path)
    _install_acceptance_fakes(monkeypatch, _test_toolchain(tmp_path))
    original = fixtures._rename_directory_no_replace
    competing_identity: tuple[int, int] | None = None

    def create_competing_directory(source: Path, destination: Path) -> None:
        nonlocal competing_identity
        destination.mkdir()
        metadata = destination.lstat()
        competing_identity = (metadata.st_dev, metadata.st_ino)
        original(source, destination)

    monkeypatch.setattr(
        fixtures,
        "_rename_directory_no_replace",
        create_competing_directory,
    )
    outcome = accept_fixture(
        definition,
        repository_root=repository,
        commit_sha=FULL_COMMIT,
        candidates=ToolchainCandidates(),
    )

    assert outcome.status == "blocked"
    assert competing_identity is not None
    current = definition.output_root.lstat()
    assert (current.st_dev, current.st_ino) == competing_identity
    assert list(definition.output_root.iterdir()) == []


def _suite_result(language: Language, status: str) -> FixtureAcceptanceOutcome:
    return FixtureAcceptanceOutcome(
        language=language,
        status=status,  # type: ignore[arg-type]
        blocker=None if status == "accepted" else "synthetic_blocker",
        detail=None,
        manifest=None,
        acceptance=None,
        manifest_path=None,
        acceptance_path=None,
        manifest_sha256=None,
        acceptance_sha256=None,
        baseline_commands=(),
        reference_commands=(),
    )


@pytest.mark.parametrize(
    ("statuses", "minimum", "full"),
    [
        (("accepted", "blocked", "accepted"), True, False),
        (("blocked", "accepted", "blocked"), False, False),
        (("accepted", "accepted", "accepted"), True, True),
    ],
)
def test_language_results_are_independent_and_minimum_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statuses: tuple[str, str, str],
    minimum: bool,
    full: bool,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    definitions = tuple(
        FixtureDefinition(
            language=language,
            fixture_revision=f"fixture-{language.value}",
            fixture_root=repository,
            reference_root=repository,
            policy_path=repository / "policy",
            output_root=repository / ".artifacts" / language.value,
        )
        for language in Language
    )
    monkeypatch.setattr(fixtures, "fixture_definitions", lambda *_args: definitions)
    monkeypatch.setattr(fixtures, "verify_repository_provenance", lambda *_args: FULL_COMMIT)
    monkeypatch.setattr(fixtures, "verify_fixture_sources_committed", lambda *_args: None)
    monkeypatch.setattr(fixtures, "discover_toolchain_candidates", ToolchainCandidates)
    index = 0

    def accept(*_args: Any, **_kwargs: Any) -> FixtureAcceptanceOutcome:
        nonlocal index
        result = _suite_result(definitions[index].language, statuses[index])
        index += 1
        return result

    monkeypatch.setattr(fixtures, "accept_fixture", accept)
    outcome = fixtures.accept_phase6_fixtures(
        repository,
        repository / ".artifacts" / "phase6" / "fixture-acceptance" / "test",
        confirm_local_execution=True,
    )

    assert [result.status for result in outcome.results] == list(statuses)
    assert outcome.engineering_minimum_met is minimum
    assert outcome.full_target_met is full


def test_language_scoped_acceptance_invokes_only_java(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    output_root = repository / ".artifacts" / "phase6" / "fixture-acceptance" / "new-head"
    invoked: list[Language] = []
    monkeypatch.setattr(fixtures, "verify_repository_provenance", lambda *_args: FULL_COMMIT)
    monkeypatch.setattr(fixtures, "verify_fixture_sources_committed", lambda *_args: None)
    monkeypatch.setattr(fixtures, "discover_toolchain_candidates", ToolchainCandidates)

    def accept(definition: FixtureDefinition, **_kwargs: Any) -> FixtureAcceptanceOutcome:
        invoked.append(definition.language)
        assert definition.output_root == output_root / "java"
        return _suite_result(definition.language, "accepted")

    monkeypatch.setattr(fixtures, "accept_fixture", accept)
    outcome = fixtures.accept_phase6_fixtures(
        repository,
        output_root,
        language=Language.JAVA,
        confirm_local_execution=True,
    )

    assert invoked == [Language.JAVA]
    assert [result.language for result in outcome.results] == [Language.JAVA]


def test_language_scoped_cli_returns_success_for_one_accepted_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentlab.cli.accept_phase6_fixtures",
        lambda *_args, **_kwargs: fixtures.FixtureAcceptanceSuiteOutcome(
            commit_sha=FULL_COMMIT,
            results=(_suite_result(Language.JAVA, "accepted"),),
        ),
    )

    result = CLI_RUNNER.invoke(
        app,
        [
            "accept-phase6-fixtures",
            "--language",
            "java",
            "--confirm-local-execution",
        ],
    )

    assert result.exit_code == 0
    assert "java: accepted" in result.output


def test_language_scoped_cli_rejects_duplicate_before_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("domain must not be invoked")

    monkeypatch.setattr(fixtures, "accept_phase6_fixtures", forbidden)
    result = CLI_RUNNER.invoke(
        app,
        [
            "accept-phase6-fixtures",
            "--language",
            "java",
            "--language",
            "java",
            "--confirm-local-execution",
        ],
    )

    assert result.exit_code == 2
    assert "at most once" in result.output
    assert called is False


def test_language_scoped_cli_rejects_unknown_language_at_parse_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("domain must not be invoked")

    monkeypatch.setattr(fixtures, "accept_phase6_fixtures", forbidden)
    result = CLI_RUNNER.invoke(
        app,
        [
            "accept-phase6-fixtures",
            "--language",
            "rust",
            "--confirm-local-execution",
        ],
    )

    assert result.exit_code == 2
    assert called is False


def test_acceptance_output_rejects_lexical_traversal_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    called = False

    def forbidden() -> ToolchainCandidates:
        nonlocal called
        called = True
        raise AssertionError("discovery must not run")

    monkeypatch.setattr(fixtures, "discover_toolchain_candidates", forbidden)
    with pytest.raises(FixtureAcceptanceError, match="must not contain"):
        fixtures.accept_phase6_fixtures(
            repository,
            Path(".artifacts/phase6/fixture-acceptance/../collision"),
            language=Language.JAVA,
            confirm_local_execution=True,
        )
    assert called is False


def test_acceptance_output_symlink_parent_is_rejected_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    artifact_root = repository / ".artifacts" / "phase6" / "fixture-acceptance"
    artifact_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifact_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(fixtures, "verify_repository_provenance", lambda *_args: FULL_COMMIT)
    monkeypatch.setattr(fixtures, "verify_fixture_sources_committed", lambda *_args: None)
    monkeypatch.setattr(fixtures, "discover_toolchain_candidates", ToolchainCandidates)

    outcome = fixtures.accept_phase6_fixtures(
        repository,
        artifact_root / "linked",
        language=Language.JAVA,
        confirm_local_execution=True,
    )

    assert outcome.results[0].status == "blocked"
    assert list(outside.iterdir()) == []
