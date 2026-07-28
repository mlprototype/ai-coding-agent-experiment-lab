# Repository Rules for Coding Agents

## Scope and truthfulness

- 実装前に対象Phase、受入条件、Non-goalsを確認する。
- 現在のPhaseを越える実装は、明示的な承認なしに追加しない。
- 未実装機能をREADMEや文書で実装済みと表現しない。
- 実験結果を一般的なモデル性能として断定しない。課題、Gate、環境、時期へ限定する。
- Provider比較はモデル、ツール、Agent Harnessを含むシステム比較として扱う。

## Provider and execution safety

- Live Providerを通常テストやCIから呼び出さない。Provider経路の通常テストはReplay
  または短時間のfake executableを使い、Runner統合テストも合成local helperだけを使う。
- Phase 1 Replayから外部AI、network、subprocess、品質Gateコマンドを呼び出さない。
- 相対的なRecording pathはExperimentSpecファイルの親directoryを基準に解決する。
- 合成RecordingをProvider性能の実験結果として扱わない。
- `--force`でも入力SpecやRecording、そのsymlink/hard linkを上書きしない。
- Recordingでは型強制、重複JSON key、非有限数を許可しない。
- Live実行を追加する場合は明示的opt-in、Record、redaction、timeout、停止条件を必須に
  する。
- 外部コマンドは引数配列で起動し、`shell=True`を使用しない。
- stdoutとstderrを分離し、短いtimeoutと終了コードを扱う。
- CLI未導入や未確認能力を推測で補完せず、`not_verified`として扱う。
- Gate実行には`--confirm-execution`による明示確認を要求する。
- Fixture sourceを直接実行・変更せず、検証済みの使い捨てコピーだけを実行する。
- timeout時だけでなくcommand終了時もprocess groupの残存子processを回収する。
- 親processの環境を丸ごと継承せず、秘密情報を子processへ渡さない。
- stdout、stderr、diffの設定上限を超えてEvidenceへ保持しない。
- signal終了、timeout、spawn、process回収、Evidence収集のHarness障害を品質不合格へ
  変換しない。
- Phase 2 Safe Runnerをsecurity sandbox、network隔離、完全なfilesystem隔離と表現しない。
- 通常テストとCIでLive Providerを呼ばず、短時間のfake executableだけを使う。
- Live Codexは`--confirm-live-codex`による明示確認を必須とする。
- 実装agentから実Codex CLIを再帰的に起動しない。version/helpのread-only確認だけを
  preflightとして許可する。
- Prompt本文をargv、Recording、Evidenceへ保存せず、stdinからだけ渡す。
- raw Codex JSONL、raw stderr、agent message、reasoning、command outputを保存しない。
- `OPENAI_API_KEY`と`CODEX_API_KEY`をProvider processへ渡さない。
- Codex processとGate processの環境を分離し、Gateへ`CODEX_HOME`を渡さない。
- Provider失敗、Gate通常不合格、Harness障害を異なるfailure kindとして扱う。
- 実Codex Live smokeはレビュー後に手動で1回ずつ実行し、通常CIへ追加しない。

## Data and security

- 永続化する契約にschema versionを付け、未知フィールドを黙って無視しない。
- 秘密情報、認証情報、Token、完全なPrompt内の機密情報を成果物、fixture、log、
  recordingへ保存しない。
- Usage欠損を0として保存しない。Provider報告値と推定値を区別する。

## Engineering workflow

- 既存変更を確認し、無関係なユーザー変更を上書きしない。
- 必要になるまで将来用の空directory、抽象class、依存関係を追加しない。
- Python 3.12以上、src layout、Pydantic v2、Typerを維持する。
- 次のコマンドを実行する。

```console
uv run pytest
uv run ruff check .
uv run mypy src
```

- CLI契約を変更した場合は、次も実行する。

```console
uv run agentlab doctor --json
uv run agentlab validate experiments/examples/workflow-smoke.yaml
uv run agentlab replay experiments/examples/workflow-smoke.yaml \
  --output .artifacts/runs/workflow-smoke-run-001.json \
  --force
uv run agentlab run-gates experiments/examples/workflow-smoke.yaml \
  --task-id smoke-task \
  --run-id phase2-runner-smoke-001 \
  --output .artifacts/evidence/phase2-runner-smoke-001.json \
  --confirm-execution \
  --force
```

- 完了時は変更ファイル、実行したテストと結果、未完了事項、環境制約を報告する。
