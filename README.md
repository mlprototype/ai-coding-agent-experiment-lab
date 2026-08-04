# AI Coding Agent Experiment Lab

AIコーディングのWorkflowとProviderを分離し、同じ課題・Prompt・品質Gateの下で再現可能に
評価するR&D基盤です。実行条件からEvidence、公開ReportまでをSHA-256で束縛し、成功だけでなく
停止・失敗・欠測も追跡可能な実験記録として残します。

## 解決する問題

AI支援開発の比較では、model、tool、Prompt、Workflow、Fixture、評価方法が同時に変わると、
結果の原因を説明できません。本プロジェクトは次の比較軸を分離します。

- Workflow比較: 同一Provider上の`one_shot`と`staged`
- Provider比較: model、利用可能なtool、Agent Harnessを含むProvider system全体

実験前に条件を固定し、保存Artifactから結果を再検証できる境界を提供します。

## 主な特徴

- version付きstrict schemaとcanonical JSON／JSONL
- Spec → Plan → Campaign → Recording／EvidenceのSHA-256 binding
- unknown field、型強制、入力drift、Artifact不整合を拒否するfail-closed validation
- Acceptance、Regression、lint、typecheckの品質Gate
- Provider失敗、品質不合格、Harness／cleanup failureの分離
- Prompt本文、raw Provider output、認証情報を保存しないredaction境界
- 人間承認、exact Provider-call budget、create-only出力によるLive実行制御
- 決定的なPublic Suite renderer、checksums、bundle外External Anchor
- 保存Recordingだけを読むoffline Replay

## Verified status

| Phase | Status | Verified outcome |
|---|---|---|
| 0–2 | Complete | 契約、offline Replay、使い捨てRunner、Evidence／Gate |
| 3 | Complete | Codex CLI Providerの最小Live vertical sliceとoffline Replay |
| 4 | Complete | `one_shot`／`staged` Campaign、3/3 complete pairs |
| 5 | Blocked | Antigravity配布Artifactの署名検証失敗で実行前停止 |
| 6 | Complete | Python／Javaの評価とaccepted Public Suite |
| 7 | Planned | 運用結果を踏まえた任意拡張 |

各Phaseの受入条件と履歴は[Roadmap](docs/ROADMAP.md)を参照してください。

## Phase 6 results snapshot

- accepted Suite ID: `phase6-java-evaluated-0e6d894d-001`
- Python: `evaluated`、1 Fixture・1 complete pair
- Java: `evaluated`、1 Fixture・1 complete pair
- TypeScript: `not_ready`、Public Suite未掲載
- Antigravity: `not_evaluated / upstream_artifact_signature_invalid`
- automatic winner、leaderboard、統計的有意性: なし

この結果は各言語1 Fixture・1 pair、固定Prompt／Gate、当該環境・実行時期の観測に限定されます。
一般的なWorkflow、Provider、model性能の優劣や、cached inputを無視した確定的なコスト差は
主張しません。

Codex agent内のnested `codex exec`ではpermission failureが観測されましたが、OS-level root
causeは未確定です。Mac Terminalと明示的な絶対`CODEX_HOME`ではJava Campaignが成功したため、
現行運用では個別承認済みLive CampaignだけをHost Terminalから実行します。これはCodex CLIが
常にsandboxから実行不能という主張ではありません。手順は
[Host Terminal Live Campaign Runbook](docs/HOST_TERMINAL_LIVE_RUNBOOK.md)を参照してください。

accepted releaseのManifest／checksums／Anchor、Campaign履歴、schema互換性、Provider accounting、
human Acceptance／supersession provenanceは
[Phase 6詳細設計](docs/PHASE6_MULTI_LANGUAGE_PUBLIC_REPORT.md)に固定しています。

## Architecture and evaluation flow

```text
Experiment Spec
    ↓ strict load / canonicalization
Workflow Plan
    ↓ human approval / exact call budget
Campaign scheduler ──→ Provider adapter
    ↓                       ↓
Recording          normalized Evidence
    └──────────┬────────────┘
               ↓ cross-artifact validation
      Workflow Report / Public Suite
               ↓
       checksums + External Anchor
```

Workflow Promptだけを比較する場合、Provider、exact model、reasoning effort、Fixture、Gate、
sandbox、network設定、timeout、停止条件を固定します。Provider adapterはPromptをstdinから渡し、
GateはProvider成功後の同じ使い捨てWorkspaceで実行します。CampaignやReportは入力とrun identityを
再照合し、矛盾したpairを正常結果として扱いません。

## Quick Start

Python 3.12以上と[uv](https://docs.astral.sh/uv/)を使用します。

```console
uv sync --extra dev
```

read-only診断とSpec検証:

```console
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab validate-workflow experiments/examples/workflow-ab.yaml
```

保存Recordingからのoffline Replay。`--output`には未作成pathを指定してください。

```console
uv run agentlab replay experiments/examples/workflow-smoke.yaml \
  --output .artifacts/runs/workflow-smoke-local.json
```

品質検証:

```console
uv run pytest
uv run ruff check .
uv run mypy src
```

`doctor`のversion／help確認、`validate`、`replay`はAI Promptを送信しません。Live commandは通常の
Quick Startに含めません。Liveには人間の個別Approvalと
[Host Terminal Live Campaign Runbook](docs/HOST_TERMINAL_LIVE_RUNBOOK.md)が必要です。

## Safety and reproducibility principles

- Live Providerを通常テストやCIから呼ばず、fake executableまたはReplayを使う。
- Liveはreviewed commit、canonical Plan、exact call budget、create-only出力へ束縛する。
- retry、fallback、resumeで失敗runを選別しない。
- Fixture sourceを直接実行せず、検証済みの使い捨てコピーだけを実行する。
- Provider process、Gate process、Workspaceを分離し、終了時に残存processを回収する。
- Prompt、raw stream、secretを永続化せず、hash、byte数、安全な分類だけを保存する。
- Usage欠損を0へ変換せず、Provider報告値と不明値を区別する。
- 公開bundleはcanonical renderer、checksums、bundle外Anchorで固定する。

Phase 2 Runnerは完全なsecurity sandbox、network隔離、filesystem隔離ではありません。信頼済みの
Spec、Fixture、commandだけに使用してください。

## Documentation guide

| Topic | Document |
|---|---|
| 実験原則と比較軸 | [Experiment Design](docs/EXPERIMENT_DESIGN.md) |
| Phase別statusと受入条件 | [Roadmap](docs/ROADMAP.md) |
| Workflow A/B契約 | [Workflow A/B](docs/WORKFLOW_AB.md) |
| Codex adapter、redaction、failure taxonomy | [Codex Provider](docs/CODEX_PROVIDER.md) |
| Host Terminalでの承認済みLive | [Host Terminal Live Campaign Runbook](docs/HOST_TERMINAL_LIVE_RUNBOOK.md) |
| Runner／Gate／process lifecycle | [Safe Runner](docs/SAFE_RUNNER.md) |
| Recordingとoffline Replay | [Replay Format](docs/REPLAY_FORMAT.md) |
| Phase 6 schema、Campaign、Public Suite監査 | [Phase 6詳細設計](docs/PHASE6_MULTI_LANGUAGE_PUBLIC_REPORT.md) |
| Antigravity blockerと再開条件 | [Antigravity Provider](docs/ANTIGRAVITY_PROVIDER.md) |
| 過去のread-only CLI観測 | [CLI Capability Matrix](docs/CLI_CAPABILITY_MATRIX.md) |

## Known limitations

- Phase 6の公開評価はPython／Javaそれぞれ1 Fixture・1 complete pairに限られる。
- TypeScriptはtoolchain入力未解決、Antigravityは上流署名blockerのため未評価である。
- nested Codex permission failureの詳細なOS-level root causeは未確定である。
- Liveの成功は将来のmodel availability、quota、vendor event互換性を保証しない。
- token値だけから一般的なコスト差を断定できない。
- dashboard、leaderboard、統計的検定、並列schedulerは提供していない。

## Roadmap

Phase 6は`Complete`です。Phase 7は`Planned`のままで、可視化、追加Provider、信頼区間、
実験catalog、approval workflowなどは、問題・契約・受入条件を別途定義してから判断します。
詳細は[Roadmap](docs/ROADMAP.md)を参照してください。

> 実験結果は固定した課題、Prompt、Gate、環境、実行時期に限定されます。Provider比較は
> model単体ではなく、model、利用可能なtool、Agent Harnessを含むsystem比較です。
