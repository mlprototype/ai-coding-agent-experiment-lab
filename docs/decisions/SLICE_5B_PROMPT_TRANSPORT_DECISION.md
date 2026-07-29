# Slice 5B Prompt Transport Decision Record

- Status: Blocked / Deferred
- Date: 2026-07-29
- Decision Driver: Antigravity CLI Provider Prompt Delivery Security Invariant

## Context

AgentLabにおけるAntigravity CLI Provider (Phase 5)の統合にあたり、Slice 5B (Headless Runner準備とオフライン統合)の実装可否を評価するための調査を行った。

Antigravity CLI (Headless mode) へ生成されたPrompt本文を安全に伝送する手段について、既存のセキュリティ契約および脅威モデルに基づき、公式ドキュメントからの確認状況と今後の対応方針を記録する。

## Existing Security Invariant

AgentLabのリポジトリセキュリティ契約 (`AGENTS.md`) では、以下が必須要件として定められている。

- Prompt本文をargv、Recording、Evidenceへ保存せず、stdinからだけ渡す。
- CLI未導入や未確認能力を推測で補完せず、`not_verified`として扱う。

Codex ProviderをはじめとするすべてのProviderにおいて、Prompt本文をプロセス起動引数 (argv) に含めることは厳格に禁止されている。

## Threat Model

Prompt本文がプロセス引数 (argv) に含まれる（argv transport）場合、以下の重大な情報漏洩リスクが生じる。

1. **process list**: `ps aux` や OS のプロセス一覧表示コマンド、システム監視ツール等を通じて、ローカルシステム上の非特権ユーザーや他プロセスから Prompt 本文が参照可能になる。
2. **shell history**: シェル経由で実行された場合、`.bash_history` や `.zsh_history` 等の履歴ファイルに Prompt 本文が平文で永続化される。
3. **logs / telemetry**: OS のプロセス起動ログ（Auditd, Sysmon, Event Log等）やサードパーティのプロセストラッキングツールに Prompt 本文が自動記録される。
4. **crash reports**: プロセス異常終了時やコアダンプ出力時、コマンドライン引数として Prompt 本文がクラッシュレポート内に含まれ、外部送信や永続化される。
5. **Evidence への意図しない残存**: プロセス起動引数がデバッグログや Harness のプロセス生成記録として保持された場合、AgentLab の Evidence や Recording 内に redacted されていない機密 Prompt が漏洩する。

## Official Documentation Evidence

使用した一次情報源（2026-07-29確認）:
- Google Antigravity Headless mode: `https://antigravity.google/docs/cli/headless`
- Google Antigravity CLI Reference: `https://antigravity.google/docs/cli/reference`
- Google Antigravity Permissions: `https://antigravity.google/docs/cli/permissions`
- Google Antigravity Sandbox: `https://antigravity.google/docs/cli/sandbox`
- Google Antigravity Changelog: `https://antigravity.google/changelog`

**確認された事実と要約:**
- 公式資料で明記されている Headless 実行形式は `-p` / `--prompt` のフラグ値として Prompt を指定する形式のみである。
- 公式資料において、標準入力 (stdin) や `--prompt-file` 等の non-argv transport に関する具体的な対応構文・バージョン・制約は確認できなかった。
- 「公式資料で確認できなかった」ことを「機能が存在しない」と断定することを意味しないが、確証がない状態での推測補完はリポジトリ規約により禁止されている。

## Transport Capability Classification

判定: **`not_verified`**

- 公式ドキュメントにおいて、非 argv の Prompt 伝送方式（stdin, prompt-file 等）の存在・動作・仕様を確認できないため、安全ルールに従い `not_verified` と判定する。

## Considered Options

1. **公式 non-argv transport が確認できるまで Slice 5B 実装を保留 (Adopted)**
   - 既存のセキュリティ契約 (argv exposure 禁止) を一切緩和せず維持する。
   - `not_verified` に基づき fail-closed とし、Slice 5B 実装を Blocked / Deferred にする。

2. **合成 Prompt 限定で argv exposure を承認して Slice 5B 実装を進める (Not Approved)**
   - 非機密なテスト用 Prompt に限って argv 経由での伝送を暫定許可する案。
   - 危険性: セキュリティ例外を導入することになり、既存の no-argv invariant を破壊する。また将来の実 Live 実行時に漏洩事故を引き起こすリスクがある。
   - 結論: **Not Approved**（承認しない）。

## Decision

1. **既存の no-argv security 契約を維持する。**
2. **argv transport 案は `Not approved` とする（非機密の合成 Prompt 限定であっても承認しない）。**
3. **公式 non-argv transport が確認できないため、Slice 5B は `Blocked / Deferred`（保留）とする。**
4. **Slice 5B の fake executable 限定オフライン実装を行う場合であっても、別途の明示的承認を必須とする。**
5. **Slice 5C の Live smoke には、さらに別の明示的承認を必須とする。**
6. **本 Decision Record は、Slice 5B 実装や Live 実行の承認を意味しない。**

## Consequences

- Antigravity CLI Provider の Slice 5B コード実装は一切行われず、fail-closed の状態が維持される。
- リポジトリ内のセキュリティ契約は堅牢に保護され、プロセス引数経由の Prompt 漏洩リスクは回避される。
- オフラインでの fake `agy` による Runner 実装も、今後の non-argv transport の公式確認または安全方針の再評価と明示承認が行われるまで保留される。

## Reconsideration Triggers

本 Decision は以下のいずれかが発生した場合に限り再評価・再検討を行う。

1. Antigravity 公式ドキュメントまたは公式リリースノートにおいて、標準入力 (stdin) や `--prompt-file` などの non-argv Prompt transport 仕様が明記・提供された場合。
2. プロジェクトオーナー／レビューアーにより、セキュリティ要件や Prompt 伝送アーキテクチャに関する明示的な方針変更・承認が行われた場合。

## Authorization Boundary for Slice 5B / Slice 5C

- **Slice 5B fake executable オフライン実装**: 未承認 (Unapproved)
- **Slice 5C Live smoke / Provider 実行**: 未承認 (Unapproved)
