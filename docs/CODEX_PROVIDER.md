# Codex CLI Provider

## Scope

Phase 3は、1 task・1 Codex Provider・1 repetition・`one_shot`を人間が手動実行する最小
vertical sliceである。scheduler、staged Workflow、比較実験はPhase 4の独立契約として
実装し、Phase 3 Providerの責務へ混在させない。自動retryは実装しない。通常テストはfake
Codexだけを使う。manual Liveは累計8試行で、
008のAgentLab製品経路とoffline ReplayによってPhase 3の最小vertical slice受入を完了した。
過去runは再実行せず、新しいLiveにはレビュー済みcommit、新しいSpec／run-id／出力先、
別の明示承認を必要とする。

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
Evidence 1.5ではOS pipeへの書込みだけを`not_started`、`partial`、`complete`、`unknown`
の固定Enumと書込み済みbyte数で観測する。`complete`はHarnessが全Prompt bytesをpipeへ
書いたことだけを意味し、Codexによる読取り、Prompt送信、model API到達、quota消費を
証明しない。観測を失った場合はbyte数を合成せず`unknown`とする。

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
`CODEX_HOME`を受け取らない。Provider processとGate commandにはそれぞれrun専用system
temporary root配下のHarness管理`PYTHONPYCACHEPREFIX`を設定し、親の同名変数を継承しない。
実値はEvidence、Recording、Diagnosticへ保存せず、run cleanupでcache rootも回収する。

## Incremental JSONL parser

stdoutはraw全体を保持せずchunkごとに処理する。各行でstrict UTF-8、非空、JSON object、
duplicate key、非有限数、string `type`、1行/全体byte上限を確認する。

[固定commitのJSONL event processor](https://github.com/openai/codex/blob/ff75c5b939c477c49eb1bd5248da6dab71b109d1/codex-rs/exec/src/event_processor_with_jsonl_output.rs)
と
[exec event定義](https://github.com/openai/codex/blob/ff75c5b939c477c49eb1bd5248da6dab71b109d1/codex-rs/exec/src/exec_events.rs)
に合わせ、`thread.started`を一意な最初のcore eventとする。通常成功は
`thread.started`、`turn.started`、exactly one `turn.completed`の順である。
`thread.started`後かつ`turn.started`前は、config warningとして到達可能な
`item.completed/error`だけを限定的に許容する。item-level `error`は非fatalな件数であり、
単独ではProvider失敗にしない。

top-level `error`は独立した観測件数として保持し、直ちにterminalへ変換しない。その後の
正規な`turn.failed`を失敗turnのterminalとして保持し、この列を
`provider_turn_failed`とする。構造、順序、item type、Usageをすべて検証してから1 eventの
状態を一括反映するため、拒否したeventはevent数、lifecycle、item数、Usage、terminalを
部分更新しない。受信byte数だけは実際のstdout観測値として維持する。

top-level `error.message`、`turn.failed.error.message`、存在する場合のerror item
`message`はstringだけを受理する。固定CLI sourceではこれらがfree-form messageであり、
安定したerror codeは確認できない。Evidence 1.5は最大4096 bytesのmessageをmemory内でだけ
狭いallowlistへ照合し、`authentication`、`model_access`、`quota_or_rate_limit`、
`connectivity`、`service`、`policy_or_entitlement`、`unknown`、`conflicting`の固定Enum
だけをadvisory hintとして保存する。同じ分類の複数sourceは一つへ集約し、異なる分類は
`conflicting`とする。成功時は`not_applicable`である。raw message、substring、正規化文字列、
hash、正規表現結果は保存せず、hintは`failure_kind`や根本原因を変更しない。

item payloadは保存せずitem type件数だけを数え、認識していないitem type文字列は本文を
保持せず`unknown`へ集約する。未知eventはraw payloadを破棄してunknown件数へ加算し、
core lifecycleが正しければ許容する。Evidenceにはthread/turn開始数、turn terminal種別、
completed/failed/top-level error数を正規化し、全event件数との一致を検証する。item type
keyは安全なEnumと`unknown`だけを許可する。

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
表現する。1.4はこのlifecycle契約を継承し、top-level `error`件数とturn terminalを
独立して表現する。1.5はstdin write観測とadvisory Provider failure hintを追加する。
新規実行だけを1.5で生成し、1.1〜1.4の既存fieldとterminal/error対応は変更しない。
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
`gate_executed=true`は、少なくとも1件のGate command invocationを試行したことを意味する。
共有in-memory trackerを各command起動試行の直前に単方向で更新するため、その後Gate executor
から例外が漏れて戻り値を得られなくても`false`へ戻さない。

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

既存CodexExecutionEvidence 1.1は`failure_stage`なし、1.2は1.3以降のlifecycle fieldなしで
引き続きstrict loaderが受理する。1.1〜1.4のvalidatorを緩めず、新規Evidenceだけを
1.5で保存する。新規Live Artifact 1.1は成功・失敗を問わず正確なevaluation durationを
保持する。保存済みCodex Evidence 1.1〜1.4、Recording 1.1、Live Artifact 1.0、
Failure Diagnostic 1.0を変換・上書きせず後方互換で読み込める。旧1.2の
`provider_orchestration` fallbackはrunner内部の観測状態を保持しなかったため、そこから
Provider起動、Prompt送信、model API到達、quota消費、process group回収の有無を確定しては
ならない。

## Persisted and excluded fields

CodexExecutionEvidenceにはrequested model/reasoning、sandbox/approval/network条件、
CLI profile/version、execution stage、時刻/duration、process開始有無、status/exit、
thread/turn/terminal/event/unknown/item件数、Usage、stdout/stderr byte数と上限状態、
process termination、safe failure kind、1.2以降のsafe failure stageを保存する。
1.3以降はrunner／invocation／cleanupの固定状態も保存し、1.4ではtop-level errorを
turn terminalと分離して保存する。1.5ではstdin write状態／byte数とadvisory Provider
failure hintを保存する。

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

EvidenceはRecording bytesのSHA-256を一方向参照する。Live Artifact 1.1のGate Evidence、
diff、Workspace lifecycle、evaluation durationからRecordingのevaluation summaryを完全に
再構成できる。成功Recording 1.1は外部CLI、subprocess、network、GateなしでReplay Resultへ
変換できる。失敗RecordingはMetricsがない理由を示して拒否する。Recording 1.0とPhase 1
Result bytesは変更しない。

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
manual Live 001〜005は5試行すべて成功受入に達していない。2回目は
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
受入成功を意味しない。

004はSpec予約commit `b54ab576d352553227877c8d59d4af611e79b884`に対して1回だけ
実行し、Codex Evidence validation失敗で停止した。Failure Diagnostic 1.0だけが作成され、
Evidence／Recordingは未作成、Gate／Replayは未実行である。DiagnosticではProvider
process開始とprocess group cleanup成功を観測したが、Prompt送信、model API到達、quota
消費、実際のJSONL event列は確定不能である。固定CLI sourceとの比較で、pre-turn warningと
top-level `error`→`turn.failed`を拒むparser互換性欠陥、および拒否eventが件数を部分更新する
欠陥は確定したためオフライン修正した。ただし004のevent列は復元できず、この欠陥が004の
直接原因だったとは断定しない。004は再実行していない。

005はSpec予約commit `b63024ab2214611b059fb75da0444d200d3d32d9`で、修正実装commit
`2cb4eadfbfdc54e3d71f1d6a1a070bd3e53a3566`を親に持つ状態に対して1回だけ実行した。
`overall_status=provider_error`、`failure_kind=provider_turn_failed`、exit 1で、
event 5件（thread開始1、turn開始1、top-level error 1、error item 1、turn.failed 1）を
Evidence 1.4へ保存した。Evidence 1.4とRecording 1.1はstrict再読込でき、Recording
SHA-256一致、Workspace `removed`、process group回収、redactionを確認した。Gateは0件、
Metricsはnull、Replayは未実行で、Failure Diagnosticは作成されていない。raw turn errorを
保存しない契約のため根本原因は復元不能であり、Evidence 1.4にはstdin write観測がないため
Prompt書込み完了、model API到達、quota消費も確定不能である。認証、model access、quota、
network、serviceのいずれかを原因と推測しない。課題は1行の`TODO`を`COMPLETE`へ変える合成
fixtureであり、複雑さを失敗理由とはしない。001／002成果物4件、004 Diagnostic、005
Evidence／RecordingはGit管理外で保持し、005は再実行しない。

この履歴を補完せず、commit `a5193e3693afe380bddc181895c1e29b8624c24c`でEvidence 1.5の
stdin write観測とadvisory failure hintをoffline追加した。commit
`f994560bc931a73b11424f44ee03c3343d63d89d`では、末尾改行なしの不正JSONL eventを
nonzero exitより優先してprotocol failureへ分類し、正常な`turn.failed`と出力なしの
nonzero exitの既存分類を維持した。

006は`gpt-5.6`でProviderを起動し、stdin writeは`complete`だったが、
`provider_turn_failed`／advisory hint `model_access`で停止した。Gateは未実行であり、
Provider message本文を保存しない契約上、hintを根本原因とは断定しない。Evidence 1.5と
Recording 1.1、redaction、process／Workspace cleanupは成立したが、成功受入ではない。

007は製品Provider外の診断wrapperでPrompt delivery前に停止し、
`inconclusive_prompt_delivery_failure`として保持する。固定Prompt送信、Live AI turn、
model API到達は確認できず、Artifactも作成されていない。007を成功またはモデル失敗へ
分類し直さない。

008は人間がselectable catalogから明示選択したexact model ID `gpt-5.6-sol`を使用した。
これはCLI default／recommendedの推測ではない。Spec予約commit
`cc97e53bf0bac426b08346f63e6f527ed7d5be9e`のAgentLab製品経路で1回だけ実行し、
agent call 1、retry／fallback 0、Provider exit 0、`turn.completed` 1件、
`turn.failed` 0件となった。使い捨てFixtureでは`task.txt`だけが`status=TODO`から
`status=COMPLETE`へ変更され、acceptance、regression、lint、typecheckがすべてPASSした。
Evidence 1.5／Recording 1.1のstrict再読込、Recording SHA-256、redaction、process group／
Workspace cleanupを確認し、Failure Diagnosticは成功契約どおり作成されていない。
成功Recordingのoffline Replayは外部AI、Provider、Codex CLIを呼ばずにRunResultを再構成し、
Evidence／RecordingとMetricsが一致した。

manual Live 006／007の失敗・不明履歴を上書きせず、008の成功だけを選別しない。Phase 3の
最小vertical slice受入は完了したが、この結果は一般的な`gpt-5.6-sol`性能やProvider比較を
示さない。Phase 3 manual Liveは累計8試行のままである。

Phase 4はCurrentである。2026-07-28に事前登録済みWorkflow A/B Campaignを1回だけ開始し、
予定6 Provider callsのうち1 callを実行した。最初の`staged` turnと4種類のGateは成功したが、
`py_compile`が作成したbytecode cacheのbinary diffにより完全な行数Evidenceを構築できず、
`harness_error`／`evidence_error`で安全停止した。残り5 runは`not_run`、complete pairは0、
reportは`not_estimable`であり、Phase 4 Campaignや失敗run、reportは再実行しない。この1 call
はPhase 3の累計へ加算せず、一般的なモデル性能またはWorkflowの優位性を示す結果として
扱わない。Phase 5はPlannedのままである。
