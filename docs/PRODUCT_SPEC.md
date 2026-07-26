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

Phase 3ではSpec検証、ローカルCLI能力確認、保存済みRecordingのReplayに加え、
信頼済み合成Fixtureの使い捨てコピー上で品質Gateを実行し、Evidenceを保存できる。
明示確認された単一のone-shot Codex CLI実行だけを同じWorkspaceのGateへ接続できる。
通常テストとCIはfake CLIだけを使い、外部AI Providerを実行しない。

## 機能要件

### Phase 0

- ExperimentSpecはスキーマ版、仮説、比較軸、固定条件、反復、乱数seed、品質Gate、
  停止条件、明示的な実行モードを表現する。
- 一つのSpecでWorkflowとProviderを同時に変更できない。
- Replay/Liveの設定は実行モードと一致し、Liveは
  `require_explicit_confirmation: true`の明示入力を必須とする。
- RunMetricsは品質、時間、呼出回数、変更量を表現する。
- UsageMetricsは欠損可能で、欠損しても結果を保存できる。値がある場合はsourceを必須と
  し、`not_available`と数値の併存を拒否する。
- `agentlab validate` はYAMLを検証し、失敗理由と非0終了コードを返す。
- `agentlab doctor` はCodex/Antigravity CLIを読み取り専用で調査し、人間向けまたは
  JSONでCapabilityReportを返す。
- commandが利用不可の場合、CapabilityReportは実行path、version、supported能力を
  報告済みとして扱わない。

### Phase 1

- UTF-8 JSONL Recordingは`run_started`と`run_completed`を各1件だけ保持する。
- Recording loaderはschema、未知field、型、重複key、有限数、sequence、ID、時刻順を
  厳密に検証する。
- ReplayはSpecとRecordingのtask、workflow、provider、反復を照合する。
- RunResultはRecordingの完了時刻と保存済みMetricsから決定論的に生成する。
- 相対的なRecording pathはExperimentSpecファイルの親directoryを基準にする。
- Resultは完成済み一時fileからatomicに公開する。通常時は既存fileを置換せず、
  `--force`時だけreplaceする。SpecとRecordingは常に上書き対象外とする。
- Replayは外部AI、network、外部CLI、品質Gateコマンドを呼び出さない。

### Phase 2

- ExperimentSpecは任意の`runner`設定を持ち、既存Phase 0・1 Specはrunnerなしで読める。
- Runner設定は相対Fixture pathと、command timeout、termination grace、stdout/stderr、
  diffの現実的な正の上限を表現する。
- `agentlab run-gates`はtask/run/outputと`--confirm-execution`を必須とし、Specに
  列挙された品質Gate argvだけを実行する。
- Fixture sourceのroot/配下symlinkと特殊fileを拒否し、sourceではなくsystem temporary
  directory内のコピーを実行する。
- local commandは`cwd`をWorkspace、stdinを閉じ、`shell=False`、分離pipe、最小環境で
  新しいPOSIX session/process groupとして起動する。
- timeout時はSIGTERM、grace、SIGKILLで停止し、正常終了時もbackground childを回収する。
- stdout/stderrは別々に上限付きで最後までdrainし、truncationと不正UTF-8変換の有無を
  個別flagで記録する。
- 実行前後snapshotから、安定順のchanged file、text行数、上限付きunified diff、
  binary/non-UTF-8 pathを生成する。
- version付きEvidenceはcommand status、termination、Spec/Fixture hash、Runner設定、
  diff、failure kind、任意RunMetricsをstrict JSONとしてatomic保存する。
- 通常の非0終了とsignal終了、timeout、spawn、output収集、process cleanup、
  unsupported platform、Evidence errorを区別する。Harness障害または不完全な行数では
  Metricsを生成しない。
- `--force`でもSpec、Replay Recording、Fixture sourceとそのsymlink/hard linkを
  置換できない。

### Phase 3

- 既存LiveSettingsを後方互換に保ち、Prompt/model/reasoning、Provider timeout、
  Prompt/event/output上限をstrictなPhase 3設定として追加する。
- `agentlab live-codex`はlive/codex/one_shot、Runner、task、repetition、完全なLive設定、
  `--confirm-live-codex`を必須とする。
- 確認flagなしではread-only preflightを含むsubprocessを起動しない。
- preflightはPATH、`--version`、`exec --help`、必要flagだけを固定出力上限、timeout、
  新規process groupで確認し、Login、auth file読取り、AI呼出しを行わない。flag不足、
  UTF-8不正、timeout、残存process、preflight一時directory削除失敗はfail closedし、
  一時directoryの作成・準備・cleanup失敗はProvider能力ではなくHarness障害として扱う。
  process group回収失敗は一時directoryやWorkspaceのcleanup失敗より優先し、実際の
  preflight termination情報とともに保持する。`mkdtemp()`後のpath解決に失敗した場合も
  作成済みrootを削除し、Live Workspaceは削除結果をlifecycleへ記録する。
- PromptはSpec基準の通常UTF-8 fileから上限付きで一度読み、stdinだけで渡す。本文を
  argv、Recording、Evidenceへ保存せず、SHA-256、byte数、redacted flagだけを保存する。
- Codex processはworkspace-write、明示configによるapproval never、ephemeral、JSONL、
  user config/rules無視、strict configで起動し、web searchとmodel-generated command
  networkを無効にする。CLI profile、version、execution stageをEvidenceへ保存し、
  approval値と根拠はProvider argvによる起動を試みた場合だけ保存する。
- ChatGPT-managed CLI authだけを対象とし、API key、auth file copy/parseを実装しない。
  `CODEX_HOME`は明示された絶対pathの既存directoryを必須とし、暗黙fallbackしない。
  CodexとGateの環境を分離し、Gateへ`CODEX_HOME`を渡さない。
- JSONLはincrementalにUTF-8、JSON object、duplicate key、有限数、line/total上限、
  lifecycleを検証する。raw payloadを保存せずthread/turn/terminal/event/item件数と
  Provider報告Usageだけを正規化し、item type keyを安全なEnumへ限定する。
- Provider成功時だけ同じ使い捨てWorkspaceでPhase 2 Gateを実行する。Provider失敗、
  signal終了、Gate通常不合格、Harness障害を別taxonomyで保存する。Workspace状態は
  `not_created`、`removed`、`cleanup_failed`で区別する。
- Provider process生成後のselector、pipe、収集処理の未知例外でもprocess groupを回収し、
  Evidence収集失敗またはprocess cleanup失敗として扱う。
- Recording 1.1はredaction済み`run_started`と`run_completed`または`run_failed`の2件
  だけを保存する。terminal eventにはGate件数、evaluation duration、diff、Workspace
  lifecycleのredaction済みsummaryを含め、成功時はMetricsと照合する。成功Recordingは
  外部呼出しなしでReplayでき、失敗RecordingはMetrics欠損理由付きで拒否する。
- Live EvidenceはRecording SHA-256を一方向参照し、raw Prompt/JSONL/stderrを保存しない。

### 将来要件

- Antigravity Provider、scheduler、Workflow実験、集計と比較レポートを段階的に追加する。
  実装順はROADMAPに従う。

## 非機能要件

- Python 3.12以上、src layout、型検査可能なコードを使う。
- データ契約はバージョン付きで、未知フィールドを拒否する。
- 同一記録から同一評価を再生成できる決定性を目指す。
- 外部コマンドは引数配列、短いtimeout、分離したstdout/stderrで起動し、
  `shell=True` を使わない。
- CLI未導入や能力不明を、実験失敗と混同せず明示する。
- 秘密情報、認証情報、機密プロンプトを成果物に保存しない。
- 通常CIはネットワークやLive AI Providerに依存しない。
- Live Codexは外部送信とquota消費を表示し、人間の明示確認を必須とする。
- Gate実行には明示確認を要求し、親processの環境を無条件に継承しない。
- Harness障害を品質Gate不合格へ変換しない。
- Phase 2 RunnerをOS security sandboxやnetwork隔離として扱わない。

## Non-goals

Phase 3では以下を実装しない。

- AntigravityのLive課題実行、OpenAI/Gemini API直接呼出し、API key認証
- Prompt本文、raw JSONL、reasoning、agent message、command outputの保存
- session resume、任意Codex追加flag、実行中の自動retry
- Docker、VM、Git worktree、seccomp、user namespace、firewall
- filesystemの完全隔離、CPU/memory/process quota、悪意あるcodeの完全な封じ込め
- Java/Python/Reactの実Task Fixture、LLM Judge、コスト計算、比較レポート
- GitHub ActionsからのLive実行、独立レビューエージェント
- 一般的なモデル能力ランキング
- 複数task、複数treatment、複数反復、stop conditionのscheduler
- Workflow A/B実験、Provider比較、framework固有diagnostic parser
- 実Codexの通常テスト/CI実行、外部networkの完全遮断

## 成功条件

- 必須データ契約と単一軸制約が自動テストされている。
- サンプルSpecをCLIで検証でき、不正Specは理由付きで拒否される。
- CLIがなくてもdoctorは正常終了し、JSON結果を処理できる。
- Capability Matrixは実測事実と`not_verified`を分離する。
- Provider境界、Record/Replay、任意Usage指標の意思決定がADRに残る。
- READMEが実装済み範囲と未実装範囲を正確に表す。
- 合成Recordingから同一内容・同一byte列のRunResultを外部呼出しなしで生成できる。
- 明示確認後、合成Fixtureの使い捨てコピー上で全Gateを実行し、再読込可能なEvidenceを
  保存できる。
- timeout時と正常終了時にprocess groupを回収し、親環境の合成secretを子へ渡さない。
- Gate不合格ではcommand単位Metricsを生成し、signal終了を含むHarness障害ではMetricsを
  `null`にする。
- 実行に用いたSpecモデルとEvidenceのSpec SHA-256を、同じ一回の入力bytesから生成する。
- Phase 1 Replayのbyte決定性とSpec/Recording入力保護が後退しない。
- redaction済みLive Recording 1.1を再読込し、成功記録をoffline Replayできる。
- Prompt、raw JSONL、stderr、thread/session ID、認証情報がArtifactに存在しない。
- Provider失敗時はGateを実行せず、Provider/Gate/Harness failureを区別する。
- 現行CLIのread-only preflightが成功しても、実Codex manual smokeが成功するまでは
  Phase 3を完了扱いにしない。
- manual Live smokeは実装レビュー後の明示承認まで未実行とする。
