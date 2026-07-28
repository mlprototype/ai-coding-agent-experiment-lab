# Antigravity CLI Provider Offline Design

## Status

- Phase: 5
- Phase status: Current
- Design status: Ready for offline implementation
- Design date: 2026-07-28
- Design base: `feature/phase5` at
  `d3a8cf814623ea0bdd071d12c948a582f38a827d`
- Implementation owner: Antigravity

This document is the implementation contract for the first offline slice of the
Antigravity CLI Provider. It does not authorize an actual Antigravity agent run,
model request, authentication flow, quota use, provider comparison, or Live
artifact creation.

## Goal

Connect Antigravity CLI to the existing Provider boundary without changing the
meaning or compatibility of the completed Replay, Safe Runner, Codex Provider,
or Workflow A/B paths.

The first slice establishes only the parts that can be verified without an
external AI call:

1. versioned Antigravity contracts;
2. a strict `stream-json` parser;
3. bounded read-only `--version` and `--help` preflight;
4. normalized, redacted Evidence construction;
5. fake-`agy` offline tests;
6. a fail-closed boundary for capabilities that are not yet verified.

## Authoritative product facts

The following facts were checked against Google documentation on 2026-07-28.
They are product documentation, not a replacement for a versioned local
preflight.

- The executable is `agy`.
- Headless mode is one prompt and one process invocation using `-p`,
  `--print`, or `--prompt`.
- `--output-format stream-json` emits NDJSON with one `init`, zero or more
  `step_update`, and one terminal `result`.
- `--model` pins a model slug. An unknown pinned model fails instead of silently
  selecting another model.
- `--effort` accepts `low`, `medium`, or `high`.
- `--print-timeout` bounds the CLI's own wait, while AgentLab must still enforce
  its independent process-group timeout.
- `--sandbox` requests terminal sandbox restrictions.
- Headless mode uses cached credentials. Without cached authentication, a
  non-interactive run exits with an authentication error instead of waiting for
  browser input.
- `stdout` carries structured output; diagnostics and permission notices use
  `stderr`.
- In headless mode, a tool that needs an unavailable approval can be
  soft-denied while the run continues and exits `0`.
- Workspace file reads and writes are normally auto-allowed. Other operations,
  including shell commands and web access, depend on the effective permission
  policy.
- The stream can contain conversation IDs, absolute paths, response text, tool
  parameters, tool output, subagent metadata, and usage data. Raw stream content
  is sensitive and must not be persisted.

Primary sources:

- [Headless mode](https://antigravity.google/docs/cli/headless)
- [Permissions](https://antigravity.google/docs/cli/permissions)
- [Sandbox](https://antigravity.google/docs/cli/sandbox)
- [Installation and authentication](https://antigravity.google/docs/cli/install)
- [CLI reference](https://antigravity.google/docs/cli/reference)
- [Changelog](https://antigravity.google/changelog)

## Critical gaps and decisions

### Prompt transport is unresolved for Live

The documented Antigravity headless interface accepts the Prompt as the value
of `-p`/`--prompt`. No documented stdin or `--prompt-file` transport was found.
Passing the generated Prompt directly would therefore expose it in process argv.

The repository currently requires Provider Prompt content not to appear in argv.
This requirement remains authoritative. The offline slice must not weaken
`AGENTS.md`, introduce a Live exception, or claim that an undocumented `@file`
form is equivalent to a Prompt-file API.

Consequences:

- no production `agy -p <generated-prompt>` invocation in this slice;
- no `live-antigravity` CLI command in this slice;
- no Antigravity call from tests or CI;
- no attempt to prove Prompt delivery through a model call;
- future Live work requires a separate human decision:
  - wait for a documented non-argv transport; or
  - explicitly accept argv exposure only for reviewed, non-secret synthetic
    Prompts and update the repository security contract.

### Exit code zero is not enough

Because a headless permission request can be soft-denied, `exit_code == 0` and
`result.status == SUCCESS` do not prove that all requested tools executed.
Quality Gates remain the source of truth for the resulting Workspace. Evidence
must not infer successful command, web, MCP, or subagent execution from the
terminal status.

### Sandbox and permission claims must be observed separately

`--sandbox` is a request to the CLI. It is not proof that every Provider action
was contained, and it does not by itself describe file, web, MCP, or approval
policy. Persist requested settings and observable stream values separately.
Never label AgentLab's process-group runner as the Antigravity sandbox.

### Provider-internal retries are opaque

AgentLab must perform no retry, fallback, conversation resume, or replacement
run. Any retry internal to Antigravity's service or harness is Provider
behavior, not an AgentLab retry. If the stream does not expose it with a stable
typed field, record it as unavailable rather than `0`.

## Scope

### In scope: offline slice 5A

- Antigravity-specific enums and Evidence models with a new schema version;
- a versioned CLI profile model with no unverified production version selected;
- strict NDJSON parsing from supplied bytes;
- normalization of terminal state and Provider-reported token usage;
- bounded version/help preflight;
- lifecycle and failure mapping that reuse existing shared primitives where
  their semantics match;
- fake executable and parser fixtures;
- compatibility tests proving no existing Artifact bytes or loaders change;
- documentation of the later Live and Provider-comparison boundaries.

### Out of scope

- actual `agy -p` execution;
- interactive `agy`, login, browser OAuth, keyring inspection, logout, or account
  modification;
- `agy models`, `agy agents`, quota lookup, model catalog lookup, or any command
  that may require a service request;
- Antigravity subscription or API usage;
- real model, Prompt, Fixture, Workspace, or Gate execution;
- `--dangerously-skip-permissions`;
- changes to global `~/.gemini` files;
- a temporary HOME/authentication experiment;
- Provider comparison Spec, scheduler, report, or winner selection;
- Phase 4 Campaign modification or replay;
- PR creation or main merge.

## Architecture

Keep the Antigravity contract adjacent to, but independent from, Codex-specific
contracts.

Suggested modules:

- `src/agentlab/antigravity_provider.py`
  - version/help preflight;
  - profile selection;
  - strict stream parser;
  - normalization helpers;
  - no production Live process runner in slice 5A.
- `src/agentlab/models.py`
  - Antigravity enums and Evidence 1.0;
  - shared `UsageMetrics` mapping only where semantics match;
  - no change to existing Codex schema versions.
- `tests/test_antigravity_provider.py`
  - parser, preflight, Evidence, redaction, and failure matrix.
- `tests/fixtures/antigravity/`
  - small synthetic NDJSON samples only when a fixture is clearer than inline
    test data;
  - no copied real conversation or user data.

Do not introduce an abstract Provider hierarchy only for symmetry. Reuse
process-group, strict JSON, time, redaction, and atomic persistence helpers by
composition when their current contract is truly provider-neutral. Do not
rename Codex fields or relax Codex validators to make Antigravity fit.

## Versioned preflight

### Allowed operations

Offline preflight may execute only:

```console
agy --version
agy --help
```

The production implementation must use an argv array, `shell=False`, separated
stdout/stderr, bounded bytes, strict timeout, a new process group, residual
process cleanup, and a sanitized environment. A fake executable is mandatory in
normal tests.

Preflight must not execute `agy -p`, `agy models`, authentication commands,
model catalog calls, or a sample task.

### Required help markers

The first selectable profile should require all of these documented
capabilities:

- `--prompt` or an equivalent documented headless alias;
- `--output-format`;
- `stream-json`;
- `--model`;
- `--effort`;
- `--print-timeout`;
- `--sandbox`.

The presence of `--dangerously-skip-permissions` is neither required nor
treated as a capability AgentLab will use.

### Version policy

Do not copy a version number from a web page or codelab into a production
allowlist. The user's local `agy --version` result must be reviewed later and
registered with an exact profile in a separate commit.

Until then:

- command unavailable: `not_verified`;
- version/help failure: preflight failure;
- supported flags but unregistered version: profile `not_selected`;
- no selected profile: Provider invocation prohibited.

## Strict `stream-json` contract

### Framing

- Input is UTF-8 NDJSON.
- Each non-empty line must be exactly one JSON object.
- Reject invalid UTF-8, duplicate keys, non-finite numbers, non-object values,
  an empty line, an oversized line, and total output above the configured bound.
- Parse incrementally across arbitrary byte chunk boundaries.
- Raw lines are discarded after bounded normalization.

### Event order

For the initial versioned profile:

1. exactly one `init` event first;
2. zero or more `step_update` events;
3. exactly one `result` event last;
4. no event after `result`.

Unknown top-level events fail closed as `provider_protocol_error`. Unknown
`step_type` values also fail closed for a selected version profile until
explicitly reviewed and added. Do not silently map schema drift to success.

### Normalized fields

Persist only bounded, non-content observations:

- event counts by fixed enum;
- step counts by fixed `step_type`;
- `init.permission_mode`;
- whether requested model and agent fields were present;
- terminal status;
- terminal `num_turns`;
- terminal duration in milliseconds after finite/range validation;
- Provider-reported usage;
- stdout/stderr byte counts and truncation flags;
- process lifecycle and cleanup result.

Do not persist:

- `conversation_id` or session identifiers;
- `cwd` or any local absolute path;
- tool list;
- response or `text_delta`;
- error message text;
- tool name, parameters, output, or error text;
- subagent role, conversation ID, log URI, or Workspace URI;
- raw stdout/stderr or raw NDJSON;
- reasoning or agent messages.

### Terminal mapping

- `SUCCESS`
  - requires one terminal result, process exit `0`, and `num_turns == 1`;
  - means the Provider produced a response, not that every tool ran.
- `ERROR`
  - Provider failure; classify a bounded advisory hint only if a conservative
    redaction-safe classifier is available.
- `CANCELED` or `INTERRUPTED`
  - Provider interruption, distinct from a Harness cleanup failure.
- `INVALID`, `WAITING`, or `RUNNING`
  - invalid terminal state for a completed one-shot run.
- missing/multiple result, result not last, malformed fields, or schema drift
  - protocol failure.
- timeout, signal termination, spawn failure, output limit, collection failure,
  and process cleanup failure
  - remain separate Harness/Provider failure kinds as in the Codex boundary.

### Usage mapping

Map only Provider-reported integer values:

| Antigravity result | AgentLab |
|---|---|
| `input_tokens` | `input_tokens` |
| `cache_read_tokens` | `cached_input_tokens` |
| `output_tokens` | `output_tokens` |
| `thinking_tokens` | `reasoning_output_tokens` |

`total_tokens` is a cross-check field, not an additional AgentLab metric. The
documented examples treat thinking tokens as part of output and cache-read
tokens as part of input, so do not add all five fields together.

Reject booleans, strings, negative values, overflow, and inconsistent totals.
If the terminal usage object is missing, persist `not_available` with null
values. Never convert missing usage to zero.

## Antigravity Evidence 1.0

Define a dedicated strict model with `extra="forbid"`. At minimum it records:

- schema version and `provider=antigravity`;
- exact CLI version and selected profile;
- preflight timestamp and verified flags;
- requested model slug and effort;
- requested output format;
- prompt transport state;
- whether Prompt argv exposure would occur;
- execution, invocation, and cleanup stages;
- requested sandbox flag;
- observed permission mode when available;
- raw stream persistence `false`;
- Provider status and normalized terminal status;
- event/step counts;
- usage source and normalized values;
- stdout/stderr byte counts and truncation;
- timeout/signal/process-group termination result;
- fixed failure kind and failure stage.

Evidence must distinguish requested, observed, and unavailable values. It must
not state that sandbox, authentication, Prompt delivery, model API receipt,
quota consumption, tool execution, or network blocking succeeded unless a
stable observation proves that exact fact.

Existing Recording 1.0/1.1, Codex Evidence 1.1-1.5, Live Artifact 1.0/1.1,
Failure Diagnostic 1.0, Workflow Plan, Campaign, and report loaders must remain
byte- and schema-compatible.

## Offline acceptance tests

The Antigravity implementation must add tests for at least:

### Preflight

- missing `agy`;
- version/help success from stdout or stderr;
- non-zero exit;
- timeout;
- spawn/collection/process cleanup failure;
- missing each required flag;
- unregistered exact version;
- proof that only `--version` and `--help` were invoked;
- secret parent environment not inherited by fake processes.

### Parser

- valid `init` → `step_update`* → `result`;
- arbitrary chunk boundaries and split multibyte UTF-8;
- zero step updates;
- multiple `agent_response` deltas without retaining their text;
- tool and subagent payloads discarded;
- duplicate keys, invalid UTF-8, blank line, non-object JSON;
- oversized line and total output limit;
- missing, duplicate, early, or non-final result;
- event after result;
- unknown event and unknown step type;
- every terminal status;
- `SUCCESS` with non-zero exit or `num_turns != 1`;
- usage present, absent, malformed, negative, boolean, non-finite, overflow, and
  inconsistent totals.

### Evidence and compatibility

- no Prompt, response, error text, conversation ID, path, tool payload, raw
  stream, auth value, or secret in serialized Evidence;
- strict round-trip and unknown-field rejection;
- missing usage remains missing;
- fixed failure kinds remain distinct;
- no external AI, network, auth, model catalog, Gate, or real `agy -p`;
- all existing Replay/Codex/Workflow tests remain unchanged and pass.

Required validation:

```console
uv run pytest
uv run ruff check .
uv run mypy src
uv run agentlab doctor --json
```

`doctor --json` may use the existing read-only version/help probes. Tests must
still fake them. If running doctor on a developer machine would touch a real
`agy`, report that separately and do not use it as an offline test result.

## Later slices

### Slice 5B: manual Antigravity vertical slice

Not authorized by this design. Before it can start, a reviewed commit and a
separate human approval must resolve:

- Prompt transport and argv exposure;
- exact local CLI version/profile;
- sanitized temporary HOME and cached keyring authentication behavior;
- immutable, run-local permission settings;
- sandbox and web/MCP denial policy;
- exact model slug and effort;
- one run = one fresh conversation = one Provider process;
- explicit `--confirm-live-antigravity`;
- Provider call ceiling of one for the first smoke;
- no retry, fallback, continuation, resume, or subagent delegation;
- create-only Recording/Evidence/Diagnostic paths;
- process, Workspace, Prompt adapter, HOME, cache, and settings cleanup.

The first real run must be a new experiment and must not reuse any Phase 3 or
Phase 4 Artifact.

### Slice 5C: Provider comparison

Start only after a successful, replayable Antigravity vertical slice.

Use a new strict Provider-comparison Spec rather than overloading Workflow Spec
2.0 or changing ExperimentSpec 1.0. Fix:

- one Workflow;
- Task, Fixture, Acceptance and all Gate commands;
- generated Prompt intent and versioned provider adapters;
- repetitions and seeded block order;
- Workspace construction;
- timeout and stop conditions;
- network/tool policy as closely as each Provider permits.

Models, tool implementations, authentication surface, Prompt transport, and
Agent Harness are Provider-specific treatment components. Therefore the result
is a Codex-system versus Antigravity-system comparison, not a base-model
comparison. Different model IDs or reasoning-effort labels must be reported,
not described as equivalent.

Reuse the Phase 4 principles:

- pre-register a canonical Plan;
- execute sequentially;
- one run = one Provider turn = one AgentLab Provider call;
- no retry, fallback, resume, or failed-run replacement;
- append-only Campaign;
- safe stop with remaining runs recorded as `not_run`;
- offline-only report;
- preserve missing usage and Provider-specific missing observations;
- no general model-performance or statistical-significance claim from the
  initial small experiment.

## Implementation handoff

The Antigravity implementation agent must:

1. verify `feature/phase5`, the design commit, remote parity, and a clean tracked
   worktree before editing;
2. read `AGENTS.md`, this document, `docs/ROADMAP.md`,
   `docs/CODEX_PROVIDER.md`, `docs/REPLAY_FORMAT.md`,
   `src/agentlab/models.py`, `src/agentlab/capabilities.py`, and the relevant
   tests;
3. implement only offline slice 5A;
4. preserve existing schemas and completed Phase behavior;
5. use fake executables and synthetic streams only;
6. stop rather than invoke real `agy -p` or another Provider task,
   authenticate, inspect credentials, query models/quota, or weaken the Prompt
   rule; only the bounded version/help probes are allowed;
7. run the required offline validation;
8. update documentation only for what is actually implemented;
9. commit and push only reviewed Phase 5 files to `feature/phase5`;
10. do not create a PR, merge main, or start Slice 5B/5C.

The completion report must list changed files, test counts, remaining
unverified capabilities, and confirm that real Antigravity Provider calls and
quota use were zero.
