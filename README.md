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

Phase 0〜2を完了し、現在は
**Phase 3: Codex CLI Provider** です。次を提供します。

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

Replayは引き続きRecording内の保存済みMetricsだけを再構成し、外部処理を呼びません。
通常テストは短時間のfake Codex executableだけを使い、実Codex、外部AI、network、
quotaを呼びません。Phase 3のmanual Live smokeは累計3回実行し、3回とも成功受入に
達していないため、Phase 3は引き続きCurrentです。003ではstrict lifecycle Evidenceを
構築できず、Evidence／Recordingは作成されませんでした。003のProvider起動、Prompt送信、
model API到達、quota消費は確定不能です。003は再実行せず、次回Liveには修正commitの
レビューと別の明示承認が必要です。
scheduler、staged Workflow、Workflow A/B、Antigravity、Provider比較、比較レポートは
未実装です。

## Quick Start

Python 3.12以上と[uv](https://docs.astral.sh/uv/)を使う例です。

```console
uv sync --extra dev
uv run agentlab doctor
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab validate experiments/examples/codex-live-smoke.yaml
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

次はPhase 3で使用したmanual smokeのCLI形式です。実Codexを使うため、通常テストやCIでは
実行しません。承認済みmanual smokeは累計3回で、003は再実行しません。新しい実行は
レビュー済みの別commitと個別の明示承認なしには行いません。

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
profileと確認済みflagを保持しつつ、approval policyはnullのままです。新規Codex
Evidence 1.3はrunner、Popen試行、process生成、process group回収の観測状態を固定Enumで
保持します。旧1.2の`provider_orchestration` fallbackはこの状態を正確に保持できないため、
2回目のProvider起動・Prompt送信・model API到達・quota消費は確定不能です。manual Live
受入は累計3回とも未達です。003はstrict lifecycle Evidence構築に失敗してpaired成果物が
なく、Provider活動を確定できません。新しいFailure Diagnosticは、この種の失敗時に
固定Enumの観測値だけをatomic・上書き禁止で保存します。Evidence／Recordingの代替でも
Replay入力でもなく、作成されても受入成功を意味しません。

`--confirm-live-codex`なしではversion/help preflightを含むsubprocessを起動しません。
確認付き実行はCodex model APIへのPrompt送信とquota消費を伴います。Promptはargvへ
含めずstdinから渡し、Prompt本文、raw Codex JSONL、raw stderr、agent message、
reasoning、command output、thread/session IDをRecordingやEvidenceへ保存しません。
PromptはSHA-256とbyte数だけを保存します。

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
- Phase 3: Codex CLI Provider（現在、manual Live smokeのoffline修正・再レビュー中）
- Phase 4: Workflow A/B Experiment
- Phase 5: Antigravity CLI Provider
- Phase 6: Multi-language Fixtures and Public Report
- Phase 7: Optional Enhancements

詳細は [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。

> 実験結果は本ベンチマーク条件内のものです。一般的なモデル性能を断定するものでは
> ありません。Provider比較はモデル単体ではなく、モデル、利用可能なツール、Agent
> Harnessを含むシステム比較です。
