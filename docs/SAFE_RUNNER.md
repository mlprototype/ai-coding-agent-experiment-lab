# Safe Runner

## Threat model

Phase 2 Safe Runnerは、信頼済みのExperimentSpec、Fixture、品質Gate commandをlocalで
再現可能に評価するHarnessである。任意の不審なcodeを安全に実行するsecurity sandbox
ではない。

信頼するものは、review済みSpec、Specが参照する小さなFixture、`quality_gate`にargv
配列として列挙されたcommandである。信頼しないものは、親processの任意環境変数、
commandの実行時間、出力量、終了code、background childの有無、変更fileのtext性である。
Spec reviewではGateが外部AIやnetworkを呼ばないことも確認する。Runnerはargvの意味を
推測するdenylistではなく、列挙済みargvだけを許可する境界である。

## Workspace lifecycle

`fixture_path`はSpec fileの親directoryを基準とする相対POSIX pathだけを許可する。
空path、絶対path、親directory参照、rootまたは配下のsymlink、FIFO、socket、deviceを
拒否する。

一回の実行は次の順序で進む。

1. Fixture sourceを検証し、内容のSHA-256 snapshotを作る。
2. system temporary directoryにWorkspaceと専用環境directoryを作る。
3. Fixtureをsymlinkをdereferenceせずコピーし、コピー後snapshotを照合する。
4. Workspaceの初期snapshotを保持する。
5. 品質GateをWorkspace内で順番に実行する。
6. 最終snapshot、diff、Evidenceを作る。
7. 成否やHarness例外にかかわらずtemporary rootを削除する。
8. sourceが変更されていないことを再確認し、完成したEvidenceをatomicに保存する。

Artifact出力はFixture source内へ置けない。`--force`でもExperimentSpec、Replay
Recording、Fixture source file、またはそれらへのsymlink/hard linkを置換できない。
Git worktreeは使用しない。

## Command allowlist

Runnerが実行するのは`quality_gate.acceptance`、`regression`、`lint`、`typecheck`に
記載されたargvだけである。group順とSpec内順を維持し、CLIからcommandを追加できない。
`shell=False`、`stdin=DEVNULL`、Workspace `cwd`、分離したstdout/stderrを使う。
command文字列のshell解釈、`shlex.split`、`/bin/sh -c`、PowerShell経由実行は行わない。

通常の非0終了後も後続Gateを実行する。timeoutまたはHarness障害では、安全のため残りを
停止する。Phase 2は`stop_conditions` schedulerを実装しない。

## Process group and timeout

POSIXでは各commandを新しいsession/process groupのleaderとして起動する。durationは
monotonic clock、時刻はtimezone-aware UTCで記録する。

timeout時はgroupへSIGTERMを送り、`termination_grace_ms`まで待ち、残存時はSIGKILLへ
escalateする。親processをwaitしてpipeをdrainする。親が正常終了しても同じgroupに
background childが残ればSIGTERM/SIGKILLで回収する。group消滅またはcollector完了を
確認できない場合は`process_cleanup_error`であり、成功や品質不合格として扱わない。

process group APIを保証できないplatformではcommand起動前に
`unsupported_platform`としてfail closedする。

## Environment allowlist

親環境から渡せるのは`PATH`、`LANG`、`LC_ALL`と、必要なplatform変数
`SYSTEMROOT`、`COMSPEC`、`PATHEXT`だけである。`HOME`、`TMPDIR`、
`XDG_CACHE_HOME`はrun専用temporary directoryへ差し替える。
`PYTHONDONTWRITEBYTECODE=1`も固定設定する。その他のsecret、Token、認証変数は継承
しない。

これはnetworkを遮断しない。信頼済みGate自体がnetworkへ接続しない設計を別途守る。

## stdout and stderr limits

stdoutとstderrは別pipeとして最後までnon-blockingでdrainする。それぞれ
`max_output_bytes`までしか保持せず、超過後もdeadlockを避けるため読み捨てを続ける。
超過は個別の`*_truncated` flagで示す。不正UTF-8は置換文字へ変換する。

temporary Workspaceは`<WORKSPACE>`、専用HOME/cache/tempはplaceholderへ正規化する。
一時絶対pathや親環境のsecretをEvidenceへ保存しない。

## Evidence contract

Evidence JSONはschema version `1.0`を持ち、未知field、型強制、timezoneなし日時、
非有限数を拒否する。UTF-8、key sort、2-space indent、末尾newline、
`allow_nan=False`で、同一directoryの完成済みtemporary fileからatomicに公開する。
既存fileは既定で置換せず、`--force`時だけreplaceする。

command Evidenceはgate、group内index、argv、status、return code、UTC時刻、duration、
stdout/stderr、truncation、termination結果を保持する。statusは`passed`、`failed`、
`timed_out`、`spawn_error`である。

Artifactはrun/experiment/task ID、Spec SHA-256、初期Fixture SHA-256、Runner設定、
command配列、diff、overall status、failure kind、任意RunMetricsを保持する。

failure kindは次を区別する。

- `none`: 全Gate成功
- `quality_gate_failure`: 一つ以上のcommandが通常の非0終了
- `timeout`: command timeout後の停止に成功
- `command_unavailable`: executableが見つからない
- `spawn_error`: その他のcommand起動失敗
- `process_cleanup_error`: process groupまたはpipeを回収できない
- `evidence_error`: snapshot、diff、Workspace cleanupなどのEvidence不完全
- `unsupported_platform`: 必要なPOSIX保証がない

## Diff Evidence

標準library snapshotを比較し、relative POSIX pathを安定順に保存する。text fileは
unified diffと正確な追加・削除行数を生成する。diff本文は`max_diff_bytes`で切り捨てるが、
事前に計算できた行数は完全なままである。

binary、NULを含むfile、不正UTF-8の変更はpathを保存し、line countを不完全とする。
推測した行数は保存しない。この場合はRunMetricsも生成しない。

## Command-level Metrics

全commandが`passed`または通常の`failed`として完了し、process group回収、diff行数、
Workspace削除が完全な場合だけRunMetricsを生成する。

- `acceptance_tests_total`: acceptance argv数
- `acceptance_tests_passed`: exit code 0のacceptance argv数
- `regression_failures`: 非0終了したregression argv数
- `lint_errors`: 非0終了したlint argv数
- `typecheck_errors`: 非0終了したtypecheck argv数

これらはframework内のtest case数やdiagnostic数ではなくcommand単位である。
`agent_duration_ms`、`agent_call_count`、`retry_count`は0、evaluationとtotal durationは
Gate wall-clock時間、UsageMetricsは`null`である。

timeout、spawn、process cleanup、unsupported platform、Evidence/diff不完全ではMetricsを
`null`にし、品質不合格へ変換しない。

## Supported operating systems

保証対象は、PythonのPOSIX session、process group、SIGTERM、SIGKILL、non-blocking pipe
APIを提供するmacOSとLinuxである。local受入試験はmacOSで実施する。WindowsはPhase 2で
未対応で、command起動前にfail closedする。新しいOSはprocess tree統合testを通すまで
対応済みと表現しない。

## Isolation not provided

Phase 2は次を保証しない。

- Docker、VM、seccomp、user namespaceによるOS sandbox
- filesystem権限の完全な封じ込め
- firewallまたはnetwork namespaceによる通信遮断
- CPU、memory、process数の厳密なquota
- process groupから離脱する悪意あるprogramの完全な停止
- 巨大な信頼済みFixture自体のmemory/resource上限

## Phase 3 boundary

Phase 2はProviderを起動せず、Prompt、Live Recording、redaction、Token、費用を扱わない。
Codex CLI Providerと明示的Live RecordはPhase 3の範囲であり、このRunnerへ暗黙に接続
しない。
