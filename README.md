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

Phase 0を完了し、現在は **Phase 1: Replay Vertical Slice** です。次を提供します。

- バージョン付きExperimentSpec、RunMetrics、UsageMetrics、CapabilityReport
- YAML Specの検証
- Codex CLIとAntigravity CLIの読み取り専用能力確認
- 実験設計、Provider境界、ロードマップの文書
- `run_started`と`run_completed`だけを持つ厳密なJSONL Recording契約
- 1件の保存済みRecordingから1件のRunResultを生成・保存するReplay CLI

ReplayはRecording内の保存済みMetricsを再構成するだけです。外部AI、ネットワーク、
Codex/Antigravity CLI、品質Gateコマンドを実行しません。Runner、Gate実行、Evidence、
Live AI実行、複数runのscheduler、比較レポート生成は未実装です。

## Quick Start

Python 3.12以上と[uv](https://docs.astral.sh/uv/)を使う例です。

```console
uv sync --extra dev
uv run agentlab doctor
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab replay experiments/examples/workflow-smoke.yaml \
  --output .artifacts/runs/workflow-smoke-run-001.json
uv run pytest
uv run ruff check .
uv run mypy src
```

`doctor` はローカルコマンドの存在、バージョン、helpだけを確認します。ログイン、API呼び
出し、AIタスク実行はしません。`replay`も外部処理を実行せず、Specファイルからの相対
pathでRecordingを読みます。既存Resultを明示的に置換する場合だけ`--force`を指定します。

サンプルRecordingはReplay pipelineを検証するための合成fixtureです。Provider性能の
実験結果ではありません。形式と検証規則は
[docs/REPLAY_FORMAT.md](docs/REPLAY_FORMAT.md)を参照してください。

## ロードマップ

- Phase 0: Foundation and Capability Spike（完了）
- Phase 1: Replay Vertical Slice（現在）
- Phase 2: Safe Runner, Evidence and Quality Gate
- Phase 3: Codex CLI Provider
- Phase 4: Workflow A/B Experiment
- Phase 5: Antigravity CLI Provider
- Phase 6: Multi-language Fixtures and Public Report
- Phase 7: Optional Enhancements

詳細は [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。

> 実験結果は本ベンチマーク条件内のものです。一般的なモデル性能を断定するものでは
> ありません。Provider比較はモデル単体ではなく、モデル、利用可能なツール、Agent
> Harnessを含むシステム比較です。
