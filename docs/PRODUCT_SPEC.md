# Product Specification

## 背景

AIコーディングエージェントの評価は、課題や品質Gateだけでなく、Provider、モデル、
ツール、プロンプト、Workflow、実行環境にも影響される。条件と証跡が残らない試行は
再現できず、チーム標準を改善する根拠にならない。AI Coding Agent Experiment Labは、
開発プロセスに関する仮説を単一軸の実験として定義し、同じ契約で評価するR&D基盤である。

## 対象ユーザー

- AI支援開発の標準手順を設計する開発者、Tech Lead、Developer Productivity担当
- Coding Agent Providerの採用判断を行うPlatform Engineering担当
- 実験条件と判断根拠をレビューするEngineering Manager、セキュリティ担当

一般利用者向けのAIアプリケーションは対象外とする。

## ユースケース

1. Workflowだけを変え、`one_shot` と `staged` の品質と所要時間を比較する。
2. Workflowを固定し、Providerシステム間の結果を比較する。
3. 過去のLive実行記録をReplayし、データ処理と品質Gate評価を決定論的に検証する。
4. 実験Spec、実行証跡、評価結果を紐付け、意思決定の根拠を再確認する。
5. 検証済みの知見を標準手順へ反映する。

Phase 1ではSpec検証、ローカルCLI能力確認、および保存済み合成RecordingのReplayだけを
実行できる。

## 機能要件

### Phase 0

- ExperimentSpecはスキーマ版、仮説、比較軸、固定条件、反復、乱数seed、品質Gate、
  停止条件、明示的な実行モードを表現する。
- 一つのSpecでWorkflowとProviderを同時に変更できない。
- Replay/Liveの設定は実行モードと一致し、Liveは
  `require_explicit_confirmation: true`の明示入力を必須とする。
- RunMetricsは品質、時間、呼出回数、変更量を表現する。
- UsageMetricsは欠損可能で、欠損しても結果を保存できる。
- `agentlab validate` はYAMLを検証し、失敗理由と非0終了コードを返す。
- `agentlab doctor` はCodex/Antigravity CLIを読み取り専用で調査し、人間向けまたは
  JSONでCapabilityReportを返す。
- commandが利用不可の場合、CapabilityReportは実行path、version、supported能力を
  報告済みとして扱わない。

### Phase 1

- UTF-8 JSONL Recordingは`run_started`と`run_completed`を各1件だけ保持する。
- Recording loaderはschema、未知field、sequence、ID、時刻順を厳密に検証する。
- ReplayはSpecとRecordingのtask、workflow、provider、反復を照合する。
- RunResultはRecordingの完了時刻と保存済みMetricsから決定論的に生成する。
- 相対的なRecording pathはExperimentSpecファイルの親directoryを基準にする。
- Resultは既存fileを暗黙に上書きせず、一時fileとatomic replaceで保存する。
- Replayは外部AI、network、外部CLI、品質Gateコマンドを呼び出さない。

### 将来要件

- 隔離Runner、品質Gate実行、証跡保存、Live Provider、集計と比較レポートを段階的に
  追加する。実装順はROADMAPに従う。

## 非機能要件

- Python 3.12以上、src layout、型検査可能なコードを使う。
- データ契約はバージョン付きで、未知フィールドを拒否する。
- 同一記録から同一評価を再生成できる決定性を目指す。
- 外部コマンドは引数配列、短いtimeout、分離したstdout/stderrで起動し、
  `shell=True` を使わない。
- CLI未導入や能力不明を、実験失敗と混同せず明示する。
- 秘密情報、認証情報、機密プロンプトを成果物に保存しない。
- 通常CIはネットワークやLive AI Providerに依存しない。

## Non-goals

Phase 1では以下を実装しない。

- Codex/AntigravityのLive課題実行、OpenAI/Gemini API
- 品質Gateの実行、Docker、Git worktree
- Java/Python/React Task Fixture、LLM Judge、コスト計算、比較レポート
- GitHub ActionsからのLive実行、独立レビューエージェント
- 一般的なモデル能力ランキング
- 複数task、複数treatment、複数反復のscheduler

## 成功条件

- 必須データ契約と単一軸制約が自動テストされている。
- サンプルSpecをCLIで検証でき、不正Specは理由付きで拒否される。
- CLIがなくてもdoctorは正常終了し、JSON結果を処理できる。
- Capability Matrixは実測事実と`not_verified`を分離する。
- Provider境界、Record/Replay、任意Usage指標の意思決定がADRに残る。
- READMEが実装済み範囲と未実装範囲を正確に表す。
- 合成Recordingから同一内容・同一byte列のRunResultを外部呼出しなしで生成できる。
