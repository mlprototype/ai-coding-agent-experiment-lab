# AI Coding Agent Experiment Lab

同一の開発課題と品質Gateを使い、AIコーディングの「Workflow」と「Coding Agent
Provider」を再現可能な形で比較するためのR&D基盤です。仮説、実験条件、評価結果、
学習内容、標準手順を蓄積し、開発プロセスの改善判断を支援します。

## 解決する問題

AI支援開発の比較では、モデル、ツール、プロンプト、実行手順、課題、評価方法が同時に
変わりやすく、結果の原因を説明できなくなります。本プロジェクトは、実験Specと結果の
データ契約、Provider境界、固定条件を明示し、比較可能性と追跡可能性を確保します。

Workflow比較（`one_shot` / `staged`）とProvider比較（Codex CLI / Google
Antigravity CLI / Replay Provider）は分離します。一度に変える独立変数を一つに限定し、
差が実行手順によるものかProviderシステムによるものかを混同しないためです。

## 現在の状態

Phase 0〜3を完了しました。**Phase 4: Workflow A/B Experiment** はCurrentです。
offline実装とfake Codexによる受入を完了し、レビュー済みcommitに対する事前登録済みの
実Codex Live A/B受入は未実施です。Phase 4までに次を提供します。

- バージョン付きExperimentSpec、RunMetrics、UsageMetrics、CapabilityReport
- YAML Specの検証
- Codex CLIとAntigravity CLIの読み取り専用能力確認
- 実験設計、Provider境界、ロードマップの文書
- Recording 1.0の`run_started`/`run_completed`契約と、Live 1.1の
  `run_started`/`run_completed|run_failed`契約
- 1件の保存済みRecordingから1件のRunResultを生成・保存するReplay CLI
- 信頼済みFixtureの使い捨てコピーと、Specに列挙されたargvだけを実行するlocal Runner
- timeout、process group停止、残存子process回収、環境変数allowlist
- 上限付きstdout/stderr、実行前後diff、終了状態を持つversion付きEvidence
- 通常のGate不合格とHarness障害を分ける`agentlab run-gates`
- read-only Codex CLI preflightと、明示確認が必要な`agentlab live-codex`
- stdin Prompt、上限付きincremental JSONL parser、redaction済みCodex Evidence
- 2イベントのLive Recording 1.1と、その成功記録のoffline Replay
- Provider成功後に同じ使い捨てWorkspaceで品質Gateを実行する最小vertical slice
- strict paired成果物を構築できない場合だけの独立したFailure Diagnostic 1.0
- 後方互換なExperimentSpec 1.0とは分離したstrict Workflow A/B Spec 2.0
- Task要件を共有しWorkflow指示だけを変えるversion付きPrompt builder
- SHA-256によるseed付きblock順序と、byte決定的なcanonical Plan 1.1
- 1 run = 1 Provider turn = 1 agent callを維持する逐次Campaign scheduler
- append-only Campaign 1.1、stop condition、固定run/failure/停止理由Enum
- 保存済みPlan、Campaign、Recording、Evidenceだけを読むJSON/Markdown集計

ReplayはRecording内の保存済みMetricsだけを再構成し、外部処理を呼びません。通常テストは
短時間のfake Codex executableだけを使い、実Codex、外部AI、network、quotaを呼びません。
Phase 3のmanual Liveは累計8試行です。006はProvider起動後の`model_access`境界、
007は`inconclusive_prompt_delivery_failure`として履歴を保持します。008は人間が
selectable catalogから明示選択した`gpt-5.6-sol`を、commit
`cc97e53bf0bac426b08346f63e6f527ed7d5be9e`のAgentLab製品経路で1回実行して成功しました。
agent callは1、retry／fallbackは0で、`turn.completed` 1件、Provider exit 0、
`task.txt`の期待した1行変更、4種類のQuality Gate全PASS、Evidence 1.5／Recording 1.1の
strict再読込、redaction、process／Workspace cleanup、成功Recordingのoffline Replay、
Replay Metrics一致を確認しました。この最小vertical sliceの結果は、一般的なモデル性能や
Provider比較結果を示すものではありません。Phase 4のoffline成果物も合成fake Providerの
受入証跡であり、Provider性能やWorkflowの優劣を示しません。Antigravity、Provider比較、
dashboard、notebook、統計的検定、並列schedulerは未実装です。

## Quick Start

Python 3.12以上と[uv](https://docs.astral.sh/uv/)を使う例です。

```console
uv sync --extra dev
uv run agentlab doctor
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab validate experiments/examples/codex-live-smoke.yaml
uv run agentlab validate-workflow experiments/examples/workflow-ab.yaml
uv run agentlab plan-workflow experiments/examples/workflow-ab.yaml \
  --output .artifacts/workflow-ab/plan.json
uv run agentlab replay experiments/examples/workflow-smoke.yaml \
  --output .artifacts/runs/workflow-smoke-run-001.json
uv run agentlab run-gates experiments/examples/workflow-smoke.yaml \
  --task-id smoke-task \
  --run-id phase2-runner-smoke-001 \
  --output .artifacts/evidence/phase2-runner-smoke-001.json \
  --confirm-execution
uv run pytest
uv run ruff check .
uv run mypy src
```

`plan-workflow`は外部AI、Provider、network、Gateを呼びません。canonical Planには現在時刻を
混入させず、同じSpec bytes、Task Prompt、Fixture、seedから同じbytesを生成します。作成時刻は
Plan SHA-256を持つ`plan.metadata.json`へ分離します。Planと予約Artifact pathはcreate-onlyで、
開始済みCampaignの変更や自動resumeは行いません。
Plan 1.1は`one_shot`／`staged`それぞれの生成Prompt SHA-256とbyte数も事前登録します。
Campaign開始時にTask Prompt bytesとFixture全file bytesを一度だけ固定し、全runのadapterを
そのin-memory snapshotから構築します。各run開始前のsource integrity checkで変更を検出した
場合は、次のProvider callを開始せず`input_changed`で停止します。

将来のLive Campaign形式は次です。今回は実行していません。`--confirm-live-codex`と、
Planに表示された予定Provider call総数と一致する`--confirm-provider-calls`の両方がなければ、
version/help preflightを含むsubprocessを起動しません。
`experiments/examples/workflow-ab.yaml`のmodelはfake受入専用であり、実Codex Liveには
使用しません。Live前に人間がexact model IDを明示したレビュー済みSpecを別途事前登録します。

```console
uv run agentlab run-workflow-campaign <reviewed-phase4-spec.yaml> \
  --plan .artifacts/workflow-ab/plan.json \
  --campaign .artifacts/workflow-ab/campaign.jsonl \
  --confirm-live-codex \
  --confirm-provider-calls 6

uv run agentlab report-workflow <reviewed-phase4-spec.yaml> \
  --plan .artifacts/workflow-ab/plan.json \
  --campaign .artifacts/workflow-ab/campaign.jsonl \
  --output .artifacts/workflow-ab/report.json \
  --markdown .artifacts/workflow-ab/report.md
```

`one_shot`と`staged`はいずれも単一Prompt、単一Provider process、単一turn、agent call 1です。
`staged`の調査、計画、テスト確認・追加、実装、自己レビューは一つのturn内の論理段階で、
内部の計画、reasoning、agent message、stage出力は保存・評価しません。比較軸はWorkflow
Promptだけで、Provider、exact model ID、reasoning effort、Fixture、Gate、sandbox、
network設定、timeout、停止条件はCampaign全体で固定します。詳細は
[docs/WORKFLOW_AB.md](docs/WORKFLOW_AB.md)を参照してください。

adapter用Prompt fileはArtifact root外のsystem temporary directoryだけに作成します。
cleanup結果はCampaignへ`cleared`／`failed`として保存し、失敗時は`cleanup_failure`で即時停止
してPrompt fileをbest-effortでredactします。offline reportはPlan、Campaign、Evidence、
Recordingのrun identity、outcome、Provider call数、Prompt/Fixture fingerprint、model、
reasoning effortを相互照合し、矛盾するpairを拒否します。

次はPhase 3で使用したmanual smokeのCLI形式です。実Codexを使うため、通常テストやCIでは
実行しません。承認済みmanual smokeは累計8試行で、過去runを再実行しません。新しい実行は
レビュー済みcommit、新しいSpec／run-id／出力先、個別の明示承認なしには行いません。

```console
uv run agentlab live-codex experiments/examples/codex-live-smoke.yaml \
  --task-id codex-live-smoke \
  --repetition-index 0 \
  --run-id codex-live-smoke-001 \
  --output .artifacts/evidence/codex-live-smoke-001.json \
  --confirm-live-codex
```

現在確認済みの`codex-cli 0.146.0-alpha.3.1`は
`headless_exec_explicit_never_v2` profileのversion allowlistと必須flagを満たし、
read-only preflightに成功します。このprofileは
`--config approval_policy="never"`をargvで明示し、存在しない
`--ask-for-approval`には依存しません。profile名、CLI version、明示設定の根拠を
Evidenceへ保存します。preflightを完了できない場合はprofileを`not_selected`とし、
approval policyを適用済みとは記録しません。preflight完了後もProvider起動前なら、選択済み
profileと確認済みflagを保持しつつ、approval policyはnullのままです。Codex Evidence 1.3は
runner、Popen試行、process生成、process group回収の観測状態を固定Enumで保持し、新規
Evidence 1.4はtop-level `error`をturn terminalと分離して数えます。Evidence 1.5は
PromptのOS stdin pipe書込みを`not_started`／`partial`／`complete`／`unknown`で観測し、
free-formなProvider失敗messageを保存せず固定Enumのadvisory hintへ分類します。このhintは
`failure_kind`や根本原因の代替ではありません。1.1〜1.4は従来の制約のままstrict load
できます。固定CLI sourceで到達可能なpre-turn warningと
`error`→`turn.failed`へparserをオフラインで合わせましたが、004の実際のevent列は
復元できず、この欠陥が004の直接原因だったとは断定しません。Failure Diagnosticは、
strict paired成果物を構築できない場合だけ固定Enumの観測値をatomic・上書き禁止で保存し、
Evidence／Recordingの代替でもReplay入力でもありません。

`--confirm-live-codex`なしではversion/help preflightを含むsubprocessを起動しません。
確認付き実行はCodex model APIへのPrompt送信とquota消費を伴います。Promptはargvへ
含めずstdinから渡し、Prompt本文、raw Codex JSONL、raw stderr、agent message、
reasoning、command output、thread/session IDをRecordingやEvidenceへ保存しません。
PromptはSHA-256とbyte数だけを保存します。
Evidence 1.5のstdin状態はHarnessがOS pipeへ書いたbyte数だけを表し、CodexがPromptを
読んだこと、model APIへ送ったこと、quotaを消費したことを証明しません。

Phase 3の認証対象は既存Codex CLIのChatGPT-managed authenticationだけです。
`OPENAI_API_KEY`と`CODEX_API_KEY`はProvider processへ継承せず、API key方式は未対応です。
Live実行前に`CODEX_HOME`が明示された絶対pathの既存directoryであることを要求し、
`HOME/.codex`へ暗黙fallbackしません。
Codex自身のmodel API通信は必要ですが、model-generated commandのnetwork accessと
web searchを無効化します。OS-levelの完全なnetwork遮断ではありません。詳細は
[docs/CODEX_PROVIDER.md](docs/CODEX_PROVIDER.md)を参照してください。

`doctor` はローカルコマンドの存在、バージョン、helpだけを確認します。ログイン、API呼び
出し、AIタスク実行はしません。`replay`も外部処理を実行せず、Specファイルからの相対
pathでRecordingを読みます。既存Resultを明示的に置換する場合だけ`--force`を指定します。
`--force`でも入力元のExperimentSpecやRecording、そのsymlink/hard linkは置換できません。

`run-gates`はlocal subprocessを起動するため、`--confirm-execution`を必須とします。
ExperimentSpecの`runner`にある相対`fixture_path`をSpecの親directoryから解決し、sourceを
直接実行せずsystem temporary directory内の使い捨てコピーでGateを実行します。Evidence
はstdout、stderr、終了状態、termination結果、diffを保存します。全commandが通常終了し、
diffの行数が完全で、Workspaceを削除できた場合だけRunMetricsを生成します。通常の非0
終了は品質不合格ですが、signal終了、timeout、起動失敗、output収集失敗、回収失敗、
Evidence不完全はHarness障害であり、Metricsは`null`です。

サンプルRecordingはReplay pipelineを検証するための合成fixtureです。Provider性能の
実験結果ではありません。形式と検証規則は
[docs/REPLAY_FORMAT.md](docs/REPLAY_FORMAT.md)を参照してください。

`experiments/examples/fixtures/runner-smoke`もRunnerとEvidenceを確認するためだけの小さな
合成Fixtureです。Phase 6で計画するmulti-language Task Fixtureではなく、Python
コーディング能力やProvider性能を測定しません。

Phase 2 RunnerはOS security sandboxではありません。process group、環境allowlist、
出力上限、使い捨てコピーによる事故範囲の縮小は行いますが、filesystemの完全隔離、
network遮断、CPU/memory quota、悪意あるprocessの完全な封じ込めは保証しません。
信頼済みのSpec、Fixture、commandだけに使用してください。詳細は
[docs/SAFE_RUNNER.md](docs/SAFE_RUNNER.md)を参照してください。
macOSはlocal process-tree testで検証済みです。Linux実装経路は有効ですが、同等の実機
またはCI検証が完了するまでは「対応設計済み・未検証」です。

## ロードマップ

- Phase 0: Foundation and Capability Spike（完了）
- Phase 1: Replay Vertical Slice（完了）
- Phase 2: Safe Runner, Evidence and Quality Gate（完了）
- Phase 3: Codex CLI Provider（完了）
- Phase 4: Workflow A/B Experiment（Current、offline/fake受入済み、Live A/B未実施）
- Phase 5: Antigravity CLI Provider
- Phase 6: Multi-language Fixtures and Public Report
- Phase 7: Optional Enhancements

詳細は [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。

> 実験結果は本ベンチマーク条件内のものです。一般的なモデル性能を断定するものでは
> ありません。Provider比較はモデル単体ではなく、モデル、利用可能なツール、Agent
> Harnessを含むシステム比較です。
