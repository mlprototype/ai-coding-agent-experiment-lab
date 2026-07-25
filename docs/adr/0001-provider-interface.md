# ADR 0001: Providerを共通インターフェースの背後に置く

- Status: Accepted
- Date: 2026-07-25

## Context

Codex CLI、Antigravity CLI、Replayは、起動方法、event形式、能力、Usage指標、失敗表現が
異なる。Orchestratorや評価処理が各CLIの詳細へ依存すると、Provider変更とWorkflow変更が
混ざり、通常テストも外部サービスに依存する。

## Decision

Provider固有の起動とevent解釈をadapterへ閉じ込め、Orchestratorには正規化した要求、
event、終了状態、Capabilityを渡す。Replayも同じ境界を実装する。共通化するのは実験に
必要な最小意味だけとし、未確認能力を架空の共通機能で埋めない。

Phase 0では境界のデータ契約とCapability確認を定めるが、実行interfaceや未使用adapter
classは先行実装しない。Phase 1のReplay vertical sliceで必要な形を確定する。

## Consequences

- Orchestrator、Gate、結果処理をProvider非依存にテストできる。
- Provider比較でHarness差を記録しやすくなる。
- Provider固有情報の一部は拡張eventまたはEvidenceとして保持する必要がある。
- 最小共通分母へ過度に抽象化しないため、adapter間に意図的な非対称性が残りうる。

