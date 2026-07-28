# Phase 4 Workflow A/B Experiment

## Scope

Phase 4は、同一Codex Provider、exact model ID、reasoning effort、Task Fixture、品質Gate、
実行環境のもとでWorkflow Promptだけを比較する。Provider比較や一般的なモデル性能評価では
ない。結論は事前登録したFixture、Task Prompt revision、Workflow revision、Gate、環境、
実行時期の範囲に限定する。

controlは`one_shot`、treatmentは`staged`である。両方とも次を固定する。

```text
1 run = 1 Provider turn = 1 agent call
retry = 0
fallback = 0
```

`one_shot`は共有Task要件と、実装・必要な確認をAgentへ委ねる指示を一つのPromptで渡す。
詳細な作業順序は指定しない。`staged`は同じTask要件へ、調査、計画、テストの確認・追加、
実装、自己レビューと必要な修正を加える。これらは単一turn内の論理段階であり、複数Codex
process、multi-turn session、session resumeではない。内部の調査結果、計画、reasoning、
agent message、stage出力は保存・評価しない。

## Contracts and CLI

ExperimentSpec 1.0のstrict loaderと未知field拒否は変更しない。Phase 4は独立した
Workflow A/B Spec 2.0を使い、次で検証する。

```console
agentlab validate-workflow experiments/examples/workflow-ab.yaml
```

Spec 2.0はcomparison axis、control/treatment、固定Provider、exact model、reasoning effort、
Task/Prompt/Workflow/Fixture revision、品質Gate、反復数、seed、Provider timeout、stop
conditions、Artifact rootを一つのCampaign条件として保持する。Workflow別のProvider、
model、Fixture、Gate、sandbox、network、timeout設定は存在しない。

`plan-workflow`は外部AI、Provider、network、subprocess、Gateを呼ばずPlan 1.1を作る。
task×repetition blockは`SHA-256(seed, "block", task_id, repetition)`で並べ、各block内の
Workflow順は`SHA-256(seed, "workflow", task_id, repetition, workflow)`で決める。Pythonの
暗黙hashは使わない。各blockはcontiguousで`one_shot`と`staged`を1件ずつ含む。

canonical PlanはSpec SHA-256、Task Prompt/Fixture SHA-256、Workflow別の生成Prompt
SHA-256とbyte数、全run、run ID、初期`planned`状態、予定Provider call 1、revision、
相対Artifact予約pathを持つ。現在時刻を含まないため、同一入力から同一bytesになる。
作成時刻はcanonical Plan SHA-256付きの`*.metadata.json`へ分離する。両fileは
create-onlyかつatomicに公開し、暗黙上書きしない。

`run-workflow-campaign`はCampaign開始時にTask Prompt bytesとFixture全file bytesを一度だけ
固定し、Plan順の全runを同じin-memory snapshotから逐次実行する。各run開始前にsourceの
integrityを確認し、変更されていれば次のProvider call前に停止する。schedulerから
`agentlab live-codex`を子process起動せず、Phase 3 core orchestrationを同じprocess内で
再利用する。Campaign 1.1はrun前の`started`とrun後のterminal状態をappend-only JSONLへ
fsyncする。run状態は`planned`、`started`、`completed`、`failed`、`interrupted`、
`not_run`を区別する。

結果は通常成功、Quality Gate不合格、Provider失敗、Provider timeout、Harness障害、
cleanup失敗、人間中断、stop condition未実行を固定Enumで分離する。retry、fallback、
結果差し替え、中断Campaignのresume、並列実行は行わない。
repositoryの`workflow-ab.yaml`はfake Provider受入専用の合成Specで、実Codex Liveへ
使用しない。Live用Specは人間がexact model IDと非機密Promptを明示レビューした後に
新しいPlanとして事前登録する。

## Stop conditions

- `fail_fast=true`: 最初のProvider失敗、Provider timeout、Quality Gate不合格後に停止する。
- `max_failures`: Provider失敗、Provider timeout、Quality Gate不合格の累計へ適用する。
- Harness障害、Provider process cleanup失敗、Workspace cleanup失敗: 件数によらず停止する。
- PromptまたはFixture sourceの変更: 次runのProvider call前に`input_changed`で停止する。
- `max_total_duration_ms`: 次run開始前にmonotonic elapsed timeを確認し、到達後は新しいrunを
  開始しない。実行中runには個別Provider timeoutを適用する。
- 停止後のrun: Provider call 0の`not_run`として固定停止理由を保存する。
- `KeyboardInterrupt`/`SystemExit`: Phase 3 cleanupを維持し、実行中runを可能な範囲で
  `interrupted`、残りを`not_run`としてCampaignへ記録してから再送出する。

Provider callを開始していないrunはmanual Live試行数へ加算しない。Phase 4は自動retryや
失敗runの置換をしない。

## Live boundary

Campaign全体に`--confirm-live-codex`と、Planの予定Provider call総数に一致する
`--confirm-provider-calls`を要求する。不足または不一致ならCodex version/help preflightを
含むsubprocessを一切起動しない。Live Campaignは通常テストやCIから実行しない。通常テストは
短時間のfake Codex executableだけを使う。

Phase 3と同じstdin Prompt、ChatGPT-managed auth、API key除外、approval `never`、
ephemeral、workspace-write、web search無効、model-generated command network無効、
timeout、process group回収、Provider/Gate環境分離、source Fixture保護を維持する。
Prompt本文、raw JSONL、raw stderr、agent message、reasoning、command output、認証情報、
ローカル絶対pathは成果物へ保存しない。Prompt SHA-256とbyte数だけをEvidence/Recordingへ
保存する。Live対象は人間が事前レビューした非機密のTask Promptだけとする。Safe Runnerは
事故範囲を縮小するが、OS security sandbox、完全なfilesystem/
network隔離、CPU/memory quotaを提供しない。

adapter用Promptと固定Fixture copyはArtifact root外のsystem temporary directoryへだけ
materializeする。cleanup結果をCampaignへ`cleared`または`failed`として保存し、失敗は
`cleanup_failure`として即時停止する。cleanup失敗時もPrompt fileはbest-effortで
redactまたはunlinkする。

## Offline report

`report-workflow`は保存済みPlan、Campaign、Run Evidence、Recordingだけをstrict loadする。
Codex CLI、外部AI、network、Provider、品質Gate、Fixture変更、Prompt復元は行わない。同じ
version付き集計modelからstrict JSONとMarkdownを生成する。

Workflowごとにscheduled/attempted/completed、Gate pass/fail、Provider/Harness/cleanup/
interrupted/not_run、4種Gate command結果、duration、agent call、retry、変更file/行数、
Usageを集計する。各metricはdenominator、observed、missingを保持し、欠測値を0へ変換しない。
Provider報告Tokenと推定Tokenは分離する。paired結果がなければ`not_estimable`とし、自動winner、
有意差検定、信頼区間、leaderboardは作らない。

集計前にPlan、Campaign、Evidence、Recordingのtask、Workflow、repetition、run ID、
Campaign outcome、Provider call数、Evidence status/failure kind/execution stage、
Prompt/Fixture fingerprint、exact model、reasoning effortを相互照合する。意味が矛盾する
Artifactはestimableへ含めず、report生成自体を拒否する。失敗RecordingについてもEvidenceの
Gate件数・結果、diff、Workspace lifecycle、evaluation durationからevaluation summaryを
再構成し、完全一致しないArtifactを拒否する。

## Non-goals and current acceptance

Phase 4ではAntigravity、Provider比較、multi-language Fixture、public benchmark、dashboard、
notebook、parallel/distributed scheduler、retry/fallback、resume、multi-turn staged、
Prompt自動最適化、model自動選択、adaptive sampling、統計的検定、OS security sandbox、
Phase 5以降を実装しない。

offline実装とfake Provider受入後、reviewed commit
`2abd653a7b42f8932c0005e6d7d3fdd1252845e0`、canonical Plan SHA-256
`9caf1847677adfcd6ef7aac59b2298bfbc9113577d75e49a7976de0b068e19de`のLive Campaignを
2026-07-28T09:26:35Zに1回だけ実行した。予定6 run／6 Provider callsのうちPlan先頭の
`staged` runだけをattemptし、actual call 1、unknown call 0だった。Provider turnと4種類の
Gateは成功したが、lintの`py_compile`が作成したbytecode cacheをbinary diffとして検出し、
完全な行数Evidenceを構築できなかったため、`harness_error`／`evidence_error`で停止した。
Provider process group、Workspace、adapter cleanupは成功している。

残り5 runは`not_run`、retry／fallback／resumeは0である。offline reportは1回だけ生成し、
scheduled pair 3、complete pair 0、`pairing.status=not_estimable`となった。比較可能なpairが
ないためWorkflowの優劣を述べない。ArtifactはGit管理外で保持し、Campaign、失敗run、
reportを再実行しない。Phase 4は`Current`、Phase 5は`Planned`のままである。
Workflow別には、`one_shot`がscheduled 3／attempted 0／completed 0／not_run 3、
`staged`がscheduled 3／attempted 1／completed 0／harness_failed 1／not_run 2である。
`staged`の4 Gate commandは4件ともPASSしたが、run自体はHarness障害のため
quality-gate-passed runへ数えない。

Campaign 001のArtifact、report、上記失敗分類は変更しない。その後のoffline Harness修正で、
Provider processと各Gate commandへHarness管理`PYTHONPYCACHEPREFIX`を設定し、Python
bytecode cacheをrun専用system temporary root内かつ評価Workspace外へ隔離した。binary diff
拒否やline count完全性は緩めず、Harness管理外のbinary変更は引き続き`evidence_error`である。
Campaign 001は再実行しない。修正後のLiveを行う場合も、新experiment ID、新Artifact root、
新canonical Plan、reviewed commit、別の人間による明示承認を必要とする。Phase 4は
`Current`、Phase 5は`Planned`のままである。

bytecode cache修正commit
`11dd86801bb9f87b49d51d351c736dd0667cb94e`の既存Task Prompt、Fixture、Workflow Prompt、
Gateを変更せず、Campaign 002を新experiment ID `workflow-ab-codex-live-002`と新Artifact
rootへoffline事前登録した。ProviderはCodex、exact modelは`gpt-5.6-sol`、reasoning effortは
`high`、Taskは`tag-normalizer`、Workflowは`one_shot`／`staged`、各3反復、seedは4401である。
acceptance／regression／lint／typecheckを各1件とし、Provider timeout 600000 ms、
Campaign上限3600000 ms、`workspace-write`、network無効、最大失敗2、fail-fast無効を固定した。
canonical Plan SHA-256は
`375675a105b3de6b371551ab09c25014e3198d256bf09717e80fe20e747125ee`で、
planned runs／Provider callsは6／6、retry／fallback／resumeは0である。

Campaign 002のPlan／metadataはGit管理外に保持し、Campaign、Recording、Evidence、
Diagnostic、reportは作成していない。全6 runは未実行であり、実行にはreviewed commit、
Plan SHA-256、`CODEX_HOME`、CLI version、最大6 Provider callsを含む別の人間による明示承認を
必要とする。Campaign 001は`harness_failure`、complete pair 0の履歴のまま再実行、resume、
report再生成を行わない。Phase 4は`Current`、Phase 5は`Planned`のままである。
