# CLI Capability Matrix

最終read-only再確認: 2026-07-26（Asia/Tokyo）

この文書はPhase 3実装時のローカル環境で、指定された読み取り専用コマンドだけを実行した
結果である。利用できないCLIの能力は推測しない。CLIや環境の更新後は
`agentlab doctor --json`と元コマンドで再確認する。

| 項目 | Codex CLI | Google Antigravity CLI |
|---|---|---|
| PATH上のcommand | `/Applications/ChatGPT.app/Contents/Resources/codex` | `not_verified`（`agy`未検出） |
| Version | `codex-cli 0.146.0-alpha.3.1` | `not_verified` |
| 指定help | `codex exec --help` 成功 | `not_verified` |
| Non-interactive | verified: helpに「Run Codex non-interactively」 | `not_verified` |
| Structured output | verified: `--json`でJSONL event出力 | `not_verified` |
| Phase 3 CLI profile | `headless_exec_internal_never_v1`のread-only preflight成功 | `not_verified` |
| Usage metrics | `not_verified`（指定helpに明示なし） | `not_verified` |
| Live課題実行 | 未実施 | 未実施 |

## 確認できた事実

### Codex CLI

- `command -v codex`相当で上記の実行pathを確認した。
- `codex --version`は終了コード0で上記versionを返した。
- `codex exec --help`は終了コード0で、非対話実行、`--json`によるJSONL event出力、
  `--output-schema`を表示した。
- Phase 3で必要な`--ephemeral`、`--sandbox`、`--skip-git-repo-check`、
  `--ignore-user-config`、`--ignore-rules`、`--strict-config`、`--model`、`--config`を
  helpで確認した。`--ask-for-approval`はこのversionの`exec --help`に存在せず、
  Phase 3の現profileも要求しない。
- 上記version/flag集合で、boundedなread-only Phase 3 preflightが成功した。
- [OpenAI Codex公式source](https://github.com/openai/codex/blob/main/codex-rs/exec/src/lib.rs)
  ではheadless modeのapproval policyが既定で`Never`へ設定される。Evidenceはこの根拠を
  profile名とCLI versionへ結び付ける。
- 起動時にPATH aliasを作成できない旨のwarningがstderrへ出たが、version/help確認は
  成功した。

### Google Antigravity CLI

- `command -v agy`相当でcommandを検出できなかった。
- そのため`agy --version`と`agy --help`はcommand not foundとなった。

## 未確認事項

- どちらのProviderについても認証、Login状態、API接続、Live AI実行、実際のevent
  schema、Token/コスト取得、quota情報、終了/timeout時の挙動は確認していない。
- Codex JSONL parserと変換はfake CLIでoffline検証済みだが、実CLI eventとのLive統合は
  manual smoke未実施のため未確認である。
- Antigravityの非対話実行、構造化出力、Usage指標を含む全能力は`not_verified`である。
