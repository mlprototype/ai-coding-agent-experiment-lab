# ADR 0002: Live実行を必ずRecordし、通常CIではReplayだけを使う

- Status: Accepted
- Date: 2026-07-25

## Context

Live Agent実行は非決定的で、外部サービス、認証、quota、費用、Provider更新、networkに
依存する。通常CIから実行すると、再現性のない失敗、意図しない費用、外部への情報送信が
発生しうる。一方、Live結果のeventと実行条件がなければ、不具合の再現と評価処理の検証が
できない。

## Decision

将来のLive実行は明示的opt-inとし、Specに
`require_explicit_confirmation: true`が明記されていることを要求する。許可された入力、
正規化event、終了状態、能力・版、必要なEvidenceを必ずRecordする。Record前に秘密情報、
認証情報、完全な機密Promptを除去する。記録できないLive実行は有効な実験runとして
扱わない。

通常のローカルテストとCIはReplay Providerだけを使い、外部AIを呼ばない。Live smoke
testが必要になった場合は、通常CIと分離された手動承認workflow、費用上限、秘密管理、
停止条件を別途設計する。

## Consequences

- 評価とOrchestratorを高速かつ決定論的に回帰テストできる。
- Provider障害やquotaが通常CIを不安定にしない。
- Recording schema、redaction、retention、互換性管理が必要になる。
- Replayは過去の挙動を再現するもので、現在のProvider品質を保証しない。
