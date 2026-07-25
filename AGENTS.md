# Repository Rules for Coding Agents

## Scope and truthfulness

- 実装前に対象Phase、受入条件、Non-goalsを確認する。
- 現在のPhaseを越える実装は、明示的な承認なしに追加しない。
- 未実装機能をREADMEや文書で実装済みと表現しない。
- 実験結果を一般的なモデル性能として断定しない。課題、Gate、環境、時期へ限定する。
- Provider比較はモデル、ツール、Agent Harnessを含むシステム比較として扱う。

## Provider and execution safety

- Live Providerを通常テストやCIから呼び出さない。通常テストはReplayを使う。
- Phase 1 Replayから外部AI、network、subprocess、品質Gateコマンドを呼び出さない。
- 相対的なRecording pathはExperimentSpecファイルの親directoryを基準に解決する。
- 合成RecordingをProvider性能の実験結果として扱わない。
- Live実行を追加する場合は明示的opt-in、Record、redaction、timeout、停止条件を必須に
  する。
- 外部コマンドは引数配列で起動し、`shell=True`を使用しない。
- stdoutとstderrを分離し、短いtimeoutと終了コードを扱う。
- CLI未導入や未確認能力を推測で補完せず、`not_verified`として扱う。

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
  --output .artifacts/runs/workflow-smoke-run-001.json
```

- 完了時は変更ファイル、実行したテストと結果、未完了事項、環境制約を報告する。
