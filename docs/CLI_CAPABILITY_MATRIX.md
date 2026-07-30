# CLI Capability Matrix

最終read-only再確認: 2026-07-30（Asia/Tokyo）

この文書は、Phase 3のread-only CLI確認とPhase 5の実行前artifact静的監査を記録する。
利用できないCLIの能力は推測しない。CLIや環境の更新後は、承認されたread-only手順で
再確認する。

| 項目 | Codex CLI | Google Antigravity CLI |
|---|---|---|
| PATH上のcommand | `/Applications/ChatGPT.app/Contents/Resources/codex` | `not_verified`（`agy`未検出、標準配置なし） |
| Version | `codex-cli 0.146.0-alpha.3.1` | `not_verified`（payload manifestは1.1.8、binary未実行） |
| 指定help | `codex exec --help` 成功 | `not_verified`（binary受入拒否のため未実行） |
| Non-interactive | verified: helpに「Run Codex non-interactively」 | `not_verified` |
| Structured output | verified: `--json`でJSONL event出力 | `not_verified` |
| Phase 3 CLI profile | `headless_exec_explicit_never_v2`のallowlist/read-only preflight成功 | `not_verified` |
| Platform artifact受入 | 対象外 | `rejected`（macOS署名検証失敗） |
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
- 上記versionをprofileのallowlistへ固定し、必要flag集合でboundedなread-only Phase 3
  preflightが成功した。allowlist外のversionはflagが同じでも拒否する。
- [OpenAI Codex 0.146.0-alpha.3.1 releaseの固定commitにある公式source](https://github.com/openai/codex/blob/ff75c5b939c477c49eb1bd5248da6dab71b109d1/codex-rs/exec/src/lib.rs)
  ではheadless modeの初期approval policyが、解決後の`approvals_reviewer`に応じて
  再構築されうる。Phase 3は内部既定へ依存せず、
  `--config approval_policy="never"`を明示し、その根拠をprofile名とCLI versionへ
  結び付ける。
- 起動時にPATH aliasを作成できない旨のwarningがstderrへ出たが、version/help確認は
  成功した。

### Google Antigravity CLI

- `command -v agy`相当でcommandを検出できなかった。
- 標準配置先`$HOME/.local/bin/agy`にもbinaryは存在しない。
- 公式manifestから取得したAntigravity CLI 1.1.8 `darwin_arm64` payloadは、manifest記載
  SHA-512と実測SHA-512が完全一致し、archive安全性とMach-O arm64形式を確認した。
- embedded signature、TeamIdentifier、Hardened Runtimeは存在したが、
  `codesign --verify --deep --strict`はexit code 1と
  `invalid signature (code or signature have been modified)`で失敗した。
- 改ざんや悪意は断定せず、`upstream_artifact_signature_invalid`としてbinary受入を
  拒否した。binaryは配置・実行せず、`agy --version`と`agy --help`も実行していない。
- `payload_integrity=verified`、`archive_safety=verified`、
  `platform_signature=failed`、`binary_acceptance=rejected`である。
- stdin transportは`not_verified`、Phase 5／Slice 5B／Slice 5Cは`Blocked`である。

## 未確認事項

- どちらのProviderについても認証、Login状態、API接続、Live AI実行、実際のevent
  schema、Token/コスト取得、quota情報、終了/timeout時の挙動は確認していない。
- Codex JSONL parserと変換はfake CLIでoffline検証済みだが、実CLI eventとのLive統合は
  manual smoke未実施のため未確認である。
- AutoReview相当の設定を置くfakeテストは明示configがargvへ含まれることだけを確認し、
  実CLIのcloud/managed config解決後の最終approval policyは確認していない。
- Antigravityの非対話実行、構造化出力、Usage指標、stdin transportを含むProvider能力は
  すべて`not_verified`である。認証、Provider call、Prompt送信、quota利用、Live実行は
  0件である。
