# Slice 5B Prompt Transport Decision Record

- Status: Blocked / Deferred
- Date: 2026-07-29
- Decision Driver: Antigravity CLI Provider Prompt Delivery Security Invariant & Capability Verification

## Context

AgentLabにおけるAntigravity CLI Provider (Phase 5)の統合にあたり、Slice 5B (Headless Runner準備とオフライン統合)の実装可否を評価するための調査を行った。

Antigravity CLI (Headless mode) へ生成されたPrompt本文を安全に伝送する手段について、既存のセキュリティ契約および脅威モデルに基づき、公式ドキュメントおよび公式Changelogからの確認状況と今後の対応方針を記録する。

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

## Official Documentation Evidence & Investigation Trace

使用した一次情報源（2026-07-29確認）ごとの調査証跡および個別判定は以下の通り。

| 情報源 | URL / タイトル | Version／日付 | 確認日 | 確認語／検索語 | 確認結果 | 個別判定 |
| --- | --- | --- | --- | --- | --- | --- |
| Headless mode | `https://antigravity.google/docs/cli/headless`<br>Google Antigravity Docs - Headless mode | CLI 1.1.8 | 2026-07-29 | `stdin`, `prompt-file`, `piped`, `-p` | `-p`/`--print`/`--prompt` による flag 形式の記載が中心。stdin や `--prompt-file` の明示的な実行構文は未掲載。 | 部分確認 (flag形式中心) |
| CLI Reference | `https://antigravity.google/docs/cli/reference`<br>Google Antigravity Docs - CLI Reference | CLI 1.1.8 | 2026-07-29 | `stdin`, `--prompt-file`, `file input`, `@file` | Headless Prompt transportや-p／--promptの仕様は未掲載 | 確認不可 (transport対象外) |
| Changelog 1.1.2 | `https://antigravity.google/changelog`<br>Google Antigravity Changelog | CLI 1.1.2<br>(2026-07-13) | 2026-07-29 | `stdin`, `piped prompt` | 「piped promptによってstdinが使用される場合への対応」についての記述を確認。piped Prompt 伝送経路の存在に公式言及。 | 対応への公式言及あり |
| Changelog 1.1.1 | `https://antigravity.google/changelog`<br>Google Antigravity Changelog | CLI 1.1.1<br>(2026-07-10) | 2026-07-29 | `stdin`, `prompt flag` | 「Promptをflagで指定した場合はstdinを読まないよう修正」についての記述を確認。Prompt 伝送方式に応じた stdin 読み取り分岐の存在に公式言及。 | transport分岐への公式言及あり |
| Permissions | `https://antigravity.google/docs/cli/permissions`<br>Google Antigravity Docs - Permissions | CLI 1.1.8 | 2026-07-29 | `stdin`, `prompt` | パーミッションモデルおよび Headless での soft-deny 仕様を記載。Prompt 伝送に関する記載なし。 | 確認不可 (対象外) |
| Sandbox | `https://antigravity.google/docs/cli/sandbox`<br>Google Antigravity Docs - Sandbox | CLI 1.1.8 | 2026-07-29 | `stdin`, `prompt` | サンドボックス制限仕様を記載。Prompt 伝送構文の記載なし。 | 確認不可 (対象外) |

### 調査結果の分析と事実

- 公式 Changelog 1.1.1 および 1.1.2 にて、Antigravity CLI に「piped prompt による stdin 読み取り機能」および「flag 指定時との stdin 処理分岐」が存在することが明記されている。
- しかし、現行の Headless mode ドキュメントおよび CLI Reference では、piped Prompt (stdin) 実行時の完全なコマンド構文（例: `cat prompt.txt | agy -p` なのか `agy` 単体なのか）、フラグとの併用規約、入力終了条件（EOF判定等）、エラー処理、およびバージョンごとの動作制約が明確に定義されていない。

## Transport Capability Classification

判定: **`not_verified`**

- **判定理由**: 公式 Changelog において stdin (piped Prompt) 伝送経路の存在に対する公式な言及は確認されたものの、AgentLab が安全かつ決定論的に profile 化・実行契約として固定できるレベルで構文・バージョン対応・動作制約をドキュメントから確定できないため、リポジトリ規約に従い `not_verified` として fail-closed に分類する。

## Considered Options

1. **公式 non-argv transport の実行契約が確定するまで Slice 5B 実装を保留 (Adopted)**
   - 既存のセキュリティ契約 (argv exposure 禁止) を一切緩和せず維持する。
   - Changelog に stdin への言及はあるが、仕様・構文が不完全であるため `not_verified` とし、Slice 5B 実装を Blocked / Deferred とする。

2. **合成 Prompt 限定で argv exposure を承認して Slice 5B 実装を進める (Not Approved)**
   - 非機密なテスト用 Prompt に限って argv 経由での伝送を暫定許可する案。
   - 危険性: セキュリティ例外を導入することになり、既存の no-argv invariant を破壊する。また将来の実 Live 実行時に漏洩事故を引き起こすリスクがある。
   - 結論: **Not Approved**（承認しない）。

3. **Changelog の記述のみを根拠に piped Prompt (stdin) 対応として実装を進める (Not Approved)**
   - Changelog に piped prompt の記載があることから、推測で CLI 実行構文を補完して Slice 5B を実装する案。
   - 危険性: 「CLI未導入や未確認能力を推測で補完せず `not_verified` として扱う」という `AGENTS.md` のルールに違反する。
   - 結論: **Not Approved**（承認しない）。

## Decision

1. **既存の no-argv security 契約を維持する。**
2. **argv transport 案は `Not approved` とする（非機密の合成 Prompt 限定であっても承認しない）。**
3. **公式 Changelog に stdin (piped Prompt) 伝送経路の存在が明記されていることは確認したものの、具体的かつ決定論的な実行構文・動作制約が確認できないため、判定を `not_verified` とし Slice 5B は `Blocked / Deferred`（保留）とする。**
4. **Slice 5B の fake executable 限定オフライン実装を行う場合であっても、別途の明示的承認を必須とする。**
5. **Slice 5C の Live smoke には、さらに別の明示的承認を必須とする。**
6. **本 Decision Record は、Slice 5B 実装や Live 実行の承認を意味しない。**

## Consequences

- Antigravity CLI Provider の Slice 5B コード実装は一切行われず、fail-closed の状態が維持される。
- リポジトリ内のセキュリティ契約は堅牢に保護され、プロセス引数経由の Prompt 漏洩リスクは回避される。
- 公式ドキュメントで具体的な実行契約が明確になり、その対象version／help markerを別途承認されたread-only Preflightで確認できるまで、Slice 5B実装を保留する。

## Reconsideration Triggers

本 Decision は以下のいずれかが発生した場合に限り再評価・再検討を行う。

1. Antigravity 公式ドキュメントにおいて、piped Prompt (stdin) や `--prompt-file` などの non-argv Prompt transport の具体的なコマンド構文・パラメータ・制約仕様が更新・公表され、かつその対象version／help markerを別途承認されたread-only Preflightで確認できる状態となった場合。
2. プロジェクトオーナー／レビューアーにより、セキュリティ要件や Prompt 伝送アーキテクチャに関する明示的な方針変更・承認が行われた場合。

## Authorization Boundary for Slice 5B / Slice 5C

- **Slice 5B fake executable オフライン実装**: 未承認 (Unapproved)
- **Slice 5C Live smoke / Provider 実行**: 未承認 (Unapproved)
