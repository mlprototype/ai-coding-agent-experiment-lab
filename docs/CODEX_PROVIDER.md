# Codex CLI Provider

## Scope

Phase 3は、1 task・1 Codex Provider・1 repetition・`one_shot`を人間が手動実行する最小
vertical sliceである。scheduler、staged Workflow、比較実験、自動retryはPhase 4以降で
あり、実装していない。通常テストはfake Codexだけを使う。manual Live smokeは累計3回
実行し、3回とも成功受入に達していない。003は再実行せず、次回Liveには新しい修正commit
のレビューと別の明示承認が必要である。

## Read-only preflight

`live-codex`は確認flag、Spec/Prompt/Fixture/output検証後、`codex`のPATH存在、
`codex --version`、`codex exec --help`だけを固定上限、短いtimeout、分離stdout/stderr、
strict UTF-8、`shell=False`で確認する。各probeも新規POSIX session/process groupで
起動し、timeout、正常終了後の残存子process、SIGTERM無視時のSIGKILLをboundedに扱う。
selector生成や収集loopの未知例外でもprocess groupを緊急回収する。一時directoryの
作成・Workspace準備・cleanup失敗はProvider能力エラーへ変換せず`evidence_error`とするが、
process group回収も失敗した場合は`process_cleanup_error`を優先する。preflight commandの
process回収に失敗した場合は、実際のtermination情報を
`preflight_not_completed`のCodex Evidenceへ保持する。`mkdtemp()`後は未解決pathを先に
保持し、path解決に失敗しても作成済みのpreflight rootをcleanupする。
AI Prompt、Login、auth file読取り、network refreshは行わない。

helpには`--json`、`--ephemeral`、`--sandbox`、
`--skip-git-repo-check`、`--ignore-user-config`、`--ignore-rules`、`--strict-config`、
`--model`、`--config`がすべて必要である。不足時は互換性を推測せずfail closedする。
現在のprofileは`headless_exec_explicit_never_v2`で、対応versionを
`codex-cli 0.146.0-alpha.3.1`へallowlistする。flag集合が同じでもallowlist外のversionは
model呼出し前にfail closedする。
[OpenAI Codex 0.146.0-alpha.3.1の固定commitにあるheadless `exec`実装](https://github.com/openai/codex/blob/ff75c5b939c477c49eb1bd5248da6dab71b109d1/codex-rs/exec/src/lib.rs)
では初期値の`Never`が解決後の`approvals_reviewer`に応じて再構築されうるため、内部既定や
`--ignore-user-config`だけを根拠にしない。profileはCLIに存在しない
`--ask-for-approval`を要求せず、`--config approval_policy="never"`を明示する。
永続化するpreflight metadataはprofile、CLI version、確認時刻、flag名、明示approval
設定の根拠で、実行pathは保存しない。選択済みprofileはstatusにかかわらず必須flag集合との
完全一致を要求する。preflight未完了時はprofileを`not_selected`、execution stageを
`preflight_not_completed`、approval policy/basisをnullにする。preflight完了後、
Provider起動試行前の失敗ではallowlist済みversion、profile、flagを保持し、stageを
`preflight_completed`、approval policy/basisをnullにする。argvを使った起動を試みた
時点でstageを`provider_invocation_attempted`とし、初めて`never`と
`explicit_config_never`を記録する。対応versionや設定方法を変える場合は、同じprofileを
書き換えず別profileとして追加する。

## Fixed invocation and Prompt

構築するargvの意味は次で固定し、CLIから追加flagを受け付けない。

```text
codex exec
  --json
  --ephemeral
  --sandbox workspace-write
  --skip-git-repo-check
  --ignore-user-config
  --ignore-rules
  --strict-config
  --model <Spec model>
  --config approval_policy="never"
  --config model_reasoning_effort="<Spec effort>"
  --config sandbox_workspace_write.network_access=false
  --config web_search="disabled"
  -
```

最後の`-`によりPromptをstdinから渡す。Prompt本文はargv、process list、Recording、
Evidenceへ入れない。Prompt pathはSpec基準の相対pathで、symlink、通常file以外、不正
UTF-8、NUL、空白のみ、上限超過を拒否する。SHA-256、byte数、redacted=trueだけを保存する。

`--skip-git-repo-check`は検証済みFixtureの使い捨てコピーだけに使用する。一般repositoryを
対象にするCLI機能ではない。danger-full-access、full-auto、yolo、resumeは使わない。

## Sandbox, network, and authentication

Codexが生成するcommandはworkspace-write sandboxとargvで明示したapproval neverで動く。
workspace-write network accessをfalse、web searchをdisabledにする。Codex自身のmodel
API通信は必要で、Phase 3はfirewall、VM、containerによる完全なnetwork遮断を保証しない。

認証は既存CLIのChatGPT-managed authだけを対象にする。`OPENAI_API_KEY`、
`CODEX_API_KEY`、親の任意secretを継承しない。auth fileをcopy、read、parseせず、
明示された絶対pathかつ既存directoryの`CODEX_HOME`だけをCodex processへ渡し、その値も
保存しない。未設定、相対path、存在しないpathはpreflight前に拒否し、`HOME/.codex`へ
fallbackしない。品質Gateは別の
Phase 2 allowlist環境と専用の一時HOME/TMP/cacheで起動するため、Provider側の一時環境や
`CODEX_HOME`を受け取らない。

## Incremental JSONL parser

stdoutはraw全体を保持せずchunkごとに処理する。各行でstrict UTF-8、非空、JSON object、
duplicate key、非有限数、string `type`、1行/全体byte上限を確認する。

成功lifecycleは`thread.started`、`turn.started`、exactly one `turn.completed`の順である。
`turn.failed`とterminal `error`はProvider失敗にする。item payloadは保存せずitem type件数
だけを数え、認識していないitem type文字列は本文を保持せず`unknown`へ集約する。
未知eventはraw payloadを破棄してunknown件数へ加算し、core lifecycleが正しければ
許容する。Evidenceにはthread/turn開始数、terminal種別、completed/failed/error数を
正規化し、全event件数との一致を検証する。item type keyは安全なEnumと`unknown`だけを
許可する。

`turn.completed.usage`に存在する非負integerのinput/cached input/output/reasoning output
Tokenだけを`provider_reported`として写す。Usage欠損は0にせず`not_available`とする。
cost、quota、価格計算は行わない。

## Safe failure location Evidence

CodexExecutionEvidence 1.2は、失敗時に固定Enumの`failure_stage`を必須とする。
1.3はrunner構築、runner entry、runtime precheck、JSONL parser初期化、argv構築、
Provider環境構築、process起動試行、pipe/selector初期化、process収集、Codex Evidence
構築、runner result構築／抽出、Provider orchestrationを区別する。さらに
`runner_state`、`invocation_state`、`cleanup_state`を固定Enumで保存し、runner未開始、
Popen未試行、Popen試行済みでprocess未生成、process生成済み、回収済み、回収失敗を
表現する。
`Popen`を呼んだ時点から`provider_invocation_attempted`とapproval
`never`/`explicit_config_never`を記録し、spawn失敗では`process_started=false`を保つ。
それ以前は`preflight_completed`、`process_started=false`、approval policy/basis null
である。runnerとorchestratorは同じin-memory lifecycle trackerを共有する。runner内部の
未知例外ではprocess group回収を行ってから固定状態だけを外側へ渡し、外側fallbackが
未観測の`process_started=false`や回収済みを合成しない。strict Evidenceを構築できない
場合はpaired Artifactを公開しない。

この場合だけ、Evidence／Recordingとは独立したFailure Diagnostic 1.0を保存できる。
Diagnosticはrun／experiment／taskの識別子、Harness failure kind、固定
`diagnostic_code`、共有lifecycle trackerから得た固定stageとrunner／invocation／cleanup
状態、Workspace lifecycle、Gate実行有無、作成時刻だけを持つ。未観測状態は`unknown`で
保持し、`false`や未起動へ丸めない。Provider活動を判定するための観測が不足すれば
`provider_activity_determined=unknown`とし、Diagnosticの存在からProvider未起動、
Prompt未送信、API未到達、quota未消費を推論しない。

`diagnostic_code`はCodex Evidence validation、lifecycle fallback Evidence validation、
Recording構築、Live Artifact構築、paired output公開を区別する。Diagnostic自体の公開失敗
は固定`diagnostic_publication_failed`としてCLI境界へ返すが、書けなかったDiagnosticが
存在するとは記録しない。Diagnosticは単独fileとして一時fileからatomic createし、既存file
を置換しない。Replay対象ではなく、作成されてもLive受入成功を意味しない。
`live.diagnostic_to`を指定すれば将来のSpecで出力先を明示でき、未指定の既存Specでは
Evidence出力名から別名を導出する。既存Specのstrict load契約は変えない。

Prompt本文、raw JSONL／stderr、agent message、reasoning、command本文／output、PID、
thread/session ID、`CODEX_HOME`、executable path、認証情報、親process環境、例外message、
traceback、filesystem pathはDiagnosticへ保存しない。任意の例外class名も含めず、
固定Enum以外の失敗理由を永続化しない。

既存CodexExecutionEvidence 1.1は`failure_stage`なし、1.2は1.3のlifecycle fieldなしで
引き続きstrict loaderが受理する。新規Evidenceだけを1.3で保存するため、保存済み
Codex Evidence 1.1／1.2、Recording 1.1、Live Artifact 1.0を変換・上書きせず
後方互換で読み込める。旧1.2の`provider_orchestration` fallbackはrunner内部の観測状態を
保持しなかったため、そこからProvider起動、Prompt送信、model API到達、quota消費、
process group回収の有無を確定してはならない。

## Persisted and excluded fields

CodexExecutionEvidenceにはrequested model/reasoning、sandbox/approval/network条件、
CLI profile/version、execution stage、時刻/duration、process開始有無、status/exit、
thread/turn/terminal/event/unknown/item件数、Usage、stdout/stderr byte数と上限状態、
process termination、safe failure kind、1.2以降のsafe failure stageを保存する。
1.3ではrunner／invocation／cleanupの固定状態も保存する。

Prompt本文、agent最終回答、reasoning、command本文/output、file content、raw JSONL、
raw stderr、thread/session ID、executable path、HOME/CODEX_HOME、認証情報は保存しない。
Live Artifactのdiffは品質評価に必要なWorkspace変更Evidenceであり、Codex event payload
とは別契約である。

## Process and Workspace lifecycle

ProviderもPhase 2と同じPOSIX新規session/process group、monotonic timeout、SIGTERM、
grace、SIGKILL、正常終了後のbackground child回収を使う。stdin/stdout/stderrを
non-blockingで回収し、大量出力時もraw streamをmemoryへ蓄積しない。Popen成功後は
selector生成、pipe設定、収集loopの未知例外も緊急cleanup境界で処理し、group回収成功を
`evidence_error`、回収失敗を`process_cleanup_error`として保存する。parser summary、
Codex Evidence構築、runner result構築で失敗しても同じ全体例外境界で回収状態を維持する。
`KeyboardInterrupt`と`SystemExit`でもprocess groupを緊急回収し、中断は通常の
Provider失敗へ変換せずそのまま再送出するため、paired Evidence／Recordingは公開しない。

Provider成功時だけ同じWorkspaceでPhase 2 Gateをgroup順に実行する。Provider失敗時は
Gateを実行しない。最終diff後は成否に関係なくtemporary rootを削除し、Fixture/Prompt
入力へaliasする出力がないことを保存直前に再確認する。Promptは最初に一度だけ読んだ
同じbytesからSHA-256を計算してstdinへ渡す。
Workspace状態は`not_created`、`removed`、`cleanup_failed`の三値で保存する。
Spec、Prompt、Fixture、output、`CODEX_HOME`などの早期入力/configuration errorでは
Artifactを作らない。preflight後のWorkspace準備失敗は安全な`run_failed`とEvidenceを
保存する。

## Failure taxonomy and Metrics

Provider: `provider_turn_failed`、`provider_cli_nonzero`、
`provider_signal_termination`、`provider_timeout`、
`provider_unavailable`、`provider_spawn_error`、`provider_input_error`、
`provider_protocol_error`、`provider_output_limit`。

品質: `quality_gate_failure`。Harness: `process_cleanup_error`、
`gate_harness_error`、`evidence_error`、`unsupported_platform`。
Provider process cleanupとWorkspace cleanupが同時に失敗した場合は、Codex Evidenceと
top-level failure kindの両方で`process_cleanup_error`を保持し、Workspace側は
`workspace_lifecycle=cleanup_failed`で併記する。

Provider成功、全Gate通常完了、完全なtext diff、Workspace cleanupが揃う場合だけMetricsを
生成する。Gate不合格でもMetricsを生成する。agent durationはCodex wall clock、evaluation
はGate wall clock、totalは合計、agent call=1、retry=0、UsageはCodex terminal event由来。

## Recording 1.1 and Replay

Live Recordingは`run_started`と`run_completed`または`run_failed`の2行だけである。
strict UTF-8 JSONL、key sort、finite number、末尾newline、atomic公開を使う。
terminal eventはCodex summaryに加え、Gate種別ごとのcommand/pass/fail件数、全command
通常完了flag、evaluation duration、changed files/line counts/diff completeness、
Workspace lifecycleを保存する。`run_completed`ではこのsummaryとMetricsを照合し、
acceptance Gateが1件以上あることを要求して、矛盾をloaderで拒否する。
`preflight_not_completed`は`not_created`だけ、
`provider_invocation_attempted`は作成済みWorkspaceだけを許容する。

EvidenceはRecording bytesのSHA-256を一方向参照する。成功Recording 1.1は外部CLI、
subprocess、network、GateなしでReplay Resultへ変換できる。失敗RecordingはMetricsが
ない理由を示して拒否する。Recording 1.0とPhase 1 Result bytesは変更しない。

## 保証できないこととPhase 4境界

workspace-writeとprocess groupはOS security boundaryではない。filesystem readの完全隔離、
firewall、CPU/memory/process quota、悪意あるprocessの完全な封じ込めは保証しない。
Codex model API、認証状態、model availability、quota、vendor eventの将来互換もmanual
smoke成功だけでは保証できない。ProviderがPrompt本文をWorkspace変更へ意図的に複製した場合、
その変更は品質Evidenceのdiff契約に現れうるため、PromptとFixtureは非機密または別途
レビュー済みでなければならない。

通常テストのAutoReview相当configケースは、fake executableが明示approval configをargvで
受け取ることだけを確認する。実Codex CLIがcloud/managed configを解決した最終policyは、
001／002の旧EvidenceだけではProvider process開始を確定できないため検証済みとはしない。

Phase 3は一つの`one_shot` taskを手動実行するだけである。複数task/条件/反復scheduler、
`staged` Workflow、A/B比較、集計はPhase 4であり、Phase 3 Providerへ先行実装しない。

## Manual smoke

実装、Prompt非保存、JSONL parser、環境分離、process tree、Recording/Replayをレビューし、
CLI helpが必須flagをすべて持ち、ChatGPT-managed auth、model/quota、送信対象を確認した後、
READMEの`live-codex ... --confirm-live-codex`を明示承認の範囲で手動実行した。

現在確認した`codex-cli 0.146.0-alpha.3.1`は
`headless_exec_explicit_never_v2`のversion allowlistとread-only preflightに成功した。
manual Live smokeは累計3回実行し、3回とも成功受入に達していない。2回目は
`overall_status=harness_error`、`failure_kind=evidence_error`、
`failure_stage=provider_orchestration`だったが、旧1.2 fallbackが起動・回収状態を
ゼロ値で合成しうる欠陥があった。この成果物だけからProvider起動、Prompt送信、model API
到達、quota消費、process group回収の有無は確定できず、過去の正確な例外も復元できない。
これらを推測せず、1.3 lifecycle trackerとoffline fault injectionで将来のEvidence契約を
修正した。

003ではstrict lifecycle Evidenceを構築できず、Evidence／Recordingは作成されなかった。
したがって003のProvider起動、Prompt送信、model API到達、quota消費は確定不能であり、
過去の内部例外も復元できない。003 Specの予約commitは保持し、003は再実行しない。
001／002のLive Evidence／Recording 4件はGit管理外で保持する。Failure Diagnosticは
将来の同種失敗を固定値で識別するためのoffline修正であり、003の過去状態を補完せず、
受入成功を意味しない。Phase 3はCurrentのままである。次回Liveは新しい修正commitの
レビューと別の明示承認がある場合だけ、新しいSpecとrun-idで1回実行する。
