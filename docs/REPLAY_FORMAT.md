# Replay Recording Format

## 目的と範囲

Phase 1のReplay Recordingは、外部AIや品質Gateを再実行せず、保存済みMetricsから1件の
RunResultを再構成するためのUTF-8 JSONL契約である。1行が1個のJSON objectで、空行は
許可しない。各eventは`extra="forbid"`で未知fieldを拒否する。重複JSON key、`NaN`、
`Infinity`、`-Infinity`を拒否し、integerとboolean fieldの文字列・boolean等からの
暗黙変換を行わない。

Phase 1で扱うeventは`run_started`と`run_completed`の2件だけである。途中event、tool
call、stdout/stderr、diff、EvidenceはPhase 1の契約に含めない。

Phase 3は既存1.0を変更せず、redaction済みLive Recording 1.1を追加する。1.1も2イベント
だけで、開始後は成功なら`run_completed`、Metricsを生成できない失敗なら`run_failed`
を持つ。Prompt本文、raw Provider JSONL、stderr、agent message、reasoning、command、
file content、thread/session IDは含めない。

## Live Recording 1.1

`run_started`は既存ID/条件/時刻に加え、`execution_mode=live`、Prompt SHA-256とbyte数、
`prompt_redacted=true`、requested model/reasoning effort、CLI versionを保持する。

`run_completed`はProviderと全Gateが必要なEvidenceを残した場合に、Phase 3 RunMetrics、
redaction済みCodexExecutionEvidence 1.1（profile選択とProvider起動試行を分けるstageを
含む）、Gate種別ごとのcommand/pass/fail件数、
evaluation duration、diff/Workspace lifecycle summaryを保持する。品質Gate通常不合格でも
Metricsを生成できるためcompletedである。loaderはこのsummaryとMetricsを照合する。

`run_failed`はfailure kind、redaction済みCodexExecutionEvidence、同じ評価summaryを保持し、
`metrics_included=false`とする。Provider失敗、timeout、protocol/output上限、
process/Gate Harness、diff/cleanup不完全を品質結果へ変換しない。
loaderは`preflight_not_completed`と`workspace_lifecycle=not_created`、
`provider_invocation_attempted`と作成済みWorkspaceの対応も検証する。

1.1の開始、Provider、終了時刻はtimezone-aware UTCとし、開始条件のmodel、
reasoning effort、CLI versionはterminal Codex Evidenceと一致しなければならない。

1.1の成功記録は既存Replay経路で保存済みMetricsからRunResultを作る。Replay中にCodex、
subprocess、network、Gateを呼ばない。`run_failed`はMetricsがないため、failure kindを
示してReplay Result生成を拒否する。RunResultの`execution_mode`は`replay`のままである。

## `run_started`

| Field | Type | 意味 |
|---|---|---|
| `schema_version` | `"1.0"` | Event schema version |
| `sequence` | integer | 必ず`0` |
| `event_type` | `"run_started"` | Event種別 |
| `run_id` | string | Run識別子 |
| `experiment_id` | string | ExperimentSpec識別子 |
| `task_id` | string | Spec内のtask |
| `workflow` | `one_shot` / `staged` | 実行条件 |
| `provider` | `codex` / `antigravity` / `replay` | 記録されたProvider条件 |
| `repetition_index` | integer | 0始まりの反復index |
| `occurred_at` | timezone-aware datetime | 開始時刻 |

## `run_completed`

| Field | Type | 意味 |
|---|---|---|
| `schema_version` | `"1.0"` | Event schema version |
| `sequence` | integer | 必ず`1` |
| `event_type` | `"run_completed"` | Event種別 |
| `run_id` | string | 開始eventと同じRun識別子 |
| `experiment_id` | string | 開始eventと同じExperiment識別子 |
| `occurred_at` | timezone-aware datetime | 開始時刻以後の完了時刻 |
| `metrics` | `RunMetrics` | 保存済みの評価値 |

UsageMetricsの各値は`null`でもよく、欠損を0へ変換しない。いずれかの数値が存在する
場合は`source`を必須とする。`source: not_available`の場合はすべての数値を`null`に
限定する。float値は有限かつ非負とする。

## Recording不変条件

- sequenceは0から始まり、`0, 1`の連続した昇順で、重複・欠番を許可しない。
- 最初は`run_started`、最後は`run_completed`で、それぞれ1件だけとする。
- 両eventの`run_id`と`experiment_id`は一致する。
- 完了時刻は開始時刻より前でなく、両時刻はtimezone-awareとする。
- JSON構文エラーはRecording pathとJSONL行番号を伴うエラーにする。
- 不正UTF-8、JSON object以外、未知field、未対応schema/event、型不一致、重複key、
  非有限数を拒否する。

破損またはschema不一致のRecordingからResultを生成せず、出力fileも作成しない。

## ExperimentSpecとの照合

- `experiment_id`が一致し、`task_id`が`spec.task_ids`に含まれる。
- `repetition_index`は0以上かつ`spec.repetitions`未満とする。
- Specは明示的なReplay modeとReplay設定を持つ。
- Workflow比較ではworkflowがcontrol/treatmentsのいずれかで、providerは固定値と一致する。
- Provider比較ではproviderがcontrol/treatmentsのいずれかで、workflowは固定値と一致する。

相対的な`replay.recording_path`は、processの作業directoryではなく、ExperimentSpec
ファイルの親directoryを基準に解決する。例えば
`experiments/examples/workflow-smoke.yaml`内の`recordings/workflow-smoke.jsonl`は、
`experiments/examples/recordings/workflow-smoke.jsonl`を指す。

## 決定論的なRunResult

RunResultは開始eventのrun/experiment/task/workflow/provider/repetition条件、完了eventの
Metricsと`occurred_at`、明示的な`execution_mode: replay`から生成する。現在時刻を
注入しない。JSONはUTF-8、key sort、2-space indent、末尾newlineで保存するため、同じ
SpecとRecordingから同じbyte列を生成する。

既存Resultはデフォルトで上書きせず、明示的な`--force`を要求する。保存には同じdirectory
の完成済み一時fileを使い、通常保存は既存fileを置換しないatomic link、`--force`時は
`os.replace`を使用する。出力JSONでも非有限数を許可しない。失敗時は一時fileを除去する。

ExperimentSpecとReplay Recordingは入力証跡であり、`--force`でも出力先に指定できない。
`..`や絶対/相対pathの違いだけでなく、symlinkとhard linkによる同一fileも拒否する。

サンプルRecordingはpipeline確認用の合成fixtureであり、Provider性能を示す実験結果では
ない。
